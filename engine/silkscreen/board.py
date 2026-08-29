"""Emit a complete ``.kicad_pcb`` from a validated circuit.

This is the step the previous project never had. Its "layout engine" pasted a
board a human had already drawn, through the clipboard, into a running KiCad
window -- so it could only ever produce the one design it had been handed.

Here a :class:`~silkscreen.netlist.CircuitSpec` becomes real footprints with
real pads on real nets, placed by the CP-SAT solver, written as KiCad 8
s-expressions. No KiCad installation, no footprint library on disk, no GUI.

The output is verified by round-tripping through ``kiutils`` in the test suite:
if KiCad's own parser cannot read what we wrote, the tests fail.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .footprints import Footprint, UnsupportedPackage, for_passive, lqfp, soic, sot223
from .netlist import CircuitSpec
from .packing import Net as PackNet
from .packing import Part, Placement, pack
from .units import DEFAULT_CLEARANCE_NM, NM_PER_MM, mm

__all__ = [
    "BoardResult",
    "PlacedPart",
    "build_board",
    "emit_kicad_pcb",
    "write_board",
]

_LAYERS = """    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)"""


@dataclass
class PlacedPart:
    """A footprint with its reference designator, position and value."""

    ref: str
    footprint: Footprint
    value: str = ""
    x_nm: int = 0
    y_nm: int = 0
    rotated: bool = False


@dataclass
class BoardResult:
    parts: list[PlacedPart]
    nets: list[str]
    width_nm: int
    height_nm: int
    solver_status: str
    wirelength_nm: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.width_nm / NM_PER_MM, self.height_nm / NM_PER_MM)


def _footprint_for_device(
    name: str, pin_count: int, nets: dict[str, str]
) -> Footprint:
    """Pick a package from the pin count.

    Deliberately conservative: it covers the shapes this pipeline generates and
    raises otherwise. Guessing a land pattern is how boards come back dead.
    """
    if pin_count == 3:
        return sot223(nets)
    if pin_count in (4, 6, 8, 10, 14, 16, 20, 24, 28):
        return soic(pin_count, nets=nets)
    if pin_count in (32, 44, 48, 64, 100, 144):
        body = {32: 5.0, 44: 10.0, 48: 7.0, 64: 10.0, 100: 14.0, 144: 20.0}[pin_count]
        return lqfp(pin_count, body_mm=body, nets=nets)
    raise UnsupportedPackage(
        f"No package rule for {name!r} with {pin_count} pins. Supported: "
        f"3 (SOT-223), 4-28 even (SOIC), 32/44/48/64/100/144 (LQFP)."
    )


def _package_pin_count(pins: dict[str, str]) -> int:
    """Highest physical pin number, which is the package's pin count."""
    numbers = []
    for value in pins.values():
        try:
            numbers.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return max(numbers) if numbers else len(pins)


_POWER_TOKENS = ("gnd", "vss", "vcc", "vdd", "vbus", "vin", "vout", "agnd", "dgnd")


def _is_power(net: str) -> bool:
    lowered = net.lower().lstrip("+-/")
    return any(lowered.startswith(t) for t in _POWER_TOKENS)


