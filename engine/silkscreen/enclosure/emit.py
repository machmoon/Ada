"""Deterministic OpenSCAD emission for enclosures.

``emit_scad`` turns a validated :class:`~silkscreen.enclosure.ir.EnclosureSpec`
plus a measured :class:`~silkscreen.enclosure.board_shape.BoardEnvelope` into
``.scad`` source text. Same inputs, byte-identical output: every number goes
through one formatter (:func:`_f`, fixed three decimals), cutouts are emitted
in spec order, and nothing consults the clock, the environment, or a dict
whose order is not pinned.

Nanometres cross into millimetres **only here**, via :func:`silkscreen.units.
to_mm` inside the formatter. This is also the one module where enclosure
geometry changes coordinate frame (plan decision 10): the envelope arrives in
KiCad's Y-down frame, and :func:`_sy` maps it into OpenSCAD's Y-up frame, so

* the ``front`` face (OpenSCAD ``y=0``) is the board edge at **maximum KiCad
  Y**, ``back`` the one at minimum KiCad Y;
* ``left``/``right`` are minimum/maximum KiCad X, unchanged;
* ``top`` is the lid.

The model never types a board millimetre: every dimension below is computed
from the envelope, and the spec only chooses style within bounds.
"""

from __future__ import annotations

from ..units import mm, to_mm
from .board_shape import BoardEnvelope, PartExtent, find_part
from .errors import CutoutError
from .ir import FACES, EnclosureSpec

__all__ = ["emit_scad", "scad_params", "opening_extent"]

#: Fit gap between a friction lid's lip and the cavity wall, per side.
LIP_CLEARANCE_NM: int = mm(0.2)
#: How far a friction lid's lip descends into the cavity.
LIP_HEIGHT_NM: int = mm(2.0)
#: Height of the board standoffs (and screw bosses) above the base floor.
STANDOFF_HEIGHT_NM: int = mm(2.0)
#: Standoff/boss diameter, clamped for small cavities.
STANDOFF_DIAMETER_NM: int = mm(5.0)
#: Standoff centre inset from the cavity corner, clamped for small cavities.
STANDOFF_INSET_NM: int = mm(4.0)
#: Screw lid pilot-hole diameter (self-tapping M2.5).
SCREW_HOLE_DIAMETER_NM: int = mm(2.2)
#: Overshoot used so difference() faces never coincide exactly.
EPS_NM: int = mm(0.01)


def _f(value_nm: int) -> str:
    """One nm value as OpenSCAD text. The only nm->mm crossing in emission."""
    return f"{to_mm(value_nm):.3f}"


def _dims(spec: EnclosureSpec, envelope: BoardEnvelope) -> dict[str, int]:
    """Every derived dimension, in nm. Single source of truth for emit and
    :mod:`.verify` (which redisplays them as ``params_mm``)."""
    board_x = envelope.x_max_nm - envelope.x_min_nm
    board_y = envelope.y_max_nm - envelope.y_min_nm
    standoff_h = STANDOFF_HEIGHT_NM if spec.standoffs else 0
    cavity_x = board_x + 2 * spec.clearance_nm
    cavity_y = board_y + 2 * spec.clearance_nm
    cavity_z = (
        standoff_h + envelope.thickness_nm + envelope.max_height_nm
        + spec.clearance_nm
    )
    return {
        "board_x": board_x,
        "board_y": board_y,
        "board_z": envelope.thickness_nm,
        "parts_z": envelope.max_height_nm,
        "clearance": spec.clearance_nm,
        "wall": spec.wall_nm,
        "corner_radius": spec.corner_radius_nm,
        "standoff_h": standoff_h,
        "cavity_x": cavity_x,
        "cavity_y": cavity_y,
        "cavity_z": cavity_z,
        "outer_x": cavity_x + 2 * spec.wall_nm,
        "outer_y": cavity_y + 2 * spec.wall_nm,
        "base_z": cavity_z + spec.wall_nm,
        "lid_z": spec.wall_nm,
    }


def scad_params(spec: EnclosureSpec, envelope: BoardEnvelope) -> dict[str, float]:
    """The emitted parameter header as mm floats, for display (fit receipt)."""
    dims = _dims(spec, envelope)
    return {name: round(to_mm(value), 3) for name, value in dims.items()}


def _sx(x_nm: int, spec: EnclosureSpec, envelope: BoardEnvelope) -> int:
    """KiCad absolute X -> outer-box frame X (nm)."""
    return spec.wall_nm + spec.clearance_nm + (x_nm - envelope.x_min_nm)


def _sy(y_nm: int, spec: EnclosureSpec, envelope: BoardEnvelope) -> int:
    """KiCad absolute Y (down) -> outer-box frame Y (up). The one flip."""
    return spec.wall_nm + spec.clearance_nm + (envelope.y_max_nm - y_nm)


