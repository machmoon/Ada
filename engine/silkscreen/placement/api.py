"""JSON boundary for Silkscreen placement repair."""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from typing import Any

from .agent import PlacementAgent, PlacementRun
from .grader import (
    board_from_dict,
    board_to_dict,
    outcome_reward,
    progress_reward,
    quality_reward,
)
from .pcb_repair import CompanyProfile, demo_board, evaluate, get_profile

__all__ = ["apply_feedback", "repair_request", "run_to_dict"]


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _merged_groups(
    current: tuple[tuple[str, ...], ...], additions: Any
) -> tuple[tuple[str, ...], ...]:
    groups = list(current)
    for group in additions if isinstance(additions, list) else []:
        refs = _strings(group)
        if len(refs) >= 2 and refs not in groups:
            groups.append(refs)
    return tuple(groups)


def _merged_thermal_pairs(
    current: tuple[tuple[str, str, float], ...], additions: Any
) -> tuple[tuple[str, str, float], ...]:
    pairs = list(current)
    for item in additions if isinstance(additions, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        distance = float(item[2])
        if not math.isfinite(distance):
            raise ValueError("thermal pair distance must be finite")
        pair = (str(item[0]), str(item[1]), distance)
        if distance > 0 and pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


_WEIGHT_NAMES = {
    "compactness_weight",
    "grouping_weight",
    "connector_edge_weight",
    "thermal_weight",
}


def _feedback_weights(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    weights = {key: float(raw) for key, raw in value.items() if key in _WEIGHT_NAMES}
    if any(not math.isfinite(weight) for weight in weights.values()):
        raise ValueError("profile weights must be finite")
    if any(weight < 0 for weight in weights.values()):
        raise ValueError("profile weights cannot be negative")
    return weights


def apply_feedback(
    profile: CompanyProfile, feedback: dict[str, Any] | None
) -> CompanyProfile:
    """Fold explicit engineer corrections into a company profile.

    Feedback stays structured. Gemini may suggest it in the product, but this
    function is the authority that validates and applies it.
    """
    if not feedback:
        return profile
    if not isinstance(feedback, dict):
        raise ValueError("feedback must be an object")

    fixed = tuple(
        dict.fromkeys(
            profile.fixed_refs + _strings(feedback.get("fixed_refs_add"))
        )
    )
    edges = tuple(
        dict.fromkeys(profile.edge_refs + _strings(feedback.get("edge_refs_add")))
    )

    return replace(
        profile,
        fixed_refs=fixed,
        edge_refs=edges,
        groups=_merged_groups(profile.groups, feedback.get("groups_add")),
        thermal_pairs=_merged_thermal_pairs(
            profile.thermal_pairs, feedback.get("thermal_pairs_add")
        ),
        **_feedback_weights(feedback.get("weights")),
    )


def _profile(payload: dict[str, Any]) -> CompanyProfile:
    value = payload.get("profile", "compact-control")
    if isinstance(value, str):
        return get_profile(value)
    if isinstance(value, dict):
        return CompanyProfile.from_dict(value)
    raise ValueError("profile must be a profile name or object")


def _profile_dict(profile: CompanyProfile) -> dict[str, Any]:
    value = asdict(profile)
    value["fixed_refs"] = list(profile.fixed_refs)
    value["edge_refs"] = list(profile.edge_refs)
    value["groups"] = [list(group) for group in profile.groups]
    value["thermal_pairs"] = [list(pair) for pair in profile.thermal_pairs]
    return value


def _evaluation_dict(evaluation: Any) -> dict[str, Any]:
    """Expose the two meaningful axes without leaking the internal rank scalar."""
    return {
        "hard": evaluation.hard,
        "soft": evaluation.soft,
        "violations": [asdict(violation) for violation in evaluation.violations],
        "terms": evaluation.terms,
    }


def _receipt_dict(receipt: Any) -> dict[str, Any]:
    return {**asdict(receipt), "action": asdict(receipt.action)}


def _candidate_dict(candidate: Any) -> dict[str, Any]:
    return {
        "lane": candidate.lane,
        "prompt": candidate.prompt,
        "response": candidate.response,
        "proposed": [asdict(action) for action in candidate.proposed],
        "accepted": [asdict(action) for action in candidate.accepted],
        "receipts": [_receipt_dict(receipt) for receipt in candidate.receipts],
        "hard_after": candidate.hard_after,
        "soft_after": candidate.soft_after,
        "elapsed_ms": candidate.elapsed_ms,
        "error": candidate.error,
        "status": candidate.status,
        "duplicate_of_lane": candidate.duplicate_of_lane,
        "input_tokens": candidate.input_tokens,
        "output_tokens": candidate.output_tokens,
        "cost_usd": candidate.cost_usd,
    }


def _speculation_dict(step: Any) -> dict[str, Any] | None:
    if not step.candidates:
        return None
    input_tokens = [
        item.input_tokens
        for item in step.candidates
        if item.input_tokens is not None
    ]
    output_tokens = [
        item.output_tokens
        for item in step.candidates
        if item.output_tokens is not None
    ]
    costs = [item.cost_usd for item in step.candidates if item.cost_usd is not None]
    return {
        "width": len(step.candidates),
        "winner_lane": step.winner_lane,
        "wall_ms": step.speculative_wall_ms,
        "early_commit": step.early_commit,
        "timed_out_lanes": [
            item.lane
            for item in step.candidates
            if item.status in {"deadline", "backend-timeout"}
        ],
        "cancelled_lanes": [
            item.lane
            for item in step.candidates
            if item.status == "cancelled-after-early-commit"
        ],
        "duplicate_lanes": [
            item.lane for item in step.candidates if item.duplicate_of_lane is not None
        ],
        "error_lanes": [
            item.lane for item in step.candidates if item.status == "error"
        ],
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "cost_usd": round(sum(costs), 9) if costs else None,
        "candidates": [_candidate_dict(item) for item in step.candidates],
    }


def _proposal_metrics(run: PlacementRun) -> dict[str, Any]:
    model_steps = [step for step in run.steps if step.proposer != "deterministic"]
    input_tokens = [
        step.input_tokens for step in model_steps if step.input_tokens is not None
    ]
    output_tokens = [
        step.output_tokens for step in model_steps if step.output_tokens is not None
    ]
    costs = [step.cost_usd for step in model_steps if step.cost_usd is not None]
    return {
        "wall_ms": round(sum(step.elapsed_ms for step in model_steps), 3),
        "backend_calls": sum(len(step.candidates) or 1 for step in model_steps),
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "cost_usd": round(sum(costs), 9) if costs else None,
        "gemini_recovery_used": any(
            step.proposer == "gemini-recovery" for step in model_steps
        ),
    }


def run_to_dict(
    run: PlacementRun, feedback: dict[str, Any] | None = None
) -> dict[str, Any]:
    before = evaluate(run.start, run.profile)
    after = evaluate(run.board, run.profile)
    return {
        "policy": run.policy,
        "completed": run.completed,
        "profile": _profile_dict(run.profile),
        "feedback_applied": feedback or {},
        "start": board_to_dict(run.start),
        "board": board_to_dict(run.board),
        "score": {
            "before": _evaluation_dict(before),
            "after": _evaluation_dict(after),
            "hard_penetration_removed_mm": round(before.hard - after.hard, 6),
            "preference_cost_change": round(after.soft - before.soft, 6),
        },
        "reward": {
            "outcome": outcome_reward(run.board, run.profile),
            "progress": progress_reward(run.start, run.board, run.profile),
            "preference": quality_reward(run.start, run.board, run.profile),
        },
        "proposal_metrics": _proposal_metrics(run),
        "steps": [
            {
                "turn": step.turn,
                "proposer": step.proposer,
                "prompt": step.prompt,
                "response": step.response,
                "board_before": board_to_dict(step.board_before),
                "proposed": [asdict(action) for action in step.proposed],
                "accepted": [asdict(action) for action in step.accepted],
                "receipts": [_receipt_dict(receipt) for receipt in step.receipts],
                "hard_before": step.hard_before,
                "hard_after": step.hard_after,
                "soft_before": step.soft_before,
                "soft_after": step.soft_after,
                "reason": step.reason,
                "elapsed_ms": step.elapsed_ms,
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "cost_usd": step.cost_usd,
                "speculation": _speculation_dict(step),
            }
            for step in run.steps
        ],
    }


def _supplied_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    supplied = payload.get("feedback") or {}
    if not isinstance(supplied, dict):
        raise ValueError("feedback must be an object")
    return supplied


def repair_request(
    payload: dict[str, Any],
    *,
    model=None,
    fallback_model=None,
    lane_model_factory=None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    board_value = payload.get("board")
    board = demo_board() if board_value is None else board_from_dict(board_value)
    policy = str(payload.get("policy", "deterministic")).strip().lower()
    profile = _profile(payload)

    if "profile_id" in payload:
        raise ValueError(
            "profile_id is unavailable on the unauthenticated placement endpoint"
        )
    supplied = _supplied_feedback(payload)
    profile = apply_feedback(profile, supplied)

    run = PlacementAgent(
        model,
        fallback_model=fallback_model,
        lane_model_factory=lane_model_factory,
        max_turns=payload.get("max_turns", 8),
        speculative_width=payload.get("speculative_width", 3),
        speculative_timeout_s=payload.get("speculative_timeout_s", 8.0),
        speculative_early_commit=payload.get("speculative_early_commit", True),
    ).run(board, profile, policy=policy)
    result = run_to_dict(run, supplied)
    result["profile_memory"] = "request-only" if supplied else "none"
    return result
