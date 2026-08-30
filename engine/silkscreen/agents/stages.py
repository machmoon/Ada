"""The four pipeline stages, as standalone bodies.

Each function is one stage of prompt-to-PCB: read, propose, place, review. They
hold the whole of a stage -- its model calls, its guards, and the exact events
it emits -- so that more than one driver can run the same stages. The straight
line in :mod:`silkscreen.agents.pipeline` and the ADK workflow in
:mod:`silkscreen.agents.adk` both call these, and therefore emit byte-identical
events; the service and the SPA read those event names, so a driver that grew
its own copy of a stage would silently fork the contract.

``emit`` and ``enter`` come from the driver: ``emit`` publishes one flat event
dict, ``enter`` tells the model wrapper which stage is making its calls. Nothing
here imports :mod:`silkscreen.agents.pipeline` -- that import runs the other way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..board import BoardResult, build_board
from ..netlist import CircuitSpec
from ..units import NM_PER_MM
from .datasheet import PartFacts, read_datasheet
from .model import Model
from .propose import ProposalAttempt, propose_circuit
from .review import Finding, Severity, review_circuit

__all__ = ["read_stage", "propose_stage", "place_stage", "review_stage"]

Emit = Callable[[dict[str, Any]], None]
Enter = Callable[[str], None]


def read_stage(
    agent_model: Model,
    *,
    sheets: dict[str, str] | None,
    preloaded_facts: list[PartFacts] | None,
    emit: Emit,
    enter: Enter,
) -> list[PartFacts]:
    """Read every datasheet, returning cached and freshly-read facts together.

    With no datasheets to read the stage does not run at all and emits nothing,
    rather than reporting an empty pass.
    """
    facts: list[PartFacts] = list(preloaded_facts or ())
    already = {f.part_number.strip().lower() for f in facts}
    sheets = sheets or {}
    if sheets:
        enter("read")
        emit({"event": "stage.start", "stage": "read"})
    for index, (part_number, url) in enumerate(sheets.items(), start=1):
        # A part supplied both ways is read once; the cached copy wins, since
        # re-reading it is the cost the caller was trying to avoid.
        cached = part_number.strip().lower() in already
        emit(
            {
                "event": "read.part",
                "part": part_number,
                "index": index,
                "total": len(sheets),
                "cached": cached,
            }
        )
        if cached:
            continue
        facts.append(read_datasheet(agent_model, part_number, pdf_url=url))
    if sheets:
        emit(
            {
                "event": "stage.done",
                "stage": "read",
                "parts": len(facts),
                "pins": sum(len(f.pins) for f in facts),
                "requirements": sum(len(f.requirements) for f in facts),
            }
        )
    return facts


def propose_stage(
    agent_model: Model,
    *,
    intent: str,
    facts: list[PartFacts],
    max_repairs: int,
    emit: Emit,
    enter: Enter,
    propose_on_event: Emit | None,
) -> tuple[CircuitSpec, list[ProposalAttempt]]:
    """Ask for a circuit and repair it until it validates.

    ``propose_on_event`` is handed to :func:`propose_circuit` unchanged, so the
    repair loop's own ``propose.round`` events reach the same stream -- or none
    at all when the driver has no listener.
    """
    enter("propose")
    emit({"event": "stage.start", "stage": "propose"})
    spec, attempts = propose_circuit(
        agent_model,
        intent,
        facts=facts,
        max_repairs=max_repairs,
        on_event=propose_on_event,
    )
    emit(
        {
            "event": "stage.done",
            "stage": "propose",
            "parts": spec.part_count(),
            "nets": spec.net_count(),
            "repair_rounds": max(0, len(attempts) - 1),
        }
    )
    return spec, attempts


def place_stage(
    spec: CircuitSpec,
    *,
    time_limit_s: float,
    emit: Emit,
    enter: Enter,
) -> BoardResult:
    """Solve placement for the accepted circuit. The only model-free stage."""
    enter("place")
    emit(
        {
            "event": "stage.start",
            "stage": "place",
            "time_limit_s": float(time_limit_s),
        }
    )
    board = build_board(spec, time_limit_s=time_limit_s)
    width_mm, height_mm = board.size_mm
    emit(
        {
            "event": "stage.done",
            "stage": "place",
            "solver_status": board.solver_status,
            "board_mm": [width_mm, height_mm],
            "wirelength_mm": (
                None if board.wirelength_nm is None else board.wirelength_nm / NM_PER_MM
            ),
            "warnings": len(board.warnings),
        }
    )
    return board


def review_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts],
    review: bool,
    emit: Emit,
    enter: Enter,
) -> list[Finding]:
    """Argue against the design. Skipped entirely -- and silently -- if off."""
    findings: list[Finding] = []
    if review:
        enter("review")
        emit({"event": "stage.start", "stage": "review"})
        findings = review_circuit(agent_model, spec, facts=facts)
        emit(
            {
                "event": "stage.done",
                "stage": "review",
                "findings": len(findings),
                "blockers": sum(1 for f in findings if f.severity is Severity.BLOCKER),
            }
        )
    return findings
