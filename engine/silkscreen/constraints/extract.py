"""Extract constraints from a datasheet PDF, then distrust the extraction.

Two model calls, not one: ratings tables and design requirements are read by
different prompts, because a prompt that asks for everything gets a response
that skims everything. Both calls demand a page, a section and a verbatim
quote for every entry.

Then the mechanical part: every quote is searched for in the pypdf-extracted
text of its claimed page (:func:`verify_provenance`). A quote that is found
marks its provenance ``verified``; one that is not forces ``needs_review`` on
with the reason recorded. The model asserting "page 14" is a claim; the quote
actually sitting on page 14 is evidence. Table text extracts imperfectly, so
the match is token-overlap, not equality -- tolerant of column soup, still
fatal to an invented citation.

Nothing in this module ever fabricates a number or upgrades trust. The gate
(:func:`gate`) only ever *adds* reasons to review; ``confirmed`` stays False
until a human sets it.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Any

from ..agents.model import Document, Model, ModelError, parse_json
from .schema import (
    Decoupling,
    Limit,
    PowerSequencing,
    Provenance,
    Rating,
    RatingKind,
    StrapPin,
)

__all__ = [
    "extract_ratings",
    "extract_design_requirements",
    "verify_provenance",
    "gate",
    "RATINGS_PROMPT",
    "DESIGN_PROMPT",
    "CONFIDENCE_FLOOR",
]

#: Below this the extractor's own confidence forces human review.
CONFIDENCE_FLOOR = 0.8

#: Token overlap with the page text at or above this counts as "found".
#: Tables extract as column soup, so demand most tokens, not all of them.
_MATCH_THRESHOLD = 0.6


_PROVENANCE_RULES = """\
Rules that apply to EVERY entry:
- "page" is the 1-based PDF page you read the entry from (the viewer's page
  index, not the number printed on the page).
- "section" is the table or heading name as the document gives it.
- "quote" is VERBATIM source text from that page -- the row or sentence the
  entry transcribes. Never paraphrase inside "quote".
- "confidence" is your own 0..1 estimate that the entry is a faithful
  transcription. Use low values freely; a wrong number presented confidently
  is the worst outcome possible here.
- If the document does not state something, OMIT it. An empty list is a valid
  and useful answer. Do not fill gaps from general knowledge of similar parts.
"""

RATINGS_PROMPT = f"""\
You are transcribing the ratings and characteristics tables of an electronic
component datasheet into JSON. Transcribe only what the document states.

Return ONE JSON object, no prose, no code fence:

{{
  "ratings": [
    {{"kind": "absolute_maximum|operating_condition|thermal|pin_electrical",
     "parameter": "<the parameter column, e.g. 'Supply voltage'>",
     "symbol": "<the symbol column if present, e.g. 'VDD'>",
     "min": <number or null>, "typ": <number or null>, "max": <number or null>,
     "unit": "<the unit column, e.g. 'V', 'mA', '°C'>",
     "conditions": "<the test-conditions column, verbatim, or ''>",
     "pins": ["<datasheet pin names this applies to, [] if part-wide>"],
     "page": <int>, "section": "<table name>", "quote": "<verbatim row text>",
     "confidence": <0..1>}}
  ]
}}

Which tables to transcribe, as "kind":
- absolute_maximum: the absolute maximum ratings table.
- operating_condition: recommended / general operating conditions.
- thermal: thermal characteristics (Tj max, theta-JA, power dissipation).
- pin_electrical: DC characteristics of pins (VIH, VIL, VOH, VOL, leakage,
  drive current). Transcribe the main I/O rows; skip per-peripheral duplicates.

{_PROVENANCE_RULES}"""

DESIGN_PROMPT = f"""\
You are transcribing the design requirements of an electronic component
datasheet into JSON: what the document requires around the part for it to
work. Transcribe only what the document states.

Return ONE JSON object, no prose, no code fence:

