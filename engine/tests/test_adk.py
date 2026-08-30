"""The ADK driver, held to the straight-line driver's contract.

:mod:`silkscreen.agents.adk` runs the same four stage bodies as
:mod:`silkscreen.agents.pipeline`, through a Google ADK workflow rather than a
straight line. Nearly every assertion here is one that ``test_agents.py``
already makes about the SDK driver, because the claim being tested is that a
client -- the service, and the SPA behind it -- cannot tell the two apart.

``google.adk`` is imported behind a flag rather than with
``pytest.importorskip``: ``scripts/check_docs.py`` checks the test counts the
docs quote against what pytest collects, and a module-level importorskip
collects nothing at all on a machine without the optional extra, which would
make the documented count depend on the machine instead of on the suite.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from silkscreen.agents import ModelError, ScriptedModel, generate_pcb
from silkscreen.agents.propose import ProposalError

# The circuits and the scripted model are the SDK suite's own, imported rather
# than copied so both drivers are provably fed the same bytes. Unguarded on
# purpose: the dispatch tests at the bottom of this file run on a base install
# and need them too.
from test_agents import DATASHEET_JSON, GOOD_CIRCUIT, _scripted_pipeline_model

# The try covers the third-party package and nothing else. It used to wrap the
# import of the driver as well, which meant any ImportError raised *inside*
# silkscreen.agents.adk -- a typo, a renamed stage, a bad relative import --
# was reported as "the extra is not installed" and skipped. Eleven real
# failures came back green that way. Split like this, a machine with google.adk
# installed fails loudly on internal breakage, and a machine without it still
# collects every test in the file and skips the guarded ones stably.
try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:  # a base install has no google.adk; these tests skip
    _HAS_ADK = False

if _HAS_ADK:
    from silkscreen.agents.adk import generate_pcb_adk

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")

INTENT = "a 3.3V motor driver board"
SHEETS = {"AMS1117-3.3": "https://x/ams1117.pdf"}


def _broken_circuit():
    """GOOD_CIRCUIT with the capacitors floating, so the first proposal fails."""
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    return broken


def _chain(exc):
    """Every exception under this one, the way ``service/app.py`` walks it.

    ``caused_by_model_failure`` decides 502-versus-500 by following
    ``__cause__``/``__context__``, so an upstream outage raised inside an ADK
    node has to stay findable along that same chain -- ADK re-raises it through
    machinery of its own, which a bare isinstance check would not survive.
    """
    seen: set[int] = set()
    out: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


# ---------------------------------------------------------------- events


@needs_adk
def test_events_trace_every_stage_and_model_call(tmp_path):
    """The same twelve frames, in the same order, as the SDK driver's stream."""
    model = _scripted_pipeline_model()
    events = []

    generate_pcb_adk(
        model,
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )

    assert [e["event"] for e in events] == [
        "stage.start", "read.part", "model.call", "stage.done",
        "stage.start", "model.call", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "model.call", "stage.done",
    ]
    assert [e["stage"] for e in events if e["event"].startswith("stage.")] == [
        "read", "read", "propose", "propose", "place", "place", "review", "review",
    ]
    assert all(isinstance(e["t_s"], (int, float)) for e in events)
    assert len([e for e in events if e["event"] == "model.call"]) == len(model.calls)


@needs_adk
def test_the_event_name_set_is_frozen(tmp_path):
    """The same frozen set the SDK driver is held to, from the other driver.

    ``frontend/src/lib/stream.js`` switches on these strings to turn a frame
    into a sentence, so a name that exists on one driver and not on the other
    is a silent regression over there rather than a failure here.
    """
    # One datasheet and one repair round, so every unconditional event fires.
    model = ScriptedModel(responses=[
        json.dumps(DATASHEET_JSON), json.dumps(_broken_circuit()),
        json.dumps(GOOD_CIRCUIT), json.dumps({"findings": []}),
    ])
    events = []
    generate_pcb_adk(model, INTENT, datasheets=SHEETS,
                     output=tmp_path / "b.kicad_pcb",
                     time_limit_s=10.0, on_event=events.append)

    assert {e["event"] for e in events} == {
        "stage.start", "stage.done", "read.part", "propose.round", "model.call",
    }


