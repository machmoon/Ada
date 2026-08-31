"""Multi-turn Silkscreen placement agent with deterministic verifier feedback."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol

from .grader import board_to_text
from .pcb_repair import (
    Board,
    CompanyProfile,
    PlacementAction,
    apply_actions,
    evaluate,
    parse_actions,
    repair,
)

__all__ = [
    "PlacementAgent",
    "PlacementPolicyError",
    "PlacementPolicyTimeout",
    "PlacementReceipt",
    "PlacementRun",
    "PlacementStep",
    "SpeculativeCandidate",
    "TextModel",
    "placement_prompt",
]


class PlacementPolicyError(RuntimeError):
    """A proposal backend failed or returned an unusable response."""


class PlacementPolicyTimeout(PlacementPolicyError, TimeoutError):
    """A proposal backend exceeded its configured hard deadline."""


class TextModel(Protocol):
    def generate(
        self,
        prompt: str,
        *,
        documents=None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str: ...


EvaluatedCandidate = tuple["SpeculativeCandidate", Board]


@dataclass(frozen=True)
class PlacementReceipt:
    action: PlacementAction
    accepted: bool
    hard_before: float
    hard_after: float
    soft_before: float
    soft_after: float
    reason: str


@dataclass(frozen=True)
class SpeculativeCandidate:
    lane: int
    prompt: str
    response: str
    proposed: tuple[PlacementAction, ...]
    accepted: tuple[PlacementAction, ...]
    receipts: tuple[PlacementReceipt, ...]
    hard_after: float
    soft_after: float
    elapsed_ms: float
    error: str = ""
    status: str = "completed"
    duplicate_of_lane: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class PlacementStep:
    turn: int
    proposer: str
    prompt: str
    response: str
    board_before: Board
    proposed: tuple[PlacementAction, ...]
    accepted: tuple[PlacementAction, ...]
    receipts: tuple[PlacementReceipt, ...]
    hard_before: float
    hard_after: float
    soft_before: float
    soft_after: float
    reason: str
    candidates: tuple[SpeculativeCandidate, ...] = ()
    winner_lane: int | None = None
    speculative_wall_ms: float = 0.0
    early_commit: bool = False
    elapsed_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class PlacementRun:
    start: Board
    board: Board
    profile: CompanyProfile
    steps: tuple[PlacementStep, ...]
    policy: str
    completed: bool


def _profile_text(profile: CompanyProfile) -> str:
    return "\n".join(
        (
            f"PROFILE {profile.name}",
            (
                f"HARD clearance={profile.clearance:.3f} "
                f"edge_margin={profile.edge_margin:.3f}"
            ),
            f"FIXED {','.join(profile.fixed_refs) or 'none'}",
            f"EDGE {','.join(profile.edge_refs) or 'none'}",
            "GROUPS " + ";".join(",".join(group) for group in profile.groups),
            "THERMAL "
            + ";".join(
                f"{a},{b},{distance}" for a, b, distance in profile.thermal_pairs
            ),
        )
    )


def placement_prompt(
    board: Board,
    profile: CompanyProfile,
    turn: int,
    *,
    suggested_actions: tuple[PlacementAction, ...] | None = None,
) -> str:
    result = evaluate(board, profile)
    violations = "\n".join(f"- {item.message}" for item in result.violations)
    if suggested_actions is None:
        _, suggested = repair(board, profile)
    else:
        suggested = suggested_actions
    candidates = "\n".join(action.as_text() for action in suggested[:8])
    return f"""PCB PLACEMENT REPAIR TURN {turn}

You control component placement. Choose one to four lines from CANDIDATE
ACTIONS and copy their numeric values exactly. Return action lines only:
PLACE REF X_MM Y_MM [ANGLE]
MOVE REF DX_MM DY_MM [ANGLE]

Do not output X_MM, Y_MM, DX_MM, DY_MM, brackets, commentary, or new values.
Never move a fixed component. Hard legality has priority over preferences.
The deterministic verifier accepts the longest prefix whose individual moves
improve the lexicographic score (hard violations first, then preferences).

{_profile_text(profile)}

CURRENT SCORE hard={result.hard:.6f} soft={result.soft:.6f}
VIOLATIONS
{violations or '- none; improve the profile score'}

CANDIDATE ACTIONS
{candidates or '- none; return no action'}

