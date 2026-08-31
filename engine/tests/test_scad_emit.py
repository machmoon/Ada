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
from silkscreen.packing import Layer  # noqa: E402

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
    top = [p.height_nm for p in parts if p.side is Layer.TOP]
    bottom = [p.height_nm for p in parts if p.side is Layer.BOTTOM]
    return BoardEnvelope(
        outline_nm=corners,
        x_min_nm=_nm(BOARD["x0"]), y_min_nm=_nm(BOARD["y0"]),
        x_max_nm=_nm(BOARD["x1"]), y_max_nm=_nm(BOARD["y1"]),
        thickness_nm=_nm(BOARD["thickness"]),
        parts=parts,
        max_height_nm=max(top, default=0),
        max_height_bottom_nm=max(bottom, default=0),
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
    assert "module board()" in scad
    assert "module assembly()" in scad
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


# ----------------------------------------------- demo scene: board + assembly

# One coloured box: "color([...])" then a translate line then a cube line --
# the shape of every solid in the board() demo module.
_COLOR_CUBE = re.compile(
    r"color\((\[[^\]]+\])\)\s*\n\s*"
    r"translate\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\)\s*\n\s*"
    r"cube\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\);"
)


def _boxes(board: str) -> list[tuple[str, tuple[float, ...], tuple[float, ...]]]:
    return [
        (
            m.group(1),
            tuple(float(m.group(i)) for i in (2, 3, 4)),
            tuple(float(m.group(i)) for i in (5, 6, 7)),
        )
        for m in _COLOR_CUBE.finditer(board)
    ]


def test_board_slab_is_a_green_box_of_bbox_times_thickness():
    board = _board_module(emit_scad(make_spec(), make_envelope()))
    colour, pos, dims = _boxes(board)[0]
    # PCB green, with an RGB triple (no alpha: the slab is opaque).
    assert colour.count(",") == 2 and colour == "[0.000, 0.450, 0.200]"
    # Inline math from the raw literals: the slab sits wall+clearance in
    # from the outer box and directly on the floor (standoffs off).
    assert pos[0] == pytest.approx(WALL + CLEARANCE, abs=1e-3)
    assert pos[1] == pytest.approx(WALL + CLEARANCE, abs=1e-3)
    assert pos[2] == pytest.approx(WALL, abs=1e-3)
    assert dims[0] == pytest.approx(BOARD["x1"] - BOARD["x0"], abs=1e-3)
    assert dims[1] == pytest.approx(BOARD["y1"] - BOARD["y0"], abs=1e-3)
    assert dims[2] == pytest.approx(BOARD["thickness"], abs=1e-3)


def test_board_parts_sit_on_the_slab_at_courtyard_coords():
    board = _board_module(emit_scad(make_spec(), make_envelope()))
    boxes = _boxes(board)
    assert len(boxes) == 3  # slab + J1 + U1, in envelope order
    assert "// part J1 side=top" in board
    assert "// part U1 side=top" in board
    j1_colour, j1_pos, j1_dims = boxes[1]
    u1_colour, u1_pos, u1_dims = boxes[2]
    # J1 is the tallest part -> gold; U1 -> dark grey. Both opaque RGB.
    assert j1_colour != u1_colour
    assert j1_colour.count(",") == 2 and u1_colour.count(",") == 2
    slab_top = WALL + BOARD["thickness"]  # standoffs off
    # Inline frame math (same as the cutout test): x = wall + c + kicad_x,
    # y = wall + c + (board_y1 - kicad_y_max), both parts seated on the slab.
    assert j1_pos == pytest.approx(
        (WALL + CLEARANCE + J1["x0"],
         WALL + CLEARANCE + (BOARD["y1"] - J1["y1"]),
         slab_top), abs=1e-3,
    )
    assert j1_dims == pytest.approx(
        (J1["x1"] - J1["x0"], J1["y1"] - J1["y0"], J1["h"]), abs=1e-3
    )
    assert u1_pos == pytest.approx(
        (WALL + CLEARANCE + U1["x0"],
         WALL + CLEARANCE + (BOARD["y1"] - U1["y1"]),
         slab_top), abs=1e-3,
    )
    assert u1_dims == pytest.approx(
        (U1["x1"] - U1["x0"], U1["y1"] - U1["y0"], U1["h"]), abs=1e-3
    )


def test_board_bottom_part_hangs_below_the_slab():
    envelope = make_envelope(extra_parts=(_bottom_part(),))
    scad = emit_scad(make_spec(standoffs=True), envelope)
    board = _board_module(scad)
    assert "// part C9 side=bottom" in board
    boxes = _boxes(board)
    slab_pos = boxes[0][1]
    _, c9_pos, c9_dims = boxes[3]
    # Standoffs on: the slab is raised by the emitted standoff height, and
    # the bottom part's top face coincides with the slab's bottom face.
    gap = read_params(scad)["standoff_h"]
    assert slab_pos[2] == pytest.approx(WALL + gap, abs=1e-3)
    assert c9_dims[2] == pytest.approx(BOTTOM_H, abs=1e-3)
    assert c9_pos[2] + c9_dims[2] == pytest.approx(slab_pos[2], abs=1e-3)


def test_default_scene_is_assembly_with_exploded_translucent_lid():
    scad = emit_scad(make_spec(), make_envelope())
    # assembly() is the top-level default render.
    assert scad.rstrip().endswith("assembly();")
    asm = _assembly_module(scad)
    assert ") base();" in asm and "board();" in asm
    # Friction lid: translucent (RGBA colour), flipped lip-down, footprint
    # restored via a y=outer_y translate, and lifted clear of the base.
    m = re.search(
        r"color\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+), "
        r"(-?\d+\.\d+)\]\)\s*\n\s*"
        r"translate\(\[0, (-?\d+\.\d+), (-?\d+\.\d+)\]\)\s*\n\s*"
        r"rotate\(\[180, 0, 0\]\) lid\(\);",
        asm,
    )
    assert m is not None, "exploded translucent friction lid not found"
    alpha = float(m.group(4))
    assert 0.0 < alpha < 0.5  # see-through, not opaque, not invisible
    # Inline math from the raw literals: outer_y = board_y + 2c + 2w = 36,
    # base_z = w + (board 1.6 + tallest 3.2 + c) = 7.8, lift = 1.5*base_z,
    # and the flip pivot adds the whole lid solid (plate + lip, read from
    # the file itself) so the lid's lowest point sits at the lift height.
    lip_h = float(_LIP.search(_lid_module(scad)).group(5))
    base_z = WALL + BOARD["thickness"] + J1["h"] + CLEARANCE
    assert float(m.group(5)) == pytest.approx(
        (BOARD["y1"] - BOARD["y0"]) + 2 * CLEARANCE + 2 * WALL, abs=1e-3
    )
    assert float(m.group(6)) == pytest.approx(
        1.5 * base_z + WALL + lip_h, abs=1e-3
    )


def test_screw_assembly_lid_lifts_straight_up():
    scad = emit_scad(make_spec(lid="screw", standoffs=True), make_envelope())
    asm = _assembly_module(scad)
    m = re.search(
        r"color\(\[[^\]]+, (-?\d+\.\d+)\]\)\s*\n\s*"
        r"translate\(\[0, 0, (-?\d+\.\d+)\]\) lid\(\);",
        asm,
    )
    assert m is not None, "exploded translucent screw lid not found"
    assert 0.0 < float(m.group(1)) < 0.5
    # base_z now includes the standoff height (read from the header).
    base_z = read_params(scad)["base_z"]
    assert float(m.group(2)) == pytest.approx(1.5 * base_z, abs=1e-3)


def test_lid_none_assembly_omits_the_lid_but_keeps_the_board():
    scad = emit_scad(make_spec(lid="none", cutouts=()), make_envelope())
    asm = _assembly_module(scad)
    assert "board();" in asm
    assert " lid();" not in asm


# --------------------------------------------------- lid CSG: vents and label

# Single-line "translate([...]) cube([...]);" -- the vent-slot form. The lip
# and top cutouts are emitted across multiple lines, so this regex isolates
# the vents.
_ONE_LINE_CUBE = re.compile(
    r"translate\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\) "
    r"cube\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\);"
)

# The friction lip: "translate([x, y, lid_z])" followed by a cube line.
_LIP = re.compile(
    r"translate\(\[(-?\d+\.\d+), (-?\d+\.\d+), lid_z\]\)\s*\n\s*"
    r"cube\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\);"
)


def _lid_module(scad: str) -> str:
    start = scad.index("module lid() {")
    return scad[start:scad.index("\nmodule board()", start)]


def _board_module(scad: str) -> str:
    start = scad.index("module board() {")
    return scad[start:scad.index("\nmodule assembly()", start)]


def _assembly_module(scad: str) -> str:
    start = scad.index("module assembly() {")
    return scad[start:scad.index("\nassembly();", start)]


def test_friction_vents_pierce_plate_and_lip():
    scad = emit_scad(make_spec(vents=True, cutouts=()), make_envelope())
    lid = _lid_module(scad)
    # The plate and lip are unioned *inside* the difference, so the vent
    # subtractions apply to both solids.
    diff_at = lid.index("difference() {")
    union_at = lid.index("union() {")
    lip_match = _LIP.search(lid)
    assert lip_match is not None, "friction lip cube not found"
    assert diff_at < union_at < lip_match.start()
    lip_h = float(lip_match.group(5))
    # lid_z is the wall thickness; lip height is read from the file itself.
    lid_z = WALL
    vents = [m for m in _ONE_LINE_CUBE.finditer(lid)]
    assert len(vents) == 5
    for m in vents:
        # Every vent is a subtrahend (after the union closes) and spans the
        # whole lid solid: from below z=0 to above lid_z + lip.
        assert m.start() > lip_match.end()
        z0, dz = float(m.group(3)), float(m.group(6))
        assert z0 < 0.0
        assert z0 + dz > lid_z + lip_h
    # Inline slot math from the raw literals: cavity_x = board_x + 2c = 42,
    # pitch = 42/6 = 7, x_i = wall + 7(i+1) - slot_w/2 with slot_w = 1.5.
    xs = sorted(float(m.group(1)) for m in vents)
    for i, x in enumerate(xs):
        assert x == pytest.approx(WALL + 7.0 * (i + 1) - 0.75, abs=1e-3)


def test_screw_lid_vents_pierce_the_plate():
    scad = emit_scad(
        make_spec(lid="screw", standoffs=True, vents=True, cutouts=()),
        make_envelope(),
    )
    lid = _lid_module(scad)
    vents = [m for m in _ONE_LINE_CUBE.finditer(lid)]
    assert len(vents) == 5
    for m in vents:
        z0, dz = float(m.group(3)), float(m.group(6))
        assert z0 < 0.0 and z0 + dz > WALL  # lid_z == wall


def test_friction_top_cutout_pierces_the_lip():
    spec = make_spec(
        cutouts=(Cutout(id="window", ref="U1", face="top", margin_nm=_nm(MARGIN)),)
    )
    scad = emit_scad(spec, make_envelope())
    lid = _lid_module(scad)
    lip_h = float(_LIP.search(lid).group(5))
    # The cube emitted right after the window's comment must cut through
    # plate + lip, not just the plate.
    after = lid[lid.index("// cutout window"):]
    depth = float(
        re.search(r"cube\(\[.*?, .*?, (-?\d+\.\d+)\]\);", after).group(1)
    )
    assert depth > WALL + lip_h


def test_label_is_a_raised_emboss_on_the_outer_face():
    # Screw lid assembles as printed: outer face is z = lid_z, emboss on top.
    scad = emit_scad(
        make_spec(lid="screw", standoffs=True, label="KAL1"), make_envelope()
    )
    lid = _lid_module(scad)
    m = re.search(
        r"translate\(\[(-?\d+\.\d+), (-?\d+\.\d+), lid_z\]\)\s*\n\s*"
        r"linear_extrude\((-?\d+\.\d+)\) text\(\"KAL1\", size = (-?\d+\.\d+)",
        lid,
    )
    assert m is not None, "raised label on the lid_z face not found"
    # Centred on the lid: outer = board + 2*clearance + 2*wall (inline math).
    cx = (BOARD["x1"] - BOARD["x0"]) / 2 + CLEARANCE + WALL
    cy = (BOARD["y1"] - BOARD["y0"]) / 2 + CLEARANCE + WALL
    assert float(m.group(1)) == pytest.approx(cx, abs=1e-3)
    assert float(m.group(2)) == pytest.approx(cy, abs=1e-3)
    # A printable emboss, not a hairline buried in the plate.
    assert float(m.group(3)) >= 0.2
    assert float(m.group(4)) > 0.0
    # The label is unioned after the difference closes -- raised, not a hole.
    assert lid.rindex("text(") > lid.rindex("    }")


def test_friction_label_sits_on_the_flipped_outer_face_mirrored():
    # A friction lid assembles lip-down, so the model's z=0 plane is the
    # case's visible face: the emboss extrudes below z=0 and is mirrored.
    scad = emit_scad(make_spec(label="KAL2"), make_envelope())
    lid = _lid_module(scad)
    m = re.search(
        r"translate\(\[(-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)\]\)\s*\n\s*"
        r"mirror\(\[1, 0, 0\]\)\s*\n\s*"
        r"linear_extrude\((-?\d+\.\d+)\) text\(\"KAL2\"",
        lid,
    )
    assert m is not None, "mirrored raised label on the z=0 face not found"
    z0, height = float(m.group(3)), float(m.group(4))
    assert height >= 0.2
    # Extrudes exactly up to the outer face: z0 + height == 0.
    assert z0 == pytest.approx(-height, abs=1e-3)
    assert "engraved" not in scad


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


def test_defaulted_height_warns_but_strict_never_promotes_it():
    # height_default is a *board* fact: no spec edit can fix it, so promoting
    # it under strict would wedge the repair loop. It rides the report only.
    envelope = make_envelope(u1_default=True)
    report = verify_fit(make_spec(), envelope)
    assert any("U1" in w and "default" in w for w in report.warnings)
    strict_report = verify_fit(make_spec(), envelope, strict=True)
    assert any("U1" in w and "default" in w for w in strict_report.warnings)


def test_strict_promotes_only_spec_fixable_warnings():
    # Tight clearance is the model's own choice -> promoted; the defaulted
    # height on the same board is not, and must not appear in the batch.
    envelope = make_envelope(u1_default=True)
    with pytest.raises(EnclosureValidationError) as excinfo:
        verify_fit(make_spec(clearance_nm=_nm(0.3)), envelope, strict=True)
    assert any("clearance" in e for e in excinfo.value.errors)
    assert not any("default" in e for e in excinfo.value.errors)


def test_tight_clearance_warns_but_passes():
    report = verify_fit(make_spec(clearance_nm=_nm(0.3)), make_envelope())
    assert any("clearance" in w for w in report.warnings)


def test_cutout_far_from_its_face_warns():
    spec = make_spec(
        cutouts=(Cutout(id="mid", ref="U1", face="left", margin_nm=0),)
    )
    report = verify_fit(spec, make_envelope())
    assert any("mid" in w and "left" in w for w in report.warnings)


# --------------------------------------------------- bottom-side Z accounting

BOTTOM_H = 1.5  # mm, the bottom part's height in the tests below


def _bottom_part(height_mm: float = BOTTOM_H) -> PartExtent:
    return PartExtent(
        ref="C9",
        x_min_nm=_nm(30.0), y_min_nm=_nm(2.0),
        x_max_nm=_nm(32.0), y_max_nm=_nm(4.0),
        height_nm=_nm(height_mm), height_default=False,
        side=Layer.BOTTOM,
    )


def test_bottom_part_does_not_inflate_the_cavity_height():
    # A 5 mm part under the board must not grow the cavity above it: parts_z
    # stays the tallest *top* part (J1, inline literal).
    envelope = make_envelope(extra_parts=(_bottom_part(5.0),))
    params = read_params(emit_scad(make_spec(standoffs=True), envelope))
    assert params["parts_z"] == pytest.approx(J1["h"], abs=1e-3)
    assert params["cavity_z"] == pytest.approx(
        params["standoff_h"] + BOARD["thickness"] + J1["h"] + CLEARANCE,
        abs=1e-3,
    )


def test_bottom_part_taller_than_standoff_gap_fails_signed():
    # Standoffs off: the board sits on the floor, the gap is zero, and a
    # 1.5 mm bottom part collides by exactly its own height.
    envelope = make_envelope(extra_parts=(_bottom_part(),))
    with pytest.raises(CavityFitError) as excinfo:
        verify_fit(make_spec(standoffs=False), envelope)
    assert excinfo.value.margins_nm["z"] == -_nm(BOTTOM_H)
    assert excinfo.value.margins_nm["x"] == _nm(CLEARANCE)


def test_bottom_part_within_standoff_gap_passes_with_true_margin():
    envelope = make_envelope(extra_parts=(_bottom_part(),))
    spec = make_spec(standoffs=True)
    report = verify_fit(spec, envelope)
    # The gap is the emitted standoff height -- read from the .scad header by
    # the independent reader, not from an emitter constant.
    gap = read_params(emit_scad(spec, envelope))["standoff_h"]
    assert to_mm_f(report.margins_nm["z"]) == pytest.approx(
        min(CLEARANCE, gap - BOTTOM_H), abs=1e-3
    )


def to_mm_f(value_nm: int) -> float:
    return value_nm / 1_000_000


def test_top_z_margin_is_still_signed_against_the_budget():
    # A caller-supplied envelope can under-state max_height_nm; the top-side
    # check still catches it (the pre-existing over_z behaviour).
    parts = (
        PartExtent(
            ref="U1",
            x_min_nm=_nm(10.0), y_min_nm=_nm(10.0),
            x_max_nm=_nm(20.0), y_max_nm=_nm(20.0),
            height_nm=_nm(4.0), height_default=False,
        ),
    )
    envelope = BoardEnvelope(
        outline_nm=((0, 0),),
        x_min_nm=_nm(0.0), y_min_nm=_nm(0.0),
        x_max_nm=_nm(40.0), y_max_nm=_nm(30.0),
        thickness_nm=_nm(1.6),
        parts=parts,
        max_height_nm=_nm(2.0),  # understated by 2 mm
    )
    with pytest.raises(CavityFitError) as excinfo:
        verify_fit(make_spec(cutouts=()), envelope)
    assert excinfo.value.margins_nm["z"] == _nm(CLEARANCE) - _nm(2.0)


# ---------------------------------------------------------------- empty board


def _empty_envelope() -> BoardEnvelope:
    return BoardEnvelope(
        outline_nm=(
            (_nm(0.0), _nm(0.0)), (_nm(40.0), _nm(0.0)),
            (_nm(40.0), _nm(30.0)), (_nm(0.0), _nm(30.0)),
        ),
        x_min_nm=_nm(0.0), y_min_nm=_nm(0.0),
        x_max_nm=_nm(40.0), y_max_nm=_nm(30.0),
        thickness_nm=_nm(1.6),
        parts=(),
        max_height_nm=0,
    )


def test_empty_board_warns_instead_of_a_quiet_zero():
    report = verify_fit(make_spec(cutouts=()), _empty_envelope())
    assert any("no extractable parts" in w for w in report.warnings)


def test_empty_board_warning_is_never_promoted():
    # Board-derived, not spec-fixable: strict must not turn it into an error.
    report = verify_fit(make_spec(cutouts=()), _empty_envelope(), strict=True)
    assert any("no extractable parts" in w for w in report.warnings)


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
