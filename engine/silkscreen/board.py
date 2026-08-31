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

from dataclasses import dataclass, field
from pathlib import Path

from .footprints import (
    Footprint,
    UnsupportedPackage,
    for_passive,
    lqfp,
    silk_segments,
    soic,
    sot223,
)
from .ids import stable_uuid
from .netlist import CircuitSpec
from .packing import Layer, Part, Placement, pack
from .packing import Net as PackNet
from .routing import RoutePad, RouteResult, Track, Via, route
from .units import DEFAULT_CLEARANCE_NM, NM_PER_MM, mm

__all__ = [
    "BoardResult",
    "PlacedPart",
    "DEFAULT_BOARD_MARGIN_NM",
    "build_board",
    "board_pads",
    "part_anchor",
    "placed_half_extents",
    "rotate_offset",
    "route_board",
    "emit_kicad_pcb",
    "write_board",
]

#: Gap between the outermost courtyard and the board edge. Shared by the
#: emitter and the router: the router must know where the copper may go, and
#: the edge is drawn from the same number, so they cannot drift apart.
DEFAULT_BOARD_MARGIN_NM = mm(2.0)

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
    layer: Layer = Layer.TOP


@dataclass
class BoardResult:
    parts: list[PlacedPart]
    nets: list[str]
    width_nm: int
    height_nm: int
    solver_status: str
    wirelength_nm: int | None = None
    warnings: list[str] = field(default_factory=list)
    #: Copper laid by :func:`route_board`. Empty means the board is placed but
    #: unrouted -- which is what KiCad draws as a ratsnest.
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    #: Nets the router could not finish, each mapped to the reason. Read this
    #: before telling anyone the board is routed.
    unrouted_nets: dict[str, str] = field(default_factory=dict)
    routed_nets: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.width_nm / NM_PER_MM, self.height_nm / NM_PER_MM)

    @property
    def is_routed(self) -> bool:
        """True only when every routable net came out fully connected."""
        return bool(self.tracks) and not self.unrouted_nets

    @property
    def route_completion(self) -> float:
        """Fraction of routable nets finished.

        1.0 with both lists empty means there was nothing to route, not that a
        refusal succeeded: :func:`route_board` names every net a refusal covers
        in ``unrouted_nets``, so a skipped route reads 0.0 rather than 100%.
        """
        total = len(self.routed_nets) + len(self.unrouted_nets)
        return 1.0 if total == 0 else len(self.routed_nets) / total


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
    time_limit_s: float | None = 20.0,
    edge_refs: set[str] | None = None,
    rotatable_refs: set[str] | None = None,
    two_sided: bool = False,
) -> BoardResult:
    """Turn a validated circuit into footprints, nets and a placement.

    ``rotatable_refs`` names parts the solver may turn 90 degrees. Rotation is
    off by default because it only pays when a board is tight, and it costs
    determinism nothing either way.

    With ``two_sided``, passives may go on either side while ICs stay on top --
    an IC on the underside complicates assembly and rework for little area
    saved, whereas moving decoupling capacitors under their chip is standard
    practice and shortens the loop.
    """
    spec.validate()

    # The schematic emitter numbers parts from the same call, so R2 on the
    # drawing is R2 on the board. Numbering them separately would give two
    # self-consistent files that describe different circuits.
    ref_of = spec.assign_refs()

    # A ref that names nothing is a caller error, not a no-op -- the same
    # convention pack() and kicad.py already follow. Silently ignoring it means
    # a part the caller asked to rotate quietly does not, and the board looks
    # like the solver simply preferred it that way.
    known = set(ref_of.values())
    unknown = sorted(set(rotatable_refs or ()) - known)
    if unknown:
        raise ValueError(
            f"rotatable_refs names {unknown}, which are not on this board; "
            f"known refs: {sorted(known)}"
        )

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
        ref = ref_of[device.name]
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
        ref = ref_of[passive.name]
        placed.append(PlacedPart(ref=ref, footprint=fp, value=passive.value))

    parts = [
        Part(
            width_nm=p.footprint.courtyard_w_nm * 2,
            height_nm=p.footprint.courtyard_h_nm * 2,
            ref=p.ref,
            must_be_on_edge=p.ref in (edge_refs or set()),
            allow_rotation=p.ref in (rotatable_refs or set()),
            layer=Layer.EITHER if two_sided and p.ref.startswith(("C", "R"))
            else Layer.TOP,
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
            part.layer = placement.layer

    return BoardResult(
        parts=placed,
        nets=[c.net for c in spec.connections],
        width_nm=result.board_width_nm,
        height_nm=result.board_height_nm,
        solver_status=result.status.value,
        wirelength_nm=result.wirelength_nm,
        warnings=list(result.warnings),
    )


def placed_half_extents(part: PlacedPart) -> tuple[int, int]:
    """The part's courtyard half-extents **as placed**, not as drawn.

    A 90-degree rotation swaps them. The placer reserves the swapped box and
    reports its bottom-left corner, so anything deriving a position from that
    corner has to swap too. Not swapping is the recorded bug this function
    exists to end: the anchor came out short by exactly ``(ch-cw, cw-ch)``, the
    file still parsed, the courtyard still drew, and only the pads were wrong.
    """
    fp = part.footprint
    if part.rotated:
        return fp.courtyard_h_nm, fp.courtyard_w_nm
    return fp.courtyard_w_nm, fp.courtyard_h_nm


def part_anchor(part: PlacedPart) -> tuple[int, int]:
    """The footprint anchor in the solver's Y-up frame.

    One definition, used by the emitter and by the router's pad geometry. Two
    copies of this arithmetic is how the copper and the footprints ended up in
    different places while both looked right on their own.
    """
    half_w, half_h = placed_half_extents(part)
    return part.x_nm + half_w, part.y_nm + half_h


def rotate_offset(x_nm: int, y_nm: int, *, rotated: bool) -> tuple[int, int]:
    """A footprint-local offset mapped into the board frame.

    Only 0 and 90 degrees occur, and both are exact in integer nanometres, so
    this stays integer arithmetic rather than going through sin/cos. KiCad
    turns a footprint counter-clockwise on screen and screen Y points down, so
    the board-frame image of a local point is a rotation by ``-angle``: at 90
    degrees, ``(x, y) -> (y, -x)``. That is the same mapping
    :func:`silkscreen.kicad._rotate_mm` applies when reading a board back, and
    the two must agree or a board does not survive a round trip.
    """
    if not rotated:
        return x_nm, y_nm
    return y_nm, -x_nm


def board_pads(board: BoardResult) -> list[RoutePad]:
    """Every pad on the board, absolute, in the solver's Y-up frame.

    The placer hands back a courtyard's bottom-left corner with Y up; a pad
    offset inside a footprint is measured from the anchor with Y **down**,
    because that is the frame it is written to the file in. Composing the two
    is the single place those frames meet outside the emitter, and getting it
    wrong is the silent-geometry bug class: the router would lay copper to
    coordinates no pad occupies, the run would report success, and the board
    would come back with tracks ending in bare laminate.

    A rotated part turns its pads with it: the offset is mapped through
    :func:`rotate_offset` and the pad's own width and height swap, because a
    1.95 x 0.6 mm pad laid on its side is 0.6 x 1.95 mm of copper for the
    router to keep clear of.
    """
    pads: list[RoutePad] = []
    for part in board.parts:
        anchor_x, anchor_y = part_anchor(part)
        for pad in part.footprint.pads:
            ox, oy = rotate_offset(pad.x_nm, pad.y_nm, rotated=part.rotated)
            w_nm, h_nm = (
                (pad.h_nm, pad.w_nm) if part.rotated else (pad.w_nm, pad.h_nm)
            )
            pads.append(
                RoutePad(
                    net=pad.net,
                    x_nm=anchor_x + ox,
                    # The one Y flip: pad offsets are written Y-down, the
                    # router works Y-up.
                    y_nm=anchor_y - oy,
                    w_nm=w_nm,
                    h_nm=h_nm,
                    layer=part.layer,
                    ref=part.ref,
                    number=pad.number,
                )
            )
    return pads


def route_board(
    board: BoardResult,
    *,
    margin_nm: int = DEFAULT_BOARD_MARGIN_NM,
    two_layer: bool = True,
    **kwargs,
) -> RouteResult:
    """Route ``board`` in place, filling its tracks, vias and unrouted nets.

    Placement and routing are separate calls, not one, so the placed board can
    be written out and inspected before any copper exists. That is the step the
    pipeline used to skip past.

    Rotated parts route like any other. They used to abort here, because the
    emitter misplaced a rotated footprint's anchor and routing to those
    coordinates would have turned a latent placement bug into copper landing on
    bare laminate. That bug is fixed: :func:`part_anchor` is now the one
    definition of where a part sits, and the emitter and this function both
    read it.
    """
    result = route(
        board_pads(board),
        min_x_nm=-margin_nm,
        min_y_nm=-margin_nm,
        max_x_nm=board.width_nm + margin_nm,
        max_y_nm=board.height_nm + margin_nm,
        two_layer=two_layer,
        **kwargs,
    )
    board.tracks = list(result.tracks)
    board.vias = list(result.vias)
    board.unrouted_nets = dict(result.unrouted)
    board.routed_nets = list(result.routed)
    board.warnings.extend(result.warnings)
    return result


def _uuid(seed: str) -> str:
    """Stable UUID from a seed, so output is byte-identical across runs."""
    return stable_uuid(seed)


def emit_kicad_pcb(
    board: BoardResult, *, margin_nm: int = DEFAULT_BOARD_MARGIN_NM
) -> str:
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
        # part_anchor owns the rotation swap, so this cannot drift from the
        # pad geometry the router was handed.
        anchor_up_x, anchor_up_y = part_anchor(part)
        anchor_x = anchor_up_x
        anchor_y = board.height_nm - anchor_up_y
        angle = 90 if part.rotated else 0
        at = f"{f(anchor_x)} {f(anchor_y)}" + (f" {angle}" if angle else "")

        # A bottom-side footprint lives on B.Cu with its pads and graphics on
        # the B layers; KiCad renders it mirrored from the same coordinates.
        bottom = part.layer is Layer.BOTTOM
        side = "B" if bottom else "F"
        out.append(f'  (footprint "silkscreen:{fp.name}"')
        out.append(f'    (layer "{side}.Cu")')
        out.append(f'    (uuid "{_uuid(part.ref)}")')
        out.append(f"    (at {at})")
        out.append(f'    (descr "{fp.description}")')
        out.append(
            f'    (property "Reference" "{part.ref}"'
            f" (at 0 {f(-fp.courtyard_h_nm - mm(0.7))} 0)"
            f' (layer "{side}.SilkS") (uuid "{_uuid(part.ref + "ref")}")'
            f" (effects (font (size 0.8 0.8) (thickness 0.12))))"
        )
        out.append(
            f'    (property "Value" "{part.value}"'
            f" (at 0 {f(fp.courtyard_h_nm + mm(0.7))} 0)"
            f' (layer "{side}.Fab") (uuid "{_uuid(part.ref + "val")}")'
            f" (effects (font (size 0.8 0.8) (thickness 0.12))))"
        )

        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        cpts = [(-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch)]
        for i in range(4):
            sx, sy = cpts[i]
            ex, ey = cpts[(i + 1) % 4]
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.05) (type solid)) (layer "{side}.CrtYd")'
                f' (uuid "{_uuid(part.ref + f"crt{i}")}"))'
            )

        # The body outline, clipped clear of the pads: stroking the raw body
        # rectangle put ink on every pad the body edge touches (see
        # footprints.silk_segments), and ink on a pad is a solderability
        # defect a fab will clip or flag.
        for i, (sx, sy, ex, ey) in enumerate(silk_segments(fp)):
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.12) (type solid)) (layer "{side}.SilkS")'
                f' (uuid "{_uuid(part.ref + f"silk{i}")}"))'
            )

        for pad in fp.pads:
            idx = net_index.get(pad.net, 0)
            net_decl = f' (net {idx} "{pad.net}")' if idx else ""
            out.append(
                f'    (pad "{pad.number}" smd roundrect'
                f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                f' (layers "{side}.Cu" "{side}.Paste" "{side}.Mask")'
                f" (roundrect_rratio 0.25){net_decl}"
                f' (uuid "{_uuid(part.ref + "p" + pad.number)}"))'
            )
        out.append("  )")

    # Copper last, after every footprint, matching KiCad's own ordering.
    # The Y flip here is the same one applied to footprint anchors above: the
    # router works in the placer's Y-up frame and this is the only place its
    # output crosses into KiCad's Y-down frame.
    def flip_y(y_nm: int) -> int:
        return board.height_nm - y_nm

    for index, track in enumerate(board.tracks):
        layer = "B.Cu" if track.layer is Layer.BOTTOM else "F.Cu"
        idx = net_index.get(track.net, 0)
        out.append(
            f"  (segment (start {f(track.start_x_nm)} {f(flip_y(track.start_y_nm))})"
            f" (end {f(track.end_x_nm)} {f(flip_y(track.end_y_nm))})"
            f" (width {f(track.width_nm)}) (layer \"{layer}\") (net {idx})"
            f' (uuid "{_uuid(f"seg{index}:{track.net}")}"))'
        )

    for index, via in enumerate(board.vias):
        idx = net_index.get(via.net, 0)
        out.append(
            f"  (via (at {f(via.x_nm)} {f(flip_y(via.y_nm))})"
            f" (size {f(via.diameter_nm)}) (drill {f(via.drill_nm)})"
            f' (layers "F.Cu" "B.Cu") (net {idx})'
            f' (uuid "{_uuid(f"via{index}:{via.net}")}"))'
        )

    out.append(")")
    return "\n".join(out) + "\n"


def write_board(board: BoardResult, path: str | Path) -> Path:
    """Write the board to ``path`` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pcb(board), encoding="utf-8")
    return path
