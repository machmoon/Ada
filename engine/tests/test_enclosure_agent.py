"""Workstream C: the enclosure agent loop, stage wiring, and render gate.

ScriptedModel-driven and offline throughout, per the seam in
:mod:`silkscreen.agents.model`. The only tests that touch an OpenSCAD binary
are gated on it being installed, copying ``test_spice.py``'s convention
exactly (``HAS_OPENSCAD``, ``needs_openscad``), so the default suite stays
offline and dependency-free.
"""

from __future__ import annotations

import json
import struct

import pytest
from silkscreen.agents import ModelError, ScriptedModel, generate_pcb
from silkscreen.agents.enclosure import (
    ENCLOSURE_PROMPT,
    EnclosureProposalError,
    propose_enclosure,
)
from silkscreen.enclosure import render
from silkscreen.enclosure.board_shape import BoardEnvelope, PartExtent
from silkscreen.enclosure.emit import emit_scad
from silkscreen.enclosure.errors import EmptyGeometryError, RenderUnavailable
from silkscreen.enclosure.ir import parse_enclosure_spec
from silkscreen.units import mm

# The circuits and the pipeline model are the SDK suite's own, imported rather
# than copied so the enclosure stage is provably driven through the same
# pipeline bytes every other stage test uses.
from test_agents import _scripted_pipeline_model

# ---------------------------------------------------------------- fixtures


# These tests drive the pipeline through the SDK driver; the ADK parity test
# at the bottom exercises the other one explicitly.
@pytest.fixture(autouse=True)
def _pin_sdk_engine(monkeypatch):
    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")


HAS_OPENSCAD = render.available()
needs_openscad = pytest.mark.skipif(
    not HAS_OPENSCAD, reason="openscad is not installed on this machine"
)

INTENT = "a 3.3V motor driver board"
SHEETS = {"AMS1117-3.3": "https://x/ams1117.pdf"}

GOOD_ENCLOSURE = {
    "wall_mm": 2.0,
    "clearance_mm": 1.0,
    "corner_radius_mm": 2.0,
    "lid": "friction",
    "cutouts": [],
    "standoffs": True,
    "vents": False,
    "label": "silkscreen",
}

#: A proposal that parses but cannot print: two independent problems, so the
#: repair-prompt tests can assert the batch arrives whole.
BAD_ENCLOSURE = {
    "wall_mm": 0.5,
    "lid": "zip",
    "cutouts": [],
    "standoffs": True,
    "vents": False,
}


def _envelope() -> BoardEnvelope:
    """A 40 x 30 mm board with two parts of known (non-defaulted) height.

    Built directly rather than via ``board_envelope`` so the agent tests need
    no board file: the loop's contract is against the envelope dataclass.
    ``J1`` sits against the left edge so a left-face cutout is legitimate.
    """
    u1 = PartExtent(
        ref="U1",
        x_min_nm=mm(15.0), y_min_nm=mm(10.0), x_max_nm=mm(25.0), y_max_nm=mm(20.0),
        height_nm=mm(1.75), height_default=False,
    )
    j1 = PartExtent(
        ref="J1",
        x_min_nm=mm(0.5), y_min_nm=mm(12.0), x_max_nm=mm(8.0), y_max_nm=mm(18.0),
        height_nm=mm(3.2), height_default=False,
    )
    return BoardEnvelope(
        outline_nm=((0, 0), (mm(40.0), 0), (mm(40.0), mm(30.0)), (0, mm(30.0))),
        x_min_nm=0, y_min_nm=0, x_max_nm=mm(40.0), y_max_nm=mm(30.0),
        thickness_nm=mm(1.6),
        parts=(u1, j1),
        max_height_nm=mm(3.2),
    )


# ---------------------------------------------------------------- propose


def test_propose_accepts_a_valid_spec_first_try():
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    spec, rounds = propose_enclosure(model, _envelope())
    assert rounds == 0
    assert spec.lid == "friction"
    assert spec.wall_nm == mm(2.0)


def test_prompt_carries_the_frozen_marker_and_measured_facts():
    """The model chooses style within bounds; the measured board is injected
    into the prompt deterministically, never invented by the model."""
    assert "ENCLOSURE-SPEC v1" in ENCLOSURE_PROMPT
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    propose_enclosure(model, _envelope(), style_hint="rounded corners")
    prompt = model.calls[0]["prompt"]
    assert "ENCLOSURE-SPEC v1" in prompt
    assert "40.00 x 30.00 mm" in prompt          # outline size
    assert "U1" in prompt and "J1" in prompt     # part rects by ref
    assert "3.20 mm tall" in prompt              # heights
    assert "near faces: left" in prompt          # edge-adjacent ref -> face
    assert "rounded corners" in prompt           # the style hint