{{
  "decoupling": [
    {{"rail": "<supply pin/rail name, e.g. 'VDD', 'VDDA'>",
     "value": <number or null>, "unit": "<e.g. 'nF', 'uF', '' if unstated>",
     "count": <int or null>, "per_pin": <true if one per supply pin>,
     "max_distance_mm": <number or null, only if the document gives a distance>,
     "placement": "<the document's placement wording, e.g. 'as close as
                    possible to the pin', or ''>",
     "cap_type": "<dielectric/type demands, or ''>",
     "page": <int>, "section": "<heading>", "quote": "<verbatim>",
     "confidence": <0..1>}}
  ],
  "power_sequencing": [
    {{"rails": ["<rail names in required order, first up first>"],
     "requirement": "<the requirement in the document's words, incl. timing>",
     "page": <int>, "section": "<heading>", "quote": "<verbatim>",
     "confidence": <0..1>}}
  ],
  "strap_pins": [
    {{"pin": "<datasheet pin name, e.g. 'BOOT0', 'NRST', 'EN'>",
     "required_state": "high|low|pull-up|pull-down|no-float|external-resistor",
     "resistor_value": <number or null>, "resistor_unit": "<e.g. 'kOhm' or ''>",
     "condition": "<when it applies, e.g. 'to boot from main flash'>",
     "page": <int>, "section": "<heading>", "quote": "<verbatim>",
     "confidence": <0..1>}}
  ]
}}

Notes:
- decoupling covers bypass/decoupling/bulk capacitors the document requires
  on supply pins, including reference and analog supplies.
- power_sequencing: if the document explicitly says no sequencing is
  required, record that as an entry with "rails": [] -- it is a fact worth
  keeping. If it says nothing, return [].
- strap_pins covers boot/mode/configuration/enable/reset pins the document
  requires tied, pulled or never floated.

{_PROVENANCE_RULES}"""


# --------------------------------------------------------------------------
# parsing model output into schema objects
# --------------------------------------------------------------------------


def _real(value: Any) -> float | None:
    """A finite real number, or None. Never a bool: JSON ``true`` is not 1.0,
    and a malformed field must degrade trust, not maximise it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _text(entry: dict, key: str) -> str:
    """A string field, or empty. An explicit JSON null must not become the
    four-letter string "None" -- which could even *verify* against a page."""
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _str_list(entry: dict, key: str) -> tuple[str, ...]:
    """A list of strings, or empty. A bare string is not one string -- it is
    malformed, and iterating it would yield per-character 'pins'."""
    value = entry.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _provenance(entry: dict) -> Provenance:
    page = entry.get("page")
    good_page = isinstance(page, int) and not isinstance(page, bool) and page > 0
    return Provenance(
        page=page if good_page else 0,
        section=_text(entry, "section"),
        quote=_text(entry, "quote"),
    )


def _confidence(entry: dict) -> float:
    value = _real(entry.get("confidence"))
    if value is not None and 0.0 <= value <= 1.0:
        return value
    return 0.0  # an unusable confidence is no confidence


def _number(entry: dict, key: str) -> float | None:
    return _real(entry.get(key))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unnamed"


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


_KIND_PREFIX = {
    RatingKind.ABSOLUTE_MAXIMUM: "abs-max",
    RatingKind.OPERATING_CONDITION: "op",
    RatingKind.THERMAL: "thermal",
    RatingKind.PIN_ELECTRICAL: "pin",
}


