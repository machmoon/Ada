"""Board envelope extraction for enclosure generation.

Reads a finished ``.kicad_pcb`` and reduces it to the geometry an enclosure
needs: the ``Edge.Cuts`` outline, every footprint's absolute XY extent, and a
Z height per footprint from :mod:`.heights` (the board file itself carries no
Z). Everything is **integer nanometres** in **KiCad's Y-down frame** -- this
module introduces no new Y flip (plan decision 10); the one place enclosure
geometry changes frame is :mod:`.emit`.

Board reading is delegated to :mod:`silkscreen.kicad` (``load_board`` /
``extract_parts``) rather than re-parsed here, so courtyard shapes, rotation
and the anchor-vs-bbox distinction stay defined in exactly one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..kicad import extract_parts, load_board
from ..units import mm
from .heights import HEIGHTS_NM, height_for

__all__ = [
    "DEFAULT_BOARD_THICKNESS_NM",
    "PartExtent",
    "BoardEnvelope",
    "board_envelope",
    "find_part",
]

#: Substrate thickness used when the board file does not state one.
DEFAULT_BOARD_THICKNESS_NM: int = mm(1.6)

_MM_TO_NM = 1_000_000


@dataclass(frozen=True)
class PartExtent:
    """One footprint's absolute courtyard extent plus its table height."""

    ref: str
    x_min_nm: int
    y_min_nm: int
    x_max_nm: int
    y_max_nm: int  # KiCad Y-down, absolute board coordinates
    height_nm: int
    #: True when the heights table had no entry for this footprint's class;
    #: the verifier surfaces this as a warning rather than guessing silently.
    height_default: bool


@dataclass(frozen=True)
class BoardEnvelope:
    """Everything the enclosure emitter and verifier need to know."""

    outline_nm: tuple[tuple[int, int], ...]  # Edge.Cuts polygon, KiCad Y-down
    x_min_nm: int
    y_min_nm: int
    x_max_nm: int
    y_max_nm: int
    thickness_nm: int  # board substrate
    parts: tuple[PartExtent, ...]
    max_height_nm: int


def _nm_point(x_mm: float, y_mm: float) -> tuple[int, int]:
    return mm(x_mm), mm(y_mm)


def _outline_geometry(
    board,
) -> tuple[list[tuple[int, int]], list[tuple[tuple[int, int], tuple[int, int]]]]:
    """All ``Edge.Cuts`` bounding points, plus the line segments among them.

    Points bound the board (bbox); segments are kept separately so a
    plain-lines outline (which is what :func:`silkscreen.kicad.set_board_outline`
    writes) can be chained back into an ordered polygon.
    """
    points: list[tuple[int, int]] = []
    segments: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for item in getattr(board, "graphicItems", []) or []:
        if getattr(item, "layer", None) != "Edge.Cuts":
            continue
        kind = type(item).__name__
        start = getattr(item, "start", None)
        end = getattr(item, "end", None)
        if kind == "GrLine" and start is not None and end is not None:
            a = _nm_point(start.X, start.Y)
            b = _nm_point(end.X, end.Y)
            points += [a, b]
            segments.append((a, b))
        elif kind == "GrRect" and start is not None and end is not None:
            for px in (start.X, end.X):
                for py in (start.Y, end.Y):
                    points.append(_nm_point(px, py))
        elif kind == "GrCircle":
            center = getattr(item, "center", None)
            if center is not None and end is not None:
                radius = ((end.X - center.X) ** 2 + (end.Y - center.Y) ** 2) ** 0.5
                points += [
                    _nm_point(center.X - radius, center.Y - radius),
                    _nm_point(center.X + radius, center.Y + radius),
                ]
        elif getattr(item, "coordinates", None):
            points += [_nm_point(pt.X, pt.Y) for pt in item.coordinates]
        else:
            # Arcs and anything else: bound by the stored points. An arc's
            # bulge past ``mid`` is not recovered here; generated outlines are
            # rectangles, and a hand-drawn arc still contributes its samples.
            for pt in (start, getattr(item, "mid", None), end):
                if pt is not None:
                    points.append(_nm_point(pt.X, pt.Y))
    return points, segments


