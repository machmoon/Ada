"""Emit a complete ``.kicad_sch`` from a validated circuit.

Until this module existed the pipeline had no schematic at all. It went from a
:class:`~silkscreen.netlist.CircuitSpec` straight to placed footprints, so the
one artifact a hardware person opens first -- the drawing that says what the
circuit *is* -- was the only one never produced. A board with no schematic
cannot be reviewed by a human, cannot be re-annotated, and cannot be handed to
anyone who did not watch it being generated.

The ``CircuitSpec`` already holds exactly what a schematic needs: devices with
named pins, two-terminal passives, and nets whose endpoints are pin-level. This
module renders that as KiCad 8 s-expressions.

Symbols are **generated, not looked up**, in the same spirit as
:mod:`silkscreen.footprints`: the file carries its own ``lib_symbols`` block,
so it opens on a machine with no KiCad symbol libraries installed and cannot
pick up a different part than the one it was drawn for.

Connections are drawn as a short wire stub from each pin to a **net label**
rather than as point-to-point wires. That is ordinary KiCad practice, it is
electrically identical, and it sidesteps the one part of schematic drawing that
has no good automatic answer -- routing wires around symbols without crossing
them ambiguously. The netlist KiCad extracts from this file is the netlist the
board was built from.

**Coordinate frames.** KiCad symbol libraries are Y-up; the schematic sheet is
Y-down. This module owns that flip in exactly one place, :func:`_pin_on_sheet`,
mirroring the rule :mod:`silkscreen.kicad` follows for the board. Nothing else
here may flip a sign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .ids import stable_uuid
from .netlist import CircuitSpec, PassiveType
from .units import NM_PER_MM, mm

__all__ = [
    "SchematicResult",
    "build_schematic",
    "emit_kicad_sch",
    "emit_kicad_pro",
    "write_schematic",
    "write_project",
]

#: KiCad's schematic grid. Pins land on it or they do not connect, so every
#: coordinate this module produces is quantised to it.
GRID_NM = mm(1.27)

#: A4 landscape, KiCad's default sheet.
PAGE_W_NM = mm(297.0)
PAGE_H_NM = mm(210.0)

#: Sheet border plus room for the title block.
_MARGIN_NM = mm(12.7)

#: Length of the wire stub between a pin and its net label.
_STUB_NM = mm(2.54)

#: Gap left beside each column for the net labels attached to its pins.
_LABEL_GUTTER_NM = mm(25.4)

_PIN_PITCH_NM = mm(2.54)
_PIN_LEN_NM = mm(2.54)

#: Half-extents of the standard two-terminal passive body.
_PASSIVE_BODY_W_NM = mm(1.016)
_PASSIVE_BODY_H_NM = mm(2.54)
#: Pin connection points of a passive, measured from its anchor.
_PASSIVE_PIN_Y_NM = mm(3.81)

_ICON_LIB = "silkscreen"


def _f(nm: int) -> str:
    """Nanometres as the millimetre literal KiCad files carry."""
    return f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".") or "0"


def _snap(nm: int) -> int:
    """Quantise to the schematic grid, rounding to nearest."""
    return int(round(nm / GRID_NM)) * GRID_NM


def _esc(text: str) -> str:
    """Escape a string for an s-expression literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class SymbolPin:
    """One pin of a generated symbol, in symbol-local Y-up coordinates.

    ``x_nm``/``y_nm`` is the *connection point* -- the far end of the pin from
    the body, which is where a wire must meet it. ``angle`` is the direction
    the pin extends from that point toward the body, in KiCad's convention
    (0 right, 90 up, 180 left, 270 down).
    """

    number: str
    name: str
    x_nm: int
    y_nm: int
    angle: int

    @property
    def stub(self) -> tuple[int, int]:
        """Unit direction a wire leaves this pin, away from the body."""
        return {0: (-1, 0), 90: (0, -1), 180: (1, 0), 270: (0, 1)}[self.angle]


@dataclass
class SymbolShape:
    """A generated schematic symbol: a body outline plus pins.

    ``graphics`` holds already-rendered s-expression lines for the body, so a
    resistor rectangle and a capacitor's two plates can share everything else.
    """

    lib_id: str
    pins: list[SymbolPin]
    graphics: list[str]
    hide_pin_numbers: bool = False
    #: Half-extents used only for sheet layout, wide enough for the pins.
    extent_w_nm: int = 0
    extent_h_nm: int = 0


