"""The additive ``enclosure`` request opt-in and response key (workstream D).

Workstream C's ``generate_pcb`` kwargs land concurrently, so these tests stub
``generate_pcb`` at the service boundary (the existing convention): the stub
strips the enclosure kwargs, runs the real pipeline with the scripted model,
and reattaches an ``enclosure`` result built from the real ``FitReport`` type.
The response shape asserted here is the frozen contract in docs/ai-cad-plan.md,
field-for-field what frontend/src/lib/enclosure.js reads.
"""

import json
import threading
from types import SimpleNamespace

import pytest
from silkscreen.enclosure.verify import FitReport
from silkscreen.units import mm

from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import post, post_stream, scripted


@pytest.fixture
def server():
    """The scripted-model server, same wiring as test_app's fixture.

    Defined locally rather than imported: ruff reads an imported fixture as an
    unused name that every test then shadows (F811).
    """
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None

REQUEST = {"intent": "a regulator", "time_limit_s": 5}

SCAD = "// silkscreen enclosure\nmodule base() {}\nmodule lid() {}\nbase();\n"

FIT = FitReport(
    margins_nm={"x": mm(1.0), "y": mm(1.0), "z": mm(0.55)},
    warnings=("U1 height defaulted to 3.0 mm (no table entry)",),
    params_mm={"board_x": 48.2, "wall": 2.0, "clearance": 1.0},
)


class _WithEnclosure:
    """A PipelineResult plus the enclosure attribute C's contract adds."""

    def __init__(self, result, enclosure):
        self._result = result
        self.enclosure = enclosure

    def __getattr__(self, name):
        return getattr(self._result, name)


def _stub_pipeline(monkeypatch, enclosure, seen=None, events=()):
    """Run the real pipeline minus the enclosure kwargs, then reattach."""
    import service.app as app

    real = app.generate_pcb

    def fake(model, intent, **kw):
        kw.pop("enclosure", None)
        kw.pop("enclosure_style", None)
        if seen is not None:
            seen.update(kw)
        on_event = kw.get("on_event")
        if on_event is not None:
            for event in events:
                on_event(dict(event))
        return _WithEnclosure(real(model, intent, **kw), enclosure)

    monkeypatch.setattr(app, "generate_pcb", fake)
    return fake


def _success_enclosure():
    return SimpleNamespace(
        spec=None, scad=SCAD, fit=FIT, repair_rounds=1, rendered=False
    )


def test_enclosure_block_matches_the_frozen_shape(monkeypatch, server):
    _stub_pipeline(monkeypatch, _success_enclosure())
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "usb left"}
    )
    assert status == 200
    enclosure = body["enclosure"]
    # Exactly the five contract keys -- the one-shot response must never grow
    # raw model output, and an extra key here would be where it leaked.
    assert set(enclosure) == {"scad", "params", "fit", "warnings", "repair_rounds"}
    assert enclosure["scad"] == SCAD
    assert enclosure["params"] == {"board_x": 48.2, "wall": 2.0, "clearance": 1.0}
    assert enclosure["fit"] == {"margins_mm": {"x": 1.0, "y": 1.0, "z": 0.55}}
    assert enclosure["warnings"] == [
        "U1 height defaulted to 3.0 mm (no table entry)"
    ]
    assert enclosure["repair_rounds"] == 1


def test_enclosure_kwargs_reach_the_pipeline(monkeypatch, server):
    import service.app as app

    real = app.generate_pcb
    seen = {}

    def fake(model, intent, **kw):
        seen["enclosure"] = kw.pop("enclosure", None)
        seen["enclosure_style"] = kw.pop("enclosure_style", None)
        return _WithEnclosure(real(model, intent, **kw), _success_enclosure())

    monkeypatch.setattr(app, "generate_pcb", fake)
    status, _ = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "  usb left  "}
    )
    assert status == 200
    assert seen["enclosure"] is True
    assert seen["enclosure_style"] == "usb left"


def test_enclosure_failure_degrades_to_null_plus_warning(monkeypatch, server):
    """Decision 5: an exhausted repair budget never fails the run."""
    _stub_pipeline(monkeypatch, None)
    status, body = post(server, {**REQUEST, "enclosure": True})
    assert status == 200
    assert body["enclosure"] is None
    assert any("enclosure" in w for w in body["warnings"])
    # The board itself is still the product.
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_without_opt_in_the_response_is_unchanged(monkeypatch, server):
    seen = {}
    import service.app as app

    real = app.generate_pcb

    def spy(model, intent, **kw):
        seen.update(kw)
        return real(model, intent, **kw)

    monkeypatch.setattr(app, "generate_pcb", spy)
    status, body = post(server, dict(REQUEST))
    assert status == 200
    assert "enclosure" not in body
    assert "enclosure" not in seen
    assert "enclosure_style" not in seen


