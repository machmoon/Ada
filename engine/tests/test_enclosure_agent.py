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
from silkscreen.enclosure.errors import (
    CavityFitError,
    EmptyGeometryError,
    RenderUnavailable,
)
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
    spec, fit, rounds = propose_enclosure(model, _envelope())
    assert rounds == 0
    assert spec.lid == "friction"
    assert spec.wall_nm == mm(2.0)
    # The accepted round's receipt rides along instead of being discarded, so
    # no caller has to re-run verify_fit on a spec the loop already verified.
    assert fit.margins_nm == {"x": mm(1.0), "y": mm(1.0), "z": mm(1.0)}
    assert fit.warnings == ()


def test_defaulted_heights_do_not_burn_repair_rounds():
    """A board-derived warning is not the model's to fix (the new verify_fit
    contract): parts with defaulted heights must be accepted on round 1, the
    warning surfaced on the returned FitReport rather than raised or fed to
    the repair loop."""
    envelope = _envelope()
    defaulted = tuple(
        PartExtent(
            ref=p.ref,
            x_min_nm=p.x_min_nm, y_min_nm=p.y_min_nm,
            x_max_nm=p.x_max_nm, y_max_nm=p.y_max_nm,
            height_nm=p.height_nm, height_default=True,
        )
        for p in envelope.parts
    )
    envelope = BoardEnvelope(
        outline_nm=envelope.outline_nm,
        x_min_nm=envelope.x_min_nm, y_min_nm=envelope.y_min_nm,
        x_max_nm=envelope.x_max_nm, y_max_nm=envelope.y_max_nm,
        thickness_nm=envelope.thickness_nm,
        parts=defaulted,
        max_height_nm=envelope.max_height_nm,
    )
    # One scripted response: a second request would raise ModelError, so
    # success here proves no repair round was spent on the height warnings.
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    _spec, fit, rounds = propose_enclosure(model, envelope)
    assert rounds == 0
    assert len(model.calls) == 1
    defaulted_warnings = [w for w in fit.warnings if "height defaulted" in w]
    assert len(defaulted_warnings) == 2  # one per part, visible, not raised
    assert any("U1" in w for w in defaulted_warnings)
    assert any("J1" in w for w in defaulted_warnings)


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
    spec, _fit, rounds = propose_enclosure(model, _envelope())
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
    sent back in rigorous mode: each round runs verify_fit(strict=True)."""
    ghost = dict(
        GOOD_ENCLOSURE,
        cutouts=[{"id": "usb", "ref": "J9", "face": "left", "margin_mm": 0.5}],
    )
    model = ScriptedModel(responses=[json.dumps(ghost), json.dumps(GOOD_ENCLOSURE)])
    _spec, _fit, rounds = propose_enclosure(model, _envelope(), rigorous=True)
    assert rounds == 1
    assert "'J9'" in model.calls[1]["prompt"]
    assert "not on this board" in model.calls[1]["prompt"]


def test_strict_fit_warnings_feed_the_repair_loop():
    tight = dict(GOOD_ENCLOSURE, clearance_mm=0.3)
    model = ScriptedModel(responses=[json.dumps(tight), json.dumps(GOOD_ENCLOSURE)])
    _spec, _fit, rounds = propose_enclosure(model, _envelope(), rigorous=True)
    assert rounds == 1
    assert "the board may bind" in model.calls[1]["prompt"]


# ------------------------------------------------------------- fast vs rigorous


def _overhanging_envelope() -> BoardEnvelope:
    """The 40 x 30 board with J1 overhanging the left edge by 3 mm.

    GOOD_ENCLOSURE's 1.0 mm clearance minus a 3 mm overhang is a -2 mm x
    margin, so verify_fit raises CavityFitError on an otherwise valid spec.
    """
    base = _envelope()
    j1 = PartExtent(
        ref="J1",
        x_min_nm=-mm(3.0), y_min_nm=mm(12.0), x_max_nm=mm(8.0), y_max_nm=mm(18.0),
        height_nm=mm(3.2), height_default=False,
    )
    return BoardEnvelope(
        outline_nm=base.outline_nm,
        x_min_nm=base.x_min_nm, y_min_nm=base.y_min_nm,
        x_max_nm=base.x_max_nm, y_max_nm=base.y_max_nm,
        thickness_nm=base.thickness_nm,
        parts=(base.parts[0], j1),
        max_height_nm=base.max_height_nm,
    )


def test_fast_mode_downgrades_a_cavity_fit_failure_and_still_ships():
    """The fast default never lets verify_fit block: the spec is accepted on
    round 1, the failure rides the receipt as a warning (signed margins kept),
    and the .scad still emits -- the demo gets its artifact."""
    envelope = _overhanging_envelope()
    # One scripted response: a repair round would raise ModelError.
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    spec, fit, rounds = propose_enclosure(model, envelope)
    assert rounds == 0
    assert len(model.calls) == 1
    assert any("fit verification failed" in w for w in fit.warnings)
    assert any("does not fit the cavity" in w for w in fit.warnings)
    # The receipt stays honest: the collision keeps its sign and size.
    assert fit.margins_nm["x"] == -mm(2.0)
    scad = emit_scad(spec, envelope)
    assert "module base()" in scad


def test_rigorous_mode_blocks_on_a_cavity_fit_failure():
    """rigorous=True is exactly the old strict behaviour: a hard fit failure
    feeds the repair loop and exhausts the three-round budget."""
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good, good, good, good])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _overhanging_envelope(), rigorous=True)
    assert excinfo.value.attempts == 4  # rigorous default: 3 repairs + 1
    assert "does not fit the cavity" in str(excinfo.value)


def test_fast_mode_makes_at_most_one_repair_round():
    """Fast-mode budget: one repair round, then give up loudly. The third
    (good) response proves the loop stopped asking, not that it ran out."""
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, json.dumps(GOOD_ENCLOSURE)])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope())
    assert excinfo.value.attempts == 2
    assert len(model.calls) == 2


def test_rigorous_mode_keeps_the_three_round_budget():
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, bad, bad])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope(), rigorous=True)
    assert excinfo.value.attempts == 4


def test_fast_mode_still_repairs_spec_validation_errors():
    """Speed skips the fit gate, not the parser: an unparseable proposal is
    still sent back, within the one-round budget."""
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    spec, _fit, rounds = propose_enclosure(model, _envelope())
    assert rounds == 1
    assert spec.wall_nm == mm(2.0)


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
    # The written .scad is a first-class artifact, reported like the rest.
    assert result.enclosure.scad_path == scad_path
    assert scad_path in result.artifacts
    assert result.artifacts.index(scad_path) < result.artifacts.index(
        result.board_path
    ), "artifacts stay in the order the stages produced them"
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
    assert result.enclosure.scad_path is None
    assert result.board_path is None
    assert list(tmp_path.iterdir()) == []


def test_board_only_case_into_a_missing_directory_still_delivers_the_board(
    tmp_path, offline_pdf_fetch
):
    """Regression: ``--board-only --case`` into a not-yet-existing output
    directory used to die on the ``enclosure.scad`` write (FileNotFoundError
    escaping before ``_finish``); the run must complete and write the board,
    and ``--board-only`` promises only the routed board -- no .scad file."""
    out = tmp_path / "brand" / "new" / "board.kicad_pcb"
    assert not out.parent.exists()
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=out,
        time_limit_s=15.0,
        emit_stages=False,
        enclosure=True,
    )
    assert out.exists()
    assert result.enclosure is not None
    assert "module base()" in result.enclosure.scad  # the text still ships
    assert result.enclosure.scad_path is None
    assert not (out.parent / "enclosure.scad").exists()
    assert result.artifacts == [out]


def test_board_envelope_valueerror_degrades_to_enclosure_failed(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """A ValueError from measuring the board is the stage's own failure, not
    the run's: it must surface as enclosure.failed and the run must finish."""
    from silkscreen.agents import stages

    def broken_envelope(path):
        raise ValueError("board has no Edge.Cuts outline")

    monkeypatch.setattr(stages, "board_envelope", broken_envelope)
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
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
    assert "Edge.Cuts" in failed[0]["error"]
    # The run finished: review closed the stream and the board was written.
    assert [e["stage"] for e in events if e["event"] == "stage.done"][-1] == "review"
    assert (tmp_path / "board.kicad_pcb").exists()


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
    # Two visible rounds -- the fast default's one-repair budget -- preceded
    # the give-up: an honest fix-it loop, just a short one.
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 2
    # The run finished: review closed the stream and the board was written.
    assert [e["stage"] for e in events if e["event"] == "stage.done"][-1] == "review"
    assert (tmp_path / "board.kicad_pcb").exists()
    assert not (tmp_path / "enclosure.scad").exists()
    assert result.findings  # review still ran after the degradation