@dataclass
class PlacedSymbol:
    """A symbol instance on the sheet, anchored at ``x_nm``/``y_nm``."""

    ref: str
    value: str
    shape: SymbolShape
    footprint: str = ""
    #: Sheet coordinates, Y-**down**.
    x_nm: int = 0
    y_nm: int = 0
    #: ``{pin_number: net}``; a pin missing from this map is left unconnected.
    pin_nets: dict[str, str] = field(default_factory=dict)


@dataclass
class SchematicResult:
    symbols: list[PlacedSymbol]
    nets: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def refs(self) -> list[str]:
        return [s.ref for s in self.symbols]


# --------------------------------------------------------------------------
# Symbol generation
# --------------------------------------------------------------------------


def _stroke(width_nm: int = mm(0.254)) -> str:
    return f"(stroke (width {_f(width_nm)}) (type default))"


def _rect(x0: int, y0: int, x1: int, y1: int, *, fill: str = "none") -> str:
    return (
        f"(rectangle (start {_f(x0)} {_f(y0)}) (end {_f(x1)} {_f(y1)}) "
        f"{_stroke()} (fill (type {fill})))"
    )


def _polyline(points: list[tuple[int, int]], *, width_nm: int = mm(0.254)) -> str:
    pts = " ".join(f"(xy {_f(x)} {_f(y)})" for x, y in points)
    return f"(polyline (pts {pts}) {_stroke(width_nm)} (fill (type none)))"


def _passive_shape(ptype: PassiveType) -> SymbolShape:
    """A two-terminal symbol, pin 1 at the top and pin 2 at the bottom.

    The glyphs differ per type because a schematic whose capacitors look like
    resistors is worse than no schematic: it reads as correct and is not.
    """
    w, h = _PASSIVE_BODY_W_NM, _PASSIVE_BODY_H_NM
    y = _PASSIVE_PIN_Y_NM
    graphics: list[str]

    if ptype in (PassiveType.RESISTOR, PassiveType.INDUCTOR):
        graphics = [_rect(-w, -h, w, h)]
    elif ptype is PassiveType.CAPACITOR:
        plate = mm(2.54)
        gap = mm(0.508)
        graphics = [
            _polyline([(-plate, gap), (plate, gap)], width_nm=mm(0.508)),
            _polyline([(-plate, -gap), (plate, -gap)], width_nm=mm(0.508)),
        ]
    elif ptype is PassiveType.DIODE:
        s = mm(1.27)
        graphics = [
            # Anode triangle on top (pin 1), cathode bar beneath it.
            _polyline([(-s, s), (s, s), (0, -s), (-s, s)]),
            _polyline([(-s, -s), (s, -s)], width_nm=mm(0.508)),
        ]
    else:  # crystal
        plate = mm(1.27)
        graphics = [
            _rect(-mm(0.508), -mm(1.27), mm(0.508), mm(1.27)),
            _polyline([(-plate, mm(1.778)), (plate, mm(1.778))], width_nm=mm(0.508)),
            _polyline([(-plate, -mm(1.778)), (plate, -mm(1.778))], width_nm=mm(0.508)),
        ]

    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{ptype.value[:1].upper()}",
        pins=[
            SymbolPin("1", "~", 0, y, 270),
            SymbolPin("2", "~", 0, -y, 90),
        ],
        graphics=graphics,
        hide_pin_numbers=True,
        extent_w_nm=mm(2.54),
        extent_h_nm=y + _STUB_NM,
    )


#: Pin names KiCad conventionally draws on the left of an IC body regardless of
#: their number, because power and inputs entering from the right reads wrong.
_LEFT_TOKENS = ("vcc", "vdd", "vin", "vbus", "vss", "gnd", "en", "nrst", "reset")


