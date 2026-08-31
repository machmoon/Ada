from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from pcb_verifier.agent import PlacementAgent, PlacementPolicyError
from pcb_verifier.api import repair_request, run_to_dict
from pcb_verifier.grader import (
    apply_model_output,
    board_from_dict,
    board_score,
    board_to_dict,
    board_to_json,
    is_legal,
    outcome_reward,
    progress_reward,
    quality_reward,
)
from pcb_verifier.ollama_policy import OllamaPlacementModel
from pcb_verifier.pcb_repair import (
    Board,
    CompanyProfile,
    Component,
    Keepout,
    apply_actions,
    demo_board,
    evaluate,
    get_profile,
    parse_actions,
    repair,
)
from pcb_verifier.synthetic import corrupt, trajectory
from pcb_verifier.tinker_policy import TinkerPlacementModel
from pcb_verifier.traces import JsonlFailureTraceStore, build_failure_traces


@dataclass
class QueueModel:
    responses: list[str]
    calls: list[dict] = field(default_factory=list)

    def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.responses.pop(0)


def test_demo_board_is_corrupted_and_repair_is_legal() -> None:
    board = demo_board()
    profile = get_profile("compact-control")
    assert not is_legal(board, profile)

    repaired, actions = repair(board, profile)

    assert actions
    assert is_legal(repaired, profile)
    assert board_score(repaired, profile) < board_score(board, profile)
    assert repaired.component("J1") == board.component("J1")


def test_profiles_produce_distinct_legal_placements() -> None:
    board = demo_board()
    compact, _ = repair(board, get_profile("compact-control"))
    thermal, _ = repair(board, get_profile("thermal-first"))

    assert is_legal(compact, get_profile("compact-control"))
    assert is_legal(thermal, get_profile("thermal-first"))
    compact_xy = [(part.ref, part.x, part.y) for part in compact.components]
    thermal_xy = [(part.ref, part.x, part.y) for part in thermal.components]
    assert compact_xy != thermal_xy


def test_parser_ignores_garbage_unknown_refs_and_fixed_moves() -> None:
    board = demo_board()
    profile = get_profile("compact-control")
    actions = parse_actions(
        "thinking aloud\nMOVE NOPE 2 3\nPLACE J1 20 20\nPLACE C1 10.5 4 90"
    )
    moved = apply_actions(board, actions, profile)

    assert len(actions) == 3
    assert moved.component("J1") == board.component("J1")
    assert moved.component("C1").x == 10.5
    assert moved.component("C1").angle == 90


def test_keepout_and_boundary_are_hard_violations() -> None:
    profile = CompanyProfile("test", clearance=0.5, edge_margin=1.0)
    board = Board(
        20,
        10,
        (Component("U1", -1, 2, 4, 4), Component("C1", 12, 2, 3, 3)),
        (Keepout("boss", 11, 1, 5, 5),),
    )
    result = evaluate(board, profile)

    assert {violation.kind for violation in result.violations} == {
        "boundary",
        "keepout",
    }
    assert result.hard > 0


def test_rewards_prioritize_legality_and_track_progress() -> None:
    start = demo_board()
    profile = get_profile("compact-control")
    final, _ = repair(start, profile)

    assert outcome_reward(start, profile) == 0.0
    assert outcome_reward(final, profile) == 1.0
    assert progress_reward(start, final, profile) == 1.0
    assert 0 < quality_reward(start, final, profile) <= 0.1


def test_serialization_round_trip() -> None:
    board = demo_board()
    restored = board_from_dict(json.loads(board_to_json(board)))
    assert board_to_dict(restored) == board_to_dict(board)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_board_geometry_is_rejected(value: float) -> None:
    board = board_to_dict(demo_board())
    board["components"][0]["x"] = value

    with pytest.raises(ValueError, match="finite number"):
        board_from_dict(board)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_profile_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        repair_request(
            {"profile": {"name": "unsafe", "clearance": value}}
        )


def test_non_finite_feedback_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        repair_request(
            {
                "profile": "compact-control",
                "feedback": {"weights": {"thermal_weight": math.nan}},
            }
        )


def test_scripted_agent_accepts_improving_move_and_ignores_hallucination() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    model = QueueModel(responses=["PLACE C1 12 3\nMOVE MADE_UP 3 3"])

    run = PlacementAgent(model, max_turns=2).run(board, profile, policy="gemini")

    assert run.completed
    assert len(run.steps) == 1
    assert [action.ref for action in run.steps[0].proposed] == ["C1", "MADE_UP"]
    assert [action.ref for action in run.steps[0].accepted] == ["C1"]
    assert [receipt.accepted for receipt in run.steps[0].receipts] == [True, False]
    assert is_legal(run.board, profile)
    assert model.calls[0]["system"].startswith("You are a PCB placement")


def test_agent_rejects_a_worse_batch() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(20, 12, (Component("U1", 2, 2, 4, 4),))
    model = QueueModel(responses=["PLACE U1 -4 -4"])

    run = PlacementAgent(model, max_turns=1).run(board, profile, policy="gemini")

    assert run.steps[0].accepted == ()
    assert run.board == board


def test_hybrid_policy_uses_gemini_only_after_fast_policy_stalls() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    fast = QueueModel(responses=["PLACE C1 3 3"])
    recovery = QueueModel(responses=["PLACE C1 12 3"])

    run = PlacementAgent(
        fast, fallback_model=recovery, max_turns=1
    ).run(board, profile, policy="hybrid")

    assert run.completed
    assert [step.proposer for step in run.steps] == ["tinker", "gemini-recovery"]
    assert run.steps[0].accepted == ()
    assert run.steps[1].accepted


