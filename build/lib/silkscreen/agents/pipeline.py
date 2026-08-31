"""Prompt to PCB, with the model checked at every step.

    intent ─► datasheets ─► propose ─► validate/repair ─► .kicad_sch ─► place
                                            │                            │
                                            │                          route
                                            │                            │
                                            └───────► review ──────► .kicad_pcb

Two gates sit between the model and the board. The first is structural: the
circuit IR refuses to build something malformed and hands every error back for
repair. The second is semantic: a reviewer re-reads the datasheets and argues
against the design. Neither existed in the project this replaces, which is why
its netlists were confidently wrong.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..board import BoardResult, write_board
from ..netlist import CircuitSpec
from ..routing import RouteResult
from .datasheet import PartFacts
from .model import Document, Model
from .propose import ProposalAttempt
from .review import Finding, Severity
from .stages import (
    NO_ARTIFACTS,
    SchematicArtifacts,
    place_stage,
    propose_stage,
    read_stage,
    review_stage,
    route_stage,
    schematic_stage,
)

__all__ = ["PipelineResult", "generate_pcb"]

#: Event strings are capped so a stream stays a progress signal, never a
#: payload. No event may carry board text or datasheet text, and none carries
#: model output unless the caller asked for it.
MAX_EVENT_TEXT = 160
#: The single opt-in exception: a ``model.response`` event carries the model's
#: own answer verbatim, clipped here, for a client debugging what it was told.
MAX_RESPONSE_TEXT = 16_000


@dataclass
class PipelineResult:
    intent: str
    spec: CircuitSpec
    board: BoardResult
    facts: list[PartFacts] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    attempts: list[ProposalAttempt] = field(default_factory=list)
    board_path: Path | None = None
    #: The routed copper, or None when routing was turned off.
    route: RouteResult | None = None
    #: The other files a run leaves behind, so every stage is inspectable in
    #: KiCad rather than only the last one.
    schematic_path: Path | None = None
    project_path: Path | None = None
    placed_board_path: Path | None = None

    @property
    def artifacts(self) -> list[Path]:
        """Every file this run wrote, in the order the stages produced them."""
        ordered = [
            self.project_path,
            self.schematic_path,
            self.placed_board_path,
            self.board_path,
        ]
        return [p for p in ordered if p is not None]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def repair_rounds(self) -> int:
        """How many times the model had to be corrected before it validated."""
        return max(0, len(self.attempts) - 1)

    def summary(self) -> str:
        w, h = self.board.size_mm
        lines = [
            f"{self.spec.part_count()} parts, {self.spec.net_count()} nets",
            f"board {w:.2f} x {h:.2f} mm  [{self.board.solver_status}]",
        ]
        if self.repair_rounds:
            lines.append(f"{self.repair_rounds} repair round(s) before it validated")
        if self.route is not None:
            lines.append(self.route.summary())
        blockers = len(self.blockers)
        lines.append(
            f"{len(self.findings)} finding(s), {blockers} blocker(s)"
            if self.findings
            else "no findings"
        )
        return " · ".join(lines)


class _EventingModel:
    """A :class:`Model` that reports every round-trip it makes.

    Delegates to the wrapped model unchanged. ``stage`` is set by the pipeline
    before each stage, so a call can be attributed to the stage that made it.
    """

    def __init__(
        self,
        model: Model,
        emit: Callable[[dict[str, Any]], None],
        include_responses: bool = False,
    ):
        self._model = model
        self._emit = emit
        self.stage = ""
        self.include_responses = include_responses

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        # A failover model keeps an append-only attempt log. Anything appended
        # during this call is a provider that failed on the way to an answer;
        # a model without such a log simply produces no retry events.
        log = getattr(self._model, "log", None)
        seen = len(log) if isinstance(log, list) else 0
        started = time.monotonic()
        try:
            text = self._model.generate(
                prompt,
                documents=documents,
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
        except Exception:
            self._emit_retries(log, seen)
            self._emit_call(started, ok=False, chars=0)
            raise
        self._emit_retries(log, seen)
        self._emit_call(started, ok=True, chars=len(text))
        if self.include_responses:
            self._emit(
                {
                    "event": "model.response",
                    "stage": self.stage,
                    "provider": getattr(self._model, "last_provider", None),
                    "chars": len(text),
                    "truncated": len(text) > MAX_RESPONSE_TEXT,
                    "text": text[:MAX_RESPONSE_TEXT],
                }
            )
        return text

    def _emit_call(self, started: float, *, ok: bool, chars: int) -> None:
        self._emit(
            {
                "event": "model.call",
                "stage": self.stage,
                "provider": getattr(self._model, "last_provider", None),
                "model": getattr(self._model, "model", None),
                "elapsed_s": round(time.monotonic() - started, 3),
                "ok": ok,
                "chars": chars,
            }
        )

    def _emit_retries(self, log: object, seen: int) -> None:
        if not isinstance(log, list):
            return
        for attempt in log[seen:]:
            if getattr(attempt, "ok", True):
                continue
            self._emit(
                {
                    "event": "model.retry",
                    "stage": self.stage,
                    "provider": getattr(attempt, "provider", None),
                    "error": str(getattr(attempt, "error", "") or "")[:MAX_EVENT_TEXT],
                    "elapsed_s": round(float(getattr(attempt, "elapsed_s", 0.0)), 3),
                }
            )


def _wire_events(
    model: Model,
    on_event: Callable[[dict[str, Any]], None] | None,
    include_responses: bool,
) -> tuple[Callable[[dict[str, Any]], None], Model, Callable[[str], None]]:
    """Build the ``(emit, agent_model, enter)`` trio every driver hands the stages.

    With no callback the model is passed through unwrapped and ``emit`` does
    nothing, so an unwatched run pays nothing for the seam.
    """
    t0 = time.monotonic()

    def emit(evt: dict[str, Any]) -> None:
        if on_event is None:
            return
        evt["t_s"] = round(time.monotonic() - t0, 3)
        on_event(evt)

    tap: _EventingModel | None = None
    agent_model: Model = model
    if on_event is not None:
        tap = _EventingModel(model, emit, include_responses)
        agent_model = tap

    def enter(stage: str) -> None:
        if tap is not None:
            tap.stage = stage

    return emit, agent_model, enter


def _finish(
    *,
    intent: str,
    spec: CircuitSpec,
    board: BoardResult,
    facts: list[PartFacts],
    findings: list[Finding],
    attempts: list[ProposalAttempt],
    output: str | Path | None,
    route: RouteResult | None = None,
    artifacts: SchematicArtifacts = NO_ARTIFACTS,
) -> PipelineResult:
    """Write the board if asked, then assemble the result. Emits nothing.

    This runs after the route stage, so the board it writes carries the copper
    the router laid. Writing it earlier would leave the headline artifact --
    the one named after what the caller asked for -- as the only unrouted file
    in the project.
    """
    path = None
    if output is not None:
        path = write_board(board, output)

    return PipelineResult(
        intent=intent,
        spec=spec,
        board=board,
        facts=facts,
        findings=findings,
        attempts=attempts,
        board_path=path,
        route=route,
        schematic_path=artifacts.schematic_path,
        project_path=artifacts.project_path,
        placed_board_path=artifacts.placed_board_path,
    )


def _generate_pcb_sdk(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    preloaded_facts: list[PartFacts] | None = None,
    output: str | Path | None = None,
    max_repairs: int = 3,
    time_limit_s: float = 20.0,
    review: bool = True,
    route: bool = True,
    emit_stages: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    include_responses: bool = False,
) -> PipelineResult:
    """Run the stages as a straight line. See :func:`generate_pcb`."""
    emit, agent_model, enter = _wire_events(model, on_event, include_responses)

    facts = read_stage(
        agent_model,
        sheets=datasheets,
        preloaded_facts=preloaded_facts,
        emit=emit,
        enter=enter,
    )
    spec, attempts = propose_stage(
        agent_model,
        intent=intent,
        facts=facts,
        max_repairs=max_repairs,
        emit=emit,
        enter=enter,
        propose_on_event=emit if on_event is not None else None,
    )
    board = place_stage(spec, time_limit_s=time_limit_s, emit=emit, enter=enter)
    artifacts = schematic_stage(
        spec,
        board,
        output=output,
        emit_stages=emit_stages,
        emit=emit,
        enter=enter,
    )
    route_result = route_stage(board, route=route, emit=emit, enter=enter)
    findings = review_stage(
        agent_model, spec, facts=facts, review=review, emit=emit, enter=enter
    )

    return _finish(
        intent=intent,
        spec=spec,
        board=board,
        facts=facts,
        findings=findings,
        attempts=attempts,
        output=output,
        route=route_result,
        artifacts=artifacts,
    )


def generate_pcb(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    preloaded_facts: list[PartFacts] | None = None,
    output: str | Path | None = None,
    max_repairs: int = 3,
    time_limit_s: float = 20.0,
    review: bool = True,
    route: bool = True,
    emit_stages: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    include_responses: bool = False,
    engine: str = "",
) -> PipelineResult:
    """Generate a placed board from a natural-language intent.

    Args:
        model: The model to use for every stage.
        intent: What to build, in plain language.
        datasheets: ``{part_number: pdf_url}`` to read before designing. Parts
            without a datasheet still work, but nothing can be cited about them.
        preloaded_facts: Facts already read for some parts, from a cache. These
            join the freshly-read ones and are used identically. A caller that
            skips a datasheet read because it has the facts already **must**
            pass them here: omitting them does not merely lose a citation, it
            designs and reviews the board as though the part were undocumented.
        output: Where to write the ``.kicad_pcb``. Skipped if omitted.
        max_repairs: How many times the proposal may be sent back for repair.
        time_limit_s: Placement solver budget.
        review: Run the adversarial review pass.
        route: Lay copper after placing. Off leaves a placed board whose pads
            carry nets and whose copper is empty -- which KiCad draws as a
            ratsnest, and which is what every run produced before routing
            existed. Nets the router cannot finish are listed in
            ``result.route.unrouted`` whether this is on or not.
        emit_stages: Alongside the board, write the ``.kicad_sch``, the
            ``.kicad_pro`` that ties the two together, and the pre-routing
            ``.placed.kicad_pcb``, all named after ``output``. Ignored when
            ``output`` is None, since there is nowhere to put them.
        on_event: Called with one flat dict per stage boundary and per model
            round-trip, each carrying ``event`` and ``t_s`` -- seconds since
            this call began. Events carry counts and status only, never board
            text or datasheet text; raw model output appears only in
            ``model.response`` events, only when ``include_responses`` is set,
            truncated to ``MAX_RESPONSE_TEXT``. An exception raised by the
            callback deliberately propagates and abandons the run, which is how
            a service cancels work for a client that has disconnected. The
            event shape mirrors Google ADK's callback and event model, so this
            seam can be driven by an ADK runner without changing its callers.
        engine: Which driver runs the stages -- ``"sdk"`` for the straight line
            in this module, ``"adk"`` for the Google ADK workflow in
            :mod:`silkscreen.agents.adk`. Both call the same stage bodies and
            emit the same events. Empty means read ``SILKSCREEN_ENGINE`` from
            the environment, falling back to ``"adk"`` -- the default since
            the 2026-08-30 live-run gate passed; an explicit argument always
            wins over the variable, and ``SILKSCREEN_ENGINE=sdk`` is the kill
            switch back to the straight line.

    Raises:
        ProposalError: no valid circuit emerged within the repair budget.
        UnsupportedPackage: a part's pin count has no footprint rule.
        RuntimeError: the engine name is unknown, or ``"adk"`` was asked for
            without the ``adk`` extra installed.
    """
    chosen = engine or os.environ.get("SILKSCREEN_ENGINE", "") or "adk"
    if chosen == "sdk":
        return _generate_pcb_sdk(
            model,
            intent,
            datasheets=datasheets,
            preloaded_facts=preloaded_facts,
            output=output,
            max_repairs=max_repairs,
            time_limit_s=time_limit_s,
            review=review,
            route=route,
            emit_stages=emit_stages,
            on_event=on_event,
            include_responses=include_responses,
        )
    if chosen == "adk":
        # Imported here, never at module scope: a base install has no google.adk,
        # and silkscreen.agents is imported by the service on every request path.
        try:
            from .adk.runner import generate_pcb_adk
        except ImportError as exc:
            raise RuntimeError(
                "the 'adk' engine needs the adk extra: pip install 'silkscreen[adk]'"
            ) from exc
        return generate_pcb_adk(
            model,
            intent,
            datasheets=datasheets,
            preloaded_facts=preloaded_facts,
            output=output,
            max_repairs=max_repairs,
            time_limit_s=time_limit_s,
            review=review,
            route=route,
            emit_stages=emit_stages,
            on_event=on_event,
            include_responses=include_responses,
        )
    # RuntimeError, not ValueError: the service answers a pipeline ValueError as
    # a 400 with the raw message, and a bad engine name is not a client's fault.
    raise RuntimeError(f"unknown engine {chosen!r}: expected 'sdk' or 'adk'")