def _resolve(cutout, envelope: BoardEnvelope) -> PartExtent:
    part = find_part(envelope, cutout.ref)
    if part is None:
        known = sorted(p.ref for p in envelope.parts)
        raise CutoutError(
            f"cutout {cutout.id!r} names ref {cutout.ref!r}, which is not on "
            f"this board. Available: {known}"
        )
    return part


def opening_extent(
    spec: EnclosureSpec, envelope: BoardEnvelope, cutout
) -> tuple[tuple[int, int], tuple[int, int]]:
    """A cutout's opening as ``((x_lo, x_hi), (y_lo, y_hi))`` in outer-frame nm.

    For a side face the through-wall axis spans that wall; the other axis is
    the part's courtyard interval widened by the cutout margin. Raises
    :class:`CutoutError` for a ref not on the board or a face outside
    ``FACES`` -- hard errors per the ``edge_refs`` convention.
    """
    if cutout.face not in FACES:
        raise CutoutError(
            f"cutout {cutout.id!r} names unknown face {cutout.face!r}; "
            f"expected one of {list(FACES)}"
        )
    part = _resolve(cutout, envelope)
    d = _dims(spec, envelope)
    x_lo = _sx(part.x_min_nm, spec, envelope) - cutout.margin_nm
    x_hi = _sx(part.x_max_nm, spec, envelope) + cutout.margin_nm
    # KiCad y_max maps to the *smaller* OpenSCAD y.
    y_lo = _sy(part.y_max_nm, spec, envelope) - cutout.margin_nm
    y_hi = _sy(part.y_min_nm, spec, envelope) + cutout.margin_nm
    if cutout.face == "left":
        return (0, spec.wall_nm), (y_lo, y_hi)
    if cutout.face == "right":
        return (d["outer_x"] - spec.wall_nm, d["outer_x"]), (y_lo, y_hi)
    if cutout.face == "front":
        return (x_lo, x_hi), (0, spec.wall_nm)
    if cutout.face == "back":
        return (x_lo, x_hi), (d["outer_y"] - spec.wall_nm, d["outer_y"])
    return (x_lo, x_hi), (y_lo, y_hi)  # top


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _rbox_module() -> list[str]:
    return [
        "module _rbox(x, y, z, r) {",
        "    if (r > 0) {",
        "        hull() {",
        "            for (px = [r, x - r], py = [r, y - r])",
        "                translate([px, py, 0]) cylinder(h = z, r = r);",
        "        }",
        "    } else {",
        "        cube([x, y, z]);",
        "    }",
        "}",
    ]


def _standoff_centres(d: dict[str, int], spec: EnclosureSpec) -> list[tuple[int, int]]:
    inset = min(STANDOFF_INSET_NM, d["cavity_x"] // 4, d["cavity_y"] // 4)
    x0 = spec.wall_nm + inset
    x1 = d["outer_x"] - spec.wall_nm - inset
    y0 = spec.wall_nm + inset
    y1 = d["outer_y"] - spec.wall_nm - inset
    return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]


def _side_cutout_lines(spec, envelope, d) -> list[str]:
    lines: list[str] = []
    z_lo = spec.wall_nm + d["standoff_h"]
    for cutout in spec.cutouts:
        (x_lo, x_hi), (y_lo, y_hi) = opening_extent(spec, envelope, cutout)
        if cutout.face == "top":
            continue
        if cutout.face in ("left", "right"):
            open_lo, open_hi = y_lo, y_hi
        else:
            open_lo, open_hi = x_lo, x_hi
        lines.append(
            f"        // cutout {cutout.id} ref={cutout.ref} face={cutout.face} "
            f"open=[{_f(open_lo)}, {_f(open_hi)}]"
        )
        lines.append(
            f"        translate([{_f(x_lo - EPS_NM)}, {_f(y_lo - EPS_NM)}, "
            f"{_f(z_lo)}])"
        )
        lines.append(
            f"            cube([{_f(x_hi - x_lo + 2 * EPS_NM)}, "
            f"{_f(y_hi - y_lo + 2 * EPS_NM)}, "
            f"{_f(d['base_z'] - z_lo + EPS_NM)}]);"
        )
    return lines


def _top_cutout_lines(spec, envelope, d) -> list[str]:
    lines: list[str] = []
    for cutout in spec.cutouts:
        if cutout.face != "top":
            continue
        (x_lo, x_hi), (y_lo, y_hi) = opening_extent(spec, envelope, cutout)
        lines.append(
            f"        // cutout {cutout.id} ref={cutout.ref} face=top "
            f"open=[{_f(x_lo)}, {_f(x_hi)}] open_y=[{_f(y_lo)}, {_f(y_hi)}]"
        )
        lines.append(
            f"        translate([{_f(x_lo)}, {_f(y_lo)}, {_f(-EPS_NM)}])"
        )
        lines.append(
            f"            cube([{_f(x_hi - x_lo)}, {_f(y_hi - y_lo)}, "
            f"{_f(d['lid_z'] + 2 * EPS_NM)}]);"
        )
    return lines