def test_failed_policy_step_becomes_recovery_training_pair(tmp_path) -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    fast = QueueModel(responses=["PLACE C1 3 3"])
    recovery = QueueModel(responses=["PLACE C1 12 3"])
    run = PlacementAgent(
        fast, fallback_model=recovery, max_turns=1
    ).run(board, profile, policy="hybrid")

    traces = build_failure_traces(
        run_to_dict(run),
        model_id="Qwen/Qwen3.5-4B",
        input_origin="demo-board",
        now=lambda: 123.0,
        id_factory=lambda: "trace-1",
    )

    assert len(traces) == 1
    trace = traces[0]
    assert trace["failure_kind"] == "all-actions-rejected"
    assert trace["chosen_source"] == "gemini-recovery"
    assert trace["rejected_response"] == "PLACE C1 3 3"
    assert trace["chosen_response"] == "PLACE C1 12 3"
    assert trace["post_training"] == {
        "prompt": trace["prompt"],
        "chosen": "PLACE C1 12 3",
        "rejected": "PLACE C1 3 3",
    }

    store = JsonlFailureTraceStore(tmp_path / "failures.jsonl")
    assert store.append(trace) == "trace-1"
    saved = json.loads((tmp_path / "failures.jsonl").read_text())
    assert saved["trace_id"] == "trace-1"


def test_turn_limit_failure_uses_deterministic_oracle_target() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (
            Component("U1", 2, 2, 5, 5),
            Component("C1", 4, 3, 3, 3),
            Component("C2", 5, 4, 3, 3),
        ),
    )
    model = QueueModel(responses=["PLACE C1 12 3"])
    run = PlacementAgent(model, max_turns=1).run(
        board, profile, policy="tinker"
    )
    assert not run.completed

    traces = build_failure_traces(
        run_to_dict(run),
        model_id="Qwen/Qwen3.5-4B",
        input_origin="demo-board",
        id_factory=lambda: "trace-2",
    )

    assert traces[0]["failure_kind"] == "turn-limit-illegal"
    assert traces[0]["chosen_source"] == "deterministic-oracle"
    assert traces[0]["chosen_response"].startswith(("PLACE ", "MOVE "))


def test_model_output_helper_uses_same_action_contract() -> None:
    profile = CompanyProfile("simple")
    board = Board(20, 12, (Component("U1", 2, 2, 4, 4),))
    moved = apply_model_output(board, "MOVE U1 2 -1", profile)
    assert (moved.component("U1").x, moved.component("U1").y) == (4, 1)


def test_synthetic_trajectory_is_deterministic_and_repairable() -> None:
    first = trajectory(7)
    second = trajectory(7)
    assert first == second
    assert first["completion"]
    assert [message["role"] for message in first["messages"]] == [
        "system",
        "user",
        "assistant",
    ]
    assert corrupt(demo_board(), 7) == corrupt(demo_board(), 7)


def test_tinker_adapter_samples_checkpoint_with_chat_rendering() -> None:
    calls: list[dict] = []

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            calls.append({"messages": messages, "template": kwargs})
            return "rendered"

        def encode(self, text):
            assert text == "rendered"
            return [1, 2, 3]

        def decode(self, tokens, **kwargs):
            assert tokens == [8, 9]
            return "PLACE C1 12 3"

    class Future:
        def result(self):
            return SimpleNamespace(
                sequences=[SimpleNamespace(tokens=[8, 9])]
            )

    class Sampling:
        def get_tokenizer(self):
            return Tokenizer()

        def sample(self, **kwargs):
            calls.append({"sample": kwargs})
            return Future()

    class Service:
        def create_sampling_client(self, **kwargs):
            calls.append({"client": kwargs})
            return Sampling()

    class ModelInput:
        @staticmethod
        def from_ints(tokens):
            return tuple(tokens)

    class SamplingParams:
        def __init__(self, **kwargs):
            self.values = kwargs

    fake_tinker = SimpleNamespace(
        types=SimpleNamespace(
            ModelInput=ModelInput,
            SamplingParams=SamplingParams,
        )
    )
    model = TinkerPlacementModel(
        model_path="tinker://run/sampler_weights/final",
        service_client=Service(),
        tinker_module=fake_tinker,
    )

    output = model.generate("board", system="rules", max_output_tokens=64)

    assert output == "PLACE C1 12 3"
    assert calls[0]["client"]["model_path"].startswith("tinker://")
    assert calls[-1]["sample"]["num_samples"] == 1


def test_ollama_adapter_uses_private_chat_endpoint() -> None:
    calls: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"message": {"content": "PLACE C1 12 3"}}
            ).encode()

    def open_request(request, *, timeout):
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data),
                "timeout": timeout,
            }
        )
        return Response()

    model = OllamaPlacementModel(
        base_url="http://127.0.0.1:11435",
        model="gemma3:4b",
        opener=open_request,
    )
    output = model.generate("board", system="rules", max_output_tokens=64)

    assert output == "PLACE C1 12 3"
    assert calls[0]["url"].endswith("/api/chat")
    assert calls[0]["payload"]["model"] == "gemma3:4b"
    assert calls[0]["payload"]["stream"] is False


@pytest.mark.parametrize(
    "payload",
    [b"{not-json", b'{"message": {}}', b'{"message": {"content": 7}}'],
)
def test_ollama_adapter_normalizes_malformed_responses(payload: bytes) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    model = OllamaPlacementModel(
        base_url="http://127.0.0.1:11435",
        opener=lambda *args, **kwargs: Response(),
    )

    with pytest.raises(PlacementPolicyError):
        model.generate("board")


@pytest.mark.parametrize("name", ["", "unknown", "Thermalish"])
def test_unknown_profile_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unknown placement profile"):
        get_profile(name)
