"""The pipeline stages, as standalone bodies.

Each function is one stage of prompt-to-PCB: read, propose, place, placement
repair, schematic, route, enclosure, and review. They hold the whole of a stage -- its
model calls, its guards, and the exact events it emits -- so that more than one
driver can run the same stages. The straight line in
:mod:`silkscreen.agents.pipeline` and the ADK workflow in
:mod:`silkscreen.agents.adk` both call these, and therefore emit byte-identical
events; the service and the SPA read those event names, so a driver that grew
its own copy of a stage would silently fork the contract.

``emit`` and ``enter`` come from the driver: ``emit`` publishes one flat event
dict, ``enter`` tells the model wrapper which stage is making its calls. Nothing
here imports :mod:`silkscreen.agents.pipeline` -- that import runs the other way.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from ..board import BoardResult, build_board, route_board, write_board
from ..enclosure.board_shape import board_envelope
from ..enclosure.emit import emit_scad
from ..enclosure.errors import EnclosureError
from ..enclosure.ir import EnclosureSpec
from ..enclosure.verify import FitReport
from ..netlist import CircuitSpec
from ..placement.adapter import GeneratedPlacement, repair_generated_board
from ..placement.agent import TextModel
from ..placement.pcb_repair import evaluate
from ..routing import RouteResult
from ..schematic import build_schematic, write_project, write_schematic
from ..units import NM_PER_MM, to_mm
from .datasheet import PartFacts, read_datasheet
from .enclosure import propose_enclosure
from .model import Model
from .propose import ProposalAttempt, propose_circuit
from .review import Finding, Severity, review_circuit

__all__ = [
    "read_stage",
    "propose_stage",
    "place_stage",
    "placement_repair_stage",
    "schematic_stage",
    "route_stage",
    "enclosure_stage",
    "review_stage",
    "SchematicArtifacts",
    "NO_ARTIFACTS",
    "EnclosureResult",
]

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

        # The download is the slowest thing in the stage and the only one that
        # can stall on a remote host. Reporting the bytes it landed is what
        # separates "still fetching" from "hung" while someone watches a demo.
        def announce(target: str, _part: str = part_number, **kwargs) -> bytes:
            from .grounding import fetch_pdf

            data = fetch_pdf(target, **kwargs)
            emit(
                {
                    "event": "read.fetch",
                    "part": _part,
                    "bytes": len(data),
                }
            )
            return data

        facts.append(
            read_datasheet(agent_model, part_number, pdf_url=url, fetch=announce)
        )
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
    time_limit_s: float | None,
    emit: Emit,
    enter: Enter,
) -> BoardResult:
    """Solve placement for the accepted circuit. The only model-free stage."""
    enter("place")
    emit(
        {
            "event": "stage.start",
            "stage": "place",
            "time_limit_s": (
                None if time_limit_s is None else float(time_limit_s)
            ),
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


def placement_repair_stage(
    board: BoardResult,
    *,
    profile: str | None,
    policy: str,
    feedback: dict[str, Any] | None,
    model: TextModel | None,
    fallback_model: TextModel | None,
    max_turns: int,
    emit: Emit,
    enter: Enter,
) -> GeneratedPlacement | None:
    """Verifier-gate a generated placement before schematic emission and routing."""
    if not profile:
        return None
    enter("placement_repair")
    emit(
        {
            "event": "stage.start",
            "stage": "placement_repair",
            "profile": profile,
            "policy": policy,
        }
    )
    result = repair_generated_board(
        board,
        profile=profile,
        policy=policy,
        feedback=feedback,
        model=model,
        fallback_model=fallback_model,
        max_turns=max_turns,
    )
    before = result.run.start
    after = result.run.board
    before_score = evaluate(before, result.run.profile)
    after_score = evaluate(after, result.run.profile)
    emit(
        {
            "event": "stage.done",
            "stage": "placement_repair",
            "profile": profile,
            "policy": result.run.policy,
            "requested_policy": result.requested_policy,
            "completed": result.run.completed,
            "applied": result.applied,
            "moves": sum(len(step.accepted) for step in result.run.steps),
            "hard_before": before_score.hard,
            "hard_after": after_score.hard,
            "policy_fallback": result.policy_fallback,
        }
    )
    return result


class SchematicArtifacts(NamedTuple):
    """The files the schematic stage wrote, or three Nones when it did not run."""

    schematic_path: Path | None = None
    project_path: Path | None = None
    placed_board_path: Path | None = None


#: What the schematic stage returns when it does not run, and the default a
#: driver passes when it has nothing to report. A module-level singleton
#: because it is immutable and shared, and because a NamedTuple constructed in
#: an argument default is the mutable-default footgun's shape even when it is
#: not one.
NO_ARTIFACTS = SchematicArtifacts()


def schematic_stage(
    spec: CircuitSpec,
    board: BoardResult,
    *,
    output: str | Path | None,
    emit_stages: bool,
    emit: Emit,
    enter: Enter,
) -> SchematicArtifacts:
    """Draw the sheet and leave every stage on disk, not just the last one.

    The only stage body that writes files. The rest hand their results back and
    let the driver's tail decide; this one cannot, because the three artifacts
    it produces have no other owner and both drivers must produce them
    identically. With no ``output`` there is nowhere to put them, so the stage
    does not run and emits nothing -- the same convention ``read_stage`` follows
    with no datasheets.

    The schematic goes out before any copper exists, because it is the artifact
    a person reads first and it does not depend on placement succeeding well.
    """
    out_path = Path(output) if output is not None else None
    if out_path is None or not emit_stages:
        return NO_ARTIFACTS

    enter("schematic")
    emit({"event": "stage.start", "stage": "schematic"})
    stem = out_path.stem
    sheet = build_schematic(
        spec,
        footprints={p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts},
    )
    board.warnings.extend(sheet.warnings)
    artifacts = SchematicArtifacts(
        schematic_path=write_schematic(
            sheet, out_path.with_name(f"{stem}.kicad_sch"), project_name=stem
        ),
        project_path=write_project(
            out_path.with_name(f"{stem}.kicad_pro"), project_name=stem
        ),
        placed_board_path=write_board(
            board, out_path.with_name(f"{stem}.placed.kicad_pcb")
        ),
    )
    emit(
        {
            "event": "stage.done",
            "stage": "schematic",
            "symbols": len(sheet.symbols),
            "warnings": len(sheet.warnings),
        }
    )
    return artifacts


def route_stage(
    board: BoardResult,
    *,
    route: bool,
    emit: Emit,
    enter: Enter,
) -> RouteResult | None:
    """Lay copper on the placed board. Skipped -- and silent -- when off.

    Mutates ``board`` in place, so the ``.kicad_pcb`` the driver's tail writes
    carries the tracks. Nets the router could not finish are named in the
    result and in ``board.unrouted_nets``; a caller reporting the board as
    routed without reading them is the failure the router exists to avoid.
    """
    if not route:
        return None
    enter("route")
    emit({"event": "stage.start", "stage": "route"})
    result = route_board(board)
    emit(
        {
            "event": "stage.done",
            "stage": "route",
            "tracks": len(result.tracks),
            "vias": len(result.vias),
            "routed_nets": len(result.routed),
            "unrouted_nets": len(result.unrouted),
            "copper_mm": round(result.routed_length_nm / NM_PER_MM, 3),
        }
    )
    return result


class EnclosureResult(NamedTuple):
    """What the enclosure stage produced (docs/ai-cad-plan.md, frozen)."""

    spec: EnclosureSpec
    scad: str
    fit: FitReport
    repair_rounds: int
    rendered: bool  # always False on the service path in v1
    #: Where ``enclosure.scad`` was written, or None when nothing was (no
    #: ``output``, or ``emit_stages`` off). Additive with a default so every
    #: existing consumer of the frozen five-field shape keeps working.
    scad_path: Path | None = None


def enclosure_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    enclosure: bool,
    enclosure_style: str,
    output: str | Path | None,
    emit_stages: bool,
    emit: Emit,
    enter: Enter,
) -> EnclosureResult | None:
    """Propose, verify, and emit a case for the routed board. Opt-in.

    No-ops silently when the run did not ask for an enclosure (the
    ``route_stage`` pattern). Runs after routing so the board it measures is
    the board the run delivers, and before review so the run's critic still
    closes the stream.

    The stage measures the board by writing it to a scratch file and reading
    it back through :func:`~silkscreen.enclosure.board_shape.board_envelope`
    -- the envelope is derived from ``.kicad_pcb`` text, the same artifact the
    caller receives, not from in-memory state the file might not carry.

    ``enclosure.scad`` is written beside the project only when ``output`` is
    set and ``emit_stages`` is on (the ``schematic_stage`` filesystem rule --
    ``--board-only`` promises only the routed board); the text itself is
    always returned so the service can ship it without touching a disk.

    Failure never fails the run (plan decision 5): any
    :class:`~silkscreen.enclosure.errors.EnclosureError` -- an exhausted
    repair budget included -- as well as a ``ValueError`` from measuring the
    board and an ``OSError`` from writing the ``.scad`` is caught here,
    surfaced as a visible ``enclosure.failed`` event, and answered with
    ``None``; the board is still the product. Everything else, callback
    exceptions and :class:`~silkscreen.agents.model.ModelError` included,
    propagates as it does from every other stage.
    """
    if not enclosure:
        return None
    enter("enclosure")
    emit({"event": "stage.start", "stage": "enclosure"})
    scad_path: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="silkscreen-enclosure-") as tmp:
            measured = write_board(board, Path(tmp) / "board.kicad_pcb")
            envelope = board_envelope(measured)
        # The loop verified every accepted spec with strict=True; the report
        # it returns is the receipt, board-derived warnings included, so the
        # stage never re-verifies what was already verified.
        spec, fit, repair_rounds = propose_enclosure(
            agent_model,
            envelope,
            style_hint=enclosure_style,
            on_event=emit,
        )
        scad = emit_scad(spec, envelope)
        if output is not None and emit_stages:
            scad_path = Path(output).with_name("enclosure.scad")
            scad_path.parent.mkdir(parents=True, exist_ok=True)
            scad_path.write_text(scad, encoding="utf-8")
    except (EnclosureError, ValueError, OSError) as exc:
        emit({"event": "enclosure.failed", "error": str(exc)[:160]})
        return None

    emit(
        {
            "event": "stage.done",
            "stage": "enclosure",
            "cutouts": len(spec.cutouts),
            "lid": spec.lid,
            "wall_mm": round(to_mm(spec.wall_nm), 3),
            "repair_rounds": repair_rounds,
            "rendered": False,
        }
    )
    return EnclosureResult(
        spec=spec,
        scad=scad,
        fit=fit,
        repair_rounds=repair_rounds,
        rendered=False,
        scad_path=scad_path,
    )


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
