"""Agent stages, driven by a scripted model.

No network, no API key. The point of the seam in :mod:`silkscreen.agents.model`
is that the whole prompt-to-PCB pipeline can be exercised deterministically,
including the failure paths that only ever fire against a badly-behaved model.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents import (
    ModelError,
    ScriptedModel,
    generate_pcb,
    propose_circuit,
    read_datasheet,
    review_circuit,
)
from silkscreen.agents.pipeline import MAX_RESPONSE_TEXT
from silkscreen.agents.propose import ProposalError
from silkscreen.agents.review import Severity
from silkscreen.netlist import parse_circuit_spec

# ---------------------------------------------------------------- fixtures


# This file is the SDK driver's suite; the ADK driver's is test_adk.py.
@pytest.fixture(autouse=True)
def _pin_sdk_engine(monkeypatch):
    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")


GOOD_CIRCUIT = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "DRV8837": {"pins": {"IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                             "OUT1": "5", "OUT2": "6", "VCC": "7", "nSLEEP": "8"}},
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_sleep": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1", "DRV8837.VM"],
        "GND": ["AMS1117-3.3.GND", "DRV8837.GND", "c_in.2", "c_out.2", "c_dec.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "DRV8837.VCC", "c_out.1", "c_dec.1",
                 "r_sleep.1"],
        "SLEEP": ["DRV8837.nSLEEP", "r_sleep.2"],
        "MOT": ["DRV8837.OUT1", "DRV8837.IN1"],
    },
}

DATASHEET_JSON = {
    "part_number": "AMS1117-3.3",
    "package": "SOT-223-3",
    "pin_count": 3,
    "pins": [
        {"number": "1", "name": "GND", "kind": "ground", "page": 1},
        {"number": "2", "name": "VOUT", "kind": "output", "page": 1},
        {"number": "3", "name": "VIN", "kind": "power", "page": 1},
    ],
    "requirements": [
        {"requirement": "Output capacitor must be >= 22uF tantalum", "page": 9}
    ],
    "auxiliaries": [
        {"name": "c_out", "type": "capacitor", "value": "22uF",
         "connects": "VOUT to GND", "why": "loop stability", "page": 9}
    ],
    "notes": "",
}


# ---------------------------------------------------------------- datasheet


def test_read_datasheet_extracts_pins_and_citations():
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    facts = read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/ams1117.pdf")
    assert facts.pin_count == 3
    assert facts.pin_map() == {"GND": "1", "VOUT": "2", "VIN": "3"}
    assert facts.requirements[0]["page"] == 9


def test_read_datasheet_sends_the_pdf_to_the_model():
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/ams1117.pdf")
    assert model.calls[0]["documents"][0].url == "https://x/ams1117.pdf"


def test_read_datasheet_tolerates_a_code_fence():
    fenced = "```json\n" + json.dumps(DATASHEET_JSON) + "\n```"
    model = ScriptedModel(responses=[fenced])
    facts = read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/a.pdf")
    assert facts.pin_count == 3


def test_read_datasheet_refuses_a_part_with_no_pinout():
    """Placing a part whose pinout is unknown is worse than failing."""
    empty = dict(DATASHEET_JSON, pins=[])
    model = ScriptedModel(responses=[json.dumps(empty)])
    with pytest.raises(ModelError, match="No pins extracted"):
        read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/a.pdf")


def test_pin_count_disagreement_is_flagged_not_hidden():
    mismatched = dict(DATASHEET_JSON, pin_count=8)
    model = ScriptedModel(responses=[json.dumps(mismatched)])
    facts = read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/a.pdf")
    assert "package choice may be wrong" in facts.notes


def test_read_datasheet_needs_a_document():
    with pytest.raises(ValueError, match="pdf_url or pdf_bytes"):
        read_datasheet(ScriptedModel(), "X")


# ---------------------------------------------------------------- propose


def test_propose_accepts_a_valid_circuit_first_try():
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    spec, attempts = propose_circuit(model, "a motor driver")
    assert spec.part_count() == 6
    assert len(attempts) == 1 and attempts[0].accepted


def test_propose_repairs_an_invalid_circuit():
    """The repair loop is the whole point: the model gets its errors back."""
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]  # caps now floating
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT)])

    spec, attempts = propose_circuit(model, "a motor driver")
    assert spec.part_count() == 6
    assert len(attempts) == 2
    assert not attempts[0].accepted and attempts[1].accepted
    assert any("floating" in e for e in attempts[0].errors)


def test_repair_prompt_contains_every_error():
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["VIN"] = ["AMS1117-3.3.NOPE", "c_in.1", "DRV8837.VM"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT)])
    propose_circuit(model, "a motor driver")
    repair_prompt = model.calls[1]["prompt"]
    assert "rejected" in repair_prompt
    assert "NOPE" in repair_prompt


def test_propose_gives_up_loudly_after_the_repair_budget():
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken)] * 3)
    with pytest.raises(ProposalError, match="No valid circuit after"):
        propose_circuit(model, "a motor driver", max_repairs=2)


def test_propose_passes_datasheet_facts_into_the_prompt():
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON),
                                     json.dumps(GOOD_CIRCUIT)])
    facts = [read_datasheet(model, "AMS1117-3.3", pdf_url="https://x/a.pdf")]
    propose_circuit(model, "a regulator", facts=facts)
    prompt = model.calls[1]["prompt"]
    assert "22uF tantalum" in prompt and "p.9" in prompt


# ---------------------------------------------------------------- review


def test_review_returns_findings_sorted_by_severity():
    response = json.dumps({"findings": [
        {"severity": "note", "title": "Silkscreen could be clearer", "detail": "",
         "parts": []},
        {"severity": "blocker", "title": "nSLEEP left floating",
         "detail": "The driver will not enable.", "parts": ["r_sleep"],
         "citation": "DRV8837 p.8", "suggested_fix": "Pull to VCC"},
        {"severity": "marginal", "title": "Decoupling is far from the pin",
         "detail": "", "parts": ["c_dec"]},
    ]})
    model = ScriptedModel(responses=[response])
    spec = parse_circuit_spec(GOOD_CIRCUIT)
    findings = review_circuit(model, spec)
    assert [f.severity for f in findings] == [
        Severity.BLOCKER, Severity.MARGINAL, Severity.NOTE
    ]
    assert findings[0].citation == "DRV8837 p.8"


def test_review_drops_findings_that_reference_nonexistent_parts():
    """A finding pointing at a part that isn't on the board helps nobody."""
    response = json.dumps({"findings": [
        {"severity": "blocker", "title": "U99 is miswired", "detail": "",
         "parts": ["U99", "c_dec"]},
    ]})
    model = ScriptedModel(responses=[response])
    findings = review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    assert findings[0].parts == ("c_dec",)


