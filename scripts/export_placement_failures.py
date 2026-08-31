"""Export stored verifier failures as portable post-training JSONL."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def read_traces(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON on line {line_number}: {exc.msg}"
                ) from exc
            if value.get("schema_version") != 1:
                raise ValueError(
                    f"unsupported trace schema on line {line_number}"
                )
            yield value


def post_training_row(trace: dict[str, Any]) -> dict[str, Any]:
    pair = trace["post_training"]
    return {
        "trace_id": trace["trace_id"],
        "prompt": pair["prompt"],
        "chosen": pair["chosen"],
        "rejected": pair["rejected"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a PCB placement repair policy. Emit placement "
                    "action lines only. The geometry verifier is authoritative."
                ),
            },
            {"role": "user", "content": pair["prompt"]},
            {"role": "assistant", "content": pair["chosen"]},
        ],
        "metadata": {
            "model_id": trace["model_id"],
            "failure_kind": trace["failure_kind"],
            "chosen_source": trace["chosen_source"],
            "profile": trace["profile"]["name"],
            "input_origin": trace["input_origin"],
            "run_completed": trace["run_completed"],
        },
    }


def export_traces(source: Path, destination: Path) -> int:
    rows = [post_training_row(trace) for trace in read_traces(source)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export verifier failures for SFT or preference training"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = export_traces(args.source, args.destination)
    print(f"exported {count} failure traces to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
