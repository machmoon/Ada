"""Posting a run summary card to a Google Chat incoming webhook.

No OAuth here: for an incoming webhook the URL itself is the credential,
which is why it is validated before anything is sent to it and masked in
every error message. Only ``https://chat.googleapis.com/v1/spaces/…`` is
accepted -- an exact host match, checked before the transport is invoked.

The card follows the project's honesty rules. Counts come from the result and
never from a template, and the routing line may only say every net is routed
when ``unrouted`` is actually empty; otherwise the card names each unfinished
net and the router's reason, verbatim. A card that says "routed" over a
ratsnest is the exact failure this codebase exists to prevent.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from .transport import GoogleError, HttpRequest, Transport, ensure_google_url, mask

__all__ = ["post_run_card", "run_card", "routing_lines", "validate_webhook"]

CHAT_HOST = "chat.googleapis.com"
#: Past this many, a space is being spammed rather than informed.
MAX_LISTED_FINDINGS = 8
MAX_LISTED_UNROUTED = 12


def validate_webhook(url: str) -> str:
    """The webhook URL, or a refusal that never repeats the secret."""
    shown = mask(url, 6)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != CHAT_HOST:
        raise GoogleError(
            "bad_webhook",
            f"GOOGLEAPPS_CHAT_WEBHOOK ({shown}) must be an https URL on "
            f"{CHAT_HOST}",
        )
    if not parsed.path.startswith("/v1/spaces/"):
        raise GoogleError(
            "bad_webhook",
            f"GOOGLEAPPS_CHAT_WEBHOOK ({shown}) does not look like an "
            "incoming-webhook URL (expected a /v1/spaces/… path)",
        )
    return ensure_google_url(url)


def routing_lines(route: Any) -> list[str]:
    """The routing story, told the way the CLI tells it.

    ``route`` is ``RouteResult`` or ``None`` (routing turned off). The first
    line is the router's own summary -- counts, copper length -- and every
    unrouted net follows by name with the router's reason.
    """
    if route is None:
        return ["not routed (routing was turned off for this run)"]
    lines = [route.summary()]
    unrouted = sorted(route.unrouted.items())
    for net, reason in unrouted[:MAX_LISTED_UNROUTED]:
        lines.append(f"unrouted {net}: {reason}")
    if len(unrouted) > MAX_LISTED_UNROUTED:
        lines.append(f"…and {len(unrouted) - MAX_LISTED_UNROUTED} more unrouted nets")
    return lines


def _verdict(result: Any) -> str:
    blockers = len(result.blockers)
    if blockers:
        return f"needs review — {blockers} blocker(s)"
    if result.route is not None and result.route.unrouted:
        return f"placed — {len(result.route.unrouted)} net(s) left unrouted"
    return "board ready"


def _paragraphs(lines: list[str]) -> list[dict[str, Any]]:
    return [{"textParagraph": {"text": line}} for line in lines]


def run_card(
    result: Any,
    *,
    stage_lines: list[str] | None = None,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """The webhook payload: a cardsV2 card plus a plain-text fallback."""
    w, h = result.board.size_mm
    # Severity is a StrEnum, so the comparison needs no engine import here.
    blockers = [f for f in result.findings if f.severity == "blocker"]
    notes = [f for f in result.findings if f.severity != "blocker"]

    review_lines = [
        f"{len(result.findings)} finding(s): {len(blockers)} blocker(s), "
        f"{len(notes)} other(s)"
        if result.findings
        else "no findings"
    ]
    for finding in blockers[:MAX_LISTED_FINDINGS]:
        review_lines.append(f"BLOCKER: {finding.title}")
    for finding in notes[: max(0, MAX_LISTED_FINDINGS - len(blockers))]:
        review_lines.append(f"{finding.severity.value}: {finding.title}")
    listed = min(len(blockers), MAX_LISTED_FINDINGS) + max(
        0, min(len(notes), MAX_LISTED_FINDINGS - len(blockers))
    )
    if len(result.findings) > listed:
        review_lines.append(f"…and {len(result.findings) - listed} more finding(s)")

    board_lines = [
        f"{result.spec.part_count()} parts, {result.spec.net_count()} nets",
        f"board {w:.2f} x {h:.2f} mm [{result.board.solver_status}]",
    ]
    if duration_s is not None:
        board_lines.append(f"finished in {duration_s:.1f} s")

    sections = [
        {"header": "Board", "widgets": _paragraphs(board_lines)},
        {"header": "Routing", "widgets": _paragraphs(routing_lines(result.route))},
        {"header": "Review", "widgets": _paragraphs(review_lines)},
    ]
    if stage_lines:
        sections.insert(1, {"header": "Stages", "widgets": _paragraphs(stage_lines)})

    fallback = " · ".join(
        [f"silkscreen: {_verdict(result)}"]
        + board_lines[:2]
        + routing_lines(result.route)
    )
    return {
        "text": fallback,
        "cardsV2": [
            {
                "cardId": "silkscreen-run",
                "card": {
                    "header": {
                        "title": f"silkscreen: {_verdict(result)}",
                        "subtitle": str(result.intent)[:120],
                    },
                    "sections": sections,
                },
            }
        ],
    }


def post_run_card(
    webhook_url: str,
    result: Any,
    *,
    transport: Transport,
    stage_lines: list[str] | None = None,
    duration_s: float | None = None,
) -> None:
    """Build the card and POST it. Raises :class:`GoogleError` on refusal."""
    url = validate_webhook(webhook_url)
    payload = run_card(result, stage_lines=stage_lines, duration_s=duration_s)
    response = transport(
        HttpRequest(
            "POST",
            url,
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(payload).encode("utf-8"),
        )
    )
    if response.status >= 300:
        raise GoogleError(
            f"http_{response.status}",
            f"the Chat webhook ({mask(webhook_url, 6)}) rejected the card",
        )
