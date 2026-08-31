"""Tests for :mod:`silkscreen.enclosure.emit` and ``.verify``.

Tier-1, always on, fully offline. The ``.scad`` output is checked by an
**independent reader defined in this file** -- regex extraction of the
numeric literals, importing no emitter constants -- so the structural
invariants (``cavity == board + 2*clearance``, ``outer - cavity == 2*wall``,
cutout openings covering their part's courtyard interval plus margin) are
asserted against arithmetic the emitter never sees. Envelopes are built by
hand from integer literals rather than extracted from a board, for the same
reason.

One optional tier-2 smoke test invokes the ``openscad`` CLI and gates on
``shutil.which`` exactly the way ``test_spice.py`` gates on ngspice.

These tests exercise Workstream A's frozen ``ir.py``/``errors.py`` contract;
until that lands in the tree they skip via ``importorskip``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip(
    "silkscreen.enclosure.ir",
    reason="Workstream A's enclosure/ir.py is not in the tree yet",
)

from silkscreen.enclosure.board_shape import BoardEnvelope, PartExtent  # noqa: E402
from silkscreen.enclosure.emit import emit_scad  # noqa: E402
from silkscreen.enclosure.errors import (  # noqa: E402
    CavityFitError,
    CutoutError,
    EnclosureValidationError,
    WallError,
)
from silkscreen.enclosure.ir import Cutout, EnclosureSpec  # noqa: E402
from silkscreen.enclosure.verify import verify_fit  # noqa: E402

HAS_OPENSCAD = shutil.which("openscad") is not None
needs_openscad = pytest.mark.skipif(
    not HAS_OPENSCAD, reason="openscad not installed"
)


def _nm(value_mm: float) -> int:
    """Raw mm literal -> nm, independent of silkscreen.units."""
    return int(round(value_mm * 1_000_000))


# ------------------------------------------------- independent .scad reader

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*) = (-?\d+\.\d+);$", re.M)
_CUTOUT = re.compile(
    r"// cutout (\S+) ref=(\S+) face=(\S+) "
    r"open=\[(-?\d+\.\d+), (-?\d+\.\d+)\]"
    r"(?: open_y=\[(-?\d+\.\d+), (-?\d+\.\d+)\])?"
)


def read_params(scad: str) -> dict[str, float]:
    return {name: float(value) for name, value in _ASSIGN.findall(scad)}


def read_cutouts(scad: str) -> dict[str, dict]:
    out = {}
    for cid, ref, face, lo, hi, ylo, yhi in _CUTOUT.findall(scad):
        out[cid] = {
            "ref": ref,
            "face": face,
            "open": (float(lo), float(hi)),
            "open_y": (float(ylo), float(yhi)) if ylo else None,
        }
    return out


# --------------------------------------------------------------- fixtures

# Board: outline (0,0)..(40,30) KiCad mm, 1.6 mm substrate.
# J1 hugs the left edge (min X); U1 sits mid-board and is the tallest part.
BOARD = dict(x0=0.0, y0=0.0, x1=40.0, y1=30.0, thickness=1.6)
J1 = dict(x0=0.0, x1=8.0, y0=12.0, y1=18.0, h=3.2)
U1 = dict(x0=15.0, x1=25.0, y0=10.0, y1=20.0, h=3.0)
WALL, CLEARANCE, MARGIN = 2.0, 1.0, 0.5


def make_envelope(*, extra_parts=(), u1_default=False) -> BoardEnvelope:
    parts = (
        PartExtent(
            ref="J1",
            x_min_nm=_nm(J1["x0"]), y_min_nm=_nm(J1["y0"]),
            x_max_nm=_nm(J1["x1"]), y_max_nm=_nm(J1["y1"]),
            height_nm=_nm(J1["h"]), height_default=False,
        ),
        PartExtent(
            ref="U1",
            x_min_nm=_nm(U1["x0"]), y_min_nm=_nm(U1["y0"]),
            x_max_nm=_nm(U1["x1"]), y_max_nm=_nm(U1["y1"]),
            height_nm=_nm(U1["h"]), height_default=u1_default,
        ),
    ) + tuple(extra_parts)
    corners = (
        (_nm(BOARD["x0"]), _nm(BOARD["y0"])),
        (_nm(BOARD["x1"]), _nm(BOARD["y0"])),
        (_nm(BOARD["x1"]), _nm(BOARD["y1"])),
        (_nm(BOARD["x0"]), _nm(BOARD["y1"])),
    )
    return BoardEnvelope(
        outline_nm=corners,
        x_min_nm=_nm(BOARD["x0"]), y_min_nm=_nm(BOARD["y0"]),
        x_max_nm=_nm(BOARD["x1"]), y_max_nm=_nm(BOARD["y1"]),
        thickness_nm=_nm(BOARD["thickness"]),
        parts=parts,
        max_height_nm=max(p.height_nm for p in parts),
    )


def make_spec(**overrides) -> EnclosureSpec:
    kwargs = dict(
        wall_nm=_nm(WALL),
        clearance_nm=_nm(CLEARANCE),
        lid="friction",
        corner_radius_nm=0,
        cutouts=(Cutout(id="usb", ref="J1", face="left", margin_nm=_nm(MARGIN)),),
        standoffs=False,
        vents=False,
        label=None,
    )
    kwargs.update(overrides)
    return EnclosureSpec(**kwargs)


# ------------------------------------------------------ structural invariants


def test_cavity_is_board_plus_twice_clearance():
    params = read_params(emit_scad(make_spec(), make_envelope()))
    assert params["cavity_x"] == pytest.approx(
        params["board_x"] + 2 * params["clearance"], abs=1e-3
    )
    assert params["cavity_y"] == pytest.approx(
        params["board_y"] + 2 * params["clearance"], abs=1e-3
    )
    # And board dims are the outline's, per the raw literals above.
    assert params["board_x"] == pytest.approx(BOARD["x1"] - BOARD["x0"], abs=1e-3)
    assert params["board_y"] == pytest.approx(BOARD["y1"] - BOARD["y0"], abs=1e-3)


def test_outer_minus_cavity_is_twice_wall():
    params = read_params(emit_scad(make_spec(), make_envelope()))
    assert params["outer_x"] - params["cavity_x"] == pytest.approx(
        2 * params["wall"], abs=1e-3
    )
    assert params["outer_y"] - params["cavity_y"] == pytest.approx(
        2 * params["wall"], abs=1e-3
    )


def test_cavity_height_budgets_board_parts_and_clearance():
    params = read_params(emit_scad(make_spec(), make_envelope()))
    assert params["cavity_z"] == pytest.approx(
        params["board_z"] + params["parts_z"] + params["clearance"], abs=1e-3
    )
    assert params["base_z"] == pytest.approx(
        params["cavity_z"] + params["wall"], abs=1e-3
    )
    assert params["parts_z"] == pytest.approx(J1["h"], abs=1e-3)  # tallest part


def test_cutout_opening_covers_the_courtyard_interval_plus_margin():
    cutouts = read_cutouts(emit_scad(make_spec(), make_envelope()))
    usb = cutouts["usb"]
    assert usb["ref"] == "J1" and usb["face"] == "left"
    lo, hi = usb["open"]
    # A left-face opening runs along OpenSCAD Y. Inline frame math from the
    # raw literals: scad_y = wall + clearance + (board_y1 - kicad_y), so the
    # part's interval flips ends, then widens by the margin on both sides.
    expected_lo = WALL + CLEARANCE + (BOARD["y1"] - J1["y1"]) - MARGIN
    expected_hi = WALL + CLEARANCE + (BOARD["y1"] - J1["y0"]) + MARGIN
    assert lo == pytest.approx(expected_lo, abs=1e-3)
    assert hi == pytest.approx(expected_hi, abs=1e-3)
    assert hi - lo == pytest.approx((J1["y1"] - J1["y0"]) + 2 * MARGIN, abs=1e-3)


def test_top_cutout_covers_both_axes():
    spec = make_spec(
        cutouts=(Cutout(id="window", ref="U1", face="top", margin_nm=_nm(MARGIN)),)
    )
    cutouts = read_cutouts(emit_scad(spec, make_envelope()))
    window = cutouts["window"]
    assert window["face"] == "top"
    x_lo, x_hi = window["open"]
    y_lo, y_hi = window["open_y"]
    assert x_lo == pytest.approx(WALL + CLEARANCE + U1["x0"] - MARGIN, abs=1e-3)
    assert x_hi == pytest.approx(WALL + CLEARANCE + U1["x1"] + MARGIN, abs=1e-3)
    assert y_lo == pytest.approx(
        WALL + CLEARANCE + (BOARD["y1"] - U1["y1"]) - MARGIN, abs=1e-3
    )
    assert y_hi == pytest.approx(
        WALL + CLEARANCE + (BOARD["y1"] - U1["y0"]) + MARGIN, abs=1e-3
    )


# ------------------------------------------------------------- determinism


def test_emission_is_byte_stable():
    a = emit_scad(make_spec(), make_envelope())
    b = emit_scad(make_spec(), make_envelope())
    assert a == b
    fancy = make_spec(lid="screw", vents=True, standoffs=True,
                      corner_radius_nm=_nm(2.0), label="silkscreen v1")
    assert emit_scad(fancy, make_envelope()) == emit_scad(fancy, make_envelope())


def test_named_modules_and_parameter_header_are_present():
    scad = emit_scad(make_spec(), make_envelope())
    assert "module base()" in scad
    assert "module lid()" in scad
    assert "module standoffs()" in scad
    for name in ("board_x", "board_y", "wall", "clearance", "cavity_x",
                 "cavity_y", "cavity_z", "outer_x", "outer_y"):
        assert re.search(rf"^{name} = -?\d+\.\d+;$", scad, re.M), name


def test_round_trip_params_equal_the_ir_values():
    spec = make_spec()
    params = read_params(emit_scad(spec, make_envelope()))
    assert params["wall"] == pytest.approx(spec.wall_nm / 1e6, abs=1e-3)
    assert params["clearance"] == pytest.approx(spec.clearance_nm / 1e6, abs=1e-3)
    assert params["corner_radius"] == pytest.approx(
        spec.corner_radius_nm / 1e6, abs=1e-3
    )
    assert params["board_z"] == pytest.approx(BOARD["thickness"], abs=1e-3)


def test_fit_report_params_match_the_emitted_header():
    spec = make_spec()
    envelope = make_envelope()
    report = verify_fit(spec, envelope)
    params = read_params(emit_scad(spec, envelope))
    for name, value in report.params_mm.items():
        assert params[name] == pytest.approx(value, abs=1e-3), name


# ------------------------------------------------------------- style options


def test_lid_none_defines_but_never_calls_lid():
    scad = emit_scad(make_spec(lid="none", cutouts=()), make_envelope())
    assert "module lid()" in scad
    assert ") lid();" not in scad


def test_standoffs_are_called_only_when_enabled():
    on = emit_scad(make_spec(standoffs=True), make_envelope())
    off = emit_scad(make_spec(standoffs=False), make_envelope())
    assert "\n    standoffs();" in on
    assert "\n    standoffs();" not in off
    # Standoffs raise the board, so the cavity grows.
    assert read_params(on)["cavity_z"] > read_params(off)["cavity_z"]
    assert read_params(on)["standoff_h"] > 0.0


def test_label_text_is_escaped():
    scad = emit_scad(make_spec(label='rev "A" \\ test'), make_envelope())
    assert 'text("rev \\"A\\" \\\\ test"' in scad


# ------------------------------------------------------------------ verify


def test_verify_happy_path_margins_equal_clearance():
    report = verify_fit(make_spec(), make_envelope())
    assert report.margins_nm == {
        "x": _nm(CLEARANCE), "y": _nm(CLEARANCE), "z": _nm(CLEARANCE)
    }
    assert report.warnings == ()


def test_part_overhanging_the_outline_beyond_clearance_fails_signed():
    overhang = PartExtent(
        ref="J2",
        x_min_nm=_nm(-2.0), y_min_nm=_nm(5.0),  # 2 mm past the left edge
        x_max_nm=_nm(3.0), y_max_nm=_nm(9.0),
        height_nm=_nm(1.0), height_default=False,
    )
    with pytest.raises(CavityFitError) as excinfo:
        verify_fit(make_spec(), make_envelope(extra_parts=(overhang,)))
    # Signed per-axis margins: 1 mm clearance minus 2 mm overhang = -1 mm.
    assert excinfo.value.margins_nm["x"] == _nm(CLEARANCE) - _nm(2.0)
    assert excinfo.value.margins_nm["y"] == _nm(CLEARANCE)


def test_wall_below_minimum_raises_wall_error():
    with pytest.raises(WallError):
        verify_fit(make_spec(wall_nm=_nm(0.8)), make_envelope())


def test_cutout_with_absent_ref_is_a_hard_error():
    spec = make_spec(
        cutouts=(Cutout(id="ghost", ref="J9", face="left", margin_nm=0),)
    )
    with pytest.raises(CutoutError, match="J9"):
        verify_fit(spec, make_envelope())
    with pytest.raises(CutoutError, match="J9"):
        emit_scad(spec, make_envelope())


def test_cutout_with_unknown_face_is_a_hard_error():
    # Constructed directly, bypassing parse-time validation: verify must not
    # trust the IR blindly.
    spec = make_spec(
        cutouts=(Cutout(id="odd", ref="J1", face="bottom", margin_nm=0),)
    )
    with pytest.raises(CutoutError, match="bottom"):
        verify_fit(spec, make_envelope())


def test_overlapping_cutouts_on_one_face_are_rejected():
    spec = make_spec(cutouts=(
        Cutout(id="a", ref="J1", face="left", margin_nm=_nm(0.5)),
        Cutout(id="b", ref="J1", face="left", margin_nm=_nm(0.5)),
    ))
    with pytest.raises(CutoutError, match="overlap"):
        verify_fit(spec, make_envelope())


def test_same_ref_cutouts_on_different_faces_are_fine():
    spec = make_spec(cutouts=(
        Cutout(id="a", ref="J1", face="left", margin_nm=_nm(0.5)),
        Cutout(id="b", ref="J1", face="top", margin_nm=_nm(0.5)),
    ))
    verify_fit(spec, make_envelope())


def test_defaulted_height_becomes_a_warning_and_strict_promotes_it():
    envelope = make_envelope(u1_default=True)
    report = verify_fit(make_spec(), envelope)
    assert any("U1" in w and "default" in w for w in report.warnings)
    with pytest.raises(EnclosureValidationError) as excinfo:
        verify_fit(make_spec(), envelope, strict=True)
    assert any("U1" in e for e in excinfo.value.errors)


def test_tight_clearance_warns_but_passes():
    report = verify_fit(make_spec(clearance_nm=_nm(0.3)), make_envelope())
    assert any("clearance" in w for w in report.warnings)


def test_cutout_far_from_its_face_warns():
    spec = make_spec(
        cutouts=(Cutout(id="mid", ref="U1", face="left", margin_nm=0),)
    )
    report = verify_fit(spec, make_envelope())
    assert any("mid" in w and "left" in w for w in report.warnings)


# ------------------------------------------------------------ gated tier 2


@needs_openscad
def test_emitted_scad_compiles_to_a_nonempty_stl(tmp_path: Path):
    scad = emit_scad(make_spec(standoffs=True, lid="screw"), make_envelope())
    src = tmp_path / "case.scad"
    src.write_text(scad, encoding="utf-8")
    out = tmp_path / "case.stl"
    result = subprocess.run(
        ["openscad", "-o", str(out), str(src)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    # A binary STL header alone is 84 bytes; a real mesh is far larger.
    assert out.exists() and out.stat().st_size > 84
