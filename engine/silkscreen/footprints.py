"""Parametric KiCad footprint generation.

To emit a board you need footprints, and a footprint is not just a symbol: it
is pads at real coordinates, a courtyard, and silkscreen. The project this
replaces sidestepped that entirely -- it pasted a board a human had already
drawn -- which is why it could never produce a design it had not been handed.

These generators build IPC-7351-shaped land patterns from the package
dimensions, so a board can be emitted without a KiCad installation and without
a footprint library on disk. Everything is nanometres (KiCad internal units).

Coverage is deliberately narrow and honest: two-terminal chip passives, SOT-23,
SOT-223, SOIC, and LQFP. That is enough for the regulator/MCU/driver circuits
this pipeline generates. Anything else raises rather than guessing -- a wrong
footprint is the single most common cause of a dead first-spin board, and
silently inventing one would be worse than refusing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .units import mm

__all__ = [
    "Pad",
    "Footprint",
    "fit_courtyard",
    "silk_segments",
    "SILK_STROKE_NM",
    "SILK_PAD_CLEARANCE_NM",
    "chip_passive",
    "sot23",
    "sot223",
    "soic",
    "lqfp",
    "for_passive",
    "CHIP_SIZES",
    "UnsupportedPackage",
]


class UnsupportedPackage(ValueError):
    """No generator covers this package; refusing to invent a land pattern."""


@dataclass(frozen=True)
class Pad:
    """One SMD pad. ``x``/``y`` are relative to the footprint anchor."""

    number: str
    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int
    net: str = ""


@dataclass
class Footprint:
    """A generated land pattern, anchored at its centre."""

    name: str
    pads: list[Pad] = field(default_factory=list)
    #: Courtyard half-extents from the anchor.
    courtyard_w_nm: int = 0
    courtyard_h_nm: int = 0
    #: Body outline for silkscreen, half-extents.
    body_w_nm: int = 0
    body_h_nm: int = 0
    description: str = ""

    def pad_by_number(self, number: str) -> Pad | None:
        for pad in self.pads:
            if pad.number == number:
                return pad
        return None


#: Body size and land pattern for the common two-terminal chip packages.
#: (body_w, body_h, pad_w, pad_h, pad_centre_spacing) in mm.
CHIP_SIZES: dict[str, tuple[float, float, float, float, float]] = {
    "0402": (1.0, 0.5, 0.60, 0.65, 0.90),
    "0603": (1.6, 0.8, 0.90, 0.95, 1.55),
    "0805": (2.0, 1.25, 1.05, 1.40, 1.90),
    "1206": (3.2, 1.6, 1.15, 1.80, 3.00),
    "1210": (3.2, 2.5, 1.15, 2.70, 3.00),
}

#: Courtyard excess over the land pattern, per IPC-7351 nominal density.
_COURTYARD_EXCESS_MM = 0.25


def fit_courtyard(fp: Footprint, excess_mm: float = _COURTYARD_EXCESS_MM) -> None:
    """Size the courtyard to enclose every pad AND the body, plus excess.

    Deriving it from one representative pad is how a footprint ends up with
    pins outside its own courtyard -- which defeats the placer's clearance
    guarantee, because the placer only ever sees the courtyard.
    """
    half_w = fp.body_w_nm
    half_h = fp.body_h_nm
    for pad in fp.pads:
        half_w = max(half_w, abs(pad.x_nm) + pad.w_nm // 2)
        half_h = max(half_h, abs(pad.y_nm) + pad.h_nm // 2)
    fp.courtyard_w_nm = half_w + mm(excess_mm)
    fp.courtyard_h_nm = half_h + mm(excess_mm)


#: Pen width both emitters stroke silkscreen with. 0.15 mm because 0.12 is
#: under the minimum legend width every house in :mod:`silkscreen.fabhouse`
#: publishes -- OSH Park prints 5 mil (0.127 mm), JLCPCB and PCBWay 0.15 mm.
#: Ink under the house minimum is not refused at checkout; it is printed badly
#: or dropped, and the board arrives with reference designators missing.
SILK_STROKE_NM = mm(0.15)

#: Minimum gap between silkscreen ink and solderable copper. Ink on a pad
#: resists solder; most fabs clip it silently, so the shipped board stops
#: matching the approved artwork. 0.2 mm is the KLC/IPC convention.
SILK_PAD_CLEARANCE_NM = mm(0.2)

#: Clipped remnants shorter than this are dropped -- a speck of ink marks
#: nothing and just reads as debris on the legend.
_MIN_SILK_SEG_NM = mm(0.2)


def silk_segments(
    fp: Footprint,
    *,
    stroke_nm: int = SILK_STROKE_NM,
    clearance_nm: int = SILK_PAD_CLEARANCE_NM,
) -> list[tuple[int, int, int, int]]:
    """The body outline as strokes, clipped clear of every pad.

    Stroking the body rectangle directly puts ink on copper wherever the body
    edge meets a pad -- on a chip passive the pads sit under the body ends, on
    an LQFP the pad row starts exactly at the body edge -- so every emitted
    footprint used to overlap its own pads by half the pen width. Instead the
    four edges are cut wherever the stroked line would come within
    ``clearance_nm`` of a pad, and what survives is returned as
    ``(x0, y0, x1, y1)`` segments in footprint-local nanometres.

    Both emitters draw these same segments (KiCad s-expressions and the Gerber
    legend), transformed exactly as they transform pads, so the clipping stays
    valid in either frame. A part whose outline is swallowed entirely (an 0603
    body barely wider than its own pads) gets no outline, which is what real
    library footprints do too.
    """
    if not fp.body_w_nm or not fp.body_h_nm:
        return []
    margin = stroke_nm // 2 + clearance_nm
    bw, bh = fp.body_w_nm, fp.body_h_nm
    # (fixed axis, fixed coordinate, span start, span end)
    edges = [
        ("y", -bh, -bw, bw),  # top
        ("y", bh, -bw, bw),  # bottom
        ("x", -bw, -bh, bh),  # left
        ("x", bw, -bh, bh),  # right
    ]
    out: list[tuple[int, int, int, int]] = []
    for axis, fixed, lo, hi in edges:
        spans = [(lo, hi)]
        for pad in fp.pads:
            if axis == "y":
                near = abs(fixed - pad.y_nm) <= pad.h_nm // 2 + margin
                cut = (pad.x_nm - pad.w_nm // 2 - margin,
                       pad.x_nm + pad.w_nm // 2 + margin)
            else:
                near = abs(fixed - pad.x_nm) <= pad.w_nm // 2 + margin
                cut = (pad.y_nm - pad.h_nm // 2 - margin,
                       pad.y_nm + pad.h_nm // 2 + margin)
            if not near:
                continue
            spans = [
                piece
                for a, b in spans
                for piece in ((a, min(b, cut[0])), (max(a, cut[1]), b))
                if piece[1] > piece[0]
            ]
        for a, b in spans:
            if b - a < _MIN_SILK_SEG_NM:
                continue
            if axis == "y":
                out.append((a, fixed, b, fixed))
            else:
                out.append((fixed, a, fixed, b))
    return out


def chip_passive(size: str = "0603", *, net1: str = "", net2: str = "") -> Footprint:
    """Two-terminal chip package (resistor, capacitor, inductor, diode)."""
    if size not in CHIP_SIZES:
        raise UnsupportedPackage(
            f"Unknown chip size {size!r}. Known: {sorted(CHIP_SIZES)}"
        )
    body_w, body_h, pad_w, pad_h, spacing = CHIP_SIZES[size]
    half = mm(spacing) // 2
    pads = [
        Pad("1", -half, 0, mm(pad_w), mm(pad_h), net1),
        Pad("2", half, 0, mm(pad_w), mm(pad_h), net2),
    ]
    fp = Footprint(
        name=f"C_{size}",
        pads=pads,
        body_w_nm=mm(body_w) // 2,
        body_h_nm=mm(body_h) // 2,
        description=f"{size} chip package",
    )
    fit_courtyard(fp)
    return fp


def sot23(nets: dict[str, str] | None = None) -> Footprint:
    """SOT-23-3. Pins 1,2 on the left, pin 3 on the right."""
    nets = nets or {}
    pw, ph = mm(1.0), mm(0.6)
    col = mm(1.0)
    row = mm(0.95)
    pads = [
        Pad("1", -col, row, pw, ph, nets.get("1", "")),
        Pad("2", -col, -row, pw, ph, nets.get("2", "")),
        Pad("3", col, 0, pw, ph, nets.get("3", "")),
    ]
    fp = Footprint(
        name="SOT-23",
        pads=pads,
        body_w_nm=mm(1.3) // 2,
        body_h_nm=mm(2.9) // 2,
        description="SOT-23-3",
    )
    fit_courtyard(fp)
    return fp


def sot223(nets: dict[str, str] | None = None) -> Footprint:
    """SOT-223-3 with the tab as pin 4 (tied to pin 2 in most regulators)."""
    nets = nets or {}
    pw, ph = mm(1.2), mm(2.2)
    pitch = mm(2.3)
    col = mm(3.2)
    pads = [
        Pad("1", -col, pitch, pw, ph, nets.get("1", "")),
        Pad("2", -col, 0, pw, ph, nets.get("2", "")),
        Pad("3", -col, -pitch, pw, ph, nets.get("3", "")),
        Pad("4", col, 0, mm(3.4), mm(6.2), nets.get("4", nets.get("2", ""))),
    ]
    fp = Footprint(
        name="SOT-223-3_TabPin2",
        pads=pads,
        body_w_nm=mm(6.5) // 2,
        body_h_nm=mm(3.5) // 2,
        description="SOT-223-3, tab on pin 4",
    )
    fit_courtyard(fp)
    return fp


def soic(pin_count: int, *, pitch_mm: float = 1.27, body_w_mm: float = 3.9,
         nets: dict[str, str] | None = None) -> Footprint:
    """SOIC / SO dual-row package. Pin 1 top-left, counting anticlockwise."""
    if pin_count < 4 or pin_count % 2:
        raise UnsupportedPackage(f"SOIC needs an even pin count >= 4, got {pin_count}")
    nets = nets or {}
    per_side = pin_count // 2
    pw, ph = mm(1.95), mm(0.6)
    col = mm(body_w_mm / 2 + 0.9)
    span = (per_side - 1) * mm(pitch_mm)
    pads: list[Pad] = []
    for i in range(per_side):
        y = span // 2 - i * mm(pitch_mm)
        pads.append(Pad(str(i + 1), -col, y, pw, ph, nets.get(str(i + 1), "")))
    for i in range(per_side):
        y = -span // 2 + i * mm(pitch_mm)
        n = str(per_side + i + 1)
        pads.append(Pad(n, col, y, pw, ph, nets.get(n, "")))
    fp = Footprint(
        name=f"SOIC-{pin_count}",
        pads=pads,
        body_w_nm=mm(body_w_mm) // 2,
        body_h_nm=span // 2 + mm(0.6),
        description=f"SOIC-{pin_count}, {pitch_mm}mm pitch",
    )
    fit_courtyard(fp)
    return fp


def lqfp(pin_count: int, *, pitch_mm: float = 0.5, body_mm: float = 7.0,
         nets: dict[str, str] | None = None) -> Footprint:
    """LQFP quad package. Pin 1 top-left, counting anticlockwise."""
    if pin_count < 16 or pin_count % 4:
        raise UnsupportedPackage(
            f"LQFP needs a pin count >= 16 divisible by 4, got {pin_count}"
        )
    nets = nets or {}
    per_side = pin_count // 4
    pw, ph = mm(1.5), mm(0.3)
    offset = mm(body_mm / 2 + 0.75)
    span = (per_side - 1) * mm(pitch_mm)
    pads: list[Pad] = []
    n = 1

    def net(i: int) -> str:
        return nets.get(str(i), "")

    for i in range(per_side):  # left, top to bottom
        pads.append(Pad(str(n), -offset, span // 2 - i * mm(pitch_mm), pw, ph, net(n)))
        n += 1
    for i in range(per_side):  # bottom, left to right
        pads.append(Pad(str(n), -span // 2 + i * mm(pitch_mm), -offset, ph, pw, net(n)))
        n += 1
    for i in range(per_side):  # right, bottom to top
        pads.append(Pad(str(n), offset, -span // 2 + i * mm(pitch_mm), pw, ph, net(n)))
        n += 1
    for i in range(per_side):  # top, right to left
        pads.append(Pad(str(n), span // 2 - i * mm(pitch_mm), offset, ph, pw, net(n)))
        n += 1

    fp = Footprint(
        name=f"LQFP-{pin_count}",
        pads=pads,
        body_w_nm=mm(body_mm) // 2,
        body_h_nm=mm(body_mm) // 2,
        description=f"LQFP-{pin_count}, {pitch_mm}mm pitch, {body_mm}mm body",
    )
    fit_courtyard(fp)
    return fp


#: Default chip size per passive type. Electrolytics and power inductors want
#: something bigger than an 0603, so they are not all the same.
_PASSIVE_DEFAULT_SIZE = {
    "resistor": "0603",
    "capacitor": "0603",
    "inductor": "0805",
    "diode": "0603",
    "crystal": "1210",
}


def for_passive(passive_type: str, value: str = "", *, net1: str = "",
                net2: str = "") -> Footprint:
    """Choose a chip package for a passive, widening it for large values.

    A 22uF part does not fit an 0603, and silently emitting one would produce a
    board whose parts do not physically exist in that size.
    """
    size = _PASSIVE_DEFAULT_SIZE.get(passive_type)
    if size is None:
        raise UnsupportedPackage(
            f"No footprint rule for passive type {passive_type!r}"
        )
    if passive_type == "capacitor":
        farads = _parse_capacitance(value)
        if farads is not None:
            if farads >= 10e-6:
                size = "1206"
            elif farads >= 1e-6:
                size = "0805"
    fp = chip_passive(size, net1=net1, net2=net2)
    fp.name = f"{passive_type[:1].upper()}_{size}"
    return fp


_CAP_UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}


def _parse_capacitance(value: str) -> float | None:
    """'100nF' -> 1e-7. Returns None when the value is not parseable."""
    if not value:
        return None
    text = value.strip().lower().replace("f", "")
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([pnuµm]?)$", text)
    if not match:
        return None
    number, unit = match.groups()
    try:
        return float(number) * _CAP_UNITS.get(unit, 1.0)
    except ValueError:
        return None