def test_review_handles_a_clean_result():
    model = ScriptedModel(responses=[json.dumps({"findings": []})])
    assert review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT)) == []


def test_review_prompt_asks_the_model_to_refute():
    """An agent asked 'is this correct?' says yes."""
    model = ScriptedModel(responses=[json.dumps({"findings": []})])
    review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    prompt = model.calls[0]["prompt"].lower()
    assert "wrong" in prompt and "do not compliment" in prompt


def test_empty_circuit_is_rejected():
    """A model that returns the wrong shape must not yield an empty board."""
    from silkscreen.netlist import ValidationError, parse_circuit_spec

    with pytest.raises(ValidationError, match="no devices and no passives"):
        parse_circuit_spec({"not": "a circuit"})


def test_propose_rejects_a_response_that_is_not_a_circuit():
    model = ScriptedModel(responses=[json.dumps({"part_number": "AMS1117"})] * 3)
    with pytest.raises(ProposalError):
        propose_circuit(model, "x", max_repairs=1)


def test_review_survives_junk_output():
    model = ScriptedModel(responses=['{"findings": "not a list"}'])
    assert review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT)) == []


# ---------------------------------------------------------------- pipeline


def test_full_pipeline_prompt_to_board(tmp_path):
    """intent -> datasheet -> propose -> place -> .kicad_pcb -> review."""
    review_json = json.dumps({"findings": [
        {"severity": "blocker", "title": "Output cap is ceramic, not tantalum",
         "detail": "The AMS1117 loop needs ESR the ceramic does not provide.",
         "parts": ["c_out"], "citation": "AMS1117 p.9",
         "suggested_fix": "Use a 22uF tantalum"},
    ]})
    # Markers must be unique to one stage: "datasheet" alone also appears in
    # the proposal prompt, which silently routed the wrong response.
    model = ScriptedModel(by_marker={
        "reading an electronic component datasheet": json.dumps(DATASHEET_JSON),
        "designing a printed circuit board": json.dumps(GOOD_CIRCUIT),
        "reviewing a circuit someone else designed": review_json,
    })

    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
    )

    assert result.board_path == out and out.exists()
    assert len(result.board.parts) == 6
    assert len(result.facts) == 1
    assert len(result.blockers) == 1
    assert result.repair_rounds == 0
    assert result.placement is None
    assert "parts" in result.summary()

    from kiutils.board import Board
    assert len(Board.from_file(str(out)).footprints) == 6


