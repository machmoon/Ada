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

from dataclasses import dataclass, field
from pathlib import Path

from ..board import BoardResult, build_board, write_board
from ..netlist import CircuitSpec
from .datasheet import PartFacts, read_datasheet
from .model import Model
from .propose import ProposalAttempt, propose_circuit
from .review import Finding, Severity, review_circuit

__all__ = ["PipelineResult", "generate_pcb"]


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


def generate_pcb(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    output: str | Path | None = None,
    max_repairs: int = 3,
    time_limit_s: float = 20.0,
    review: bool = True,
) -> PipelineResult:
    """Generate a placed board from a natural-language intent.

    Args:
        model: The model to use for every stage.
        intent: What to build, in plain language.
        datasheets: ``{part_number: pdf_url}`` to read before designing. Parts
            without a datasheet still work, but nothing can be cited about them.
        output: Where to write the ``.kicad_pcb``. Skipped if omitted.
        max_repairs: How many times the proposal may be sent back for repair.
        time_limit_s: Placement solver budget.
        review: Run the adversarial review pass.

    Raises:
        ProposalError: no valid circuit emerged within the repair budget.
        UnsupportedPackage: a part's pin count has no footprint rule.
    """
    facts: list[PartFacts] = []
    for part_number, url in (datasheets or {}).items():
        facts.append(read_datasheet(model, part_number, pdf_url=url))

    spec, attempts = propose_circuit(
        model, intent, facts=facts, max_repairs=max_repairs
    )

    board = build_board(spec, time_limit_s=time_limit_s)

    findings: list[Finding] = []
    if review:
        findings = review_circuit(model, spec, facts=facts)

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