def _chain_segments(
    segments: list[tuple[tuple[int, int], tuple[int, int]]],
) -> list[tuple[int, int]] | None:
    """Order line segments into one connected polygon, or ``None`` if they
    do not chain (gaps, branches, or several disjoint loops)."""
    if not segments:
        return None
    remaining = list(segments)
    first = remaining.pop(0)
    ordered = [first[0], first[1]]
    while remaining:
        tail = ordered[-1]
        match = None
        for index, (a, b) in enumerate(remaining):
            if a == tail:
                ordered.append(b)
                match = index
                break
            if b == tail:
                ordered.append(a)
                match = index
                break
        if match is None:
            return None
        remaining.pop(match)
    if ordered[0] == ordered[-1]:
        ordered.pop()
    return ordered


def _dedupe(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for pt in points:
        if pt not in seen:
            seen.add(pt)
            out.append(pt)
    return out


def board_envelope(
    path: str | Path, *, heights: Mapping[str, int] | None = None
) -> BoardEnvelope:
    """Measure the board at ``path`` for enclosure generation.

    ``heights`` overlays the built-in class table: an entry keyed by an exact
    reference designator (``"J1"``) pins that one part's height; any other key
    is treated as an additional footprint class (longest-match, like
    :data:`~silkscreen.enclosure.heights.HEIGHTS_NM`).

    Raises ``ValueError`` when the board has no ``Edge.Cuts`` outline -- a
    board without a boundary cannot be encased, the same convention that makes
    :func:`silkscreen.kicad.set_board_outline` mandatory for edge constraints.
    """
    board = load_board(path)

    points, segments = _outline_geometry(board)
    if not points:
        raise ValueError(
            f"{path} has no Edge.Cuts outline; a board without a boundary "
            "cannot be encased. Draw one (or generate the board through the "
            "pipeline, which always writes an outline)."
        )
    outline = _chain_segments(segments) or _dedupe(points)

    x_min = min(p[0] for p in points)
    y_min = min(p[1] for p in points)
    x_max = max(p[0] for p in points)
    y_max = max(p[1] for p in points)

    thickness_mm = getattr(getattr(board, "general", None), "thickness", None)
    thickness_nm = mm(thickness_mm) if thickness_mm else DEFAULT_BOARD_THICKNESS_NM

    overrides = dict(heights or {})
    class_table = dict(HEIGHTS_NM)
    ref_overrides: dict[str, int] = {}
    infos = extract_parts(board)
    known_refs = {info.ref for info in infos}
    for key, value in overrides.items():
        if key in known_refs:
            ref_overrides[key] = value
        else:
            class_table[key] = value

    parts: list[PartExtent] = []
    # ``extract_parts`` walks ``board.footprints`` in order, so zipping the
    # two recovers each footprint's anchor for its measured local extent.
    for fp, info in zip(board.footprints, infos, strict=True):
        anchor_x = mm(fp.position.X)
        anchor_y = mm(fp.position.Y)
        name = getattr(fp, "libId", "") or getattr(fp, "entryName", "") or info.ref
        if info.ref in ref_overrides:
            height, was_default = ref_overrides[info.ref], False
        else:
            height, was_default = height_for(name, class_table)
        parts.append(
            PartExtent(
                ref=info.ref,
                x_min_nm=anchor_x + info.min_x_nm,
                y_min_nm=anchor_y + info.min_y_nm,
                x_max_nm=anchor_x + info.max_x_nm,
                y_max_nm=anchor_y + info.max_y_nm,
                height_nm=height,
                height_default=was_default,
            )
        )

    return BoardEnvelope(
        outline_nm=tuple(outline),
        x_min_nm=x_min,
        y_min_nm=y_min,
        x_max_nm=x_max,
        y_max_nm=y_max,
        thickness_nm=thickness_nm,
        parts=tuple(parts),
        max_height_nm=max((p.height_nm for p in parts), default=0),
    )


def find_part(envelope: BoardEnvelope, ref: str) -> PartExtent | None:
    """The extent for ``ref``, or ``None`` when the board has no such part.

    Callers that need the part (cutout resolution) turn ``None`` into a
    ``CutoutError`` -- a hard error per the ``edge_refs`` convention, never a
    silent no-op.
    """
    for part in envelope.parts:
        if part.ref == ref:
            return part
    return None
