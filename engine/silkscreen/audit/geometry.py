"""Read a ``.kicad_pcb`` into a flat, absolute geometry model for checking.

This is deliberately a *second* reader, independent of :mod:`silkscreen.kicad`.
That module exists to feed the placer: it returns footprint-local courtyard
boxes and pad offsets measured from a courtyard corner in the solver's Y-up
frame, because that is what the packer consumes. A checker wants the opposite
-- every pad, courtyard and track already resolved to absolute board
coordinates -- and, more importantly, a checker written in terms of the code
that produced the board shares that code's blind spots. The rule the repo
already applies to its placement tests ("compute expected geometry with
independent math") applies with more force to a tool whose whole job is to
catch geometry that is wrong.

Everything here is **integer nanometres in KiCad's own frame**: origin
top-left, X right, Y **down**. No Y flip happens anywhere in this package
except once inside the SVG renderer's viewBox, because SVG shares KiCad's
handedness and a second flip would put every marker in the wrong place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..units import NM_PER_MM

__all__ = [
    "Rect",
    "Seg",
    "AuditPad",
    "AuditPart",
    "AuditBoard",
    "load_audit_board",
    "seg_seg_distance_nm",
    "seg_rect_distance_nm",
]


def _nm(value: float | None) -> int:
    return int(round(float(value or 0.0) * NM_PER_MM))


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box in board coordinates, nanometres."""

    x0: int
    y0: int
    x1: int
    y1: int

    @staticmethod
    def around(points: list[tuple[int, int]]) -> Rect | None:
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    @property
    def width_nm(self) -> int:
        return self.x1 - self.x0

    @property
    def height_nm(self) -> int:
        return self.y1 - self.y0

    @property
    def centre(self) -> tuple[int, int]:
        return ((self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2)

    def grown(self, by_nm: int) -> Rect:
        return Rect(self.x0 - by_nm, self.y0 - by_nm, self.x1 + by_nm, self.y1 + by_nm)

    def union(self, other: Rect) -> Rect:
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersection(self, other: Rect) -> Rect | None:
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rect(x0, y0, x1, y1)

    def contains(self, other: Rect) -> bool:
        return (
            self.x0 <= other.x0
            and self.y0 <= other.y0
            and self.x1 >= other.x1
            and self.y1 >= other.y1
        )

    def gap_to(self, other: Rect) -> int:
        """Edge-to-edge separation. 0 when they touch, negative when they overlap.

        The negative case is the useful one: it is how deep the overlap goes,
        which is what a clearance violation needs to report.
        """
        dx = max(other.x0 - self.x1, self.x0 - other.x1)
        dy = max(other.y0 - self.y1, self.y0 - other.y1)
        if dx >= 0 and dy >= 0:
            return int(round(math.hypot(dx, dy)))
        if dx >= 0:
            return dx
        if dy >= 0:
            return dy
        return max(dx, dy)


@dataclass(frozen=True)
class Seg:
    """A straight run of copper or silkscreen, absolute, nanometres."""

    x0: int
    y0: int
    x1: int
    y1: int
    width_nm: int = 0
    layer: str = ""
    net: str = ""

    @property
    def bbox(self) -> Rect:
        return Rect(
            min(self.x0, self.x1),
            min(self.y0, self.y1),
            max(self.x0, self.x1),
            max(self.y0, self.y1),
        )

    @property
    def midpoint(self) -> tuple[int, int]:
        return ((self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2)

    @property
    def length_nm(self) -> int:
        return int(round(math.hypot(self.x1 - self.x0, self.y1 - self.y0)))

    @property
    def side(self) -> str:
        """``F``, ``B`` or ``*`` -- which side of the board this is on."""
        return self.layer.split(".")[0] if "." in self.layer else "*"


@dataclass(frozen=True)
class AuditPad:
    ref: str
    number: str
    net: str
    rect: Rect
    layers: tuple[str, ...] = ()
    through_hole: bool = False

    @property
    def centre(self) -> tuple[int, int]:
        return self.rect.centre

    @property
    def name(self) -> str:
        return f"{self.ref}.{self.number}"

    def shares_copper_with(self, other: AuditPad) -> bool:
        """True when both pads have copper on at least one common layer."""
        if self.through_hole or other.through_hole:
            return True
        mine = {lay for lay in self.layers if lay.endswith(".Cu")}
        theirs = {lay for lay in other.layers if lay.endswith(".Cu")}
        if any(lay.startswith("*") for lay in self.layers + other.layers):
            return True
        return bool(mine & theirs)

    @property
    def side(self) -> str:
        for lay in self.layers:
            if lay.endswith(".Cu") and "." in lay:
                return lay.split(".")[0]
        return "F"


@dataclass(frozen=True)
class AuditPart:
    ref: str
    value: str
    lib_id: str
    x_nm: int
    y_nm: int
    angle: float
    side: str
    pads: tuple[AuditPad, ...]
    courtyard: Rect | None
    silk: tuple[Seg, ...] = ()

    @property
    def extent(self) -> Rect:
        """Courtyard if the footprint has one, else the pad bounding box.

        A footprint with no courtyard is reported separately as a blind spot;
        falling back here keeps such a part visible on the render instead of
        silently absent from it.
        """
        if self.courtyard is not None:
            return self.courtyard
        boxes = [p.rect for p in self.pads]
        if not boxes:
            return Rect(self.x_nm, self.y_nm, self.x_nm, self.y_nm)
        out = boxes[0]
        for box in boxes[1:]:
            out = out.union(box)
        return out

    @property
    def centre(self) -> tuple[int, int]:
        return self.extent.centre

    @property
    def is_ic(self) -> bool:
        """Multi-pin active part -- what a decoupling rule applies to."""
        return len(self.pads) >= 4 or self.ref.upper().startswith("U")


@dataclass
class AuditBoard:
    parts: list[AuditPart] = field(default_factory=list)
    tracks: list[Seg] = field(default_factory=list)
    vias: list[tuple[int, int, int, str]] = field(default_factory=list)
    #: The Edge.Cuts bounding box, or None when the board has no outline at
    #: all -- itself a finding, since edge rules mean nothing without one.
    outline: Rect | None = None
    net_names: list[str] = field(default_factory=list)
    source: Path | None = None

    def pads(self) -> list[AuditPad]:
        return [pad for part in self.parts for pad in part.pads]

    def pads_by_net(self) -> dict[str, list[AuditPad]]:
        out: dict[str, list[AuditPad]] = {}
        for pad in self.pads():
            if pad.net:
                out.setdefault(pad.net, []).append(pad)
        return out

    def part_by_ref(self, ref: str) -> AuditPart | None:
        for part in self.parts:
            if part.ref == ref:
                return part
        return None

    @property
    def extent(self) -> Rect:
        """Everything the board occupies, outline included."""
        boxes: list[Rect] = [p.extent for p in self.parts]
        boxes += [t.bbox for t in self.tracks]
        if self.outline is not None:
            boxes.append(self.outline)
        if not boxes:
            return Rect(0, 0, NM_PER_MM, NM_PER_MM)
        out = boxes[0]
        for box in boxes[1:]:
            out = out.union(box)
        return out


def _rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """Footprint-local point to board frame.

    KiCad turns a footprint counter-clockwise on screen and screen Y points
    down, so the board-frame image of a local point is a mathematical rotation
    by ``-angle``. Written out here rather than imported so that a sign error
    in the placer's copy of this cannot hide from the checker.
    """
    if not angle_deg:
        return x, y
    r = math.radians(angle_deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _pad_rect(fp_x: float, fp_y: float, angle: float, pad) -> Rect:
    """Absolute bounding box of a pad, exact at 90 degree steps.

    Between the cardinal angles a rotated rectangle's bounding box is larger
    than the pad; that direction is the safe one for a clearance checker (it
    can over-report a gap, never miss one), and no footprint this project
    generates is placed off-cardinal.
    """
    hw = _nm(pad.size.X) / 2.0
    hh = _nm(pad.size.Y) / 2.0
    px, py = _nm(pad.position.X), _nm(pad.position.Y)
    local_angle = float(getattr(pad.position, "angle", None) or 0.0)
    total = angle + local_angle
    corners = []
    for sx in (-hw, hw):
        for sy in (-hh, hh):
            cx, cy = _rotate(px + sx, py + sy, total)
            corners.append((int(round(fp_x + cx)), int(round(fp_y + cy))))
    box = Rect.around(corners)
    assert box is not None
    return box


def _fp_line_abs(fp_x: float, fp_y: float, angle: float, item, net: str = "") -> Seg:
    sx, sy = _rotate(_nm(item.start.X), _nm(item.start.Y), angle)
    ex, ey = _rotate(_nm(item.end.X), _nm(item.end.Y), angle)
    width = 0
    stroke = getattr(item, "stroke", None)
    if stroke is not None and getattr(stroke, "width", None):
        width = _nm(stroke.width)
    elif getattr(item, "width", None):
        width = _nm(item.width)
    return Seg(
        x0=int(round(fp_x + sx)),
        y0=int(round(fp_y + sy)),
        x1=int(round(fp_x + ex)),
        y1=int(round(fp_y + ey)),
        width_nm=width,
        layer=str(getattr(item, "layer", "") or ""),
        net=net,
    )


def _reference_of(fp) -> str:
    """Reference designator, across the three ways KiCad has stored it."""
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        ref = props.get("Reference")
        if ref:
            return str(ref)
    elif props:
        for prop in props:
            if getattr(prop, "key", None) == "Reference":
                return str(getattr(prop, "value", "") or "")
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "reference":
            return str(getattr(item, "text", "") or "")
    return ""


def _value_of(fp) -> str:
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        return str(props.get("Value", "") or "")
    for prop in props or []:
        if getattr(prop, "key", None) == "Value":
            return str(getattr(prop, "value", "") or "")
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "value":
            return str(getattr(item, "text", "") or "")
    return ""


def load_audit_board(path: str | Path) -> AuditBoard:
    """Parse ``path`` into absolute geometry. Raises if the file is missing."""
    from kiutils.board import Board  # imported here: the CLI is the only caller

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such board file: {path}")
    raw = Board.from_file(str(path))

    net_by_number = {n.number: (n.name or "") for n in (raw.nets or [])}

    parts: list[AuditPart] = []
    for fp in raw.footprints or []:
        fx, fy = _nm(fp.position.X), _nm(fp.position.Y)
        angle = float(getattr(fp.position, "angle", None) or 0.0)
        side = "B" if str(getattr(fp, "layer", "F.Cu")).startswith("B") else "F"
        ref = _reference_of(fp) or str(getattr(fp, "entryName", "") or "")

        pads: list[AuditPad] = []
        for pad in fp.pads or []:
            layers = tuple(str(lay) for lay in (pad.layers or []))
            pads.append(
                AuditPad(
                    ref=ref,
                    number=str(pad.number),
                    net=str(getattr(pad.net, "name", "") or ""),
                    rect=_pad_rect(fx, fy, angle, pad),
                    layers=layers,
                    through_hole=str(pad.type) in ("thru_hole", "np_thru_hole"),
                )
            )

        courtyard_pts: list[tuple[int, int]] = []
        silk: list[Seg] = []
        for item in fp.graphicItems or []:
            layer = str(getattr(item, "layer", "") or "")
            if not hasattr(item, "start") or not hasattr(item, "end"):
                continue
            seg = _fp_line_abs(fx, fy, angle, item)
            if layer.endswith(".CrtYd"):
                courtyard_pts += [(seg.x0, seg.y0), (seg.x1, seg.y1)]
            elif layer.endswith(".SilkS"):
                silk.append(seg)

        parts.append(
            AuditPart(
                ref=ref,
                value=_value_of(fp),
                lib_id=str(getattr(fp, "libId", "") or ""),
                x_nm=fx,
                y_nm=fy,
                angle=angle,
                side=side,
                pads=tuple(pads),
                courtyard=Rect.around(courtyard_pts),
                silk=tuple(silk),
            )
        )

    tracks: list[Seg] = []
    vias: list[tuple[int, int, int, str]] = []
    for item in raw.traceItems or []:
        kind = type(item).__name__
        if kind == "Segment":
            tracks.append(
                Seg(
                    x0=_nm(item.start.X),
                    y0=_nm(item.start.Y),
                    x1=_nm(item.end.X),
                    y1=_nm(item.end.Y),
                    width_nm=_nm(item.width),
                    layer=str(item.layer),
                    net=net_by_number.get(int(item.net or 0), ""),
                )
            )
        elif kind == "Via":
            vias.append(
                (
                    _nm(item.position.X),
                    _nm(item.position.Y),
                    _nm(item.size),
                    net_by_number.get(int(item.net or 0), ""),
                )
            )

    edge_pts: list[tuple[int, int]] = []
    for item in raw.graphicItems or []:
        if not str(getattr(item, "layer", "")).startswith("Edge.Cuts"):
            continue
        for attr in ("start", "end", "position"):
            point = getattr(item, attr, None)
            if point is not None:
                edge_pts.append((_nm(point.X), _nm(point.Y)))

    return AuditBoard(
        parts=parts,
        tracks=tracks,
        vias=vias,
        outline=Rect.around(edge_pts),
        net_names=[name for name in net_by_number.values() if name],
        source=path,
    )


def _point_seg_distance(px: float, py: float, seg: Seg) -> float:
    vx, vy = seg.x1 - seg.x0, seg.y1 - seg.y0
    wx, wy = px - seg.x0, py - seg.y0
    denom = vx * vx + vy * vy
    if denom == 0:
        return math.hypot(wx, wy)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(px - (seg.x0 + t * vx), py - (seg.y0 + t * vy))


def seg_seg_distance_nm(a: Seg, b: Seg) -> int:
    """Copper-edge separation between two tracks, accounting for width."""
    if _segments_cross(a, b):
        centre = 0.0
    else:
        centre = min(
            _point_seg_distance(a.x0, a.y0, b),
            _point_seg_distance(a.x1, a.y1, b),
            _point_seg_distance(b.x0, b.y0, a),
            _point_seg_distance(b.x1, b.y1, a),
        )
    return int(round(centre - a.width_nm / 2 - b.width_nm / 2))


def _orient(ax, ay, bx, by, cx, cy) -> int:
    val = (by - ay) * (cx - bx) - (bx - ax) * (cy - by)
    return (val > 0) - (val < 0)


def _segments_cross(a: Seg, b: Seg) -> bool:
    o1 = _orient(a.x0, a.y0, a.x1, a.y1, b.x0, b.y0)
    o2 = _orient(a.x0, a.y0, a.x1, a.y1, b.x1, b.y1)
    o3 = _orient(b.x0, b.y0, b.x1, b.y1, a.x0, a.y0)
    o4 = _orient(b.x0, b.y0, b.x1, b.y1, a.x1, a.y1)
    return o1 != o2 and o3 != o4


def seg_rect_distance_nm(seg: Seg, rect: Rect) -> int:
    """Separation between a track's copper edge and a pad rectangle.

    A segment that crosses the rectangle entirely has both endpoints outside
    it and both far from the corners, so nearest-endpoint distance alone
    reports a comfortable gap for a track running straight over the pad. The
    crossing test is what makes this a distance rather than a near-miss.
    """
    edges = [
        Seg(rect.x0, rect.y0, rect.x1, rect.y0),
        Seg(rect.x1, rect.y0, rect.x1, rect.y1),
        Seg(rect.x1, rect.y1, rect.x0, rect.y1),
        Seg(rect.x0, rect.y1, rect.x0, rect.y0),
    ]
    inside = (
        rect.x0 <= seg.x0 <= rect.x1 and rect.y0 <= seg.y0 <= rect.y1
    ) or (rect.x0 <= seg.x1 <= rect.x1 and rect.y0 <= seg.y1 <= rect.y1)
    if inside or any(_segments_cross(seg, edge) for edge in edges):
        centre = 0.0
    else:
        centre = min(
            min(
                _point_seg_distance(seg.x0, seg.y0, edge),
                _point_seg_distance(seg.x1, seg.y1, edge),
                _point_seg_distance(edge.x0, edge.y0, seg),
                _point_seg_distance(edge.x1, edge.y1, seg),
            )
            for edge in edges
        )
    return int(round(centre - seg.width_nm / 2))