def extract_ratings(model: Model, doc: Document, part_number: str) -> list[Rating]:
    """One model call over the PDF; returns ungated :class:`Rating` rows."""
    prompt = f"{RATINGS_PROMPT}\nThe part is: {part_number}"
    data = parse_json(model.generate(prompt, documents=[doc], temperature=0.0,
                                     max_output_tokens=16384))
    if not isinstance(data, dict):
        raise ModelError(f"Expected a JSON object, got {type(data).__name__}")

    taken: set[str] = set()
    out: list[Rating] = []
    for entry in data.get("ratings") or []:
        if not isinstance(entry, dict) or not entry.get("parameter"):
            continue
        try:
            kind = RatingKind(str(entry.get("kind", "")))
        except ValueError:
            continue  # an entry of no known kind is not silently re-binned
        name = _text(entry, "symbol") or str(entry["parameter"])
        out.append(
            Rating(
                id=_unique_id(f"{_KIND_PREFIX[kind]}.{_slug(name)}", taken),
                kind=kind,
                parameter=str(entry["parameter"]),
                symbol=_text(entry, "symbol"),
                limit=Limit(
                    unit=_text(entry, "unit"),
                    min=_number(entry, "min"),
                    typ=_number(entry, "typ"),
                    max=_number(entry, "max"),
                    conditions=_text(entry, "conditions"),
                ),
                pins=_str_list(entry, "pins"),
                provenance=_provenance(entry),
                confidence=_confidence(entry),
            )
        )
    return out


def extract_design_requirements(
    model: Model, doc: Document, part_number: str
) -> tuple[list[Decoupling], list[PowerSequencing], list[StrapPin]]:
    """One model call; returns ungated decoupling/sequencing/strap entries."""
    prompt = f"{DESIGN_PROMPT}\nThe part is: {part_number}"
    data = parse_json(model.generate(prompt, documents=[doc], temperature=0.0,
                                     max_output_tokens=16384))
    if not isinstance(data, dict):
        raise ModelError(f"Expected a JSON object, got {type(data).__name__}")

    taken: set[str] = set()

    decoupling = [
        Decoupling(
            id=_unique_id(f"decouple.{_slug(_text(e, 'rail'))}", taken),
            rail=_text(e, "rail"),
            value=_number(e, "value"),
            unit=_text(e, "unit"),
            count=(e["count"]
                   if isinstance(e.get("count"), int)
                   and not isinstance(e.get("count"), bool) else None),
            per_pin=e.get("per_pin") is True,
            max_distance_mm=_number(e, "max_distance_mm"),
            placement=_text(e, "placement"),
            cap_type=_text(e, "cap_type"),
            provenance=_provenance(e),
            confidence=_confidence(e),
        )
        for e in data.get("decoupling") or []
        if isinstance(e, dict) and e.get("rail")
    ]

    sequencing = [
        PowerSequencing(
            id=_unique_id("power-seq", taken),
            rails=_str_list(e, "rails"),
            requirement=_text(e, "requirement"),
            provenance=_provenance(e),
            confidence=_confidence(e),
        )
        for e in data.get("power_sequencing") or []
        if isinstance(e, dict) and (e.get("requirement") or e.get("rails"))
    ]

    straps = [
        StrapPin(
            id=_unique_id(f"strap.{_slug(_text(e, 'pin'))}", taken),
            pin=_text(e, "pin"),
            required_state=_text(e, "required_state"),
            resistor_value=_number(e, "resistor_value"),
            resistor_unit=_text(e, "resistor_unit"),
            condition=_text(e, "condition"),
            provenance=_provenance(e),
            confidence=_confidence(e),
        )
        for e in data.get("strap_pins") or []
        if isinstance(e, dict) and e.get("pin")
    ]

    return decoupling, sequencing, straps


# --------------------------------------------------------------------------
# mechanical provenance verification
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9.µμ]+")
#: A signed number not glued to a letter ("SOT-223" yields 223, not -223).
_NUMBER_RE = re.compile(r"(?<![a-z0-9])-?\d+(?:\.\d+)?")


def _normalise(text: str) -> str:
    """One canonical form for quote and page alike, or nothing matches."""
    text = text.casefold()                    # folds µ (U+00B5) to μ (U+03BC)
    text = text.replace("–", "-").replace("−", "-")  # dashes
    text = text.replace("±", "").replace("+", "")
    text = re.sub(r"(\d),(?=\d{3})", r"\1", text)   # 1,100 -> 1100
    text = re.sub(r"-\s+(?=\d)", "-", text)         # "- 65" -> "-65"
    return text


