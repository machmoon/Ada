"""Adversarially review a circuit against the datasheets it was drawn from.

Structural validity is not correctness. A netlist can pass every check in
:mod:`silkscreen.netlist` and still drive an input pin, wire a regulator's
feedback divider to the wrong node, or put a ceramic cap where the part needs
tantalum. That gap -- between "connected" and "connected *correctly*" -- is what
no EDA tool checks and what this module exists for.

The reviewer is prompted to *refute* the design. An agent asked "is this
correct?" says yes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..netlist import CircuitSpec
from .datasheet import PartFacts
from .model import Model, parse_json

__all__ = ["Finding", "Severity", "review_circuit", "REVIEW_PROMPT"]


class Severity(StrEnum):
    #: The board will not work as built.
    BLOCKER = "blocker"
    #: It may work, but violates a datasheet recommendation or good practice.
    MARGINAL = "marginal"
    #: Correct, but worth knowing.
    NOTE = "note"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    title: str
    detail: str
    parts: tuple[str, ...] = ()
    citation: str = ""
    suggested_fix: str = ""

    def __str__(self) -> str:
        where = f" [{', '.join(self.parts)}]" if self.parts else ""
        cite = f"  ({self.citation})" if self.citation else ""
        return f"{self.severity.value.upper()}{where}: {self.title}{cite}"


REVIEW_PROMPT = """\
You are reviewing a circuit someone else designed. Your job is to find what is
WRONG with it. Assume it contains at least one real error and look for it.
Do not compliment the design and do not summarise it.

Check specifically:
- Is every pin connected to something appropriate for its FUNCTION? An output
  driving an output, a supply pin on a signal net, an input left floating.
- Are mode/boot/enable pins tied to a defined level where the datasheet requires it?
- Does every supply pin have decoupling, and is it the right value?
- Are capacitor dielectrics and values right for their role -- particularly
  regulator input and output capacitors, where ESR determines stability?
- Are pull-up/pull-down values sensible for what they are pulling?
- Is anything shorted: two different supplies on one net, a part with both legs
  on the same net?
- Is a required component missing entirely?

Return ONE JSON object, no prose, no code fence:

{
  "findings": [
    {"severity": "blocker|marginal|note",
     "title": "<one line, states the defect>",
     "detail": "<2-3 sentences: what is wrong, and what will physically happen>",
     "parts": ["<part ids involved>"],
     "citation": "<datasheet + page, if a supplied fact supports this>",
     "suggested_fix": "<one concrete change>"}
  ]
}

Rules:
- "blocker" means the board will not work. Use it only when you are sure.
- Cite a page ONLY when a supplied datasheet fact actually supports the claim.
  An invented citation is worse than none.
- If you genuinely find nothing, return {"findings": []}. Do not pad the list.
"""


def _spec_text(spec: CircuitSpec) -> str:
    lines = ["Devices:"]
    for d in spec.devices:
        pins = ", ".join(f"{n}={num}" for n, num in d.pins.items())
        lines.append(f"  {d.name}: {pins}")
    lines.append("Passives:")
    for p in spec.passives:
        lines.append(f"  {p.name}: {p.type.value} {p.value}")
    lines.append("Nets:")
    for c in spec.connections:
        lines.append(f"  {c.net}: {', '.join(c.endpoints)}")
    return "\n".join(lines)


def _facts_text(facts: list[PartFacts]) -> str:
    if not facts:
        return "(no datasheets supplied — do not invent citations)"
    out = []
    for f in facts:
        pins = ", ".join(f"{p.number}:{p.name}({p.kind})" for p in f.pins)
        out.append(f"  {f.part_number} [{f.package}] pins: {pins}")
        for r in f.requirements:
            out.append(
                f"    requirement: {r.get('requirement','')} (p.{r.get('page','?')})"
            )
    return "\n".join(out)


def review_circuit(
    model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts] | None = None,
) -> list[Finding]:
    """Return findings, most severe first. An empty list means nothing found."""
    facts = facts or []
    prompt = (
        f"{REVIEW_PROMPT}\n\n"
        f"The circuit under review:\n{_spec_text(spec)}\n\n"
        f"Datasheet facts available to you:\n{_facts_text(facts)}\n"
    )
    raw = model.generate(prompt, temperature=0.0, max_output_tokens=8192)
    data = parse_json(raw)

    entries = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []

    known = {d.name for d in spec.devices} | {p.name for p in spec.passives}
    findings: list[Finding] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("title"):
            continue
        try:
            severity = Severity(str(entry.get("severity", "note")).lower())
        except ValueError:
            severity = Severity.NOTE
        # Drop part references the circuit does not contain, rather than
        # surfacing a finding that points at nothing.
        parts = tuple(
            p for p in (entry.get("parts") or []) if isinstance(p, str) and p in known
        )
        findings.append(
            Finding(
                severity=severity,
                title=str(entry["title"]),
                detail=str(entry.get("detail", "")),
                parts=parts,
                citation=str(entry.get("citation", "")),
                suggested_fix=str(entry.get("suggested_fix", "")),
            )
        )

    order = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}
    findings.sort(key=lambda f: order[f.severity])
    return findings