def _device_shape(name: str, pins: dict[str, str]) -> SymbolShape:
    """A rectangular IC symbol with pins split down the two long sides.

    Ordering is by pin *number*, which is the only ordering the spec
    guarantees, with power and reset pins pulled to the left side so the symbol
    reads the way a person expects rather than the way the package numbers run.
    """
    items = sorted(pins.items(), key=lambda kv: (_pin_sort_key(kv[1]), kv[0]))
    left_names = [
        (pin_name, number)
        for pin_name, number in items
        if pin_name.lower().lstrip("+-/~").startswith(_LEFT_TOKENS)
    ]
    rest = [item for item in items if item not in left_names]
    half = (len(rest) + 1) // 2
    left = left_names + rest[:half]
    right = rest[half:]

    rows = max(len(left), len(right), 1)
    body_h = _snap((rows + 1) * _PIN_PITCH_NM // 2)
    body_w = _snap(max(mm(12.7), _widest(pins) ))
    edge_x = body_w + _PIN_LEN_NM

    symbol_pins: list[SymbolPin] = []
    for i, (pin_name, number) in enumerate(left):
        y = body_h - _PIN_PITCH_NM * (i + 1)
        symbol_pins.append(SymbolPin(str(number), pin_name, -edge_x, y, 0))
    for i, (pin_name, number) in enumerate(right):
        y = body_h - _PIN_PITCH_NM * (i + 1)
        symbol_pins.append(SymbolPin(str(number), pin_name, edge_x, y, 180))

    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        pins=symbol_pins,
        graphics=[_rect(-body_w, -body_h, body_w, body_h, fill="background")],
        extent_w_nm=edge_x + _STUB_NM,
        extent_h_nm=body_h + _PIN_PITCH_NM,
    )


def _widest(pins: dict[str, str]) -> int:
    """Body half-width that keeps the longest pin name inside the rectangle.

    0.85 mm per character at KiCad's default 1.27 mm text is a deliberate
    over-estimate; a name spilling out of its own body is the sort of thing
    that makes a generated schematic look untrustworthy.
    """
    longest = max((len(n) for n in pins), default=0)
    return mm(0.85) * longest + mm(2.54)


def _pin_sort_key(number: str) -> tuple[int, float | str]:
    """Sort pin numbers numerically when they are numbers, else by text."""
    try:
        return (0, float(number))
    except (TypeError, ValueError):
        return (1, str(number))


def _sanitise(name: str) -> str:
    """A library symbol name KiCad will accept."""
    return "".join(c if c.isalnum() or c in "._-+" else "_" for c in name) or "PART"


# --------------------------------------------------------------------------
# Sheet layout
# --------------------------------------------------------------------------


def build_schematic(
    spec: CircuitSpec, *, footprints: dict[str, str] | None = None
) -> SchematicResult:
    """Lay a validated circuit out on one A4 sheet.

    ``footprints`` maps a reference designator to the footprint name the board
    used, so the schematic's Footprint field points at the same land pattern
    that was placed. Omitting it leaves the field empty rather than guessing --
    a schematic that names a footprint the board does not use is a trap.
    """
    spec.validate()
    refs = spec.assign_refs()
    footprints = footprints or {}

    shapes: list[PlacedSymbol] = []
    for device in spec.devices:
        ref = refs[device.name]
        pin_nets_by_name = spec.nets_of(device.name)
        # The spec keys nets by pin *name*; a symbol pin is addressed by
        # number. Translating here keeps the emitter free of spec knowledge.
        by_number = {
            str(device.pins[pin_name]): net
            for pin_name, net in pin_nets_by_name.items()
            if pin_name in device.pins
        }
        shapes.append(
            PlacedSymbol(
                ref=ref,
                value=device.name,
                shape=_device_shape(device.name, device.pins),
                footprint=footprints.get(ref, ""),
                pin_nets=by_number,
            )
        )

    for passive in spec.passives:
        ref = refs[passive.name]
        shapes.append(
            PlacedSymbol(
                ref=ref,
                value=passive.value or passive.type.value,
                shape=_passive_shape(passive.type),
                footprint=footprints.get(ref, ""),
                pin_nets=spec.nets_of(passive.name),
            )
        )

    warnings = _lay_out(shapes)
    return SchematicResult(
        symbols=shapes,
        nets=[c.net for c in spec.connections],
        warnings=warnings,
    )