def _tokens(text: str) -> tuple[list[str], list[str]]:
    """``(words, numbers)`` from normalised text.

    Numbers are kept apart because they are the payload: a limit row with one
    digit wrong is exactly the fabrication the check exists to catch, so
    numbers are matched exactly and sign-sensitively, never by ratio.
    """
    text = _normalise(text)
    numbers = _NUMBER_RE.findall(text)
    words = [
        t for t in _WORD_RE.findall(text)
        if len(t) >= 2 and not re.fullmatch(r"[\d.]+", t)
    ]
    return words, numbers


def quote_on_page(quote: str, page_text: str) -> bool:
    """Is the quote plausibly on this page?

    Two tests, both required. Every *number* in the quote must appear on the
    page exactly (after shared normalisation), sign included -- ratings rows
    share nearly all their vocabulary, so a wrong or invented number is the
    one thing word overlap cannot catch, and it is also the payload. The
    *words* then need 0.6 overlap, which tolerates pypdf's column soup while
    still rejecting a row whose subject ("VBAT", "backup") is not on the page.
    A quote too short to carry evidence (under three informative tokens)
    never verifies -- unverifiable is the honest answer for it.
    """
    words, numbers = _tokens(quote)
    if len(words) + len(numbers) < 3 or not words:
        return False
    page_words, page_numbers = _tokens(page_text)
    have_numbers = set(page_numbers)
    if any(n not in have_numbers for n in numbers):
        return False
    have_words = set(page_words)
    found = sum(1 for t in words if t in have_words)
    return found / len(words) >= _MATCH_THRESHOLD


def verify_provenance(constraint: Any, pages: list[str]) -> Any:
    """Return a copy with ``provenance.verified`` set from the page text.

    ``pages`` is 0-indexed extracted text (``grounding.extract_pages``);
    provenance pages are 1-based. A quote found on the claimed page verifies;
    found elsewhere or nowhere does not, and :func:`gate` will say why.
    """
    prov = constraint.provenance
    if not prov.quote or not (1 <= prov.page <= len(pages)):
        return constraint
    if quote_on_page(prov.quote, pages[prov.page - 1]):
        return replace(
            constraint,
            provenance=Provenance(prov.page, prov.section, prov.quote, True),
        )
    return constraint


def _where_else(quote: str, pages: list[str]) -> int | None:
    for i, text in enumerate(pages):
        if quote_on_page(quote, text):
            return i + 1
    return None


def gate(constraint: Any, pages: list[str] | None = None) -> Any:
    """Decide ``needs_review`` honestly. Only ever adds reasons, never removes.

    A constraint passes the gate only when its quote was mechanically found on
    its claimed page AND the extractor's own confidence clears the floor.
    Everything else keeps ``needs_review=True`` with the reason spelled out --
    including where the quote *was* found, when it was found on a different
    page, because that is usually an off-by-one worth one human glance.
    """
    prov = constraint.provenance
    reasons: list[str] = []
    if not prov.quote:
        reasons.append("no verbatim quote was extracted")
    if prov.page < 1:
        reasons.append("no source page was extracted")
    if prov.quote and prov.page >= 1 and not prov.verified:
        reason = f"quote not found on page {prov.page}"
        if pages:
            elsewhere = _where_else(prov.quote, pages)
            if elsewhere is not None:
                reason += f" (found on page {elsewhere})"
        reasons.append(reason)
    if not constraint.confidence >= CONFIDENCE_FLOOR:  # NaN-safe ordering
        reasons.append(
            f"extractor confidence {constraint.confidence:.2f} is below "
            f"{CONFIDENCE_FLOOR:.2f}"
        )

    # Additive for real: a reason already on the constraint -- a human's
    # annotation, or a prior gate's -- survives re-gating. Only a constraint
    # with no reasons from anywhere may pass.
    existing = [r for r in constraint.review_reason.split("; ") if r]
    merged = list(dict.fromkeys(existing + reasons))
    if merged:
        return replace(
            constraint, needs_review=True, review_reason="; ".join(merged)
        )
    return replace(constraint, needs_review=False, review_reason="")
