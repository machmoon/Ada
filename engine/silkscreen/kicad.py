"""KiCad board interop, without a KiCad install and without touching the GUI.

The original pipeline placed components by driving KiCad with ``pyautogui``:
sleep six seconds, click screen coordinate (1600, 590), paste a pre-placed board
off the clipboard. That is not an integration -- it depends on one monitor
resolution, one window position, and a committed board file that no code ever
generated.

This module reads and writes ``.kicad_pcb`` files directly through ``kiutils``,
which is pure Python. No ``pcbnew``, no DLL directory, no Windows-only paths, and
nothing that breaks when a window moves.

Coordinate systems
------------------
KiCad's Y axis points **down**; the placer's Y axis points **up**. The original
code mixed the two (it took ``bbox.GetBottom()`` as a bottom-left corner), which
mirrored every layout vertically. :func:`apply_placements` does the flip in one
place, explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kiutils.board import Board

from .packing import Net, Part, Placement, Wire
from .units import NM_PER_MM

__all__ = [
    "FootprintInfo",
    "load_board",
    "extract_parts",
    "extract_wires",
    "extract_nets",
    "footprint_ref",
    "apply_placements",
    "set_board_outline",
    "to_parts",
    "save_board",
    "is_power_net",
    "POWER_NET_PATTERNS",
]

#: Nets whose name matches any of these are treated as power/ground and excluded
#: from the wirelength objective. Expanding a 50-pad ground net into a clique
#: yields 1225 "connections" that swamp every signal net -- which is what the
#: original did, collapsing the whole board into a single placement group.
POWER_NET_PATTERNS = (
    "gnd", "agnd", "dgnd", "pgnd", "vss", "avss", "dvss", "earth",
    "vcc", "vdd", "avdd", "dvdd", "vbus", "vin", "vout", "vref",
)

#: Nets with more pads than this are also treated as global rails even if the
#: name does not match, on the theory that no signal net fans out that far.
DEFAULT_MAX_NET_FANOUT = 6

#: Matches supply-rail net names that are spelled as a voltage rather than a
#: word: "+3V3", "5V", "-12V", "1V8". These are rails, not signals.
_VOLTAGE_RAIL_RE = re.compile(r"^[+-]?\d+v\d*$")

_MM_TO_NM = NM_PER_MM


def footprint_ref(fp) -> str:
    """Reference designator of a kiutils footprint.

    ``Footprint.properties`` is a plain ``dict`` in current kiutils and a list of
    property objects in older releases; KiCad 7 and earlier stored the reference
    as an ``fp_text reference`` graphic item instead. Handle all three.
    """
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        ref = props.get("Reference")
        if ref:
            return str(ref)
    elif props:
        for prop in props:
            if getattr(prop, "key", None) == "Reference":
                value = getattr(prop, "value", "")
                if value:
                    return str(value)
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "reference":
            text = getattr(item, "text", "")
            if text:
                return str(text)
    return ""


@dataclass(frozen=True)
class FootprintInfo:
    """A footprint's reference, courtyard extent, and pad-to-net mapping.

    ``min_x_nm``/``min_y_nm``/``max_x_nm``/``max_y_nm`` are the courtyard bounding
    box **in footprint-local coordinates**, i.e. relative to the footprint's
    anchor. This distinction is load-bearing: KiCad stores a footprint's position
    as its *anchor*, but the placer works in bounding-box corners, and in a real
    footprint the courtyard is centred on the anchor (so ``min_x`` is about
    ``-width/2``, not 0). Writing a bbox corner straight into ``fp.position``
    displaces every part by half its own size.
    """

    ref: str
    width_nm: int
    height_nm: int
    #: Pin offsets from the courtyard's bottom-left, in packer (Y-up) space.
    pad_offsets: dict[str, tuple[int, int]]
    pad_nets: dict[str, str]
    #: Courtyard bbox in footprint-local coordinates (KiCad frame, Y down).
    min_x_nm: int = 0
    min_y_nm: int = 0
    max_x_nm: int = 0
    max_y_nm: int = 0
    library_id: str = ""

    def local_bbox_after_rotation(self, rotated: bool) -> tuple[int, int]:
        """Local bbox top-left corner, accounting for a 90 degree rotation.

        KiCad's ``angle`` rotates a footprint counter-clockwise on screen, and
        screen Y points down, so a local point ``(x, y)`` maps to ``(y, -x)``.
        Applying that to the courtyard corners gives a new local bbox whose
        top-left is ``(min_y, -max_x)``.
        """
        if not rotated:
            return self.min_x_nm, self.min_y_nm
        return self.min_y_nm, -self.max_x_nm


def _mm(value: float) -> int:
    return int(round(value * _MM_TO_NM))


def load_board(path: str | Path) -> Board:
    """Read a ``.kicad_pcb``. Raises ``FileNotFoundError`` if it is missing."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such board file: {path}")
    return Board.from_file(str(path))


