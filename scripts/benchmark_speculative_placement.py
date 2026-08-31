"""Paired sequential-versus-parallel placement proposal benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from silkscreen.placement.agent import PlacementAgent
from silkscreen.placement.api import run_to_dict
from silkscreen.placement.ollama_policy import OllamaPlacementModel
from silkscreen.placement.opencode_policy import OpenCodePlacementModel
from silkscreen.placement.pcb_repair import demo_board, get_profile
from silkscreen.placement.synthetic import corrupt

WORKLOAD_VERSION = "speculative-placement-v1"
WORKLOAD = {
    "tune": [(0, "compact-control"), (1, "thermal-first"), (2, "compact-control")],
    "holdout": [
        (101, "thermal-first"),
        (102, "compact-control"),
        (103, "thermal-first"),
    ],
    "stress": [(999, "compact-control"), (1000, "thermal-first")],
}


@dataclass
class ScriptedLatencyModel:
    delay_s: float
    proposer_name: str = "scripted-latency"

    def generate(self, prompt: str, **kwargs: Any) -> str:
        del kwargs
        time.sleep(self.delay_s)
        lane_match = re.search(r"SPECULATIVE LANE (\d+)/", prompt)
        lane = int(lane_match.group(1)) if lane_match else 1
        block = prompt.split("CANDIDATE ACTIONS\n", 1)[1].split("\n\nPCB ", 1)[0]
        actions = [
            line
            for line in block.splitlines()
            if line.startswith(("PLACE ", "MOVE "))
        ]
        if not actions:
            return ""
        offset = min(lane - 1, len(actions) - 1)
        ordered = actions[offset:] + actions[:offset]
        self.last_usage = {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(ordered[0]) // 4,
        }
        return "\n".join(ordered[:4])


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def _factory(args: argparse.Namespace) -> Callable[[], Any]:
    if args.backend == "scripted":
        return lambda: ScriptedLatencyModel(args.scripted_delay_ms / 1000)
    if args.backend == "ollama":
        return lambda: OllamaPlacementModel(
            base_url=args.base_url,
            model=args.model,
            timeout_s=args.timeout_s,
        )
    return lambda: OpenCodePlacementModel(
        model=args.model,
        binary=args.opencode_bin,
        timeout_s=args.timeout_s,
    )


def _one_run(
    factory: Callable[[], Any],
    *,
    seed: int,
    profile_name: str,
    parallel: bool,
    timeout_s: float,
) -> dict[str, Any]:
    board = corrupt(demo_board(), seed)
    profile = get_profile(profile_name)
    started = time.perf_counter()
    run = PlacementAgent(
        factory(),
        lane_model_factory=factory,
        max_turns=1,
        speculative_width=3,
        speculative_timeout_s=timeout_s,
        speculative_early_commit=parallel,
        speculative_parallel=parallel,
    ).run(board, profile, policy="hybrid")
    wall_ms = (time.perf_counter() - started) * 1000
    result = run_to_dict(run)
    speculation = result["steps"][0]["speculation"]
    return {
        "mode": "parallel" if parallel else "sequential",
        "seed": seed,
        "profile": profile_name,
        "wall_ms": round(wall_ms, 3),
        "completed": result["completed"],
        "hard_after": result["score"]["after"]["hard"],
        "soft_after": result["score"]["after"]["soft"],
        "winner_lane": speculation["winner_lane"],
        "early_commit": speculation["early_commit"],
        "timed_out_lanes": speculation["timed_out_lanes"],
        "cancelled_lanes": speculation["cancelled_lanes"],
        "duplicate_lanes": speculation["duplicate_lanes"],
        "error_lanes": speculation["error_lanes"],
        "input_tokens": speculation["input_tokens"],
        "output_tokens": speculation["output_tokens"],
        "cost_usd": speculation["cost_usd"],
        "gemini_recovery_used": result["proposal_metrics"][
            "gemini_recovery_used"
        ],
    }


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = [record["wall_ms"] for record in records]
    costs = [record["cost_usd"] for record in records if record["cost_usd"] is not None]
    return {
        "runs": len(records),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "legality_rate": sum(record["completed"] for record in records) / len(records),
        "timeout_rate": sum(bool(record["timed_out_lanes"]) for record in records)
        / len(records),
        "recovery_rate": sum(record["gemini_recovery_used"] for record in records)
        / len(records),
        "input_tokens": sum(record["input_tokens"] or 0 for record in records),
        "output_tokens": sum(record["output_tokens"] or 0 for record in records),
        "cost_usd": round(sum(costs), 9) if costs else None,
    }


def _promotion(summary: dict[str, Any]) -> dict[str, Any]:
    tune = summary.get("tune")
    holdout = summary.get("holdout")
    stress = summary.get("stress")
    if tune is None:
        return {"status": "partial", "reason": "tune split was not run"}
    correctness = tune["parallel"]["legality_rate"] == 1.0 and tune["parallel"][
        "legality_rate"
    ] >= tune["sequential"]["legality_rate"]
    speed = tune["parallel"]["p50_ms"] <= 0.8 * tune["sequential"]["p50_ms"]
    holdout_ok = holdout is not None and holdout["parallel"]["p95_ms"] <= 1.05 * (
        holdout["sequential"]["p95_ms"]
    )
    stress_ok = stress is not None and stress["parallel"]["legality_rate"] >= stress[
        "sequential"
    ]["legality_rate"]
    passed = correctness and speed and holdout_ok and stress_ok
    return {
        "status": "complete" if passed else "rejected",
        "correctness": correctness,
        "tune_p50_at_least_20_percent_faster": speed,
        "holdout_p95_regression_within_5_percent": holdout_ok,
        "stress_legality_not_worse": stress_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=("scripted", "ollama", "opencode"),
        default="scripted",
    )
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--opencode-bin", default="opencode")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--scripted-delay-ms", type=float, default=80.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--split",
        choices=("tune", "holdout", "stress", "all"),
        default="all",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    factory = _factory(args)
    selected = WORKLOAD if args.split == "all" else {args.split: WORKLOAD[args.split]}
    records = []
    for split, cases in selected.items():
        for repeat in range(args.repeats):
            for case_index, (seed, profile_name) in enumerate(cases):
                order = (
                    (False, True)
                    if (repeat + case_index) % 2 == 0
                    else (True, False)
                )
                for parallel in order:
                    record = _one_run(
                        factory,
                        seed=seed,
                        profile_name=profile_name,
                        parallel=parallel,
                        timeout_s=args.timeout_s,
                    )
                    record.update({"split": split, "repeat": repeat})
                    records.append(record)
    summaries = {}
    for split in selected:
        summaries[split] = {
            mode: _summary(
                [
                    record
                    for record in records
                    if record["split"] == split and record["mode"] == mode
                ]
            )
            for mode in ("sequential", "parallel")
        }
    workload_json = json.dumps(WORKLOAD, sort_keys=True, separators=(",", ":"))
    report = {
        "schema_version": 1,
        "workload_version": WORKLOAD_VERSION,
        "workload_sha256": hashlib.sha256(workload_json.encode()).hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "backend": args.backend,
            "model": args.model,
            "repeats": args.repeats,
        },
        "summary": summaries,
        "promotion": _promotion(summaries),
        "records": records,
    }
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
