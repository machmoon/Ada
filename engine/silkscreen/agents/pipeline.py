"""Prompt to PCB, with the model checked at every step.

    intent ──► datasheets ──► propose ──► validate/repair ──► place ──► .kicad_pcb
                                                │                          │
                                                └──────► review ───────────┘

Two gates sit between the model and the board. The first is structural: the
circuit IR refuses to build something malformed and hands every error back for
repair. The second is semantic: a reviewer re-reads the datasheets and argues
against the design. Neither existed in the project this replaces, which is why
its netlists were confidently wrong.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..board import BoardResult, build_board, write_board
from ..netlist import CircuitSpec
from ..units import NM_PER_MM
from .datasheet import PartFacts, read_datasheet
from .model import Document, Model
from .propose import ProposalAttempt, propose_circuit
from .review import Finding, Severity, review_circuit

__all__ = ["PipelineResult", "generate_pcb"]

#: Event strings are capped so a stream stays a progress signal, never a
#: payload. No event may carry board text, model output, or datasheet text.
MAX_EVENT_TEXT = 160


@dataclass
class PipelineResult:
    intent: str
    spec: CircuitSpec
    board: BoardResult
    facts: list[PartFacts] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    attempts: list[ProposalAttempt] = field(default_factory=list)
    board_path: Path | None = None

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

    def __init__(self, model: Model, emit: Callable[[dict[str, Any]], None]):
        self._model = model
        self._emit = emit
        self.stage = ""

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
    on_event: Callable[[dict[str, Any]], None] | None = None,
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
        on_event: Called with one flat dict per stage boundary and per model
            round-trip, each carrying ``event`` and ``t_s`` -- seconds since
            this call began. Events carry counts and status only, never board
            text, model output or datasheet text. An exception raised by the
            callback deliberately propagates and abandons the run, which is how
            a service cancels work for a client that has disconnected. The
            event shape mirrors Google ADK's callback and event model, so this
            seam can be driven by an ADK runner without changing its callers.

    Raises:
        ProposalError: no valid circuit emerged within the repair budget.
        UnsupportedPackage: a part's pin count has no footprint rule.
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
        tap = _EventingModel(model, emit)
        agent_model = tap

    def enter(stage: str) -> None:
        if tap is not None:
            tap.stage = stage

    facts: list[PartFacts] = list(preloaded_facts or ())
    already = {f.part_number.strip().lower() for f in facts}
    sheets = datasheets or {}
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

    enter("propose")
    emit({"event": "stage.start", "stage": "propose"})
    spec, attempts = propose_circuit(
        agent_model,
        intent,
        facts=facts,
        max_repairs=max_repairs,
        on_event=emit if on_event is not None else None,
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
    )