@needs_adk
def test_events_carry_no_payload(tmp_path):
    """No event may carry board text, model output or datasheet text."""
    model = _scripted_pipeline_model()
    events = []
    generate_pcb_adk(model, INTENT, datasheets=SHEETS,
                     output=tmp_path / "board.kicad_pcb",
                     time_limit_s=15.0, on_event=events.append)

    assert [e for e in events if e["event"] == "model.response"] == [], (
        "raw model output is opt-in: a default stream carries none of it"
    )
    for event in events:
        assert "kicad_pcb" not in event
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 500)


@needs_adk
def test_response_events_carry_each_answer_verbatim(tmp_path):
    """The debug stream, from the other driver, mirroring the SDK suite's test.

    ``include_responses`` is threaded from the driver into ``_wire_events`` and
    observed nowhere else, so dropping it from this driver's call passed the
    whole suite before this test existed -- while the SPA, which sets
    ``debug: true`` on every streamed run, silently lost every model answer.
    """
    model = _scripted_pipeline_model()
    events = []

    generate_pcb_adk(model, INTENT, datasheets=SHEETS,
                     output=tmp_path / "board.kicad_pcb", time_limit_s=15.0,
                     on_event=events.append, include_responses=True)

    names = [e["event"] for e in events]
    calls = [i for i, name in enumerate(names) if name == "model.call"]
    assert calls, "the workflow made no model calls to report"
    assert all(names[i + 1] == "model.response" for i in calls)

    # What the scripted model returned for each prompt it was actually given,
    # so the assertion is about the wrapper rather than about this test's copy.
    answers = [
        next(r for marker, r in model.by_marker.items() if marker in call["prompt"])
        for call in model.calls
    ]
    responses = [e for e in events if e["event"] == "model.response"]
    assert [e["stage"] for e in responses] == ["read", "propose", "review"]
    assert [e["text"] for e in responses] == answers
    assert [e["chars"] for e in responses] == [len(a) for a in answers]
    assert all(e["truncated"] is False for e in responses)


@needs_adk
def test_events_report_a_repair_round(tmp_path):
    model = ScriptedModel(responses=[json.dumps(_broken_circuit()),
                                     json.dumps(GOOD_CIRCUIT),
                                     json.dumps({"findings": []})])
    events = []
    result = generate_pcb_adk(model, "a motor driver",
                              output=tmp_path / "b.kicad_pcb",
                              time_limit_s=10.0, on_event=events.append)

    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1
    assert rounds[0]["round"] == 1 and rounds[0]["errors"] > 0
    assert isinstance(rounds[0]["first_error"], str) and rounds[0]["first_error"]

    done = [e for e in events
            if e["event"] == "stage.done" and e["stage"] == "propose"]
    assert done[0]["repair_rounds"] == 1
    assert result.repair_rounds == 1


@needs_adk
def test_events_omit_stages_that_did_not_run(tmp_path):
    """A node that ran but had nothing to do still emits nothing at all."""
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    events = []
    generate_pcb_adk(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                     time_limit_s=10.0, on_event=events.append)

    assert [e for e in events if e.get("stage") == "review"] == []
    assert [e for e in events if e.get("stage") == "read"] == []
    assert len(model.calls) == 1


@needs_adk
def test_a_raising_callback_aborts_the_run(tmp_path):
    """A service aborts a run whose client disconnected by raising here.

    The exception has to leave the workflow as itself: ADK reporting it as a
    failed-node event instead would leave the service waiting on a run that
    nobody is listening to any more. It also has to leave *at once*. A runner
    that swallowed the node's exception, ran the remaining three stages and
    only then let the driver re-raise would satisfy the exception assertion
    alone while charging a whole pipeline of paid model calls to a client that
    has already hung up, so the run is pinned to the point it stopped as well.
    """
    model = _scripted_pipeline_model()
    seen = []

    def hang_up(event):
        seen.append(event)
        raise RuntimeError("client gone")

    with pytest.raises(RuntimeError, match="client gone"):
        generate_pcb_adk(model, "x", output=tmp_path / "b.kicad_pcb",
                         time_limit_s=10.0, on_event=hang_up)

    # No datasheets, so read emits nothing and the first event of the run is
    # propose's stage.start -- raised on before the stage has asked the model
    # for anything at all.
    assert [e["event"] for e in seen] == ["stage.start"]
    assert seen[0]["stage"] == "propose"
    assert model.calls == []