def test_repair_prompt_batches_every_validation_error():
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    spec, rounds = propose_enclosure(model, _envelope())
    assert rounds == 1
    repair = model.calls[1]["prompt"]
    # Both problems in one prompt -- the batched-ValidationError convention.
    assert "below the printable FDM minimum" in repair
    assert "'lid' is 'zip'" in repair
    assert "Fix ALL of these" in repair
    # The previous proposal rides along so the model can diff itself.
    assert json.dumps(BAD_ENCLOSURE) in repair
    assert spec.wall_nm == mm(2.0)


def test_fit_failures_feed_the_repair_loop_not_just_json_shape():
    """A cutout naming a part the board lacks parses fine and must still be
    sent back: each round runs verify_fit(strict=True)."""
    ghost = dict(
        GOOD_ENCLOSURE,
        cutouts=[{"id": "usb", "ref": "J9", "face": "left", "margin_mm": 0.5}],
    )
    model = ScriptedModel(responses=[json.dumps(ghost), json.dumps(GOOD_ENCLOSURE)])
    _spec, rounds = propose_enclosure(model, _envelope())
    assert rounds == 1
    assert "'J9'" in model.calls[1]["prompt"]
    assert "not on this board" in model.calls[1]["prompt"]


def test_strict_fit_warnings_feed_the_repair_loop():
    tight = dict(GOOD_ENCLOSURE, clearance_mm=0.3)
    model = ScriptedModel(responses=[json.dumps(tight), json.dumps(GOOD_ENCLOSURE)])
    _spec, rounds = propose_enclosure(model, _envelope())
    assert rounds == 1
    assert "the board may bind" in model.calls[1]["prompt"]


def test_each_rejected_round_emits_an_enclosure_round_event():
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    events = []
    propose_enclosure(model, _envelope(), on_event=events.append)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "enclosure.round"
    assert event["round"] == 1
    assert event["errors"] == 2
    assert event["first_error"]
    assert len(event["first_error"]) <= 160


def test_budget_exhaustion_raises_carrying_the_attempt_count():
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, bad])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope(), max_repairs=2)
    assert excinfo.value.attempts == 3
    assert "below the printable FDM minimum" in str(excinfo.value)


def test_model_error_propagates_unwrapped():
    """An upstream outage is not a bad proposal; FallbackModel and the
    service's 502 both depend on ModelError leaving as itself."""
    with pytest.raises(ModelError):
        propose_enclosure(ScriptedModel(responses=[]), _envelope())


# ---------------------------------------------------------------- pipeline


def _pipeline_model(enclosure_json: str) -> ScriptedModel:
    model = _scripted_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC v1"] = enclosure_json
    return model


def test_default_run_has_no_enclosure_and_no_enclosure_events(
    tmp_path, offline_pdf_fetch
):
    """Opt-in, the route_stage pattern: off means silent, not an empty pass."""
    events = []
    result = generate_pcb(
        _scripted_pipeline_model(),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )
    assert result.enclosure is None
    assert not any("enclosure" in str(e.get("stage", "")) for e in events)
    assert not any(e["event"].startswith("enclosure") for e in events)
    assert not (tmp_path / "enclosure.scad").exists()


def test_enclosure_run_emits_the_frozen_events_and_writes_the_scad(
    tmp_path, offline_pdf_fetch
):
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_style="rounded corners",
    )

    stages = [e["stage"] for e in events if e["event"].startswith("stage.")]
    # After route, before review -- the frozen ordering.
    assert stages == [
        "read", "read", "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route",
        "enclosure", "enclosure", "review", "review",
    ]
    done = next(
        e for e in events
        if e["event"] == "stage.done" and e["stage"] == "enclosure"
    )
    assert done["cutouts"] == 0
    assert done["lid"] == "friction"
    assert done["wall_mm"] == 2.0
    assert done["repair_rounds"] == 0
    assert done["rendered"] is False

    assert result.enclosure is not None
    assert result.enclosure.rendered is False
    assert result.enclosure.repair_rounds == 0
    assert result.enclosure.fit.margins_nm == {"x": mm(1.0), "y": mm(1.0), "z": mm(1.0)}
    assert "module base()" in result.enclosure.scad
    scad_path = tmp_path / "enclosure.scad"
    assert scad_path.read_text(encoding="utf-8") == result.enclosure.scad
    # The board is still the headline artifact.
    assert (tmp_path / "board.kicad_pcb").exists()


def test_enclosure_without_output_returns_scad_but_writes_nothing(
    tmp_path, offline_pdf_fetch
):
    """The service path: no output means no files, but the text still ships."""
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        time_limit_s=15.0,
        enclosure=True,
    )
    assert result.enclosure is not None
    assert "module lid()" in result.enclosure.scad
    assert result.board_path is None
    assert list(tmp_path.iterdir()) == []


