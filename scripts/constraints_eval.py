"""Measure datasheet-constraint extraction against hand-checked references.

    python scripts/constraints_eval.py                 # verify references only
    python scripts/constraints_eval.py --live          # + run real extraction

The references live in ``eval/reference/*.constraints.json`` and were
hand-transcribed from the PDFs in ``eval/datasheets/`` (see eval/README.md
for the method and its limits). Two things happen here:

1. **Reference self-check** (always): every reference quote is run through
   the same mechanical verification the extractor applies to model output --
   found on its claimed page, or reported. A reference that fails its own
   provenance check is not ground truth and the run says so.

2. **Extraction eval** (``--live``, needs ``GOOGLE_API_KEY``): the real
   pipeline extracts from each PDF, the result is written next to the
   reference (``eval/results/``), and the two sets are compared:

   - *matched*: a reference constraint an extracted one corresponds to,
     with agreeing numbers;
   - *value mismatch*: corresponds, but a number disagrees -- the dangerous
     class, listed in full;
   - *missed*: in the reference, absent from the extraction;
   - *extra*: extracted, no reference counterpart. Not automatically wrong
     (the references are deliberately not exhaustive for every table row) --
     listed for the human pass.

The comparison is assistance for a manual accuracy check, not a substitute:
the PR/report numbers come from a human confirming each row against the PDF.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.agents.grounding import GroundingError, extract_pages  # noqa: E402
from silkscreen.constraints import (  # noqa: E402
    ConstraintSet,
    Decoupling,
    PowerSequencing,
    Rating,
    StrapPin,
    quote_on_page,
)
from silkscreen.constraints.extract import verification_pages  # noqa: E402

EVAL = ROOT / "eval"

#: reference stem -> (pdf name, part number)
PARTS = {
    "ams1117": ("ams1117.pdf", "AMS1117"),
    "ne555": ("ne555.pdf", "NE555"),
    "stm32f030f4": ("stm32f030f4.pdf", "STM32F030F4"),
}


def _load_reference(stem: str) -> ConstraintSet | None:
    path = EVAL / "reference" / f"{stem}.constraints.json"
    if not path.exists():
        return None
    return ConstraintSet.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _pdf_pages(stem: str) -> tuple[bytes, list[str], list[str]] | None:
    """``(bytes, layout_pages, default_pages)`` -- the same two readers the
    pipeline verifies against. Checking the references against different page
    text than the extractor sees would measure the reader, not the
    extraction."""
    path = EVAL / "datasheets" / PARTS[stem][0]
    if not path.exists():
        print(f"  (no PDF at {path.relative_to(ROOT)}; skipping)")
        return None
    data = path.read_bytes()
    layout = verification_pages(data)
    try:
        plain = extract_pages(data)
    except GroundingError as exc:
        print(f"  cannot extract page text: {exc}")
        plain = []
    if not any(t.strip() for t in layout):
        layout = plain
    if not any(t.strip() for t in layout) and not plain:
        return None
    return data, layout, plain


def self_check(stem: str) -> bool | None:
    """Verify every reference quote sits on its claimed page.

    True = clean, False = quotes failed, None = could not check (no PDF on
    disk) -- three different answers, because "the PDF is not fetched" must
    not read as either "verified" or "the references are wrong".
    """
    ref = _load_reference(stem)
    if ref is None:
        print(f"  {stem}: NO REFERENCE FILE -- was it deleted or renamed?")
        return False
    loaded = _pdf_pages(stem)
    if loaded is None:
        print(f"  {stem}: PDF not on disk; quotes UNVERIFIED this run "
              f"(fetch via the reference's document.url)")
        return None
    data, pages, plain = loaded

    ok = True
    if ref.document.sha256 and ref.document.sha256 != _sha256(data):
        print(f"  FAIL {stem}: PDF sha256 does not match the reference's "
              f"document.sha256 -- different document revision?")
        ok = False
    weak = 0
    for c in ref.all_constraints():
        prov = c.provenance
        if not (1 <= prov.page <= len(pages)):
            print(f"  FAIL {c.id}: page {prov.page} out of range")
            ok = False
        elif quote_on_page(prov.quote, pages[prov.page - 1]):
            pass
        elif (prov.page <= len(plain)
              and quote_on_page(prov.quote, plain[prov.page - 1], local=False)):
            # Real provenance, but only under the reader that cannot rule out
            # a quote assembled from two rows. Named, not hidden, and it
            # gates to needs_review in the pipeline.
            weak += 1
            print(f"  WEAK {c.id}: page {prov.page} verifies only page-wide: "
                  f"{prov.quote[:60]!r}")
        else:
            print(f"  FAIL {c.id}: quote not found on page {prov.page}: "
                  f"{prov.quote[:60]!r}")
            ok = False
    total = len(ref.all_constraints())
    print(f"  {stem}: {total} reference constraints, "
          + ("CHECK FAILED" if not ok
             else f"all quotes verify ({total - weak} as one passage, "
                  f"{weak} page-wide only)"))
    return ok


# --------------------------------------------------------------------------
# comparing an extraction to the reference
# --------------------------------------------------------------------------


def _key_tokens(text: str) -> set[str]:
    return {t for t in "".join(ch.lower() if ch.isalnum() else " "
                               for ch in text).split() if len(t) >= 2}


def _corresponds(ref, ext) -> bool:
    """Do a reference and an extracted constraint describe the same thing?"""
    if type(ref) is not type(ext):
        return False
    if isinstance(ref, Rating):
        if ref.kind != ext.kind:
            return False
        # A symbol is an identity: when both rows carry one and they differ
        # (θJA vs θJC), they are different parameters, whatever the words say.
        if ref.symbol and ext.symbol and \
                ref.symbol.strip().lower() != ext.symbol.strip().lower():
            return False
        if ref.limit.unit.strip().lower() != ext.limit.unit.strip().lower():
            return False
        a = _key_tokens(ref.parameter) | _key_tokens(ref.symbol)
        b = _key_tokens(ext.parameter) | _key_tokens(ext.symbol)
        if not a or not b:
            return False
        return len(a & b) / min(len(a), len(b)) >= 0.5
    if isinstance(ref, Decoupling):
        return ref.rail.strip().lower() == ext.rail.strip().lower()
    if isinstance(ref, StrapPin):
        return ref.pin.strip().lower() == ext.pin.strip().lower()
    # Sequencing entries are rare enough to pair by hand.
    return isinstance(ref, PowerSequencing)


def _values_agree(ref, ext) -> tuple[bool, str]:
    def near(a, b):
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(a - b) <= 1e-9 + 0.001 * max(abs(a), abs(b))

    if isinstance(ref, Rating):
        for name in ("min", "typ", "max"):
            a, b = getattr(ref.limit, name), getattr(ext.limit, name)
            if not near(a, b):
                return False, f"{name}: reference {a} vs extracted {b}"
        return True, ""
    if isinstance(ref, Decoupling):
        if not near(ref.value, ext.value):
            return False, f"value: reference {ref.value} vs extracted {ext.value}"
        if ref.unit.strip().lower() != ext.unit.strip().lower():
            return False, f"unit: reference {ref.unit!r} vs extracted {ext.unit!r}"
        return True, ""
    if isinstance(ref, StrapPin):
        if ref.required_state != ext.required_state:
            return (False, f"state: reference {ref.required_state!r} vs "
                    f"extracted {ext.required_state!r}")
        return True, ""
    return True, ""


def compare(ref: ConstraintSet, ext: ConstraintSet) -> dict:
    """Pair reference and extracted constraints, then judge the pairs.

    Two rounds, deliberately: round one pairs rows that correspond AND agree
    on values, round two pairs what is left by correspondence alone. Greedy
    single-pass pairing is order-dependent -- a byte-identical extraction in
    reversed order came out "riddled with value mismatches" (adversarial
    review, 2026-08-31), with per-package thermal rows cross-paired. The
    dangerous class ("value mismatch") must contain real disagreements, not
    pairing artifacts.
    """
    matched, mismatched, missed = [], [], []
    ext_all = list(ext.all_constraints())
    used: set[int] = set()
    refs_left = []
    for rc in ref.all_constraints():
        partner = None
        for i, ec in enumerate(ext_all):
            if i not in used and _corresponds(rc, ec) \
                    and _values_agree(rc, ec)[0]:
                partner = (i, ec)
                break
        if partner is None:
            refs_left.append(rc)
            continue
        used.add(partner[0])
        matched.append((rc, partner[1]))
    for rc in refs_left:
        partner = None
        for i, ec in enumerate(ext_all):
            if i not in used and _corresponds(rc, ec):
                partner = (i, ec)
                break
        if partner is None:
            missed.append(rc)
            continue
        used.add(partner[0])
        _agree, why = _values_agree(rc, partner[1])
        mismatched.append((rc, partner[1], why))
    extra = [ec for i, ec in enumerate(ext_all) if i not in used]
    return {"matched": matched, "mismatched": mismatched,
            "missed": missed, "extra": extra}


def run_live(stem: str, model_name: str | None = None) -> None:
    from silkscreen.agents.model import DEFAULT_MODEL, GeminiModel
    from silkscreen.constraints import extract_constraints

    ref = _load_reference(stem)
    loaded = _pdf_pages(stem)
    if ref is None or loaded is None:
        print(f"  {stem}: needs both a reference and a PDF; skipping live run")
        return
    data, _pages, _plain = loaded

    name = model_name or DEFAULT_MODEL
    print(f"  extracting {PARTS[stem][1]} with {name}...")
    cset = extract_constraints(
        GeminiModel(model=name), PARTS[stem][1], pdf_bytes=data,
        on_event=lambda e: print(f"    {e.get('event')}: "
                                 f"{ {k: v for k, v in e.items() if k != 'event'} }"),
    )
    out_dir = EVAL / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem}.extracted.json"
    out.write_text(json.dumps(cset.to_dict(), indent=2, ensure_ascii=False)
                   + "\n", encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")

    total = cset.all_constraints()
    trusted = cset.trusted()
    print(f"  extracted {len(total)} constraints; {len(trusted)} passed the "
          f"gate, {len(total) - len(trusted)} need review")

    result = compare(ref, cset)
    print(f"  vs reference ({len(ref.all_constraints())} constraints):")
    print(f"    matched          {len(result['matched'])}")
    print(f"    value mismatch   {len(result['mismatched'])}")
    for rc, ec, why in result["mismatched"]:
        print(f"      {rc.id} <> {ec.id}: {why}")
    print(f"    missed           {len(result['missed'])}")
    for rc in result["missed"]:
        print(f"      {rc.id}")
    print(f"    extra            {len(result['extra'])} "
          f"(not automatically wrong; hand-check)")
    for ec in result["extra"]:
        print(f"      {ec.id}"
              + (" [needs_review]" if ec.needs_review else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="run real extraction (needs GOOGLE_API_KEY)")
    parser.add_argument("--part", choices=sorted(PARTS),
                        help="limit to one part")
    parser.add_argument("--model", default=None,
                        help="model id for --live (default: DEFAULT_MODEL). "
                             "Whichever is used is recorded in the result's "
                             "extractor field -- accuracy numbers are only "
                             "meaningful beside the model that produced them")
    args = parser.parse_args(argv)

    # The CLI convention: .env is read here, never by library code.
    env = ROOT / ".env"
    if env.exists():
        import os
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    stems = [args.part] if args.part else sorted(PARTS)
    print("reference self-check:")
    results = [self_check(stem) for stem in stems]
    if any(r is False for r in results):
        print("reference self-check FAILED; fix the references before "
              "trusting any comparison")
        return 1
    if args.live:
        print("\nlive extraction:")
        for stem in stems:
            run_live(stem, args.model)
    return 0


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