@needs_adk
def test_a_hang_up_survives_a_nested_event_loop(tmp_path):
    """The same abort from inside a caller's own loop, for either base class.

    A caller that already has a running event loop gets the workflow on a
    thread of its own, and whatever the callback raised has to come back
    across that join unchanged. The fallback caught ``Exception``, so a
    ``BaseException`` was dropped on the thread and the run returned normally
    with nothing in it -- a client hanging up, reported as "the ADK workflow
    finished without producing a board".
    """
    class Hangup(BaseException):
        """What a caller raises to get out, which is not an error."""

    def hang_up(event):
        raise RuntimeError("client gone")

    def bail_out(event):
        raise Hangup("client gone")

    def under_a_loop(callback):
        async def drive():
            return generate_pcb_adk(_scripted_pipeline_model(), "x",
                                    output=tmp_path / "b.kicad_pcb",
                                    time_limit_s=10.0, on_event=callback)

        return asyncio.run(drive())

    with pytest.raises(RuntimeError, match="client gone"):
        under_a_loop(hang_up)
    with pytest.raises(Hangup):
        under_a_loop(bail_out)


@needs_adk
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
    generate_pcb_adk(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                     time_limit_s=10.0, on_event=events.append)

    retries = [e for e in events if e["event"] == "model.retry"]
    assert len(retries) == 1
    assert retries[0]["provider"] == "primary" and retries[0]["stage"] == "propose"
    assert "ModelError" in retries[0]["error"]

    calls = [e for e in events if e["event"] == "model.call"]
    assert len(calls) == 1
    assert calls[0]["ok"] and calls[0]["provider"] == "backup"
    # The service reads this off the model afterwards to fill in served_by.
    assert model.last_provider == "backup"


# ---------------------------------------------------------------- results


@needs_adk
def test_both_drivers_build_the_same_board(tmp_path):
    """Two drivers, one placement: the graph must not change the answer.

    The comparison is the placement itself, part by part, not just the board
    it fits into -- two runs that shuffled every component can agree on size,
    status and counts. The solver is single-threaded by default, so it is
    reproducible to the nanometre and exact equality is the honest assertion;
    anything softer would let a real divergence through.
    """
    sdk = generate_pcb(_scripted_pipeline_model(), INTENT, datasheets=SHEETS,
                       output=tmp_path / "sdk.kicad_pcb", time_limit_s=15.0,
                       engine="sdk")
    adk = generate_pcb_adk(_scripted_pipeline_model(), INTENT, datasheets=SHEETS,
                           output=tmp_path / "adk.kicad_pcb", time_limit_s=15.0)

    assert adk.spec.part_count() == sdk.spec.part_count()
    assert adk.spec.net_count() == sdk.spec.net_count()
    assert adk.board.solver_status == sdk.board.solver_status
    assert adk.board.size_mm == sdk.board.size_mm
    assert adk.repair_rounds == sdk.repair_rounds
    assert len(adk.findings) == len(sdk.findings)
    assert len(adk.facts) == len(sdk.facts)

    def placement(result):
        return [(p.ref, p.x_nm, p.y_nm, p.rotated) for p in result.board.parts]

    assert placement(adk) == placement(sdk)
    assert adk.board.wirelength_nm == sdk.board.wirelength_nm


# ---------------------------------------------------------------- failures


