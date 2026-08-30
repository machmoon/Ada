"""Draw the board, and draw the findings on top of it.

A list of findings is a list of things to go and look for. A picture of the
board with the findings marked where they are is the review a hardware
engineer can act on without opening KiCad: the two overlapping courtyards are
visibly the two overlapping courtyards.

Coordinates come out of :mod:`.geometry` in KiCad's frame -- origin top-left,
Y down -- and SVG uses the same handedness, so this renderer performs **no Y
flip**. That is deliberate and load-bearing: the one flip in this project
belongs to the placer boundary, and a second one here would put every marker
in a mirrored position while still looking like a plausible board.

Severity is carried by colour; origin is carried by line style. A proven
finding is outlined solid, a suggested one dashed, with the same distinction
repeated in its badge, so the picture keeps the split the report makes.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from ..units import NM_PER_MM
from .effort import Effort
from .findings import Finding, Origin, Severity
from .geometry import AuditBoard, Rect
from .result import AuditResult

__all__ = ["render_svg", "SEVERITY_COLOUR"]

SEVERITY_COLOUR = {
    Severity.BLOCKER: "#ff5a5f",
    Severity.MARGINAL: "#ffb020",
    Severity.NOTE: "#4cc2ff",
}

#: The board is a board in either theme, the same decision the web board well
#: made: substrate dark, copper warm, silkscreen white.
_SUBSTRATE = "#132018"
_EDGE = "#8fd6a8"
_F_CU = "#c8623a"
_B_CU = "#3a6fc8"
_PAD = "#e8b23a"
_PAD_BOTTOM = "#7f93c9"
_SILK = "#e9eef2"
_COURTYARD = "#5d7f6a"
_TEXT = "#dfe7ea"


def _mm(value_nm: float) -> float:
    return value_nm / NM_PER_MM


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _rect(
    box: Rect, *, fill="none", stroke="none", width=0.05, dash="", extra=""
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{_fmt(_mm(box.x0))}" y="{_fmt(_mm(box.y0))}" '
        f'width="{_fmt(_mm(box.width_nm))}" height="{_fmt(_mm(box.height_nm))}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{_fmt(width)}"'
        f"{dash_attr}{extra}/>"
    )


def _line(x0, y0, x1, y1, *, stroke, width, cap="round", extra="") -> str:
    return (
        f'<line x1="{_fmt(_mm(x0))}" y1="{_fmt(_mm(y0))}" '
        f'x2="{_fmt(_mm(x1))}" y2="{_fmt(_mm(y1))}" stroke="{stroke}" '
        f'stroke-width="{_fmt(width)}" stroke-linecap="{cap}"{extra}/>'
    )


def _text(x, y, content, *, size=0.9, fill=_TEXT, anchor="middle", weight="400") -> str:
    return (
        f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-size="{_fmt(size)}" '
        f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
        f"{escape(content)}</text>"
    )


def _slider_svg(level: Effort, x: float, y: float) -> str:
    """The thinking slider, drawn where the picture can carry it too."""
    order = [Effort.QUICK, Effort.STANDARD, Effort.DEEP]
    span = 12.0
    out = [
        f'<line x1="{_fmt(x)}" y1="{_fmt(y)}" x2="{_fmt(x + span)}" y2="{_fmt(y)}" '
        f'stroke="{_COURTYARD}" stroke-width="0.25" stroke-linecap="round"/>'
    ]
    for index, lvl in enumerate(order):
        cx = x + span * index / (len(order) - 1)
        here = lvl is level
        out.append(
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(y)}" r="{"0.85" if here else "0.5"}" '
            f'fill="{"#7ee2a8" if here else _SUBSTRATE}" stroke="{_COURTYARD}" '
            f'stroke-width="0.18"/>'
        )
        out.append(
            _text(cx, y + 2.2, lvl.value, size=0.85,
                  fill=_TEXT if here else _COURTYARD,
                  weight="600" if here else "400")
        )
    return "".join(out)


def _badge(finding: Finding, x: float, y: float) -> str:
    colour = SEVERITY_COLOUR[finding.severity]
    proven = finding.origin is Origin.PROVEN
    fill = colour if proven else _SUBSTRATE
    text_fill = "#10171a" if proven else colour
    dash = "" if proven else ' stroke-dasharray="0.5 0.35"'
    return (
        f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="1.35" fill="{fill}" '
        f'stroke="{colour}" stroke-width="0.28"{dash}/>'
        + _text(x, y + 0.42, finding.id, size=1.15, fill=text_fill, weight="700")
    )


def render_svg(
    result: AuditResult, *, px_per_mm: float = 14.0, show_legend: bool = True
) -> str:
    """One self-contained SVG: the board, then every located finding on it."""
    board: AuditBoard = result.board
    extent = board.extent
    pad_mm = 6.0
    legend_mm = 16.0 if show_legend else 0.0

    x0 = _mm(extent.x0) - pad_mm
    y0 = _mm(extent.y0) - pad_mm
    w = _mm(extent.width_nm) + 2 * pad_mm
    h = _mm(extent.height_nm) + 2 * pad_mm + legend_mm

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_fmt(x0)} {_fmt(y0)} '
        f'{_fmt(w)} {_fmt(h)}" width="{int(w * px_per_mm)}" '
        f'height="{int(h * px_per_mm)}" role="img" '
        f'aria-label="Reviewed board with findings marked">'
    )
    out.append(
        f'<rect x="{_fmt(x0)}" y="{_fmt(y0)}" width="{_fmt(w)}" height="{_fmt(h)}" '
        f'fill="#0b1210"/>'
    )

    # Substrate and outline.
    if board.outline is not None:
        out.append(_rect(board.outline, fill=_SUBSTRATE, stroke=_EDGE, width=0.2))
    else:
        out.append(_rect(extent, fill=_SUBSTRATE, stroke="#5a3030", width=0.2,
                         dash="1 0.6"))

    # Bottom copper first, so top copper reads as nearer the eye.
    out.append('<g id="copper">')
    for track in board.tracks:
        colour = _B_CU if track.side == "B" else _F_CU
        out.append(
            _line(track.x0, track.y0, track.x1, track.y1, stroke=colour,
                  width=max(_mm(track.width_nm), 0.08))
        )
    for vx, vy, size, _net in board.vias:
        out.append(
            f'<circle cx="{_fmt(_mm(vx))}" cy="{_fmt(_mm(vy))}" '
            f'r="{_fmt(_mm(size) / 2)}" fill="{_PAD}" opacity="0.85"/>'
        )
    out.append("</g>")

    out.append('<g id="parts">')
    for part in board.parts:
        out.append(
            f'<g data-ref="{escape(part.ref)}" class="part">'
        )
        if part.courtyard is not None:
            out.append(
                _rect(part.courtyard, stroke=_COURTYARD, width=0.06, dash="0.5 0.4")
            )
        for seg in part.silk:
            out.append(
                _line(seg.x0, seg.y0, seg.x1, seg.y1, stroke=_SILK,
                      width=max(_mm(seg.width_nm), 0.08), extra=' opacity="0.55"')
            )
        for pad in part.pads:
            fill = _PAD_BOTTOM if pad.side == "B" and not pad.through_hole else _PAD
            out.append(_rect(pad.rect, fill=fill))
        cx, cy = part.centre
        out.append(
            _text(_mm(cx), _mm(cy) + 0.35, part.ref, size=1.1, fill=_SILK,
                  weight="600")
        )
        out.append("</g>")
    out.append("</g>")

    # Findings last: they must never be drawn under the board.
    out.append('<g id="findings">')
    for finding in result.visible():
        if not finding.located:
            continue
        colour = SEVERITY_COLOUR[finding.severity]
        proven = finding.origin is Origin.PROVEN
        dash = "" if proven else "0.7 0.5"
        out.append(
            f'<g class="finding sev-{finding.severity.value} '
            f'origin-{finding.origin.value}" data-finding="{finding.id}" '
            f'data-refs="{escape(" ".join(finding.refs))}">'
        )
        box = finding.extent
        if box is not None:
            box = box.grown(int(0.35 * NM_PER_MM))
            out.append(
                _rect(box, stroke=colour, width=0.22, dash=dash,
                      extra=' fill="none" opacity="0.95"')
            )
            bx, by = _mm(box.x1), _mm(box.y0)
        else:
            assert finding.point is not None
            bx, by = _mm(finding.point[0]), _mm(finding.point[1])
            out.append(
                f'<circle cx="{_fmt(bx)}" cy="{_fmt(by)}" r="1.8" fill="none" '
                f'stroke="{colour}" stroke-width="0.22"'
                + (f' stroke-dasharray="{dash}"' if dash else "")
                + "/>"
            )
        out.append(f"<title>{escape(finding.id + ': ' + finding.title)}</title>")
        out.append(_badge(finding, bx, by))
        out.append("</g>")
    out.append("</g>")

    if show_legend:
        ly = _mm(extent.y1) + pad_mm + 4.0
        lx = _mm(extent.x0)
        counts = result.counts()
        out.append('<g id="legend">')
        out.append(
            _text(lx, ly - 2.0, f"{result.source.name if result.source else 'board'}"
                  f"  ·  {counts['blocker']} blocker  {counts['marginal']} marginal"
                  f"  {counts['note']} note", size=1.2, anchor="start", weight="600")
        )
        cursor = lx
        for severity, label in (
            (Severity.BLOCKER, "blocker"),
            (Severity.MARGINAL, "marginal"),
            (Severity.NOTE, "note"),
        ):
            out.append(
                f'<rect x="{_fmt(cursor)}" y="{_fmt(ly)}" width="1.4" height="1.4" '
                f'fill="{SEVERITY_COLOUR[severity]}"/>'
            )
            out.append(_text(cursor + 2.0, ly + 1.2, label, size=1.0, anchor="start"))
            cursor += 2.0 + len(label) * 0.75 + 3.0
        out.append(
            f'<rect x="{_fmt(cursor)}" y="{_fmt(ly)}" width="1.4" height="1.4" '
            f'fill="none" stroke="{_TEXT}" stroke-width="0.2"/>'
        )
        out.append(
            _text(cursor + 2.0, ly + 1.2, "solid = proven · dashed = suggested",
                  size=1.0, anchor="start")
        )
        out.append(_slider_svg(result.profile.level, lx + 1.0, ly + 6.0))
        out.append("</g>")

    out.append("</svg>")
    return "\n".join(out)
