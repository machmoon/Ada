from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from silkscreen.placement.agent import PlacementPolicyError
from silkscreen.placement.opencode_policy import OpenCodePlacementModel


def test_opencode_policy_denies_tools_and_reads_usage() -> None:
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        events = [
            {"type": "text", "part": {"text": "PLACE C1 12 3"}},
            {
                "type": "step_finish",
                "part": {
                    "tokens": {"input": 120, "output": 8},
                    "cost": 0.0004,
                },
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    model = OpenCodePlacementModel(
        model="opencode-go/glm-5.3-flash",
        runner=runner,
    )

    assert model.generate("repair", system="actions only") == "PLACE C1 12 3"
    assert seen["command"][1:4] == ["run", "--pure", "--format"]
    assert "--agent" in seen["command"]
    config = json.loads(seen["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
    placement = config["agent"]["placement"]
    assert placement["steps"] == 1
    assert placement["tools"] == {"*": False}
    assert placement["permission"] == {"*": "deny"}
    assert model.last_usage == {
        "input_tokens": 120,
        "output_tokens": 8,
        "cost_usd": 0.0004,
    }


def test_opencode_policy_converts_timeout_to_policy_error() -> None:
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    model = OpenCodePlacementModel(
        model="opencode-go/glm-5.3-flash",
        timeout_s=0.1,
        runner=runner,
    )

    with pytest.raises(PlacementPolicyError, match="timed out"):
        model.generate("repair")