def _courtyard_extent(fp) -> tuple[float, float, float, float] | None:
    """Bounding box of the footprint's courtyard, in local mm.

    Returns ``(min_x, min_y, max_x, max_y)`` or ``None`` if the footprint has no
    courtyard layer.
    """
    xs: list[float] = []
    ys: list[float] = []
    for item in fp.graphicItems:
        layer = getattr(item, "layer", "") or ""
        if "CrtYd" not in layer:
            continue
        for attr in ("start", "end", "center", "position"):
            pt = getattr(item, attr, None)
            if pt is not None:
                xs.append(pt.X)
                ys.append(pt.Y)
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _pad_extent(fp) -> tuple[float, float, float, float]:
    """Bounding box of all pads, in local mm. Fallback when no courtyard."""
    xs: list[float] = []
    ys: list[float] = []
    for pad in fp.pads:
        px, py = pad.position.X, pad.position.Y
        hw = (pad.size.X or 0) / 2
        hh = (pad.size.Y or 0) / 2
        xs += [px - hw, px + hw]
        ys += [py - hh, py + hh]
    if not xs:
        # Degenerate footprint (e.g. a fiducial with no pads). Give it a
        # nominal 1 mm square so it still gets a slot rather than crashing.
        return -0.5, -0.5, 0.5, 0.5
    return min(xs), min(ys), max(xs), max(ys)


def extract_parts(
    board: Board, *, courtyard_margin_mm: float = 0.0
) -> list[FootprintInfo]:
    """Describe every footprint on ``board`` in placer terms.

    Sizes come from the courtyard layer where present, falling back to the pad
    bounding box. Pad offsets are converted into the placer's Y-up frame,
    measured from the part's bottom-left corner.
    """
    infos: list[FootprintInfo] = []
    for fp in board.footprints:
        ref = footprint_ref(fp) or (getattr(fp, "entryName", "") or "")

        extent = _courtyard_extent(fp) or _pad_extent(fp)
        min_x, min_y, max_x, max_y = extent
        min_x -= courtyard_margin_mm
        min_y -= courtyard_margin_mm
        max_x += courtyard_margin_mm
        max_y += courtyard_margin_mm

        width_nm = max(_mm(max_x - min_x), 1)
        height_nm = max(_mm(max_y - min_y), 1)

        pad_offsets: dict[str, tuple[int, int]] = {}
        pad_nets: dict[str, str] = {}
        for pad in fp.pads:
            name = str(pad.number)
            # Local mm -> offset from bottom-left, flipping Y (KiCad Y is down).
            off_x = _mm(pad.position.X - min_x)
            off_y = _mm(max_y - pad.position.Y)
            pad_offsets[name] = (off_x, off_y)
            if pad.net is not None and pad.net.name:
                pad_nets[name] = pad.net.name

        infos.append(
            FootprintInfo(
                ref=ref,
                width_nm=width_nm,
                height_nm=height_nm,
                pad_offsets=pad_offsets,
                pad_nets=pad_nets,
                min_x_nm=_mm(min_x),
                min_y_nm=_mm(min_y),
                max_x_nm=_mm(max_x),
                max_y_nm=_mm(max_y),
                library_id=getattr(fp, "libraryNickname", "") or "",
            )
        )
    return infos