def test_integrated_placement_runs_before_artifacts_and_copper(tmp_path):
    """Placement policy calls are visible and finish before later board stages."""
    worker = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    placement_model = ScriptedModel(responses=["NOOP"])
    events = []

    result = generate_pcb(
        worker,
        "a motor driver",
        output=tmp_path / "board.kicad_pcb",
        review=False,
        time_limit_s=10.0,
        placement_profile="compact-control",
        placement_policy="gemini",
        placement_feedback={"weights": {"compactness_weight": 1.25}},
        placement_model=placement_model,
        placement_max_turns=1,
        on_event=events.append,
        include_responses=True,
    )

    assert result.placement is not None
    assert result.placement.applied is result.placement.run.completed
    assert result.placement.requested_policy == "gemini"
    assert result.placement.run.profile.compactness_weight == 1.25
    assert result.route is not None
    assert result.placed_board_path is not None

    assert [e["stage"] for e in events if e["event"] == "stage.start"] == [
        "propose",
        "place",
        "placement_repair",
        "schematic",
        "route",
    ]
    requests = [
        e
        for e in events
        if e["event"] == "model.request" and e["stage"] == "placement_repair"
    ]
    responses = [
        e
        for e in events
        if e["event"] == "model.response" and e["stage"] == "placement_repair"
    ]
    assert len(requests) == len(responses) == 1
    assert requests[0]["call_id"].startswith("placement-")
    assert requests[0]["call_id"] == responses[0]["call_id"]
    assert "PCB PLACEMENT REPAIR" in requests[0]["prompt"]
    assert responses[0]["text"] == "NOOP"


def test_pipeline_reports_repair_rounds(tmp_path):
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT),
                                     json.dumps({"findings": []})])
    result = generate_pcb(model, "a motor driver", output=tmp_path / "b.kicad_pcb",
                          time_limit_s=10.0)
    assert result.repair_rounds == 1
    assert "1 repair round" in result.summary()


def test_pipeline_can_skip_review(tmp_path):
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0)
    assert result.findings == []
    assert len(model.calls) == 1


def test_transport_failure_is_not_a_proposal_failure():
    """An upstream outage and a bad proposal are different conditions.

    Wrapping the first in the second loses the distinction, and a caller that
    must choose between "retry later" and "give up" cannot tell them apart --
    an HTTP service deciding 502 versus 500, for instance.
    """
    class Dead:
        def generate(self, *args, **kwargs):
            raise ModelError("upstream 503")

    with pytest.raises(ModelError, match="503"):
        propose_circuit(Dead(), "a motor driver")


# ---------------------------------------------------------------- events


def _scripted_pipeline_model():
    """The full-pipeline scripted model, reused by the event tests."""
    review_json = json.dumps({"findings": [
        {"severity": "blocker", "title": "Output cap is ceramic, not tantalum",
         "detail": "The AMS1117 loop needs ESR the ceramic does not provide.",
         "parts": ["c_out"], "citation": "AMS1117 p.9",
         "suggested_fix": "Use a 22uF tantalum"},
    ]})
    return ScriptedModel(by_marker={
        "reading an electronic component datasheet": json.dumps(DATASHEET_JSON),
        "designing a printed circuit board": json.dumps(GOOD_CIRCUIT),
        "reviewing a circuit someone else designed": review_json,
    })


