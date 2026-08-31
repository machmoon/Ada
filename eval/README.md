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
  `notes` field saying it is a hand transcription.
- `results/` — live-extraction output written by
  `python scripts/constraints_eval.py --live` (needs `GOOGLE_API_KEY`).