{board_to_text(board)}
"""


class PlacementAgent:
    def __init__(
        self,
        model: TextModel | None = None,
        *,
        fallback_model: TextModel | None = None,
        lane_model_factory: Callable[[], TextModel] | None = None,
        max_turns: int = 8,
        speculative_width: int = 3,
        speculative_timeout_s: float = 8.0,
        speculative_early_commit: bool = True,
        speculative_parallel: bool = True,
    ):
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise ValueError("max_turns must be an integer")
        if max_turns <= 0 or max_turns > 16:
            raise ValueError("max_turns must be between 1 and 16")
        self.model = model
        self.fallback_model = fallback_model
        self.max_turns = max_turns
        if isinstance(speculative_width, bool) or not isinstance(
            speculative_width, int
        ):
            raise ValueError("speculative_width must be an integer")
        if speculative_width < 2 or speculative_width > 4:
            raise ValueError("speculative_width must be between 2 and 4")
        self.speculative_width = speculative_width
        if isinstance(speculative_timeout_s, bool) or not isinstance(
            speculative_timeout_s, (int, float)
        ):
            raise ValueError("speculative_timeout_s must be a number")
        if not isfinite(speculative_timeout_s) or not (
            0.1 <= speculative_timeout_s <= 60
        ):
            raise ValueError("speculative_timeout_s must be between 0.1 and 60")
        if not isinstance(speculative_early_commit, bool):
            raise ValueError("speculative_early_commit must be a boolean")
        if not isinstance(speculative_parallel, bool):
            raise ValueError("speculative_parallel must be a boolean")
        self.speculative_timeout_s = float(speculative_timeout_s)
        self.speculative_early_commit = speculative_early_commit
        self.speculative_parallel = speculative_parallel
        self.lane_model_factory = lane_model_factory

    def run(
        self,
        board: Board,
        profile: CompanyProfile,
        *,
        policy: str = "deterministic",
    ) -> PlacementRun:
        if policy == "deterministic":
            return _deterministic_run(board, profile)
        if policy not in {"gemini", "tinker", "ollama", "opencode", "hybrid"}:
            raise ValueError(
                "policy must be 'deterministic', 'gemini', 'tinker', "
                "'ollama', 'opencode', or 'hybrid'"
            )
        if self.model is None:
            raise ValueError(f"{policy} policy requires a proposal model")
        default_proposer = "tinker" if policy == "hybrid" else policy
        proposer = (
            "gemini"
            if policy == "gemini"
            else getattr(self.model, "proposer_name", default_proposer)
        )
        fallback = self.fallback_model if policy == "hybrid" else None
        return _model_run(
            board,
            profile,
            self.model,
            self.max_turns,
            policy=policy,
            proposer=proposer,
            fallback_model=fallback,
            lane_model_factory=self.lane_model_factory or (lambda: self.model),
            speculative_width=self.speculative_width,
            speculative_timeout_s=self.speculative_timeout_s,
            speculative_early_commit=self.speculative_early_commit,
            speculative_parallel=self.speculative_parallel,
        )


def _deterministic_step(
    board: Board,
    profile: CompanyProfile,
    action: PlacementAction,
    turn: int,
) -> tuple[Board, PlacementStep]:
    before = evaluate(board, profile)
    updated = apply_actions(board, (action,), profile)
    after = evaluate(updated, profile)
    step = PlacementStep(
        turn=turn,
        proposer="deterministic",
        prompt="",
        response=action.as_text(),
        board_before=board,
        proposed=(action,),
        accepted=(action,),
        receipts=(
            PlacementReceipt(
                action=action,
                accepted=True,
                hard_before=before.hard,
                hard_after=after.hard,
                soft_before=before.soft,
                soft_after=after.soft,
                reason="accepted: deterministic score improvement",
            ),
        ),
        hard_before=before.hard,
        hard_after=after.hard,
        soft_before=before.soft,
        soft_after=after.soft,
        reason="accepted: deterministic score improvement",
    )
    return updated, step


def _deterministic_run(
    board: Board, profile: CompanyProfile
) -> PlacementRun:
    final, actions = repair(board, profile)
    current = board
    steps: list[PlacementStep] = []
    for turn, action in enumerate(actions, start=1):
        current, step = _deterministic_step(current, profile, action, turn)
        steps.append(step)
    if current != final:
        raise RuntimeError("deterministic repair trace did not reproduce")
    return PlacementRun(
        start=board,
        board=final,
        profile=profile,
        steps=tuple(steps),
        policy="deterministic",
        completed=evaluate(final, profile).hard == 0,
    )


def _model_step(
    board: Board,
    profile: CompanyProfile,
    model: TextModel,
    turn: int,
    proposer: str,
) -> tuple[Board, PlacementStep]:
    before = evaluate(board, profile)
    prompt = placement_prompt(board, profile, turn)
    started = time.perf_counter()
    try:
        response = model.generate(
            prompt,
            system=(
                "You are a PCB placement repair policy. Emit placement action "
                "lines only. The geometry verifier is authoritative."
            ),
            temperature=0.0,
            max_output_tokens=256,
        )
    except Exception as exc:
        raise PlacementPolicyError(f"{proposer} proposal failed") from exc
    if not isinstance(response, str):
        raise PlacementPolicyError(f"{proposer} returned non-text output")
    try:
        proposed = tuple(parse_actions(response))
    except ValueError as exc:
        raise PlacementPolicyError(
            f"{proposer} returned invalid placement actions"
        ) from exc
    updated, accepted, receipts = _verified_prefix(board, proposed, profile)
    after = evaluate(updated, profile)
    input_tokens, output_tokens, cost_usd = _usage(model)
    if not proposed:
        reason = "rejected: no valid placement actions"
    elif len(accepted) == len(proposed):
        reason = f"accepted speculative prefix {len(accepted)}/{len(proposed)}"
    else:
        ratio = f"{len(accepted)}/{len(proposed)}"
        reason = f"accepted speculative prefix {ratio}; stopped at rejection"
    step = PlacementStep(
        turn=turn,
        proposer=proposer,
        prompt=prompt,
        response=response,
        board_before=board,
        proposed=proposed,
        accepted=accepted,
        receipts=receipts,
        hard_before=before.hard,
        hard_after=after.hard,
        soft_before=before.soft,
        soft_after=after.soft,
        reason=reason,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    return updated, step


def _lane_prompt(prompt: str, lane: int, width: int) -> str:
    strategies = (
        "Minimize hard penetration with the shortest valid prefix.",
        "Start with a later listed candidate when it can repair legality.",
        "Repair legality, then minimize the company preference cost.",
        "Prefer a multi-component prefix using distinct component refs.",
    )
    return (
        f"{prompt}\n\nSPECULATIVE LANE {lane}/{width}\n"
        f"LANE OBJECTIVE: {strategies[lane - 1]}\n"
        "Do not imitate another lane. Return only the bounded action lines."
    )


def _usage(model: TextModel) -> tuple[int | None, int | None, float | None]:
    value = getattr(model, "last_usage", None)
    if not isinstance(value, dict):
        return None, None, None
    return value.get("input_tokens"), value.get("output_tokens"), value.get("cost_usd")


def _candidate(
    board: Board,
    profile: CompanyProfile,
    model: TextModel,
    prompt: str,
    lane: int,
    width: int,
) -> EvaluatedCandidate:
    started = time.perf_counter()
    lane_prompt = _lane_prompt(prompt, lane, width)
    try:
        response = model.generate(
            lane_prompt,
            system=(
                "You are a PCB placement repair policy. Emit placement action "
                "lines only. The geometry verifier is authoritative."
            ),
            temperature=0.2,
            max_output_tokens=256,
        )
        if not isinstance(response, str):
            raise TypeError("proposal was not text")
        proposed = tuple(parse_actions(response))
        updated, accepted, receipts = _verified_prefix(board, proposed, profile)
        after = evaluate(updated, profile)
        error = ""
        status = "viable" if accepted else "stalled"
        input_tokens, output_tokens, cost_usd = _usage(model)
    except Exception as exc:
        response = ""
        proposed = ()
        accepted = ()
        receipts = ()
        updated = board
        after = evaluate(board, profile)
        error = f"{type(exc).__name__}: proposal failed"
        status = "backend-timeout" if isinstance(exc, TimeoutError) else "error"
        input_tokens = output_tokens = cost_usd = None
    candidate = SpeculativeCandidate(
        lane=lane,
        prompt=lane_prompt,
        response=response,
        proposed=proposed,
        accepted=accepted,
        receipts=receipts,
        hard_after=after.hard,
        soft_after=after.soft,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        error=error,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    return candidate, updated


def _factory_failure_candidate(
    board: Board,
    profile: CompanyProfile,
    prompt: str,
    lane: int,
    width: int,
) -> EvaluatedCandidate:
    score = evaluate(board, profile)
    return SpeculativeCandidate(
        lane=lane,
        prompt=_lane_prompt(prompt, lane, width),
        response="",
        proposed=(),
        accepted=(),
        receipts=(),
        hard_after=score.hard,
        soft_after=score.soft,
        elapsed_ms=0.0,
        error="PlacementPolicyError: lane model factory failed",
        status="error",
    ), board


def _deadline_candidate(
    board: Board,
    profile: CompanyProfile,
    prompt: str,
    lane: int,
    width: int,
    timeout_s: float,
    *,
    status: str,
) -> EvaluatedCandidate:
    score = evaluate(board, profile)
    return SpeculativeCandidate(
        lane=lane,
        prompt=_lane_prompt(prompt, lane, width),
        response="",
        proposed=(),
        accepted=(),
        receipts=(),
        hard_after=score.hard,
        soft_after=score.soft,
        elapsed_ms=round(timeout_s * 1000, 3),
        error=f"TimeoutError: lane {status}",
        status=status,
    ), board


def _mark_duplicates(
    evaluated: list[EvaluatedCandidate],
) -> tuple[EvaluatedCandidate, ...]:
    first_lane: dict[tuple[PlacementAction, ...], int] = {}
    marked = []
    for candidate, board in sorted(evaluated, key=lambda item: item[0].lane):
        duplicate = first_lane.get(candidate.proposed) if candidate.proposed else None
        if candidate.proposed and duplicate is None:
            first_lane[candidate.proposed] = candidate.lane
        marked.append((replace(candidate, duplicate_of_lane=duplicate), board))
    return tuple(marked)


def _meets_early_target(
    evaluated: EvaluatedCandidate,
    target: tuple[float, float],
) -> bool:
    candidate, _ = evaluated
    return bool(candidate.accepted) and candidate.hard_after == 0 and (
        candidate.hard_after,
        candidate.soft_after,
    ) <= target


def _candidate_rank(
    evaluated: EvaluatedCandidate,
) -> tuple[float, float, int, int]:
    candidate, _ = evaluated
    return (
        candidate.hard_after,
        candidate.soft_after,
        -len(candidate.accepted),
        candidate.lane,
    )


def _parallel_candidates(
    board: Board,
    profile: CompanyProfile,
    model_factory: Callable[[], TextModel],
    prompt: str,
    width: int,
    timeout_s: float,
    early_target: tuple[float, float],
    early_commit_enabled: bool,
) -> tuple[tuple[EvaluatedCandidate, ...], EvaluatedCandidate | None]:
    models: dict[int, TextModel] = {}
    evaluated: list[EvaluatedCandidate] = []
    for lane in range(1, width + 1):
        try:
            models[lane] = model_factory()
        except Exception:
            evaluated.append(
                _factory_failure_candidate(board, profile, prompt, lane, width)
            )
    pool = ThreadPoolExecutor(
        max_workers=width, thread_name_prefix="placement-lane"
    )
    future_lanes: dict[Future, int] = {
        pool.submit(
            _candidate,
            board,
            profile,
            models[lane],
            prompt,
            lane,
            width,
        ): lane
        for lane in models
    }
    pending = set(future_lanes)
    early_winner = None
    deadline = time.perf_counter() + timeout_s
    while pending and early_winner is None:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        done, pending = wait(
            pending,
            timeout=remaining,
            return_when=FIRST_COMPLETED,
        )
        if not done:
            break
        completed = [future.result() for future in done]
        evaluated.extend(completed)
        if early_commit_enabled:
            eligible = [
                item for item in completed if _meets_early_target(item, early_target)
            ]
            if eligible:
                early_winner = min(eligible, key=_candidate_rank)
    status = "cancelled-after-early-commit" if early_winner else "deadline"
    for future in pending:
        lane = future_lanes[future]
        future.cancel()
        cancel = getattr(models[lane], "cancel", None)
        if callable(cancel):
            cancel()
        evaluated.append(
            _deadline_candidate(
                board,
                profile,
                prompt,
                lane,
                width,
                timeout_s,
                status=status,
            )
        )
    pool.shutdown(wait=False, cancel_futures=True)
    return _mark_duplicates(evaluated), early_winner


def _serial_candidates(
    board: Board,
    profile: CompanyProfile,
    model_factory: Callable[[], TextModel],
    prompt: str,
    width: int,
) -> tuple[tuple[EvaluatedCandidate, ...], None]:
    evaluated = []
    for lane in range(1, width + 1):
        try:
            model = model_factory()
        except Exception:
            evaluated.append(
                _factory_failure_candidate(board, profile, prompt, lane, width)
            )
            continue
        evaluated.append(_candidate(board, profile, model, prompt, lane, width))
    return _mark_duplicates(evaluated), None


def _speculative_step(
    board: Board,
    profile: CompanyProfile,
    model_factory: Callable[[], TextModel],
    turn: int,
    proposer: str,
    width: int,
    timeout_s: float,
    early_commit_enabled: bool,
    parallel: bool,
) -> tuple[Board, PlacementStep]:
    before = evaluate(board, profile)
    oracle_board, suggested = repair(board, profile)
    prompt = placement_prompt(
        board,
        profile,
        turn,
        suggested_actions=tuple(suggested),
    )
    oracle = evaluate(oracle_board, profile)
    early_target = (oracle.hard, oracle.soft)
    started = time.perf_counter()
    if parallel:
        evaluated, early_winner = _parallel_candidates(
            board,
            profile,
            model_factory,
            prompt,
            width,
            timeout_s,
            early_target,
            early_commit_enabled,
        )
    else:
        evaluated, early_winner = _serial_candidates(
            board, profile, model_factory, prompt, width
        )
    wall_ms = round((time.perf_counter() - started) * 1000, 3)
    viable = tuple(item for item in evaluated if item[0].accepted)
    winner = early_winner or (min(viable, key=_candidate_rank) if viable else None)
    representative = winner or min(
        evaluated,
        key=lambda item: (
            not bool(item[0].proposed),
            bool(item[0].error),
            item[0].lane,
        ),
    )
    chosen, updated = representative
    after = evaluate(updated, profile)
    input_tokens = [
        item[0].input_tokens for item in evaluated if item[0].input_tokens is not None
    ]
    output_tokens = [
        item[0].output_tokens
        for item in evaluated
        if item[0].output_tokens is not None
    ]
    costs = [item[0].cost_usd for item in evaluated if item[0].cost_usd is not None]
    reason = (
        f"speculative lane {chosen.lane}/{width} committed "
        f"{len(chosen.accepted)}/{len(chosen.proposed)} actions"
        if winner
        else f"all {width} speculative lanes stalled"
    )
    return updated, PlacementStep(
        turn=turn,
        proposer=proposer,
        prompt=prompt,
        response=chosen.response,
        board_before=board,
        proposed=chosen.proposed,
        accepted=chosen.accepted,
        receipts=chosen.receipts,
        hard_before=before.hard,
        hard_after=after.hard,
        soft_before=before.soft,
        soft_after=after.soft,
        reason=reason,
        candidates=tuple(item[0] for item in evaluated),
        winner_lane=chosen.lane if winner else None,
        speculative_wall_ms=wall_ms,
        early_commit=early_winner is not None,
        elapsed_ms=wall_ms,
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
        cost_usd=sum(costs) if costs else None,
    )


def _verified_prefix(
    board: Board,
    proposed: tuple[PlacementAction, ...],
    profile: CompanyProfile,
) -> tuple[Board, tuple[PlacementAction, ...], tuple[PlacementReceipt, ...]]:
    """Accept the longest prefix whose individual actions improve (H, P)."""
    current = board
    accepted: list[PlacementAction] = []
    receipts: list[PlacementReceipt] = []
    for action in proposed:
        before = evaluate(current, profile)
        candidate = apply_actions(current, (action,), profile)
        after = evaluate(candidate, profile)
        improved = (after.hard, after.soft) < (before.hard, before.soft)
        receipts.append(
            PlacementReceipt(
                action=action,
                accepted=improved,
                hard_before=before.hard,
                hard_after=after.hard if improved else before.hard,
                soft_before=before.soft,
                soft_after=after.soft if improved else before.soft,
                reason=(
                    "accepted: lexicographic score improved"
                    if improved
                    else "rejected: no lexicographic improvement"
                ),
            )
        )
        if not improved:
            break
        current = candidate
        accepted.append(action)
    return current, tuple(accepted), tuple(receipts)


def _model_run(
    board: Board,
    profile: CompanyProfile,
    model: TextModel,
    max_turns: int,
    *,
    policy: str,
    proposer: str,
    fallback_model: TextModel | None,
    lane_model_factory: Callable[[], TextModel],
    speculative_width: int,
    speculative_timeout_s: float,
    speculative_early_commit: bool,
    speculative_parallel: bool,
) -> PlacementRun:
    current = board
    steps: list[PlacementStep] = []
    for turn in range(1, max_turns + 1):
        if policy == "hybrid":
            current, step = _speculative_step(
                current,
                profile,
                lane_model_factory,
                turn,
                proposer,
                speculative_width,
                speculative_timeout_s,
                speculative_early_commit,
                speculative_parallel,
            )
        else:
            current, step = _model_step(current, profile, model, turn, proposer)
        steps.append(step)
        if not step.accepted and fallback_model is not None:
            current, fallback_step = _model_step(
                current, profile, fallback_model, turn, "gemini-recovery"
            )
            steps.append(fallback_step)
        if evaluate(current, profile).hard == 0:
            break
    return PlacementRun(
        start=board,
        board=current,
        profile=profile,
        steps=tuple(steps),
        policy=policy,
        completed=evaluate(current, profile).hard == 0,
    )