def is_power_net(name: str, pad_count: int, max_fanout: int) -> bool:
    """True if ``name`` should be excluded from the wirelength objective."""
    bare = name.lower().rsplit("/", 1)[-1]
    # Split on separators so "VINT" and "VREFBUF_OUT" are not mistaken for rails
    # by a naive prefix match, while "VCC_3V3" and "+3V3" still are.
    tokens = [t for t in re.split(r"[^a-z0-9]+", bare) if t]
    if any(t in POWER_NET_PATTERNS or _VOLTAGE_RAIL_RE.match(t) for t in tokens):
        return True
    return pad_count > max_fanout


def extract_nets(
    infos: list[FootprintInfo],
    *,
    power_weight: float = 0.25,
    max_net_fanout: int = DEFAULT_MAX_NET_FANOUT,
) -> list[Net]:
    """Turn shared nets into HPWL nets for the placer.

    Power rails stay in the objective at a reduced weight rather than being
    dropped. Dropping them entirely is what leaves a decoupling capacitor with
    no objective term at all -- its only connections to the IC it bypasses are
    VCC and GND -- so it drifts to wherever the packer finds room. HPWL costs one
    bounding box per net regardless of pad count, so keeping a 50-pad ground net
    is cheap in a way a pairwise clique never was.
    """
    by_net: dict[str, list[tuple[int, str]]] = {}
    for idx, info in enumerate(infos):
        for pad_name, net_name in info.pad_nets.items():
            by_net.setdefault(net_name, []).append((idx, pad_name))

    out: list[Net] = []
    for net_name, members in by_net.items():
        # Collapse pads on the same part: HPWL over one part's own pads is not
        # a placement signal.
        seen: dict[int, tuple[int, int]] = {}
        for part_idx, pad_name in members:
            if part_idx not in seen:
                seen[part_idx] = infos[part_idx].pad_offsets.get(pad_name, (0, 0))
        if len(seen) < 2:
            continue
        weight = (
            power_weight
            if is_power_net(net_name, len(members), max_net_fanout)
            else 1.0
        )
        out.append(
            Net(
                terminals=tuple(seen.items()),
                name=net_name,
                weight=weight,
            )
        )
    return out


def extract_wires(
    infos: list[FootprintInfo],
    *,
    max_net_fanout: int = DEFAULT_MAX_NET_FANOUT,
) -> list[Wire]:
    """Turn shared nets into placer wires.

    Two fixes over the original:

    * Power and ground nets are skipped. The original expanded every net into a
      full clique, so a ground net with *p* pads contributed ``p*(p-1)/2`` edges
      and dominated the objective.
    * Remaining multi-pad nets are connected as a **star** to the net's first
      pad rather than a clique, which keeps the edge count linear in pad count
      instead of quadratic.
    """
    by_net: dict[str, list[tuple[int, str]]] = {}
    for idx, info in enumerate(infos):
        for pad_name, net_name in info.pad_nets.items():
            by_net.setdefault(net_name, []).append((idx, pad_name))

    wires: list[Wire] = []
    for net_name, members in by_net.items():
        if len(members) < 2:
            continue
        if is_power_net(net_name, len(members), max_net_fanout):
            continue
        hub_idx, hub_pad = members[0]
        hub_off = infos[hub_idx].pad_offsets.get(hub_pad, (0, 0))
        for other_idx, other_pad in members[1:]:
            if other_idx == hub_idx:
                continue
            other_off = infos[other_idx].pad_offsets.get(other_pad, (0, 0))
            wires.append(
                Wire(
                    source=hub_idx,
                    dest=other_idx,
                    offset_source=hub_off,
                    offset_dest=other_off,
                )
            )
    return wires


def to_parts(
    infos: list[FootprintInfo],
    *,
    edge_refs: set[str] | None = None,
    rotatable_refs: set[str] | None = None,
) -> list[Part]:
    """Convert footprint descriptions into placer inputs."""
    edge_refs = set(edge_refs or ())
    rotatable_refs = set(rotatable_refs or ())

    # Silently ignoring a ref that matches nothing turns a typo into a missing
    # constraint with no signal at all.
    known = {info.ref for info in infos}
    for label, given in (("edge_refs", edge_refs), ("rotatable_refs", rotatable_refs)):
        unknown = given - known
        if unknown:
            raise ValueError(
                f"{label} names refs not on this board: {sorted(unknown)}. "
                f"Available: {sorted(known)}"
            )
    return [
        Part(
            width_nm=info.width_nm,
            height_nm=info.height_nm,
            ref=info.ref,
            must_be_on_edge=info.ref in edge_refs,
            allow_rotation=info.ref in rotatable_refs,
        )
        for info in infos
    ]


