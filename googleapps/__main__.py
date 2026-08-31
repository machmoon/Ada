"""``python -m googleapps`` -- auth, check, run.

``auth``   one command, one browser click-through: opens Google's consent
           page, catches the loopback redirect, stores the token (0o600).
``check``  reports what is configured and whether the token is usable.
           Purely local: it reads the environment and the token file and
           makes no network call of any kind.
``run``    runs the real pipeline and then, on request, posts the Chat card
           (``--chat``), emails the results with the board attached
           (``--email``), and schedules a design review (``--schedule``) --
           but only when the adversarial review found blockers, and it says
           which way that went either way.

Exit codes follow the CLI's convention: 0 success, 1 a run or API failure,
2 a configuration problem.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import auth, calendar, chat, gmail
from .config import Config, ConfigError, load_config
from .runner import email_body, run_pipeline
from .transport import GoogleError, Transport, urllib_transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="googleapps",
        description="Silkscreen results into Google Chat, Gmail, and Calendar.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="sign in: browser consent, loopback redirect")
    sub.add_parser("check", help="report configuration and token state (no network)")

    run = sub.add_parser("run", help="generate a board, then deliver the results")
    run.add_argument("intent", help="what to build, in plain language")
    run.add_argument("-o", "--output", default="board.kicad_pcb",
                     help="where to write the .kicad_pcb (default: %(default)s)")
    run.add_argument("--chat", action="store_true",
                     help="post the run card to the configured Chat webhook")
    run.add_argument("--email", action="append", default=[], metavar="ADDR",
                     help="email the results (with the board attached); repeatable")
    run.add_argument("--schedule", action="store_true",
                     help="schedule a design-review Meet, only if the review "
                          "found blockers")
    run.add_argument("--attendee", action="append", default=[], metavar="ADDR",
                     help="attendee for the review event; repeatable")
    run.add_argument("--no-review", action="store_true",
                     help="skip the adversarial review pass")
    run.add_argument("--time-limit", type=float, default=20.0,
                     help="placement solver budget in seconds")
    return parser


def _cmd_auth(config: Config, transport: Transport) -> int:
    path = auth.run_auth_flow(config, transport)
    print(f"Signed in. Token stored at {path} (mode 0600).")
    return 0


def _cmd_check(config: Config) -> int:
    """Purely local. No request leaves this function."""
    view = config.redacted()
    print("googleapps configuration:")
    for key in ("client_id", "client_secret", "chat_webhook", "google_api_key",
                "model", "token_path"):
        print(f"  {key:15} {view[key]}")
    status = auth.token_status(config.token_path)
    private = auth.token_file_is_private(config.token_path)
    print(f"  {'token':15} {status}"
          + ("" if status == "missing" or private else "  (WARNING: not mode 0600)"))
    if status == "missing":
        print("Run `python -m googleapps auth` to sign in for Gmail and Calendar.")
    return 0


def _cmd_run(args: argparse.Namespace, config: Config, transport: Transport) -> int:
    config.require_api_key()
    if args.chat:
        config.require_webhook()
        chat.validate_webhook(config.chat_webhook)
    if args.email or args.schedule:
        # Fail before spending model calls, not after: a missing token would
        # otherwise surface only once the board already exists.
        auth.load_token(config.token_path)
    if args.schedule and not args.attendee:
        print("error: --schedule needs at least one --attendee", file=sys.stderr)
        return 2

    outcome = run_pipeline(
        config,
        args.intent,
        args.output,
        review=not args.no_review,
        time_limit_s=args.time_limit,
    )
    result = outcome.result
    print(result.summary())
    for path in result.artifacts:
        print(f"wrote {path}")

    board_path = result.board_path
    board_name = Path(args.output).stem
    failures = 0

    if args.chat:
        try:
            chat.post_run_card(
                config.chat_webhook,
                result,
                transport=transport,
                stage_lines=outcome.stage_lines,
                duration_s=outcome.duration_s,
            )
            print("posted the run card to Google Chat")
        except GoogleError as exc:
            failures += 1
            print(f"chat: {exc}", file=sys.stderr)

    if args.email:
        try:
            token = auth.access_token(config, transport)
            message_id = gmail.send_run_email(
                token,
                to=args.email,
                subject=f"silkscreen: {board_name} — {result.summary()}",
                body=email_body(outcome),
                board_path=board_path,
                transport=transport,
            )
            print(f"emailed {', '.join(args.email)} (message {message_id})")
        except (auth.AuthError, GoogleError) as exc:
            failures += 1
            print(f"gmail: {exc}", file=sys.stderr)

    if args.schedule:
        blockers = list(result.blockers)
        if args.no_review:
            # No review ran, so "no blockers" is not a fact we hold.
            print("review was skipped (--no-review) — no review event was created")
        elif not blockers:
            print("review found no blockers — no review event was created")
        else:
            try:
                token = auth.access_token(config, transport)
                event = calendar.schedule_review(
                    token,
                    board_name=board_name,
                    blocker_titles=[f.title for f in blockers],
                    attendees=args.attendee,
                    transport=transport,
                )
                print(f"review found {len(blockers)} blocker(s) — scheduled "
                      f"a design review: {event.html_link}")
                if event.meet_uri:
                    print(f"meet: {event.meet_uri}")
            except (auth.AuthError, GoogleError) as exc:
                failures += 1
                print(f"calendar: {exc}", file=sys.stderr)

    return 1 if failures else 0


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    args = _parser().parse_args(argv)
    transport = transport or urllib_transport()
    try:
        config = load_config()
        if args.command == "auth":
            return _cmd_auth(config, transport)
        if args.command == "check":
            return _cmd_check(config)
        return _cmd_run(args, config, transport)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GoogleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the pipeline's own failures
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