@needs_adk
def test_an_upstream_outage_stays_reachable_as_a_model_error(tmp_path):
    """The 502-versus-500 taxonomy has to survive the workflow.

    A run that failed upstream is the caller's cue to retry later; one whose
    proposal never validated is not. The service tells them apart by walking
    the cause chain, so the outage has to still be somewhere in it.
    """
    class Dead:
        def generate(self, *args, **kwargs):
            raise ModelError("upstream 503")

    try:
        generate_pcb_adk(Dead(), "x", output=tmp_path / "b.kicad_pcb",
                         review=False, time_limit_s=10.0)
    except Exception as exc:
        chain = _chain(exc)
    else:
        pytest.fail("the run reported success despite an upstream outage")

    assert any(isinstance(e, ModelError) for e in chain)


@needs_adk
def test_an_implicitly_chained_outage_is_still_reachable(tmp_path):
    """The chain the caller sees has to be the one the stage raised.

    Here the outage is reachable only through ``__context__``: the model
    re-raises inside its own ``except`` block without ``from``. ADK hands a
    node's exception back from inside an except block of its own, and Python
    rewrites ``__context__`` on the way out -- which replaced the ``ModelError``
    with ADK's own wrapper and lost it, turning a Gemini outage into a 500
    where the service owed the caller a 502.
    """
    class Wrapping:
        def generate(self, *args, **kwargs):
            try:
                raise ModelError("upstream 503")
            except ModelError:
                # No ``from``: implicit chaining is the case being pinned.
                raise RuntimeError("wrapped")  # noqa: B904

    try:
        generate_pcb_adk(Wrapping(), "x", output=tmp_path / "b.kicad_pcb",
                         review=False, time_limit_s=10.0)
    except RuntimeError as exc:
        assert "wrapped" in str(exc)
        chain = _chain(exc)
    else:
        pytest.fail("the run reported success despite an upstream outage")

    assert any(isinstance(e, ModelError) for e in chain)


@needs_adk
def test_a_proposal_that_never_validates_raises_proposal_error(tmp_path):
    """Unwrapped: a caller catching ProposalError catches it on either driver."""
    model = ScriptedModel(responses=[json.dumps(_broken_circuit())] * 4)
    with pytest.raises(ProposalError, match="No valid circuit after"):
        generate_pcb_adk(model, "a motor driver", output=tmp_path / "b.kicad_pcb",
                         max_repairs=2, review=False, time_limit_s=10.0)


# ---------------------------------------------------------------- dispatch


def test_the_sdk_engine_is_selectable_by_name(tmp_path):
    """Unguarded: naming the default driver must not need the extra."""
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0, engine="sdk")
    assert len(result.board.parts) == 6


def test_the_built_in_default_is_the_sdk_driver(monkeypatch, tmp_path):
    """Unguarded: with no keyword and no variable, the straight line runs.

    Precedence is keyword, then ``SILKSCREEN_ENGINE``, then a default baked
    into ``generate_pcb`` -- and the last of those is the only one nothing
    else pins, so a change to it would otherwise land with a green suite. This
    is the test the commit that flips the default has to edit on purpose,
    which is the point of writing it: the flip becomes visible.
    """
    from silkscreen.agents import pipeline

    monkeypatch.delenv("SILKSCREEN_ENGINE", raising=False)
    straight_line = pipeline._generate_pcb_sdk
    ran = []

    def recording_sdk(*args, **kwargs):
        ran.append(1)
        return straight_line(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_generate_pcb_sdk", recording_sdk)

    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0)

    assert ran == [1]
    assert len(result.board.parts) == 6


def test_an_unknown_engine_is_a_runtime_error():
    """RuntimeError, not ValueError.

    The service answers a pipeline ValueError as a 400 carrying the raw
    message, and a bad engine name is not a client's fault -- it is a
    misconfigured deployment.
    """
    with pytest.raises(RuntimeError, match="unknown engine"):
        generate_pcb(ScriptedModel(), "x", engine="bogus")


@needs_adk
def test_the_adk_engine_is_selectable_by_name(tmp_path):
    """Everything test_full_pipeline_prompt_to_board asserts, via the dispatcher."""
    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(_scripted_pipeline_model(), INTENT, datasheets=SHEETS,
                          output=out, time_limit_s=15.0, engine="adk")
    assert result.board_path == out and out.exists()
    assert len(result.board.parts) == 6
    assert len(result.facts) == 1
    assert len(result.blockers) == 1
    assert result.repair_rounds == 0