def _vent_lines(d: dict[str, int]) -> list[str]:
    slot_w = mm(1.5)
    slot_len = (d["cavity_y"] * 3) // 5
    y0 = (d["outer_y"] - slot_len) // 2
    count = 5
    pitch = d["cavity_x"] // (count + 1)
    lines = []
    for i in range(count):
        x = d["wall"] + pitch * (i + 1) - slot_w // 2
        lines.append(
            f"        translate([{_f(x)}, {_f(y0)}, {_f(-EPS_NM)}]) "
            f"cube([{_f(slot_w)}, {_f(slot_len)}, {_f(d['lid_z'] + 2 * EPS_NM)}]);"
        )
    return lines


def emit_scad(spec: EnclosureSpec, envelope: BoardEnvelope) -> str:
    """Emit the complete enclosure as OpenSCAD source. Deterministic."""
    d = _dims(spec, envelope)
    centres = _standoff_centres(d, spec)
    standoff_d = min(STANDOFF_DIAMETER_NM, 2 * min(STANDOFF_INSET_NM,
                     d["cavity_x"] // 4, d["cavity_y"] // 4))

    lines: list[str] = [
        "// silkscreen enclosure -- generated from board geometry; do not edit",
        "// units: mm; frame: OpenSCAD Y-up (front = KiCad max-Y board edge)",
        "",
        "/* [parameters] */",
    ]
    for name, value in _dims(spec, envelope).items():
        lines.append(f"{name} = {_f(value)};")
    lines += [
        f"lid = \"{spec.lid}\";",
        "",
        "$fn = 48;",
        "",
    ]

    lines += _rbox_module()
    lines.append("")

    # Standoffs: auto-placed at the cavity corners, never positioned by the
    # model (plan contract). The module is always defined so downstream tools
    # can rely on its presence; base() only calls it when enabled.
    lines.append("module standoffs() {")
    for cx, cy in centres:
        lines.append(
            f"    translate([{_f(cx)}, {_f(cy)}, {_f(spec.wall_nm)}]) "
            f"cylinder(h = {_f(STANDOFF_HEIGHT_NM)}, d = {_f(standoff_d)});"
        )
    lines += ["}", ""]

    # Base: outer shell minus cavity minus side cutouts.
    lines += [
        "module base() {",
        "    difference() {",
        f"        _rbox(outer_x, outer_y, base_z, {_f(spec.corner_radius_nm)});",
        f"        translate([{_f(spec.wall_nm)}, {_f(spec.wall_nm)}, "
        f"{_f(spec.wall_nm)}])",
        "            cube([cavity_x, cavity_y, base_z]);",
    ]
    lines += _side_cutout_lines(spec, envelope, d)
    lines.append("    }")
    if spec.standoffs:
        lines.append("    standoffs();")
    lines += ["}", ""]

    # Lid.
    lines.append("module lid() {")
    if spec.lid == "none":
        lines.append("    // lid style \"none\": nothing to print")
    else:
        lines += [
            "    difference() {",
            f"        _rbox(outer_x, outer_y, lid_z, {_f(spec.corner_radius_nm)});",
        ]
        lines += _top_cutout_lines(spec, envelope, d)
        if spec.vents:
            lines += _vent_lines(d)
        if spec.lid == "screw":
            for cx, cy in centres:
                lines.append(
                    f"        translate([{_f(cx)}, {_f(cy)}, {_f(-EPS_NM)}]) "
                    f"cylinder(h = {_f(d['lid_z'] + 2 * EPS_NM)}, "
                    f"d = {_f(SCREW_HOLE_DIAMETER_NM)});"
                )
        lines.append("    }")
        if spec.lid == "friction":
            lip_x = d["cavity_x"] - 2 * LIP_CLEARANCE_NM
            lip_y = d["cavity_y"] - 2 * LIP_CLEARANCE_NM
            off = spec.wall_nm + LIP_CLEARANCE_NM
            lines += [
                "    // friction lip, printed on top of the plate",
                f"    translate([{_f(off)}, {_f(off)}, lid_z])",
                f"        cube([{_f(lip_x)}, {_f(lip_y)}, {_f(LIP_HEIGHT_NM)}]);",
            ]
        if spec.label:
            lines += [
                f"    translate([{_f(d['outer_x'] // 2)}, "
                f"{_f(d['outer_y'] // 2)}, {_f(-EPS_NM)}])",
                "        // label is engraved by the printer's slicer as a"
                " raised outline",
                f"        linear_extrude({_f(EPS_NM * 2)}) "
                f"text(\"{_escape(spec.label)}\", size = 6, "
                "halign = \"center\", valign = \"center\");",
            ]
    lines += ["}", ""]

    lines.append("base();")
    if spec.lid != "none":
        lines.append(
            f"translate([{_f(d['outer_x'] + mm(5.0))}, 0, 0]) lid();"
        )
    lines.append("")
    return "\n".join(lines)