def test_events_trace_every_stage_and_model_call(tmp_path):
    """The stream is a progress signal: one event per boundary, no payload."""
    model = _scripted_pipeline_model()
    events = []

    generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )

    assert [e["event"] for e in events] == [
        "stage.start", "read.part", "model.call", "stage.done",
        "stage.start", "model.call", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "model.call", "stage.done",
    ]
    # Schematic and route are stages like any other: each opens and closes, so
    # a client ticking a stage list never sees a close it has no open for.
    assert [e["stage"] for e in events if e["event"].startswith("stage.")] == [
        "read", "read", "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route", "review", "review",
    ]
    assert all(isinstance(e["t_s"], (int, float)) for e in events)
    assert len([e for e in events if e["event"] == "model.call"]) == len(model.calls)

    # No event may carry board text, model output or datasheet text.
    assert [e for e in events if e["event"] == "model.response"] == [], (
        "raw model output is opt-in: a default stream carries none of it"
    )
    for event in events:
        assert "kicad_pcb" not in event
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 500)


def test_the_event_name_set_is_frozen(tmp_path):
    """Every event name a client can be sent, in one assertion.

    ``frontend/src/lib/stream.js`` switches on these strings to turn a frame
    into a sentence, so a renamed or added event is a silent regression over
    there rather than a failure here -- unless this set is what has to change.
    """
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    # One datasheet and one repair round, so every unconditional event fires.
    model = ScriptedModel(responses=[
        json.dumps(DATASHEET_JSON), json.dumps(broken), json.dumps(GOOD_CIRCUIT),
        json.dumps({"findings": []}),
    ])
    events = []
    generate_pcb(model, "a 3.3V motor driver board",
                 datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
                 output=tmp_path / "b.kicad_pcb",
                 time_limit_s=10.0, on_event=events.append)

    assert {e["event"] for e in events} == {
        "stage.start", "stage.done", "read.part", "propose.round", "model.call",
    }
    # The two conditional names have their own tests here: model.response fires
    # only under include_responses, model.retry only behind a failover model.


def test_response_events_carry_each_answer_verbatim(tmp_path):
    """The debug stream: what the model actually said, attributed to its stage.

    A response follows its own call immediately, so a client reading the feed
    in order never has to guess which round-trip an answer belongs to.
    """
    model = _scripted_pipeline_model()
    events = []

    generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        include_responses=True,
    )

    names = [e["event"] for e in events]
    calls = [i for i, name in enumerate(names) if name == "model.call"]
    assert calls, "the pipeline made no model calls to report"
    assert all(names[i + 1] == "model.response" for i in calls)

    # What the scripted model returned for each prompt it was actually given,
    # so the assertion is about the wrapper rather than about this test's copy.
    answers = [
        next(r for marker, r in model.by_marker.items() if marker in call["prompt"])
        for call in model.calls
    ]
    responses = [e for e in events if e["event"] == "model.response"]
    requests = [e for e in events if e["event"] == "model.request"]
    assert [e["stage"] for e in requests] == ["read", "propose", "review"]
    assert [e["prompt"] for e in requests] == [call["prompt"] for call in model.calls]
    assert [e["call_id"] for e in requests] == [e["call_id"] for e in responses]
    assert [e["stage"] for e in responses] == ["read", "propose", "review"]
    assert [e["text"] for e in responses] == answers
    assert [e["chars"] for e in responses] == [len(a) for a in answers]
    assert all(e["truncated"] is False for e in responses)


