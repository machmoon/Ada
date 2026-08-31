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

import io
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


def verification_pages(pdf_bytes: bytes) -> list[str]:
    """Page text for the provenance check, read layout-preserving.

    A second, independent read of the PDF rather than a call to
    :func:`~..agents.grounding.extract_pages` -- the same discipline
    ``audit/geometry.py`` follows in re-reading a board instead of calling
    ``kicad.py``: a check written in terms of the reader that fed the model
    shares its blind spots.

    The substantive difference is pypdf's *layout* mode. The default mode
    emits a table's labels and then its values, so the two halves of one row
    land at opposite ends of the page -- measured on the STM32F030F4
    datasheet, "VDD Standard operating voltage" sits at token 21 and its
    "2.4 3.6" at token 393. Under that text no locality requirement can tell
    a genuine row from a quote stitched out of two unrelated ones. Layout
    mode puts the row back together (those same tokens land adjacent), which
    is what makes :func:`quote_on_page`'s window meaningful.

    Falls back to the shared reader, then to no pages at all; both degrade
    into "nothing verifies", which the gate reports rather than hides.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if getattr(reader, "is_encrypted", False) and not reader.decrypt(""):
            return []
        out = []
        for page in reader.pages:
            try:
                out.append(page.extract_text(extraction_mode="layout") or "")
            except Exception:
                # One unreadable page must not lose the other ninety-two.
                out.append("")
        return out
    except Exception:
        return []


def _seq(text: str) -> list[tuple[str, str]]:
    """Tokens in page order, tagged ``"w"``/``"n"``.

    :func:`_tokens` throws position away, which is what let a quote assembled
    from two unrelated rows verify: every token existed *somewhere* on the
    page. Keeping the order lets the match be required to be local.
    """
    text = _normalise(text)
    items: list[tuple[int, str, str]] = [
        (m.start(), "n", m.group()) for m in _NUMBER_RE.finditer(text)
    ]
    items += [
        (m.start(), "w", m.group())
        for m in _WORD_RE.finditer(text)
        if len(m.group()) >= 2 and not re.fullmatch(r"[\d.]+", m.group())
    ]
    items.sort()
    return [(kind, tok) for _pos, kind, tok in items]


def quote_on_page(quote: str, page_text: str, *, local: bool = True) -> bool:
    """Is the quote plausibly on this page, as one passage?

    Three tests, all required. Every *number* in the quote must appear
    exactly, sign included -- ratings rows share nearly all their vocabulary,
    so a wrong or invented number is the one thing word overlap cannot catch,
    and it is also the payload. The *words* then need 0.6 overlap, which
    tolerates pypdf's column soup while still rejecting a row whose subject
    ("VBAT", "backup") is not on the page. A quote too short to carry
    evidence (under three informative tokens) never verifies -- unverifiable
    is the honest answer for it.

    The third test is **locality**, and it is why this is not a set
    intersection. A ratings page holds dozens of rows drawn from the same
    vocabulary, so a quote stitched together from two unrelated rows -- one
    row's parameter name against another row's number -- satisfies both of
    the other tests and verifies as provenance for a limit the datasheet
    never states. That is a fabrication wearing a page number, which is the
    single worst output this module can produce. So both tests must pass
    inside one window of the page rather than across the whole of it. The
    window is generous (three times the quote, minimum forty tokens) because
    pypdf interleaves columns and the goal is only to rule out matches
    assembled from opposite ends of a page.
    """
    q = _seq(quote)
    q_words = [t for kind, t in q if kind == "w"]
    q_numbers = [t for kind, t in q if kind == "n"]
    if len(q) < 3 or not q_words:
        return False

    page = _seq(page_text)
    span = len(page) if not local else max(len(q) * 3, len(q) + 40)
    step = max(1, span // 4)
    starts = [0] if len(page) <= span else range(0, len(page) - span + 1, step)
    need = set(q_numbers)
    for start in starts:
        window = page[start:start + span]
        have_numbers = {t for kind, t in window if kind == "n"}
        if any(n not in have_numbers for n in need):
            continue
        have_words = {t for kind, t in window if kind == "w"}
        found = sum(1 for t in q_words if t in have_words)
        if found / len(q_words) >= _MATCH_THRESHOLD:
            return True
    return False


#: Why a page-wide-only match still costs a human glance.
WEAK_PROVENANCE = (
    "quote verified only page-wide: its words and numbers are not adjacent "
    "in the layout text, so it may be assembled from separate rows"
)


def verify_provenance(
    constraint: Any, pages: list[str], weak_pages: list[str] | None = None
) -> Any:
    """Return a copy with ``provenance.verified`` set from the page text.

    ``pages`` is 0-indexed layout-mode text (:func:`verification_pages`);
    provenance pages are 1-based. A quote found *as one passage* on its
    claimed page verifies cleanly.

    ``weak_pages`` is the same document under the default reader, and exists
    because neither reader is reliably better: layout mode reassembles table
    rows but drops rotated text, while the default mode reads those pages and
    scatters the rows. A quote that only matches page-wide in ``weak_pages``
    still verifies -- refusing it would throw away real provenance on pages
    layout mode cannot read -- but it carries :data:`WEAK_PROVENANCE` into
    the gate, so it is a constraint a human is asked to confirm rather than
    one a checker silently trusts. That is the whole distinction: the weak
    match is the one that cannot rule out a quote stitched from two rows.
    """
    prov = constraint.provenance
    if not prov.quote or prov.page < 1:
        return constraint
    idx = prov.page - 1
    verified = Provenance(prov.page, prov.section, prov.quote, True)
    if idx < len(pages) and quote_on_page(prov.quote, pages[idx]):
        return replace(constraint, provenance=verified)
    if (weak_pages and idx < len(weak_pages)
            and quote_on_page(prov.quote, weak_pages[idx], local=False)):
        existing = [r for r in constraint.review_reason.split("; ") if r]
        merged = list(dict.fromkeys(existing + [WEAK_PROVENANCE]))
        return replace(constraint, provenance=verified,
                       review_reason="; ".join(merged))
    return constraint


def _where_else(quote: str, pages: list[str]) -> int | None:
    # A hint only ("found on page 13?"), so the looser test is the useful one.
    for i, text in enumerate(pages):
        if quote_on_page(quote, text, local=False):
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
