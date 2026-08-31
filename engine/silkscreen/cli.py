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


def _print_fit_receipt(fit) -> None:
    """The verified-fit receipt: signed per-axis margins, then warnings."""
    margins = ", ".join(
        f"{axis} {fit.margins_nm[axis] / 1_000_000:+.3f} mm" for axis in ("x", "y", "z")
    )
    print(f"Case fit: {margins} (negative = collision)")
    for warning in fit.warnings:
        print(f"  note: {warning}", file=sys.stderr)


def _case_main(argv: list[str]) -> int:
    """``silkscreen case board.kicad_pcb`` -- retrofit a case onto a board.

    ``--no-model`` emits the deterministic default-spec case with no API call
    at all: default :func:`parse_enclosure_spec` dict + measured board
    envelope + deterministic emitter, fully offline.
    """
    parser = argparse.ArgumentParser(
        prog="silkscreen case",
        description="Generate a 3D-printable OpenSCAD case for an existing "
        "KiCad board.",
    )
    parser.add_argument("board", help="the .kicad_pcb to fit a case around")
    parser.add_argument("-o", "--output", default="enclosure.scad",
                        help="where to write the .scad (default: %(default)s)")
    parser.add_argument("--intent", default="",
                        metavar="TEXT",
                        help="natural-language case intent, e.g. "
                             "'rounded corners, USB cutout left'")
    parser.add_argument("--stl", action="store_true",
                        help="additionally render enclosure.stl via the local "
                             "openscad binary")
    parser.add_argument("--no-model", action="store_true",
                        help="emit the deterministic default case with no "
                             "model call (fully offline)")
    parser.add_argument("--rigorous", action="store_true",
                        help="run the full strict verify-and-repair loop "
                             "(slower); default is demo-fast, where a fit "
                             "failure rides the receipt as a warning")
    args = parser.parse_args(argv)

    from .enclosure.board_shape import board_envelope
    from .enclosure.emit import emit_scad
    from .enclosure.errors import EnclosureError, RenderUnavailable
    from .enclosure.ir import parse_enclosure_spec
    from .enclosure.verify import verify_fit

    try:
        envelope = board_envelope(args.board)
    except (OSError, ValueError, EnclosureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fit = None
    if args.no_model:
        if args.intent:
            print("note: --no-model ignores --intent (deterministic default "
                  "case)", file=sys.stderr)
        spec = parse_enclosure_spec({})
        repair_rounds = 0
    else:
        _load_dotenv(Path.cwd() / ".env")
        try:
            from .agents.enclosure import propose_enclosure
            from .agents.model import CHEAP_MODEL
        except ImportError as exc:
            print(
                "error: model-driven case generation is not available in this "
                f"build ({exc}); use --no-model for the deterministic default "
                "case",
                file=sys.stderr,
            )
            return 2
        try:
            model = GeminiModel(CHEAP_MODEL)
        except ModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            # The loop's accepted FitReport is the receipt; no re-verification.
            spec, fit, repair_rounds = propose_enclosure(
                model, envelope, style_hint=args.intent, rigorous=args.rigorous
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        if fit is None:  # the --no-model spec was never verified by a loop
            fit = verify_fit(spec, envelope)
        scad = emit_scad(spec, envelope)
    except EnclosureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.write_text(scad, encoding="utf-8")
    print(f"wrote {out}")
    _print_fit_receipt(fit)
    if repair_rounds:
        print(f"  repair rounds: {repair_rounds}", file=sys.stderr)

    if args.stl:
        import importlib

        try:
            render = importlib.import_module("silkscreen.enclosure.render")
        except ImportError as exc:
            print(
                f"error: STL rendering is not available in this build ({exc})",
                file=sys.stderr,
            )
            return 2
        stl_path = out.with_suffix(".stl")
        try:
            render.render_stl(scad, stl_path)
        except RenderUnavailable as exc:
            print(
                f"error: {exc.executable!r} not found on PATH; install "
                "OpenSCAD to render an STL (the .scad above is complete "
                "without it)",
                file=sys.stderr,
            )
            return 2
        except EnclosureError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {stl_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # A tiny subcommand dispatch, kept out of argparse so the historical
    # ``silkscreen "an ldo board"`` form keeps working unchanged.
    if argv and argv[0] == "case":
        return _case_main(argv[1:])

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
    parser.add_argument("--case", action="store_true",
                        help="also generate a 3D-printable case; writes "
                             "enclosure.scad beside the output")
    parser.add_argument("--case-style", default="", metavar="TEXT",
                        help="natural-language case intent, e.g. "
                             "'rounded corners, USB cutout left'")
    parser.add_argument("--rigorous", action="store_true",
                        help="with --case: run the case proposal's full "
                             "strict verify-and-repair loop (slower); "
                             "default is demo-fast, where a fit failure "
                             "rides the receipt as a warning")
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

    # Opt-in only, so a run without --case makes the exact call it always
    # made (the plan's both-drivers-identical-by-default rule).
    case_kwargs = (
        {
            "enclosure": True,
            "enclosure_style": args.case_style,
            "enclosure_rigorous": args.rigorous,
        }
        if args.case
        else {}
    )

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
            **case_kwargs,
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

    # The case's verified-fit receipt, or its honest absence: an exhausted
    # enclosure repair budget degrades to no case, never to a failed run.
    if args.case:
        case = getattr(result, "enclosure", None)
        print()
        if case is None:
            print("case: generation failed; the board is delivered without one",
                  file=sys.stderr)
        else:
            _print_fit_receipt(case.fit)
            if case.repair_rounds:
                print(f"  repair rounds: {case.repair_rounds}", file=sys.stderr)

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
