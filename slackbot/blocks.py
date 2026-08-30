"""Turning a pipeline result into Slack Block Kit.

Two rules shape everything here, both borrowed from the web UI's honesty rules.
Counts come from the result and never from a template -- a message never says
"3 findings" unless three findings exist. And a stage line is only ticked once
its event has actually arrived, so a stalled run looks stalled rather than
finished.

Slack's own limits are hard: fifty blocks per message and three thousand
characters per section. Everything below truncates to stay inside them, because
a message that exceeds them is rejected outright rather than trimmed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents.review import Finding, Severity  # noqa: E402

__all__ = [
    "SEVERITY_EMOJI",
    "escape_mrkdwn",
    "truncate",
    "section",
    "context",
    "divider",
    "help_blocks",
    "error_blocks",
    "accepted_blocks",
    "progress_blocks",
    "result_blocks",
    "findings_blocks",
    "summary_text",
]

MAX_BLOCKS = 50
MAX_SECTION_CHARS = 2900
#: Past this many, a channel is being spammed rather than informed; the rest
#: are counted in a trailing line instead of listed.
MAX_LISTED_FINDINGS = 8
MAX_LISTED_PARTS = 24

SEVERITY_EMOJI = {
    Severity.BLOCKER: ":red_circle:",
    Severity.MARGINAL: ":large_yellow_circle:",
    Severity.NOTE: ":white_circle:",
}

STAGES = (
    ("read", "read datasheets"),
    ("propose", "propose a circuit"),
    ("validate", "validate and repair"),
    ("place", "place the board"),
    ("review", "review the design"),
)


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as markup.

    Applied to model output and user text alike. Without it a datasheet line
    containing ``<`` silently truncates the rest of the message, which reads as
    the pipeline having lost the finding rather than the formatter having eaten
    it.
    """
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def truncate(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(text)}}


def context(text: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": truncate(text, 1000)}],
    }


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def _cap(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    return blocks[: MAX_BLOCKS - 1] + [context("_…truncated to fit Slack's limits._")]


def help_blocks(help_text: str) -> list[dict[str, Any]]:
    return [section(help_text)]


def error_blocks(title: str, detail: str = "", hint: str = "") -> list[dict[str, Any]]:
    blocks = [section(f":warning: *{escape_mrkdwn(title)}*")]
    if detail:
        blocks.append(section(escape_mrkdwn(detail)))
    if hint:
        blocks.append(context(hint))
    return blocks


def accepted_blocks(intent: str, user_id: str = "") -> list[dict[str, Any]]:
    who = f" for <@{user_id}>" if user_id else ""
    return [
        section(f":gear: *Working on it*{who}\n>{escape_mrkdwn(truncate(intent, 400))}"),
        context("Placement uses CP-SAT and takes a few seconds; the model calls "
                "take longer. I'll post the board and the files here when it's done."),
    ]


def progress_blocks(
    intent: str, done: list[str], current: str = "", elapsed_s: float = 0.0
) -> list[dict[str, Any]]:
    """A live stage list. Only stages whose events arrived are ticked."""
    lines = []
    for key, label in STAGES:
        if key in done:
            lines.append(f":white_check_mark: {label}")
        elif key == current:
            lines.append(f":hourglass_flowing_sand: *{label}*")
        else:
            lines.append(f":black_small_square: {label}")
    return [
        section(f":gear: *Working on it*\n>{escape_mrkdwn(truncate(intent, 300))}"),
        section("\n".join(lines)),
        context(f"{elapsed_s:.0f}s elapsed"),
    ]


def summary_text(result: Any) -> str:
    """The plain-text fallback, which is also the notification text."""
    return f"silkscreen: {result.summary()}"


def _finding_line(finding: Finding) -> str:
    emoji = SEVERITY_EMOJI.get(finding.severity, ":white_circle:")
    where = f"  `{', '.join(finding.parts)}`" if finding.parts else ""
    line = f"{emoji} *{escape_mrkdwn(finding.title)}*{where}"
    if finding.detail:
        line += f"\n{escape_mrkdwn(truncate(finding.detail, 600))}"
    if finding.citation:
        line += f"\n_cited:_ {escape_mrkdwn(truncate(finding.citation, 300))}"
    if finding.suggested_fix:
        line += f"\n_suggested fix:_ {escape_mrkdwn(truncate(finding.suggested_fix, 400))}"
    return line


def findings_blocks(findings: list[Finding]) -> list[dict[str, Any]]:
    """The review, worst first. An empty review says so rather than saying
    nothing -- "no findings" and "the review did not run" are different facts,
    and the caller distinguishes them before getting here."""
    if not findings:
        return [section(":white_check_mark: *Review found nothing to flag.*")]

    order = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}
    ranked = sorted(findings, key=lambda f: order.get(f.severity, 3))
    counts = {sev: sum(1 for f in findings if f.severity is sev) for sev in Severity}
    headline = ", ".join(
        f"{counts[sev]} {sev.value}" for sev in Severity if counts[sev]
    )

    blocks: list[dict[str, Any]] = [section(f"*Review* — {headline}")]
    for finding in ranked[:MAX_LISTED_FINDINGS]:
        blocks.append(section(_finding_line(finding)))
    remaining = len(ranked) - MAX_LISTED_FINDINGS
    if remaining > 0:
        blocks.append(context(f"_…and {remaining} more, in the attached run log._"))
    return blocks


def result_blocks(
    result: Any,
    *,
    duration_s: float | None = None,
    reviewed: bool = True,
) -> list[dict[str, Any]]:
    """The finished run: what was built, how it placed, and what is wrong."""
    board = result.board
    width, height = board.size_mm
    blocks: list[dict[str, Any]] = [
        section(
            f":white_check_mark: *Board ready*\n>{escape_mrkdwn(truncate(result.intent, 400))}"
        ),
        section(
            f"*{result.spec.part_count()}* parts · *{result.spec.net_count()}* nets · "
            f"*{width:.2f} × {height:.2f} mm* · solver `{board.solver_status}`"
        ),
    ]

    facts: list[str] = []
    if board.wirelength_nm is not None:
        facts.append(f"wirelength {board.wirelength_nm / 1_000_000:.1f} mm")
    if result.repair_rounds:
        facts.append(f"{result.repair_rounds} repair round(s) before it validated")
    if duration_s is not None:
        facts.append(f"{duration_s:.1f}s total")
    if facts:
        blocks.append(context(" · ".join(facts)))

    parts = list(board.parts)
    listed = parts[:MAX_LISTED_PARTS]
    rows = "\n".join(
        f"`{p.ref:<5}` {escape_mrkdwn(p.footprint.name):<22} "
        f"{escape_mrkdwn(p.value or '')}"
        for p in listed
    )
    if rows:
        more = len(parts) - len(listed)
        tail = f"\n_…and {more} more._" if more > 0 else ""
        blocks.append(section(f"*Parts*\n{rows}{tail}"))

    for warning in board.warnings[:3]:
        blocks.append(context(f":warning: {escape_mrkdwn(warning)}"))

    blocks.append(divider())
    if reviewed:
        blocks.extend(findings_blocks(list(result.findings)))
    else:
        blocks.append(
            context(
                "_Review was skipped for this run. `@silkscreen review` in this "
                "thread runs the critic over it._"
            )
        )
    return _cap(blocks)
