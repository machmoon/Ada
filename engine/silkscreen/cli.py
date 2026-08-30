"""Command line: ``python -m silkscreen "an stm32 stepper driver"``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .agents import ModelError, generate_pcb
from .agents.model import DEFAULT_MODEL, GeminiModel
from .agents.review import Severity

_SEVERITY_MARK = {
    Severity.BLOCKER: "!!",
    Severity.MARGINAL: " !",
    Severity.NOTE: "  ",
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader, so a key never has to be exported by hand."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen",
        description="Generate a placed KiCad PCB from a description, and review it.",
    )
    parser.add_argument("intent", help="what to build, in plain language")
    parser.add_argument("-o", "--output", default="board.kicad_pcb",
                        help="where to write the .kicad_pcb (default: %(default)s)")
    parser.add_argument("-d", "--datasheet", action="append", default=[],
                        metavar="PART=URL",
                        help="a datasheet to read first; repeatable")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--time-limit", type=float, default=20.0,
                        help="placement solver budget in seconds")
    parser.add_argument("--repairs", type=int, default=3,
                        help="how many times to send a bad proposal back")
    parser.add_argument("--no-review", action="store_true",
                        help="skip the adversarial review pass")
    parser.add_argument("--no-route", action="store_true",
                        help="stop after placement, leaving the copper empty")
    parser.add_argument("--board-only", action="store_true",
                        help="write only the routed .kicad_pcb, no schematic, "
                             "project file or pre-routing board")
    args = parser.parse_args(argv)

    _load_dotenv(Path.cwd() / ".env")

    datasheets: dict[str, str] = {}
    for entry in args.datasheet:
        part, sep, url = entry.partition("=")
        if not sep:
            parser.error(f"--datasheet needs PART=URL, got {entry!r}")
        datasheets[part.strip()] = url.strip()

    try:
        model = GeminiModel(args.model)
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for part in datasheets:
        print(f"reading datasheet: {part}", file=sys.stderr)

    try:
        result = generate_pcb(
            model,
            args.intent,
            datasheets=datasheets,
            output=args.output,
            max_repairs=args.repairs,
            time_limit_s=args.time_limit,
            review=not args.no_review,
            route=not args.no_route,
            emit_stages=not args.board_only,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(result.summary())
    print()
    for part in result.board.parts:
        print(f"  {part.ref:<5} {part.footprint.name:<20} {part.value}")

    if result.findings:
        print()
        print("Review:")
        for f in result.findings:
            print(f"  {_SEVERITY_MARK[f.severity]} {f.title}")
            if f.detail:
                print(f"       {f.detail}")
            if f.citation:
                print(f"       cited: {f.citation}")
            if f.suggested_fix:
                print(f"       fix:   {f.suggested_fix}")

    # Say plainly which nets have no copper. A board reported as routed when
    # some nets are still ratsnest is the failure this output exists to
    # prevent -- the missing connections are invisible until fabrication.
    if result.route is not None:
        print()
        print(f"Routing: {result.route.summary()}")
        for net, reason in sorted(result.route.unrouted.items()):
            print(f"  unrouted {net}: {reason}")

    for w in result.board.warnings:
        print(f"  note: {w}", file=sys.stderr)

    if result.artifacts:
        print()
        for path in result.artifacts:
            print(f"wrote {path}")
        if result.project_path:
            print()
            print(f"open in KiCad:  {result.project_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