def test_a_long_response_is_clipped_and_says_how_long_it_really_was(tmp_path):
    """A model that answers with a wall of text cannot flood the stream.

    The count is the untruncated one: a client that only ever sees the clipped
    text still learns that there was more of it.
    """
    circuit = json.dumps(GOOD_CIRCUIT)
    # Trailing whitespace: still the same JSON, so the pipeline runs as usual.
    padded = circuit + " " * (MAX_RESPONSE_TEXT + 500 - len(circuit))
    model = ScriptedModel(responses=[padded])
    events = []

    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append, include_responses=True)

    responses = [e for e in events if e["event"] == "model.response"]
    assert len(responses) == 1
    assert responses[0]["truncated"] is True
    assert responses[0]["chars"] == MAX_RESPONSE_TEXT + 500
    assert len(responses[0]["text"]) == MAX_RESPONSE_TEXT
    assert responses[0]["text"] == padded[:MAX_RESPONSE_TEXT]


def test_events_report_a_repair_round(tmp_path):
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT),
                                     json.dumps({"findings": []})])
    events = []
    generate_pcb(model, "a motor driver", output=tmp_path / "b.kicad_pcb",
                 time_limit_s=10.0, on_event=events.append)

    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1
    assert rounds[0]["round"] == 1 and rounds[0]["errors"] > 0
    assert isinstance(rounds[0]["first_error"], str) and rounds[0]["first_error"]

    done = [e for e in events
            if e["event"] == "stage.done" and e["stage"] == "propose"]
    assert done[0]["repair_rounds"] == 1


def test_events_omit_stages_that_did_not_run(tmp_path):
    """A skipped stage emits nothing at all, rather than an empty pair."""
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    events = []
    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append)

    assert [e for e in events if e.get("stage") == "review"] == []
    assert [e for e in events if e.get("stage") == "read"] == []
    assert len(model.calls) == 1


def test_pipeline_without_a_callback_behaves_identically(tmp_path):
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0, on_event=None)
    assert result.findings == []
    assert len(model.calls) == 1
    assert len(result.board.parts) == 6


def test_a_raising_callback_aborts_the_run(tmp_path):
    """A service aborts a run whose client disconnected by raising here."""
    model = _scripted_pipeline_model()

    def hang_up(event):
        raise RuntimeError("client gone")

    with pytest.raises(RuntimeError, match="client gone"):
        generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                     time_limit_s=10.0, on_event=hang_up)


def test_events_surface_a_provider_failover(tmp_path):
    """A failed provider is visible even though the call itself succeeded."""
    from silkscreen.agents.resilience import FallbackModel, Provider

    class Dead:
        def generate(self, *args, **kwargs):
            raise ModelError("upstream 503")

    model = FallbackModel(providers=[
        Provider(name="primary", model=Dead(), attempts=1),
        Provider(name="backup",
                 model=ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])),
    ])
    events = []
    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append)

    retries = [e for e in events if e["event"] == "model.retry"]
    assert len(retries) == 1
    assert retries[0]["provider"] == "primary" and retries[0]["stage"] == "propose"
    assert "ModelError" in retries[0]["error"]

    calls = [e for e in events if e["event"] == "model.call"]
    assert len(calls) == 1
    assert calls[0]["ok"] and calls[0]["provider"] == "backup"


def test_the_project_files_sit_beside_a_dotted_board_name(tmp_path):
    """The .kicad_pro must name the board that is actually there.

    Stripping every suffix turned "revision.2.kicad_pcb" into the stem
    "revision", so the project and the schematic landed next to a board file
    they did not describe -- and opening the advertised project found no board.
    """
    out = tmp_path / "revision.2.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
    )

    assert [p.name for p in result.artifacts] == [
        "revision.2.kicad_pro",
        "revision.2.kicad_sch",
        "revision.2.placed.kicad_pcb",
        "revision.2.kicad_pcb",
    ]
    assert all(p.exists() for p in result.artifacts)


def test_routing_can_be_turned_off_without_losing_the_board(tmp_path):
    """--no-route leaves a placed board with pads on nets and empty copper."""
    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
        route=False,
    )
    assert result.route is None
    assert result.board.tracks == []
    assert out.exists()


def test_board_only_writes_the_board_and_nothing_else(tmp_path):
    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
        emit_stages=False,
    )
    assert result.artifacts == [out]
    assert list(tmp_path.iterdir()) == [out]