def test_enclosure_must_be_a_boolean(monkeypatch, server):
    import service.app as app

    def untouched(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("a field-level 400 must not run the pipeline")

    monkeypatch.setattr(app, "generate_pcb", untouched)
    status, body = post(server, {**REQUEST, "enclosure": "yes"})
    assert status == 400
    assert "'enclosure'" in body["error"]


def test_enclosure_style_must_be_a_string(monkeypatch, server):
    import service.app as app

    monkeypatch.setattr(app, "generate_pcb", lambda *a, **kw: 1 / 0)
    status, body = post(server, {**REQUEST, "enclosure": True, "enclosure_style": 7})
    assert status == 400
    assert "'enclosure_style'" in body["error"]


def test_enclosure_style_length_is_capped(monkeypatch, server):
    # The stub wraps the real pipeline first, so the at-limit request below
    # can succeed through it after the over-limit one is refused.
    _stub_pipeline(monkeypatch, None)
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "x" * 501}
    )
    assert status == 400
    assert "500" in body["error"]
    # Exactly at the limit is fine (and reaches the pipeline).
    status, _ = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "x" * 500}
    )
    assert status == 200


ENCLOSURE_EVENTS = (
    {"event": "stage.start", "stage": "enclosure"},
    {
        "event": "enclosure.round",
        "round": 1,
        "errors": 2,
        "first_error": "'wall_mm' is 0.4 mm, below the printable FDM minimum",
    },
    {
        "event": "stage.done",
        "stage": "enclosure",
        "cutouts": 1,
        "lid": "friction",
        "wall_mm": 2.0,
        "repair_rounds": 1,
        "rendered": False,
    },
)


def test_stream_forwards_enclosure_events_and_result(monkeypatch, server):
    _stub_pipeline(monkeypatch, _success_enclosure(), events=ENCLOSURE_EVENTS)
    status, _, frames = post_stream(
        server, {**REQUEST, "enclosure": True}, path="/generate/stream"
    )
    assert status == 200
    by_event = [f["event"] for f in frames]
    assert "enclosure.round" in by_event
    starts = [
        f for f in frames
        if f["event"] == "stage.start" and f.get("stage") == "enclosure"
    ]
    assert len(starts) == 1
    done = next(
        f for f in frames
        if f["event"] == "stage.done" and f.get("stage") == "enclosure"
    )
    assert done["lid"] == "friction"
    assert done["rendered"] is False
    assert frames[-1]["event"] == "run.done"
    assert frames[-1]["result"]["enclosure"]["scad"] == SCAD


def test_stream_validation_failure_is_a_400_frame(monkeypatch, server):
    """The shared taxonomy: the stream reports the same field-level 400."""
    status, _, frames = post_stream(
        server,
        {**REQUEST, "enclosure": "yes"},
        path="/generate/stream",
    )
    assert status == 200, "headers are already sent; failure lives in the frames"
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 400
    assert "'enclosure'" in frames[-1]["error"]


def test_real_pipeline_end_to_end(monkeypatch, server):
    """No stub: the request runs C's actual enclosure stage.

    The scripted model answers the frozen ``ENCLOSURE-SPEC v1`` marker with a
    valid spec, so this covers the whole real path -- request opt-in through
    ``generate_pcb(enclosure=True)`` to the stage's measured envelope, the
    verifier's receipt, and this service's response block.
    """
    import json as _json

    from silkscreen.agents.model import ScriptedModel

    from service.tests.test_app import CIRCUIT, DATASHEET, REVIEW

    def enclosure_scripted():
        return ScriptedModel(
            by_marker={
                "designing a printed circuit board": _json.dumps(CIRCUIT),
                "reviewing a circuit someone else designed": _json.dumps(REVIEW),
                "reading an electronic component datasheet": _json.dumps(
                    DATASHEET
                ),
                "ENCLOSURE-SPEC v1": _json.dumps(
                    {"wall_mm": 2.0, "lid": "friction", "cutouts": []}
                ),
            }
        )

    monkeypatch.setattr(
        Handler, "model_factory", staticmethod(enclosure_scripted)
    )
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "rounded"}
    )
    assert status == 200
    enclosure = body["enclosure"]
    assert enclosure is not None
    assert "module base()" in enclosure["scad"]
    assert set(enclosure["fit"]["margins_mm"]) == {"x", "y", "z"}
    assert all(
        isinstance(v, (int, float)) for v in enclosure["fit"]["margins_mm"].values()
    )
    assert enclosure["params"]["wall"] == 2.0
    assert enclosure["repair_rounds"] == 0
    # The board is untouched by the addition.
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_success_response_is_strict_json(monkeypatch, server):
    """The whole response, enclosure included, survives strict serialisation."""
    _stub_pipeline(monkeypatch, _success_enclosure())
    status, body = post(server, {**REQUEST, "enclosure": True})
    assert status == 200
    json.dumps(body, allow_nan=False)
