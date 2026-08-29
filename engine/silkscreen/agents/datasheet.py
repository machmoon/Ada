"""Read a datasheet into structured facts.

Gemini's native PDF vision is what makes this tractable. A datasheet's pinout
table and package drawing are *pictures*; text extraction throws away exactly
the information needed here, which is why the previous project's PyPDF2
dependency was never actually used and its one model call passed a bare URL.

Every fact carries the page it came from. A claim without a citation is not
useful in a domain where being wrong costs four weeks and a fab run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Document, Model, ModelError, parse_json

__all__ = ["PartFacts", "PinFact", "read_datasheet", "DATASHEET_PROMPT"]


@dataclass(frozen=True)
class PinFact:
    number: str
    name: str
    kind: str = ""          # power / ground / input / output / bidirectional / analog
    description: str = ""
    page: int | None = None


@dataclass
class PartFacts:
    """What we learned about one component."""

    part_number: str
    package: str = ""
    pin_count: int = 0
    pins: list[PinFact] = field(default_factory=list)
    #: Free-text requirements the reviewer will check against, each with a page.
    requirements: list[dict] = field(default_factory=list)
    #: Recommended auxiliary components, as {name, type, value, why, page}.
    auxiliaries: list[dict] = field(default_factory=list)
    notes: str = ""
    source_url: str = ""

    def pin_map(self) -> dict[str, str]:
        """``{pin_name: pin_number}`` in the shape :mod:`silkscreen.netlist` wants."""
        return {p.name: p.number for p in self.pins}

    def pin_by_name(self, name: str) -> PinFact | None:
        for pin in self.pins:
            if pin.name.lower() == name.lower():
                return pin
        return None


DATASHEET_PROMPT = """\
You are reading an electronic component datasheet. Extract only what the
document actually states. Do not infer, do not fill gaps from general
knowledge, and do not guess a pin number you cannot see.

Return ONE JSON object, no prose, no code fence:

{
  "part_number": "<exact orderable part number>",
  "package": "<e.g. LQFP-48, SOT-223-3, SOIC-8>",
  "pin_count": <integer, the package pin count>,
  "pins": [
    {"number": "<physical pin number>", "name": "<datasheet pin name>",
     "kind": "power|ground|input|output|bidirectional|analog|nc",
     "description": "<short>", "page": <page number you read this from>}
  ],
  "requirements": [
    {"requirement": "<one electrical requirement for correct operation>",
     "page": <page number>}
  ],
  "auxiliaries": [
    {"name": "<descriptive id, e.g. c_dec_vdd>",
     "type": "capacitor|resistor|inductor|diode|crystal",
     "value": "<e.g. 100nF>",
     "connects": "<which pins it goes between, using datasheet pin names>",
     "why": "<short reason>", "page": <page number>}
  ],
  "notes": "<anything a designer would be caught out by>"
}

Rules:
- "pins" must list EVERY pin of the package, numbered as the package numbers them.
- Auxiliary types are limited to the five listed. If the datasheet requires
  something outside that set, put it in "notes" instead of inventing a type.
- Every requirement and auxiliary MUST carry the page it came from.
- If the document does not state something, omit it. An empty list is a valid
  and useful answer; a fabricated one is not.
"""


def read_datasheet(
    model: Model,
    part_number: str,
    *,
    pdf_url: str | None = None,
    pdf_bytes: bytes | None = None,
) -> PartFacts:
    """Extract structured facts for ``part_number`` from its datasheet."""
    if not pdf_url and not pdf_bytes:
        raise ValueError("read_datasheet needs a pdf_url or pdf_bytes")

    doc = Document(url=pdf_url, data=pdf_bytes)
    prompt = f"{DATASHEET_PROMPT}\n\nThe part is: {part_number}"
    raw = model.generate(prompt, documents=[doc], temperature=0.0)
    data = parse_json(raw)

    if not isinstance(data, dict):
        raise ModelError(f"Expected a JSON object, got {type(data).__name__}")

    pins = []
    for entry in data.get("pins") or []:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("number", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not number or not name:
            continue
        pins.append(
            PinFact(
                number=number,
                name=name,
                kind=str(entry.get("kind", "")),
                description=str(entry.get("description", "")),
                page=entry.get("page") if isinstance(entry.get("page"), int) else None,
            )
        )

    if not pins:
        raise ModelError(
            f"No pins extracted for {part_number}. The pipeline cannot place a "
            f"part whose pinout is unknown."
        )

    facts = PartFacts(
        part_number=str(data.get("part_number") or part_number),
        package=str(data.get("package", "")),
        pin_count=int(data.get("pin_count") or 0) or len(pins),
        pins=pins,
        requirements=[
            r for r in (data.get("requirements") or []) if isinstance(r, dict)
        ],
        auxiliaries=[a for a in (data.get("auxiliaries") or []) if isinstance(a, dict)],
        notes=str(data.get("notes", "")),
        source_url=pdf_url or "",
    )

    # A pin count that disagrees with the pin list means one of them is wrong,
    # and picking a package off a wrong count produces a dead board.
    if facts.pin_count and len(facts.pins) != facts.pin_count:
        facts.notes += (
            f" [silkscreen: datasheet reports {facts.pin_count} pins but "
            f"{len(facts.pins)} were extracted; package choice may be wrong]"
        )
    return facts