def apply_placements(
    board: Board,
    infos: list[FootprintInfo],
    placements: list[Placement],
    board_height_nm: int,
    *,
    origin_mm: tuple[float, float] = (0.0, 0.0),
) -> int:
    """Move footprints on ``board`` to their solved positions.

    Two conversions happen here, and only here.

    **Axis.** The placer works bottom-left origin with Y up; KiCad works top-left
    with Y down::

        bbox_top = board_height - (y + part_height)

    **Frame.** The placer produces the courtyard's bounding-box corner, but
    ``fp.position`` is the footprint's *anchor*, and the courtyard is centred on
    the anchor rather than starting at it. Writing the bbox corner directly would
    displace every footprint by half its own size -- producing overlaps on the
    written board even though the solver's answer was overlap-free::

        anchor = bbox_corner - local_bbox_corner

    Returns the number of footprints actually moved. Refs present in
    ``placements`` but absent from the board are skipped and counted out, rather
    than silently shifting every later index -- the original desynchronised its
    size and reference lists when a footprint was missing, corrupting the whole
    layout.
    """
    by_ref = {info.ref: info for info in infos}
    fp_by_ref = {}
    for fp in board.footprints:
        ref = footprint_ref(fp)
        if ref:
            fp_by_ref[ref] = fp

    moved = 0
    for placement in placements:
        info = by_ref.get(placement.ref)
        fp = fp_by_ref.get(placement.ref)
        if info is None or fp is None:
            continue

        height_nm = info.width_nm if placement.rotated else info.height_nm
        bbox_top_nm = board_height_nm - (placement.y_nm + height_nm)

        local_x_nm, local_y_nm = info.local_bbox_after_rotation(placement.rotated)

        fp.position.X = origin_mm[0] + (placement.x_nm - local_x_nm) / _MM_TO_NM
        fp.position.Y = origin_mm[1] + (bbox_top_nm - local_y_nm) / _MM_TO_NM
        if placement.rotated:
            current = fp.position.angle or 0
            fp.position.angle = (current + 90) % 360
        moved += 1

    return moved


def set_board_outline(
    board: Board,
    width_nm: int,
    height_nm: int,
    *,
    origin_mm: tuple[float, float] = (0.0, 0.0),
    margin_nm: int = 0,
    replace: bool = True,
) -> None:
    """Draw a rectangular ``Edge.Cuts`` outline around the placed area.

    Without this the written file has no board outline at all, which matters for
    more than looks: ``Edge.Cuts`` is what KiCad measures board-edge clearance
    against, and it is the only representation of the boundary that
    :class:`~silkscreen.packing.Part`'s ``must_be_on_edge`` was solved against.
    A file with parts pinned to an edge that does not exist cannot be
    meaningfully design-rule checked.

    Args:
        board: Board to modify in place.
        width_nm, height_nm: Placed area, as reported by :func:`~silkscreen.pack`.
        origin_mm: Top-left corner, matching the value passed to
            :func:`apply_placements`.
        margin_nm: Extra space between the placed area and the cut line.
        replace: Remove any existing ``Edge.Cuts`` graphics first. Leaving a
            stale outline behind is worse than having none.
    """
    from kiutils.items.gritems import GrLine
    from kiutils.items.common import Position

    if replace:
        board.graphicItems = [
            g for g in board.graphicItems
            if getattr(g, "layer", None) != "Edge.Cuts"
        ]

    m = margin_nm / _MM_TO_NM
    x0 = origin_mm[0] - m
    y0 = origin_mm[1] - m
    x1 = origin_mm[0] + width_nm / _MM_TO_NM + m
    y1 = origin_mm[1] + height_nm / _MM_TO_NM + m

    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        board.graphicItems.append(
            GrLine(
                start=Position(X=sx, Y=sy),
                end=Position(X=ex, Y=ey),
                layer="Edge.Cuts",
                width=0.05,
            )
        )


def save_board(board: Board, path: str | Path) -> Path:
    """Write ``board`` to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    board.to_file(str(path))
    return path
