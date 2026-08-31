"""``silkscreen-constraints datasheet.pdf --part STM32F030F4 -o out.json``.

Extracts a constraint set from one datasheet and writes it as JSON, then
prints the honest tally: how many constraints came out, how many cleared the
gate, and why each of the rest needs a human. The JSON is the deliverable --
a data asset other tools (``silkscreen.constraints.check_board``, the audit
CLI, a future library) consume.

``--report`` prints a review worksheet instead: every gated constraint with
its quote, page and reason, in the order a human checking against the PDF
would want them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import extract_constraints
from .schema import ConstraintSet

__all__ = ["main"]


def _load_dotenv(path: Path) -> None:
    """Same minimal reader the other CLIs use, for the same reason."""
    import os

    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _tally(cset: ConstraintSet, out) -> None:
    total = cset.all_constraints()
    trusted = cset.trusted()
    print(
        f"{cset.part_number}: {len(total)} constraints extracted, "
        f"{len(trusted)} passed the gate, {len(total) - len(trusted)} need "
        f"human review",
        file=out,
    )
    for c in total:
        if c.needs_review:
            print(f"  review {c.id}: {c.review_reason}", file=out)


def _worksheet(cset: ConstraintSet, out) -> None:
    print(f"Review worksheet for {cset.part_number} "
          f"(document sha256 {cset.document.sha256[:12]}...)", file=out)
    for c in sorted(cset.all_constraints(), key=lambda c: c.provenance.page):
        prov = c.provenance
        mark = "OK " if not c.needs_review else "?? "
        print(f"\n{mark}{c.id}  (p.{prov.page}"
              + (f", {prov.section}" if prov.section else "") + ")", file=out)
        if prov.quote:
            print(f'    "{prov.quote}"', file=out)
        if c.needs_review:
            print(f"    review: {c.review_reason}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen-constraints",
        description="Extract machine-readable constraints from a datasheet PDF.",
    )
    parser.add_argument("pdf", help="path to the datasheet PDF")
    parser.add_argument("--part", required=True,
                        help="the part number the datasheet covers")
    parser.add_argument("--manufacturer", default="")
    parser.add_argument("-o", "--output", default=None,
                        help="where to write the JSON "
                             "(default: <part>.constraints.json)")
    parser.add_argument("--model", default=None,
                        help="Gemini model name (default: the package default)")
    parser.add_argument("--report", action="store_true",
                        help="print the human-review worksheet after extracting")
    args = parser.parse_args(argv)

    _load_dotenv(Path.cwd() / ".env")

    from ..agents.model import DEFAULT_MODEL, GeminiModel, ModelError

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"error: no such file: {pdf_path}", file=sys.stderr)
        return 2
    pdf_bytes = pdf_path.read_bytes()

    try:
        model = GeminiModel(args.model or DEFAULT_MODEL)
        cset = extract_constraints(
            model,
            args.part,
            pdf_bytes=pdf_bytes,
            manufacturer=args.manufacturer,
            on_event=lambda e: print(f"  {e}", file=sys.stderr),
        )
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.output or f"{args.part}.constraints.json")
    out_path.write_text(
        json.dumps(cset.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path}", file=sys.stderr)
    _tally(cset, sys.stderr)
    if args.report:
        _worksheet(cset, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