def test_enclosure_failure_degrades_and_the_board_still_ships(
    tmp_path, offline_pdf_fetch
):
    """Plan decision 5: exhausted repair budget -> enclosure None + a visible
    enclosure.failed event; the run continues, the board is the product."""
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),  # every round rejected
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )

    assert result.enclosure is None
    failed = [e for e in events if e["event"] == "enclosure.failed"]
    assert len(failed) == 1
    assert failed[0]["error"]
    assert len(failed[0]["error"]) <= 160
    # Four visible repair rounds preceded the give-up: an honest fix-it loop.
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 4
    # The run finished: review closed the stream and the board was written.
    assert [e["stage"] for e in events if e["event"] == "stage.done"][-1] == "review"
    assert (tmp_path / "board.kicad_pcb").exists()
    assert not (tmp_path / "enclosure.scad").exists()
    assert result.findings  # review still ran after the degradation


def test_enclosure_events_carry_no_payload(tmp_path, offline_pdf_fetch):
    """The stream stays a progress signal: no scad text, no model output."""
    events = []
    generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )
    for event in events:
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 500)


# ---------------------------------------------------------------- ADK parity

# The test_adk.py convention: the third-party import is the only thing the try
# covers, so internal breakage in the driver fails loudly on machines that
# have the extra, and machines without it skip stably.
try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")


@needs_adk
def test_both_drivers_emit_identical_enclosure_events(tmp_path, offline_pdf_fetch):
    """The parity contract of test_adk.py, extended to the new stage."""
    from silkscreen.agents.adk import generate_pcb_adk
    from silkscreen.agents.pipeline import _generate_pcb_sdk

    def run(driver, where):
        where.mkdir()
        events = []
        result = driver(
            _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
            INTENT,
            datasheets=SHEETS,
            output=where / "board.kicad_pcb",
            time_limit_s=15.0,
            on_event=events.append,
            enclosure=True,
            enclosure_style="rounded corners",
        )
        return result, events

    sdk_result, sdk_events = run(_generate_pcb_sdk, tmp_path / "sdk")
    adk_result, adk_events = run(generate_pcb_adk, tmp_path / "adk")

    strip = [  # identical frames; t_s is wall clock and may differ
        [{k: v for k, v in e.items() if k != "t_s"} for e in evs]
        for evs in (sdk_events, adk_events)
    ]
    assert strip[0] == strip[1]
    assert adk_result.enclosure is not None
    assert adk_result.enclosure.scad == sdk_result.enclosure.scad
    assert (
        (tmp_path / "adk" / "enclosure.scad").read_bytes()
        == (tmp_path / "sdk" / "enclosure.scad").read_bytes()
    )


# ---------------------------------------------------------------- render gate


def test_render_unavailable_names_the_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "find_openscad", lambda: None)
    assert render.available() is False
    with pytest.raises(RenderUnavailable) as excinfo:
        render.render_stl("cube([1,1,1]);", tmp_path / "out.stl")
    assert excinfo.value.executable == "openscad"
    assert "openscad" in str(excinfo.value)


def test_env_override_that_resolves_nowhere_means_unavailable(monkeypatch):
    monkeypatch.setenv(render.ENV_OPENSCAD, "/no/such/openscad-binary")
    assert render.find_openscad() is None


def test_stl_facet_count_reads_both_dialects(tmp_path):
    """The empty-mesh detector must not misread either STL flavour."""
    ascii_stl = tmp_path / "a.stl"
    ascii_stl.write_text(
        "solid x\n"
        "facet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 1 0 0\n"
        "  vertex 0 1 0\n endloop\nendfacet\nendsolid x\n"
    )
    assert render._stl_facet_count(ascii_stl) == 1

    empty_ascii = tmp_path / "e.stl"
    empty_ascii.write_text("solid empty\nendsolid empty\n")
    assert render._stl_facet_count(empty_ascii) == 0

    binary = tmp_path / "b.stl"
    binary.write_bytes(b"\0" * 80 + struct.pack("<I", 7))
    assert render._stl_facet_count(binary) == 7

    empty_binary = tmp_path / "eb.stl"
    empty_binary.write_bytes(b"\0" * 80 + struct.pack("<I", 0))
    assert render._stl_facet_count(empty_binary) == 0


@needs_openscad
def test_render_stl_produces_a_nonempty_mesh(tmp_path):
    """Tier 2: a real render of a real emitted enclosure, gated like ngspice."""
    spec = parse_enclosure_spec(json.dumps(GOOD_ENCLOSURE))
    scad = emit_scad(spec, _envelope())
    out = render.render_stl(scad, tmp_path / "enclosure.stl", timeout_s=120.0)
    assert out.exists()
    assert render._stl_facet_count(out) > 0


@needs_openscad
def test_empty_geometry_raises_never_passes_vacuously(tmp_path):
    """OpenSCAD can warn-and-emit-nothing; that must be an error, not a file."""
    with pytest.raises(EmptyGeometryError):
        render.render_stl(
            "// a module nobody calls\nmodule ghost() { cube([1,1,1]); }\n",
            tmp_path / "empty.stl",
            timeout_s=120.0,
        )
