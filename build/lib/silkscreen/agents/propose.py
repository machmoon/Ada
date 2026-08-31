"""Propose a circuit, then make the model fix its own mistakes.

The model never gets to hand its output straight to the board builder. Its
proposal goes through :func:`silkscreen.netlist.parse_circuit_spec`, and every
validation error is fed back as a repair prompt. The loop is bounded and each
round is strictly informed by the last, so it converges or gives up loudly --
rather than the previous project's approach, which was to ``json.loads`` raw
model text inside a worker thread with no handler and let the thread die.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..netlist import CircuitSpec, ValidationError, parse_circuit_spec
from .datasheet import PartFacts
from .model import Model

__all__ = ["propose_circuit", "ProposalError", "ProposalAttempt", "PROPOSE_PROMPT"]


class ProposalError(RuntimeError):
    """The model could not produce a valid circuit within the repair budget."""

    def __init__(self, message: str, attempts: list[ProposalAttempt]):
        self.attempts = attempts
        super().__init__(message)


@dataclass
class ProposalAttempt:
    round: int
    raw: str
    errors: list[str] = field(default_factory=list)
    accepted: bool = False


PROPOSE_PROMPT = """\
You are designing a printed circuit board. Produce a complete, buildable
circuit as ONE JSON object -- no prose, no code fence.

{
  "devices": {
    "<part number>": {"pins": {"<pin name>": "<pin number>", ...}}
  },
  "passives": {
    "<descriptive id>": {"type": "capacitor|resistor|inductor|diode|crystal",
                         "value": "<e.g. 100nF>"}
  },
  "nets": {
    "<net name>": ["<part>.<pin>", "<part>.<pin>", ...]
  }
}

Hard rules -- a proposal breaking any of these is rejected automatically:

1. Every endpoint is "<part>.<pin>". For a device the pin is its NAME from the
   pins map. For a passive it is "1" or "2" -- passives have exactly two legs.
2. Every passive must have BOTH legs on a net. A part connected on one leg is
   floating and will be rejected.
3. Every net needs at least two endpoints.
4. Only reference devices and passives you declared, and pin names you declared.
5. Use only the five passive types listed.
6. Name power nets conventionally: GND, +3V3, +5V, VIN.

Design rules to follow:
- Give every IC supply pin its own decoupling capacitor, and say which supply
  pin each one bypasses in its id (e.g. c_dec_vdd_u1).
- Include bulk capacitance on each supply rail.
- Tie mode/boot pins that must not float to a defined level through a resistor.
- Prefer parts you were given datasheet facts for. If you must add a part with
  no datasheet, keep it to a common, unambiguous one.
"""


def _facts_block(facts: list[PartFacts]) -> str:
    if not facts:
        return "No datasheets were supplied. Use widely-known pinouts only."
    chunks = []
    for f in facts:
        pins = ", ".join(f"{p.name}={p.number}" for p in f.pins)
        reqs = "\n".join(
            f"      - {r.get('requirement','')} (p.{r.get('page','?')})"
            for r in f.requirements
        )
        auxes = "\n".join(
            f"      - {a.get('type','?')} {a.get('value','')} "
            f"{a.get('connects','')} :: {a.get('why','')} (p.{a.get('page','?')})"
            for a in f.auxiliaries
        )
        chunks.append(
            f"  {f.part_number} [{f.package or 'package unknown'}, "
            f"{f.pin_count} pins]\n"
            f"    pins: {pins}\n"
            + (f"    datasheet requirements:\n{reqs}\n" if reqs else "")
            + (f"    recommended auxiliaries:\n{auxes}\n" if auxes else "")
            + (f"    notes: {f.notes}\n" if f.notes else "")
        )
    return "\n".join(chunks)


def propose_circuit(
    model: Model,
    intent: str,
    *,
    facts: list[PartFacts] | None = None,
    max_repairs: int = 3,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[CircuitSpec, list[ProposalAttempt]]:
    """Ask for a circuit and repair it until it validates.

    Returns the accepted spec and the full attempt history, so a caller can show
    how many rounds it took -- which is a genuinely useful quality signal.

    ``on_event`` receives one ``propose.round`` event per rejected round, so a
    caller can watch the repair loop while it runs; see
    :func:`silkscreen.agents.pipeline.generate_pcb` for the event contract.

    Raises:
        ProposalError: the model answered, but never with a valid circuit.
        ModelError: the model could not be reached at all. Deliberately not
            wrapped -- an upstream outage is a different condition from a bad
            proposal, and callers route them differently.
    """
    facts = facts or []
    attempts: list[ProposalAttempt] = []

    prompt = (
        f"{PROPOSE_PROMPT}\n\n"
        f"What to build:\n{intent}\n\n"
        f"Datasheet facts you must design against:\n{_facts_block(facts)}\n"
    )

    for round_no in range(max_repairs + 1):
        # A transport failure is deliberately NOT wrapped in ProposalError.
        # "the model was unreachable" and "the model could not produce a valid
        # circuit" are different conditions with different remedies -- retry
        # versus give up -- and a caller (an HTTP service deciding between 502
        # and 500) has to be able to tell them apart. ModelError propagates.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=16384)

        attempt = ProposalAttempt(round=round_no, raw=raw)
        attempts.append(attempt)

        try:
            spec = parse_circuit_spec(raw)
        except ValidationError as exc:
            attempt.errors = list(exc.errors)
            if on_event is not None:
                # Validation errors are engine-generated, not model text, so
                # they are safe to put on the wire -- truncated all the same.
                on_event(
                    {
                        "event": "propose.round",
                        "round": round_no + 1,
                        "errors": len(exc.errors),
                        "first_error": str(exc.errors[0])[:160] if exc.errors else "",
                    }
                )
            if round_no == max_repairs:
                break
            # Feed every problem back at once so one round fixes all of them.
            problems = "\n".join(f"  - {e}" for e in exc.errors)
            prompt = (
                f"{PROPOSE_PROMPT}\n\n"
                f"What to build:\n{intent}\n\n"
                f"Datasheet facts you must design against:\n{_facts_block(facts)}\n\n"
                f"Your previous proposal was rejected. Fix ALL of these and "
                f"return the corrected JSON object:\n{problems}\n\n"
                f"Your previous proposal was:\n{raw}\n"
            )
            continue

        attempt.accepted = True
        return spec, attempts

    last = attempts[-1] if attempts else None
    detail = "\n".join(f"  - {e}" for e in (last.errors if last else []))
    raise ProposalError(
        f"No valid circuit after {max_repairs + 1} attempts. "
        f"Final errors:\n{detail}",
        attempts,
    )