def test_enclosure_rigorous_kwarg_restores_the_strict_pipeline_loop(
    tmp_path, offline_pdf_fetch
):
    """enclosure_rigorous=True threads end to end: the old three-repair strict
    loop runs (four visible rounds) before the stage degrades."""
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),  # every round rejected
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_rigorous=True,
    )
    assert result.enclosure is None
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 4
    assert (tmp_path / "board.kicad_pcb").exists()


def test_fast_pipeline_ships_the_scad_when_the_fit_fails(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """The demo-fast default end to end: a CavityFitError from verify_fit
    neither fails the stage nor burns a repair round -- the .scad is written
    and the receipt carries the failure as a warning."""
    from silkscreen.agents import enclosure as agent_enclosure

    def colliding(spec, envelope, *, strict=False):
        raise CavityFitError(
            "board does not fit the cavity; per-axis margins "
            "{'x': '-2.000 mm'} (negative = collision)",
            {"x": -mm(2.0), "y": mm(1.0), "z": mm(1.0)},
        )

    monkeypatch.setattr(agent_enclosure, "verify_fit", colliding)
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )
    assert result.enclosure is not None
    assert "module base()" in result.enclosure.scad
    assert (tmp_path / "enclosure.scad").exists()
    assert result.enclosure.repair_rounds == 0
    assert any(
        "fit verification failed" in w for w in result.enclosure.fit.warnings
    )
    assert result.enclosure.fit.margins_nm["x"] == -mm(2.0)
    assert not any(e["event"] == "enclosure.failed" for e in events)
    assert not any(e["event"] == "enclosure.round" for e in events)
    done = next(
        e for e in events
        if e["event"] == "stage.done" and e.get("stage") == "enclosure"
    )
    assert done["repair_rounds"] == 0


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
    # Both drivers thread emit_stages into the stage and report the write.
    for result, where in ((sdk_result, "sdk"), (adk_result, "adk")):
        assert result.enclosure.scad_path == tmp_path / where / "enclosure.scad"
        assert result.enclosure.scad_path in result.artifacts


@needs_adk
def test_adk_driver_threads_the_rigorous_flag(tmp_path, offline_pdf_fetch):
    """Driver parity for the new kwarg: the ADK context field reaches the
    stage, so rigorous mode runs the same four rounds it does under the SDK
    driver (test_enclosure_rigorous_kwarg_restores_the_strict_pipeline_loop)."""
    from silkscreen.agents.adk import generate_pcb_adk

    events = []
    result = generate_pcb_adk(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_rigorous=True,
    )
    assert result.enclosure is None
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 4
    assert (tmp_path / "board.kicad_pcb").exists()


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
