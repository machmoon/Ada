# Constraint-extraction ground truth

Hand-checked reference constraint sets for measuring what
`silkscreen.constraints` actually extracts, against three real datasheets:

| Part | Document | Why it is here |
|---|---|---|
| AMS1117 | AMS ds1117 (8 pages) | LDO: required output cap with a dielectric demand, thermal per package, minimum load current |
| NE555 | TI SLFS022K (39 pages) | strap pin (RESET to VCC when unused), rail-relative abs-max (VI ≤ VCC), an *advisory* decoupling note the extractor must not present as a requirement, six-package thermal table (column-soup trap) |
| STM32F030F4 | ST DS9773 Rev 5 (93 pages) | per-pin VDD decoupling with values from a *figure*, VDDA level relation, rail-relative VIN limits — and **no** BOOT0 strap requirement (that lives in the reference manual, so an extractor emitting one from this PDF is fabricating) |

## Method, honestly stated

The references were transcribed by Claude reading the PDFs' extracted page
text directly — a separate path from the Gemini extraction pipeline under
test, but not a human. Each entry carries the page and verbatim quote it came
from, and `scripts/constraints_eval.py` mechanically verifies every quote
against its claimed page (the same check the extractor applies to model
output) — run it with no arguments. **A human should still spot-check the
reference rows against the PDFs before the accuracy numbers are quoted
anywhere that matters.**

The references are deliberately *not* exhaustive transcriptions of every
table row. They cover the constraint kinds the schema exists for, chosen to
include the known failure modes above. An extraction that produces rows the
reference lacks is not automatically wrong — the eval lists those as `extra`
for the human pass — but a reference row the extraction misses or contradicts
is a real miss or a real error.

## Files

- `datasheets/` — the PDFs. **Not committed** (vendor copyright, and TI's
  notice forbids redistribution); fetch them with the URLs recorded in each
  reference's `document.url`.
- `reference/*.constraints.json` — the hand-checked sets, schema
  `silkscreen.constraints` v1.0. Every entry has `confirmed: true` and a
  `notes` field saying it is a hand transcription. `provenance.verified` and
  `document.sha256` were stamped mechanically by the same `quote_on_page`
  check the extractor uses, against the recorded PDFs — the self-check
  re-verifies both on every run.
- `results/` — live-extraction output written by
  `python scripts/constraints_eval.py --live` (needs `GOOGLE_API_KEY`).

## Measured accuracy (2026-08-31, `gemini-3.1-flash-lite`)

Run: `python scripts/constraints_eval.py --live --model gemini-3.1-flash-lite`.
Quote `--model`: the repo's `DEFAULT_MODEL` (`gemini-3.7-flash`) and
`gemini-3-flash-preview` both answered 503 to every *document* request on the
key used here, while answering plain text calls normally, so these numbers
come from the flash-lite tier. **Accuracy numbers are only meaningful beside
the model that produced them** — do not carry these forward to another model.

### What the automatic comparison says

| Part | reference | extracted | matched | value mismatch | missed | extra |
|---|---|---|---|---|---|---|
| AMS1117 | 16 | 14 | 1 | 1 | 14 | 12 |
| NE555 | 16 | 15 | 7 | 4 | 5 | 4 |
| STM32F030F4 | 20 | 23 | 8 | 1 | 11 | 14 |
| **Total** | **52** | **52** | **16** | **6** | **30** | **30** |

**`matched` is a lower bound on extraction quality, not an accuracy score.**
The comparison pairs constraints by subject tokens, and it demonstrably
conflates naming: AMS1117 reports `thermal.theta-ja-sot-223` as *missed* and
`thermal.ja-3` as *extra* when they are the same table row read correctly.
Anyone quoting a percentage from this table is quoting the matcher.

### What a full hand-check says (AMS1117 only)

All 14 AMS1117 constraints were read against the PDF by hand:

- **13 of 14 are substantively correct** — every absolute-maximum row, all
  three thermal resistances, the reference voltage, current limit, minimum
  load, quiescent and adjust-pin currents, and the 22 µF output capacitor.
- **1 is fabricated**: a 10 µF adjust-terminal bypass. Its quote is genuine,
  verbatim and on the right page — and says only that bypassing the adjust
  terminal *increases the output capacitor requirement*, which 22 µF covers.
  No 10 appears anywhere in it.

NE555 and STM32F030F4 have **not** had an equivalent line-by-line hand check.
Their numbers above are matcher output only.

### The failure mode this exposed, and what now catches it

Quote verification answers *"is this text on that page"*. It does not answer
*"does that text say this"*. Two constraints in this run had genuine,
verified, correctly-paged provenance and an assertion the quote does not
support:

- the AMS1117 10 µF above;
- an STM32 `BOOT0` "no-float" strap requirement, cited to a *Boot modes*
  paragraph that merely describes that a boot pin selects among three boot
  options. This corpus exists partly to catch exactly that: BOOT0's strap
  requirement lives in the reference manual, not this datasheet.

Both passed the gate (`needs_review: false`, confidence 0.8–0.9) and would
have been enforced against a board. The gate now also requires that the
numbers a constraint *asserts* appear in the quote it cites, which moves
**6 of the 52 extracted constraints from silently trusted to human-confirm**
(48 → 42 trusted), the 10 µF among them. It is skipped for `confirmed`
constraints, because a value read off a figure — the STM32's 100 nF is drawn
in Figure 13 — is nowhere in any quotable sentence, and a person has already
signed for those.

It does **not** catch the BOOT0 case, which asserts no number. A requirement
inferred from descriptive prose is still the open hole in the trust ladder;
see TODO.txt feature 17.