def build_board(
    spec: CircuitSpec,
    *,
    clearance_nm: int = DEFAULT_CLEARANCE_NM,
    time_limit_s: float = 20.0,
    edge_refs: set[str] | None = None,
) -> BoardResult:
    """Turn a validated circuit into footprints, nets and a placement."""
    spec.validate()

    counters: dict[str, int] = {}
    ref_of: dict[str, str] = {}

    def next_ref(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    placed: list[PlacedPart] = []

    for device in spec.devices:
        pin_nets: dict[str, str] = {}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, pin_name = endpoint.rpartition(".")
                if part == device.name:
                    number = device.pins.get(pin_name)
                    if number:
                        pin_nets[str(number)] = conn.net
        fp = _footprint_for_device(
            device.name, _package_pin_count(device.pins), pin_nets
        )
        ref = next_ref("U")
        ref_of[device.name] = ref
        placed.append(PlacedPart(ref=ref, footprint=fp, value=device.name))

    for passive in spec.passives:
        legs = {"1": "", "2": ""}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, leg = endpoint.rpartition(".")
                if part == passive.name and leg in legs:
                    legs[leg] = conn.net
        fp = for_passive(
            passive.type.value, passive.value, net1=legs["1"], net2=legs["2"]
        )
        ref = next_ref(passive.ref_prefix)
        ref_of[passive.name] = ref
        placed.append(PlacedPart(ref=ref, footprint=fp, value=passive.value))

    parts = [
        Part(
            width_nm=p.footprint.courtyard_w_nm * 2,
            height_nm=p.footprint.courtyard_h_nm * 2,
            ref=p.ref,
            must_be_on_edge=p.ref in (edge_refs or set()),
        )
        for p in placed
    ]
    index_of = {p.ref: i for i, p in enumerate(placed)}

    pack_nets: list[PackNet] = []
    for conn in spec.connections:
        terminals: list[tuple[int, tuple[int, int]]] = []
        seen: set[int] = set()
        for endpoint in conn.endpoints:
            part_name, _, pin = endpoint.rpartition(".")
            ref = ref_of.get(part_name)
            if ref is None:
                continue
            idx = index_of[ref]
            if idx in seen:
                continue
            seen.add(idx)
            fp = placed[idx].footprint
            number = pin
            device = next((d for d in spec.devices if d.name == part_name), None)
            if device is not None:
                number = str(device.pins.get(pin, pin))
            pad = fp.pad_by_number(number)
            # Offsets are measured from the courtyard's bottom-left corner.
            ox = (pad.x_nm if pad else 0) + fp.courtyard_w_nm
            oy = (pad.y_nm if pad else 0) + fp.courtyard_h_nm
            terminals.append((idx, (ox, oy)))
        if len(terminals) >= 2:
            pack_nets.append(
                PackNet(
                    terminals=tuple(terminals),
                    name=conn.net,
                    weight=0.25 if _is_power(conn.net) else 1.0,
                )
            )

    result = pack(
        parts, nets=pack_nets, clearance_nm=clearance_nm, time_limit_s=time_limit_s
    )
    by_ref: dict[str, Placement] = {p.ref: p for p in result.placements}
    for part in placed:
        placement = by_ref.get(part.ref)
        if placement:
            part.x_nm = placement.x_nm
            part.y_nm = placement.y_nm
            part.rotated = placement.rotated

    return BoardResult(
        parts=placed,
        nets=[c.net for c in spec.connections],
        width_nm=result.board_width_nm,
        height_nm=result.board_height_nm,
        solver_status=result.status.value,
        wirelength_nm=result.wirelength_nm,
        warnings=list(result.warnings),
    )


def _uuid(seed: str) -> str:
    """Stable UUID from a seed, so output is byte-identical across runs."""
    h = hashlib.sha1(seed.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def emit_kicad_pcb(board: BoardResult, *, margin_nm: int = mm(2.0)) -> str:
    """Render a :class:`BoardResult` as KiCad 8 ``.kicad_pcb`` source."""

    def f(nm: int) -> str:
        return f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".") or "0"

    # Net 0 is KiCad's unconnected net and must exist.
    net_names: list[str] = [""]
    for name in dict.fromkeys(board.nets):
        if name and name not in net_names:
            net_names.append(name)
    net_index = {name: i for i, name in enumerate(net_names)}

    out: list[str] = []
    out.append('(kicad_pcb (version 20240108) (generator "silkscreen")')
    out.append("  (general (thickness 1.6))")
    out.append('  (paper "A4")')
    out.append("  (layers")
    out.append(_LAYERS)
    out.append("  )")
    for i, name in enumerate(net_names):
        out.append(f'  (net {i} "{name}")')

    m = margin_nm
    corners = [
        (-m, -m),
        (board.width_nm + m, -m),
        (board.width_nm + m, board.height_nm + m),
        (-m, board.height_nm + m),
    ]
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        out.append(
            f"  (gr_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
            f' (stroke (width 0.1) (type solid)) (layer "Edge.Cuts")'
            f' (uuid "{_uuid(f"edge{i}")}"))'
        )

    for part in board.parts:
        fp = part.footprint
        # The placer gives a bottom-left corner with Y up; KiCad wants the
        # anchor with Y down, and the courtyard is centred on the anchor.
        anchor_x = part.x_nm + fp.courtyard_w_nm
        anchor_y = board.height_nm - part.y_nm - fp.courtyard_h_nm
        angle = 90 if part.rotated else 0
        at = f"{f(anchor_x)} {f(anchor_y)}" + (f" {angle}" if angle else "")

        out.append(f'  (footprint "silkscreen:{fp.name}"')
        out.append('    (layer "F.Cu")')
        out.append(f'    (uuid "{_uuid(part.ref)}")')
        out.append(f"    (at {at})")
        out.append(f'    (descr "{fp.description}")')
        out.append(
            f'    (property "Reference" "{part.ref}"'
            f" (at 0 {f(-fp.courtyard_h_nm - mm(0.7))} 0)"
            f' (layer "F.SilkS") (uuid "{_uuid(part.ref + "ref")}")'
            f" (effects (font (size 0.8 0.8) (thickness 0.12))))"
        )
        out.append(
            f'    (property "Value" "{part.value}"'
            f" (at 0 {f(fp.courtyard_h_nm + mm(0.7))} 0)"
            f' (layer "F.Fab") (uuid "{_uuid(part.ref + "val")}")'
            f" (effects (font (size 0.8 0.8) (thickness 0.12))))"
        )

        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        cpts = [(-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch)]
        for i in range(4):
            sx, sy = cpts[i]
            ex, ey = cpts[(i + 1) % 4]
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.05) (type solid)) (layer "F.CrtYd")'
                f' (uuid "{_uuid(part.ref + f"crt{i}")}"))'
            )

        if fp.body_w_nm and fp.body_h_nm:
            bw, bh = fp.body_w_nm, fp.body_h_nm
            bpts = [(-bw, -bh), (bw, -bh), (bw, bh), (-bw, bh)]
            for i in range(4):
                sx, sy = bpts[i]
                ex, ey = bpts[(i + 1) % 4]
                out.append(
                    f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                    f' (stroke (width 0.12) (type solid)) (layer "F.SilkS")'
                    f' (uuid "{_uuid(part.ref + f"silk{i}")}"))'
                )

        for pad in fp.pads:
            idx = net_index.get(pad.net, 0)
            net_decl = f' (net {idx} "{pad.net}")' if idx else ""
            out.append(
                f'    (pad "{pad.number}" smd roundrect'
                f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                f' (layers "F.Cu" "F.Paste" "F.Mask")'
                f" (roundrect_rratio 0.25){net_decl}"
                f' (uuid "{_uuid(part.ref + "p" + pad.number)}"))'
            )
        out.append("  )")

    out.append(")")
    return "\n".join(out) + "\n"


def write_board(board: BoardResult, path: str | Path) -> Path:
    """Write the board to ``path`` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pcb(board), encoding="utf-8")
    return path