def _lay_out(symbols: list[PlacedSymbol]) -> list[str]:
    """Place symbols in columns, tallest first, and report an overflow.

    A single A4 sheet is what the pipeline promises; a circuit too big for one
    is reported rather than silently drawn off the page, where KiCad opens it
    to an apparently empty sheet.
    """
    warnings: list[str] = []
    x = _MARGIN_NM
    y = _MARGIN_NM
    column_w = 0
    limit_y = PAGE_H_NM - _MARGIN_NM

    for sym in symbols:
        h = sym.shape.extent_h_nm * 2 + _STUB_NM * 2
        w = sym.shape.extent_w_nm * 2 + _LABEL_GUTTER_NM
        if y + h > limit_y and column_w:
            x += column_w
            y = _MARGIN_NM
            column_w = 0
        sym.x_nm = _snap(x + sym.shape.extent_w_nm)
        sym.y_nm = _snap(y + h // 2)
        y = _snap(y + h)
        column_w = max(column_w, _snap(w))

    if symbols:
        rightmost = max(s.x_nm + s.shape.extent_w_nm for s in symbols)
        if rightmost + _LABEL_GUTTER_NM > PAGE_W_NM:
            warnings.append(
                f"{len(symbols)} symbols do not fit one A4 sheet; the schematic "
                f"extends past the page border and needs a larger paper size"
            )
    return warnings


def _pin_on_sheet(sym: PlacedSymbol, pin: SymbolPin) -> tuple[int, int]:
    """A pin's connection point in sheet coordinates.

    **The one Y flip in this module.** Symbol libraries are drawn Y-up and the
    sheet is Y-down, so a pin at symbol-local +y sits *above* the anchor on the
    sheet, at a smaller sheet y. Symbols are only ever placed at angle 0, so
    there is no rotation to compose with this.
    """
    return (sym.x_nm + pin.x_nm, sym.y_nm - pin.y_nm)


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------

_FONT = "(effects (font (size 1.27 1.27)))"


def _lib_symbol(shape: SymbolShape, *, ref_prefix: str) -> list[str]:
    """The ``lib_symbols`` entry for one generated symbol."""
    name = shape.lib_id.split(":", 1)[1]
    out = [
        f'    (symbol "{_esc(name)}"'
        + (" (pin_numbers hide)" if shape.hide_pin_numbers else ""),
        "      (pin_names (offset 0.508))",
        "      (exclude_from_sim no) (in_bom yes) (on_board yes)",
        f'      (property "Reference" "{_esc(ref_prefix)}" '
        f"(at 0 {_f(shape.extent_h_nm + mm(1.27))} 0) {_FONT})",
        f'      (property "Value" "{_esc(name)}" '
        f"(at 0 {_f(-shape.extent_h_nm - mm(1.27))} 0) {_FONT})",
        '      (property "Footprint" "" (at 0 0 0) '
        "(effects (font (size 1.27 1.27)) hide))",
        '      (property "Datasheet" "" (at 0 0 0) '
        "(effects (font (size 1.27 1.27)) hide))",
        f'      (symbol "{_esc(name)}_0_1"',
    ]
    out += [f"        {g}" for g in shape.graphics]
    out.append("      )")
    out.append(f'      (symbol "{_esc(name)}_1_1"')
    for pin in shape.pins:
        out.append(
            f"        (pin passive line "
            f"(at {_f(pin.x_nm)} {_f(pin.y_nm)} {pin.angle}) "
            f"(length {_f(_PIN_LEN_NM)})"
        )
        out.append(f'          (name "{_esc(pin.name)}" {_FONT})')
        out.append(f'          (number "{_esc(pin.number)}" {_FONT})')
        out.append("        )")
    out.append("      )")
    out.append("    )")
    return out


def emit_kicad_sch(
    result: SchematicResult, *, project_name: str = "silkscreen"
) -> str:
    """Render a :class:`SchematicResult` as KiCad 8 ``.kicad_sch`` source."""
    sheet_uuid = stable_uuid(f"sheet:{project_name}")
    out: list[str] = [
        "(kicad_sch",
        "  (version 20231120)",
        '  (generator "silkscreen")',
        '  (generator_version "8.0")',
        f'  (uuid "{sheet_uuid}")',
        '  (paper "A4")',
    ]

    # One lib_symbols entry per distinct symbol, not per instance: ten
    # decoupling capacitors share one definition, as they do in KiCad.
    out.append("  (lib_symbols")
    seen: dict[str, SymbolShape] = {}
    prefix_of: dict[str, str] = {}
    for sym in result.symbols:
        if sym.shape.lib_id not in seen:
            seen[sym.shape.lib_id] = sym.shape
            prefix_of[sym.shape.lib_id] = sym.ref.rstrip("0123456789") or "U"
    for lib_id, shape in seen.items():
        out += _lib_symbol(shape, ref_prefix=prefix_of[lib_id])
    out.append("  )")

    wires: list[str] = []
    labels: list[str] = []

    for sym in result.symbols:
        shape = sym.shape
        out.append(f'  (symbol (lib_id "{_esc(shape.lib_id)}")')
        out.append(f"    (at {_f(sym.x_nm)} {_f(sym.y_nm)} 0) (unit 1)")
        out.append("    (exclude_from_sim no) (in_bom yes) (on_board yes)")
        out.append("    (dnp no) (fields_autoplaced yes)")
        out.append(f'    (uuid "{stable_uuid("sym:" + sym.ref)}")')
        out.append(
            f'    (property "Reference" "{_esc(sym.ref)}" '
            f"(at {_f(sym.x_nm)} {_f(sym.y_nm - shape.extent_h_nm - mm(1.27))} 0) "
            f"{_FONT})"
        )
        out.append(
            f'    (property "Value" "{_esc(sym.value)}" '
            f"(at {_f(sym.x_nm)} {_f(sym.y_nm + shape.extent_h_nm + mm(1.27))} 0) "
            f"{_FONT})"
        )
        out.append(
            f'    (property "Footprint" "{_esc(sym.footprint)}" '
            f"(at {_f(sym.x_nm)} {_f(sym.y_nm)} 0) "
            f"(effects (font (size 1.27 1.27)) hide))"
        )
        for pin in shape.pins:
            out.append(
                f'    (pin "{_esc(pin.number)}" '
                f'(uuid "{stable_uuid(f"pin:{sym.ref}:{pin.number}")}"))'
            )
        out.append("    (instances")
        out.append(f'      (project "{_esc(project_name)}"')
        out.append(
            f'        (path "/{sheet_uuid}" '
            f'(reference "{_esc(sym.ref)}") (unit 1))'
        )
        out.append("      )")
        out.append("    )")
        out.append("  )")

        for pin in shape.pins:
            net = sym.pin_nets.get(pin.number, "")
            if not net:
                continue
            px, py = _pin_on_sheet(sym, pin)
            dx, dy = pin.stub
            ex, ey = px + dx * _STUB_NM, py + dy * _STUB_NM
            wires.append(
                f"  (wire (pts (xy {_f(px)} {_f(py)}) (xy {_f(ex)} {_f(ey)})) "
                f'(stroke (width 0) (type default)) '
                f'(uuid "{stable_uuid(f"w:{sym.ref}:{pin.number}")}"))'
            )
            # A label on a vertical stub is rotated so it reads bottom-up,
            # which is how KiCad orients its own vertical labels.
            angle = 0 if dy == 0 else 90
            justify = "left" if dx >= 0 else "right"
            labels.append(
                f'  (label "{_esc(net)}" (at {_f(ex)} {_f(ey)} {angle}) '
                f"(effects (font (size 1.27 1.27)) (justify {justify} bottom)) "
                f'(uuid "{stable_uuid(f"l:{sym.ref}:{pin.number}")}"))'
            )

    out += wires
    out += labels
    out.append('  (sheet_instances (path "/" (page "1")))')
    out.append(")")
    return "\n".join(out) + "\n"


def emit_kicad_pro(project_name: str = "silkscreen") -> str:
    """A minimal ``.kicad_pro`` so KiCad opens the pair as one project.

    Without it the schematic and the board are two loose files that KiCad will
    not associate, and "generate a schematic, then open the board from it" --
    the workflow this whole change exists to support -- does not work.
    """
    sheet_uuid = stable_uuid(f"sheet:{project_name}")
    return (
        "{\n"
        '  "board": {"design_settings": {"defaults": {}}},\n'
        '  "boards": [],\n'
        '  "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},\n'
        f'  "meta": {{"filename": "{project_name}.kicad_pro", "version": 1}},\n'
        '  "net_settings": {"classes": [{"name": "Default"}]},\n'
        '  "pcbnew": {"page_layout_descr_file": ""},\n'
        '  "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},\n'
        f'  "sheets": [["{sheet_uuid}", "Root"]],\n'
        '  "text_variables": {}\n'
        "}\n"
    )


def write_schematic(
    result: SchematicResult, path: str | Path, *, project_name: str | None = None
) -> Path:
    """Write the schematic to ``path`` and return it."""
    path = Path(path)
    name = project_name or path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_sch(result, project_name=name), encoding="utf-8")
    return path


def write_project(path: str | Path, *, project_name: str | None = None) -> Path:
    """Write the ``.kicad_pro`` beside the schematic and board."""
    path = Path(path)
    name = project_name or path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pro(name), encoding="utf-8")
    return path
