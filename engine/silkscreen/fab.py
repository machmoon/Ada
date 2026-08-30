"""Fabrication output: Gerber, Excellon, BOM and pick-and-place.

A ``.kicad_pcb`` is a design file, not an order. Nobody at a board house opens
one: what they take is a set of RS-274X Gerbers, a drill file, a bill of
materials and a pick-and-place list. Without those, "the pipeline produced a
board" stops one step short of a board anyone can actually buy.

This module renders a :class:`~silkscreen.board.BoardResult` straight into that
set, with no KiCad process and no plotting library, for the same reason the rest
of the engine has neither: the geometry is already exact, so re-deriving it
through a third tool can only lose precision.

Two coordinate frames meet here and only one of them is KiCad's.
:func:`~silkscreen.board.emit_kicad_pcb` flips Y on the way out because KiCad's
canvas is Y-down; **Gerber is Y-up**, the same handedness as the solver, so
nothing is flipped here. Instead every emitted coordinate is shifted by the
outline margin, which puts the board's bottom-left corner at exactly (0, 0):
some fabs reject a file containing negative coordinates outright.

Everything stays in integer nanometres until the final ``str()``. That is more
than house style here. Under ``%FSLAX46Y46*%`` with ``%MOMM*%`` a coordinate has
four integer and six decimal digits of a millimetre, so one Gerber coordinate
unit *is* one nanometre -- the engine's integer-nm values are the Gerber
coordinates, written verbatim, with no scaling step left to get wrong.

Output is a pure function of the board -- no timestamps, no UUIDs, no locale --
so regenerating an unchanged design produces byte-identical files.

Two limits are worth stating rather than discovering at a fab. Bottom-side
geometry is emitted at the same coordinates as the top, which is the Gerber
convention of one top-viewed frame shared by every layer; whether that agrees
with how KiCad renders a mirrored back-side footprint has not been verified
against a running KiCad, and it cannot bite today because nothing in the
pipeline enables ``two_sided`` placement. Pads are emitted as plain rectangles
where the ``.kicad_pcb`` writes ``roundrect``: standard Gerber has no roundrect
aperture, and the corners differ by a fraction of a millimetre.
"""

from __future__ import annotations

from dataclasses import dataclass

from .board import BoardResult, PlacedPart
from .packing import Layer
from .units import NM_PER_MM, mm

__all__ = [
    "GENERATOR",
    "FabLayer",
    "gerber_copper",
    "gerber_mask",
    "gerber_paste",
    "gerber_silkscreen",
    "gerber_outline",
    "excellon_drill",
    "bom_csv",
    "cpl_csv",
    "fab_files",
]

#: Name written into every file's generator attribute.
GENERATOR = "silkscreen"

#: The outline margin :func:`silkscreen.board.emit_kicad_pcb` draws around the
#: placement. The two must agree: this is the same physical rectangle, emitted
#: twice in two formats, so a change there is a change here.
_MARGIN_NM = mm(2.0)

#: Stroke widths: thin enough not to distort the geometry they trace, wide
#: enough to clear every fab's minimum-feature check.
_OUTLINE_WIDTH_NM = mm(0.1)
_SILK_WIDTH_NM = mm(0.12)

#: Soldermask expansion per side. 0.051 mm (2 mil) is the industry-default
#: opening enlargement: enough that a small registration error still leaves the
#: pad clear, small enough not to bridge the mask web between fine-pitch pads.
_MASK_EXPANSION_NM = 51_000

#: Aperture numbering starts at D10 because D00-D09 are reserved for Gerber's
#: own operation codes (D01 draw, D02 move, D03 flash).
_FIRST_APERTURE = 10


@dataclass(frozen=True)
class FabLayer:
    """One manufacturing file: its conventional name and its full text."""

    filename: str
    content: str


# ------------------------------------------------------------------ formatting


def _mm_text(nm: int) -> str:
    """Integer nanometres -> a trimmed decimal millimetre string.

    Done with integer division rather than ``nm / NM_PER_MM`` so an aperture
    size can never pick up a float artefact like ``0.8999999999999999`` -- which
    a strict Gerber reader is entitled to reject and a lax one will round.
    """
    sign = "-" if nm < 0 else ""
    whole, frac = divmod(abs(nm), NM_PER_MM)
    text = f"{whole}.{frac:06d}".rstrip("0").rstrip(".")
    return f"{sign}{text}"


def _mm4(nm: int) -> str:
    """Integer nanometres -> millimetres at exactly 4 decimals, rounded half-up.

    Four decimals is 0.1 um, finer than any pick-and-place tolerance and the
    resolution assembly importers expect. Kept in integers so the rounding is
    the same on every platform.
    """
    sign = "-" if nm < 0 else ""
    tenths_of_um = (abs(nm) + 50) // 100
    return f"{sign}{tenths_of_um // 10_000}.{tenths_of_um % 10_000:04d}"


def _csv_field(value: str) -> str:
    """Quote a CSV field only when it needs it, doubling embedded quotes."""
    if any(char in value for char in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv_row(fields: list[str]) -> str:
    return ",".join(_csv_field(field) for field in fields)


def _natural_key(ref: str) -> tuple[str, int, str]:
    """Sort C2 before C10, which plain string ordering does not."""
    prefix = ref.rstrip("0123456789")
    digits = ref[len(prefix) :]
    return (prefix, int(digits) if digits else -1, ref)


# -------------------------------------------------------------------- geometry


def _is_bottom(part: PlacedPart) -> bool:
    """Whether a part sits on the underside.

    Tested against ``BOTTOM`` rather than ``TOP`` on purpose: a leftover
    :attr:`~silkscreen.packing.Layer.EITHER` means the solver never chose a
    side, and putting it on top keeps its copper in the output instead of
    dropping the pads from both sides where nobody would notice.
    """
    return part.layer is Layer.BOTTOM


def _anchor_nm(part: PlacedPart) -> tuple[int, int]:
    """The footprint anchor in shifted Gerber coordinates.

    ``PlacedPart.x_nm``/``y_nm`` locate the courtyard's bottom-left corner in
    the solver's Y-up frame, and ``courtyard_w_nm``/``courtyard_h_nm`` are half
    extents, so the anchor is one half extent in along each axis. This is the
    same expression :func:`silkscreen.board.emit_kicad_pcb` uses; it differs
    there only by the ``board.height_nm -`` that turns Y-up into KiCad's Y-down.
    """
    fp = part.footprint
    return (
        part.x_nm + fp.courtyard_w_nm + _MARGIN_NM,
        part.y_nm + fp.courtyard_h_nm + _MARGIN_NM,
    )


def _rotate_offset(dx_nm: int, dy_nm: int, rotated: bool) -> tuple[int, int]:
    """Apply a part's 90-degree rotation to an anchor-relative offset.

    KiCad is handed the angle and rotates the footprint itself. Gerber has no
    concept of a part -- only flashes and strokes at absolute coordinates -- so
    the rotation has to be baked into every coordinate before it is written.

    A counter-clockwise quarter turn in this Y-up frame is (x, y) -> (-y, x).
    That is the same rotation the placer applies in ``packing.endpoint``, where
    it reads as (ox, oy) -> (H - oy, ox) only because those offsets are measured
    from the box's corner rather than from its centre: shifting a corner-relative
    (ox, oy) to centre-relative (ox - W/2, oy - H/2), turning it to
    (H/2 - oy, ox - W/2), then re-cornering against the rotated H x W box by
    adding (H/2, W/2) gives exactly (H - oy, ox).
    """
    return (-dy_nm, dx_nm) if rotated else (dx_nm, dy_nm)


def _rotate_size(w_nm: int, h_nm: int, rotated: bool) -> tuple[int, int]:
    """A quarter turn swaps an axis-aligned rectangle's width and height."""
    return (h_nm, w_nm) if rotated else (w_nm, h_nm)


@dataclass(frozen=True)
class _Flash:
    """A pad reduced to what a Gerber aperture can say: a centred rectangle."""

    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int


def _flashes(
    board: BoardResult, *, bottom: bool, grow_nm: int = 0
) -> list[_Flash]:
    """Every pad on one side, in board coordinates, optionally grown per side."""
    out: list[_Flash] = []
    for part in board.parts:
        if _is_bottom(part) != bottom:
            continue
        anchor_x, anchor_y = _anchor_nm(part)
        for pad in part.footprint.pads:
            off_x, off_y = _rotate_offset(pad.x_nm, pad.y_nm, part.rotated)
            width, height = _rotate_size(pad.w_nm, pad.h_nm, part.rotated)
            out.append(
                _Flash(
                    x_nm=anchor_x + off_x,
                    y_nm=anchor_y + off_y,
                    w_nm=width + 2 * grow_nm,
                    h_nm=height + 2 * grow_nm,
                )
            )
    return out


# ---------------------------------------------------------------- gerber writer


class _GerberFile:
    """Builder for one RS-274X layer.

    Apertures are assigned on first use and deduplicated by (shape, size), but
    the whole table has to appear before any command that selects from it, so
    the drawing commands are buffered and the header rendered last.
    """

    def __init__(self, file_function: str) -> None:
        self._file_function = file_function
        self._apertures: dict[tuple[str, int, int], int] = {}
        self._definitions: list[str] = []
        self._body: list[str] = []
        self._selected: int | None = None

    def _aperture(self, key: tuple[str, int, int], definition: str) -> int:
        code = self._apertures.get(key)
        if code is None:
            code = _FIRST_APERTURE + len(self._apertures)
            self._apertures[key] = code
            self._definitions.append(f"%ADD{code}{definition}*%")
        return code

    def rect(self, w_nm: int, h_nm: int) -> int:
        """A rectangular aperture, used to flash pads."""
        return self._aperture(
            ("R", w_nm, h_nm), f"R,{_mm_text(w_nm)}X{_mm_text(h_nm)}"
        )

    def circle(self, diameter_nm: int) -> int:
        """A round aperture, used as the pen for stroked outlines."""
        key = ("C", diameter_nm, diameter_nm)
        return self._aperture(key, f"C,{_mm_text(diameter_nm)}")

    def select(self, code: int) -> None:
        """Make ``code`` current, skipping a redundant reselect."""
        if self._selected != code:
            self._body.append(f"D{code}*")
            self._selected = code

    def flash(self, x_nm: int, y_nm: int) -> None:
        self._body.append(f"X{x_nm}Y{y_nm}D03*")

    def line(self, x0_nm: int, y0_nm: int, x1_nm: int, y1_nm: int) -> None:
        self._body.append(f"X{x0_nm}Y{y0_nm}D02*")
        self._body.append(f"X{x1_nm}Y{y1_nm}D01*")

    def render(self) -> str:
        # Deliberately no %TF.CreationDate: a timestamp is the one attribute
        # that would stop identical designs producing identical files.
        out = [
            "%FSLAX46Y46*%",
            "%MOMM*%",
            f"G04 Generated by {GENERATOR}*",
            "G04 Format 4.6 in mm makes one coordinate unit exactly 1 nm, so*",
            "G04 the engine's integer-nanometre geometry is written verbatim*",
            f"%TF.GenerationSoftware,{GENERATOR},{GENERATOR}*%",
            f"%TF.FileFunction,{self._file_function}*%",
            "%TF.FilePolarity,Positive*%",
            "%LPD*%",
        ]
        out += self._definitions
        out.append("G01*")
        out += self._body
        out.append("M02*")
        return "\n".join(out) + "\n"


def _rectangle(
    gerber: _GerberFile, x0_nm: int, y0_nm: int, x1_nm: int, y1_nm: int
) -> None:
    """Stroke a closed axis-aligned rectangle as four separate segments."""
    corners = [(x0_nm, y0_nm), (x1_nm, y0_nm), (x1_nm, y1_nm), (x0_nm, y1_nm)]
    for i in range(4):
        start_x, start_y = corners[i]
        end_x, end_y = corners[(i + 1) % 4]
        gerber.line(start_x, start_y, end_x, end_y)


def _flashed_layer(
    board: BoardResult, file_function: str, *, bottom: bool, grow_nm: int = 0
) -> str:
    """Render one side's pads as flashes -- the shape copper, mask and paste share."""
    gerber = _GerberFile(file_function)
    for flash in _flashes(board, bottom=bottom, grow_nm=grow_nm):
        gerber.select(gerber.rect(flash.w_nm, flash.h_nm))
        gerber.flash(flash.x_nm, flash.y_nm)
    return gerber.render()


# ---------------------------------------------------------------------- layers


def gerber_copper(
    board: BoardResult, *, bottom: bool = False, routes: object | None = None
) -> str:
    """Copper for one side: every pad on that side, flashed, plus any traces.

    ``routes`` is an optional :class:`silkscreen.route.RouteResult`. When given,
    its segments are stroked onto the top copper after the pads, which is what
    turns a placed board into a connected one. It is optional because routing
    is a separate, later step: a board with no routes still produces valid
    copper, it just has no connections in it.

    ``L1``/``L2`` name the two copper layers of the stack-up that
    :func:`silkscreen.board.emit_kicad_pcb` declares (F.Cu and B.Cu, nothing
    between them).

    Pads are emitted as plain rectangles. KiCad writes them as rounded
    rectangles with a 0.25 corner ratio, which in Gerber needs an aperture
    macro; the square-cornered approximation is within the corner radius
    everywhere and is universally accepted, but it is an approximation.
    """
    function = "Copper,L2,Bot" if bottom else "Copper,L1,Top"
    gerber = _GerberFile(function)
    for flash in _flashes(board, bottom=bottom):
        gerber.select(gerber.rect(flash.w_nm, flash.h_nm))
        gerber.flash(flash.x_nm, flash.y_nm)
    # The router is single-layer, so its segments belong on the top side only.
    if routes is not None and not bottom:
        for seg in getattr(routes, "segments", ()):
            gerber.select(gerber.circle(seg.width_nm))
            gerber.line(
                seg.x1_nm + _MARGIN_NM,
                seg.y1_nm + _MARGIN_NM,
                seg.x2_nm + _MARGIN_NM,
                seg.y2_nm + _MARGIN_NM,
            )
    return gerber.render()


def gerber_mask(
    board: BoardResult,
    *,
    bottom: bool = False,
    expansion_nm: int = _MASK_EXPANSION_NM,
) -> str:
    """Soldermask openings for one side, each pad grown by ``expansion_nm``.

    The expansion is per side, so a pad's opening is ``w + 2 * expansion_nm``
    wide. The file is a *positive* of the openings, which is the convention
    every fab's CAM step expects; it inverts it against the mask coat itself.
    """
    function = "Soldermask,Bot" if bottom else "Soldermask,Top"
    return _flashed_layer(board, function, bottom=bottom, grow_nm=expansion_nm)


def gerber_paste(board: BoardResult, *, bottom: bool = False) -> str:
    """Stencil apertures for one side, at 1:1 with the pads.

    No area reduction is applied. Reduction is a stencil-house decision that
    depends on foil thickness and the specific part, and guessing one here
    would silently change how much solder every joint gets.
    """
    function = "Paste,Bot" if bottom else "Paste,Top"
    return _flashed_layer(board, function, bottom=bottom)


def gerber_silkscreen(board: BoardResult, *, bottom: bool = False) -> str:
    """Component body outlines for one side, stroked with a 0.12 mm pen.

    Parts with no body extents contribute nothing: a zero-size rectangle would
    be four zero-length strokes, which is four dots of ink on the board.
    """
    function = "Legend,Bot" if bottom else "Legend,Top"
    gerber = _GerberFile(function)
    for part in board.parts:
        if _is_bottom(part) != bottom:
            continue
        fp = part.footprint
        if not fp.body_w_nm or not fp.body_h_nm:
            continue
        half_w, half_h = _rotate_size(fp.body_w_nm, fp.body_h_nm, part.rotated)
        anchor_x, anchor_y = _anchor_nm(part)
        gerber.select(gerber.circle(_SILK_WIDTH_NM))
        _rectangle(
            gerber,
            anchor_x - half_w,
            anchor_y - half_h,
            anchor_x + half_w,
            anchor_y + half_h,
        )
    return gerber.render()


def gerber_outline(board: BoardResult) -> str:
    """The board profile: the rectangle ``emit_kicad_pcb`` puts on Edge.Cuts.

    ``NP`` marks the profile as non-plated -- a plain routed edge, not a
    plated-through castellation.
    """
    gerber = _GerberFile("Profile,NP")
    gerber.select(gerber.circle(_OUTLINE_WIDTH_NM))
    # emit_kicad_pcb spans (-margin, -margin) to (width + margin, height +
    # margin). The +margin shift every coordinate here carries slides that same
    # rectangle to (0, 0)..(width + 2*margin, height + 2*margin).
    _rectangle(
        gerber,
        0,
        0,
        board.width_nm + 2 * _MARGIN_NM,
        board.height_nm + 2 * _MARGIN_NM,
    )
    return gerber.render()


def excellon_drill(board: BoardResult) -> str:
    """A valid but empty Excellon file.

    Every footprint this engine generates is SMD and nothing here places a via,
    so the design genuinely has no holes. Fabs still expect a drill file in the
    package, and an empty well-formed one is the honest answer: inventing a hole
    to make the file look populated would put a hole in the board.

    ``board`` is unused and stays in the signature so every emitter in this
    module can be called the same way.
    """
    return (
        "\n".join(
            [
                "M48",
                f"; Generated by {GENERATOR}",
                "; No holes: every pad in this design is SMD and no vias are"
                " placed.",
                "METRIC",
                "%",
                "M30",
            ]
        )
        + "\n"
    )


# ------------------------------------------------------------------ assembly


def bom_csv(board: BoardResult) -> str:
    """Bill of materials, one row per distinct (value, package) pair.

    The column names are the ones JLCPCB, PCBWay and Altium all read, so the
    file can be uploaded without being reshaped first.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for part in board.parts:
        groups.setdefault((part.value, part.footprint.name), []).append(part.ref)

    rows = ["Comment,Designator,Footprint,Quantity"]
    for (value, footprint), refs in sorted(groups.items()):
        designators = ";".join(sorted(refs, key=_natural_key))
        rows.append(_csv_row([value, designators, footprint, str(len(refs))]))
    return "\n".join(rows) + "\n"


def cpl_csv(board: BoardResult) -> str:
    """Pick-and-place list: one anchor position per part, in millimetres.

    The coordinates are the same shifted, Y-up anchors the Gerbers use, so the
    assembler's origin lines up with the copper without anyone having to know
    about the outline margin.
    """
    rows = ["Designator,Mid X,Mid Y,Layer,Rotation"]
    for part in sorted(board.parts, key=lambda p: _natural_key(p.ref)):
        x_nm, y_nm = _anchor_nm(part)
        rows.append(
            _csv_row(
                [
                    part.ref,
                    _mm4(x_nm),
                    _mm4(y_nm),
                    "Bottom" if _is_bottom(part) else "Top",
                    "90" if part.rotated else "0",
                ]
            )
        )
    return "\n".join(rows) + "\n"


def fab_files(
    board: BoardResult, *, routes: object | None = None
) -> list[FabLayer]:
    """Every file a board house needs, in a fixed order.

    Both sides are always present. A side with no parts yields an empty but
    valid Gerber, which is what fabs expect: a missing file reads as an
    oversight and stalls the order, whereas an empty one reads as "no copper
    here" and passes CAM untouched.
    """
    return [
        FabLayer("silkscreen-F_Cu.GTL", gerber_copper(board, routes=routes)),
        FabLayer("silkscreen-B_Cu.GBL", gerber_copper(board, bottom=True)),
        FabLayer("silkscreen-F_Mask.GTS", gerber_mask(board)),
        FabLayer("silkscreen-B_Mask.GBS", gerber_mask(board, bottom=True)),
        FabLayer("silkscreen-F_Paste.GTP", gerber_paste(board)),
        FabLayer("silkscreen-B_Paste.GBP", gerber_paste(board, bottom=True)),
        FabLayer("silkscreen-F_Silkscreen.GTO", gerber_silkscreen(board)),
        FabLayer(
            "silkscreen-B_Silkscreen.GBO",
            gerber_silkscreen(board, bottom=True),
        ),
        FabLayer("silkscreen-Edge_Cuts.GKO", gerber_outline(board)),
        FabLayer("silkscreen-NPTH.DRL", excellon_drill(board)),
        FabLayer("silkscreen-BOM.csv", bom_csv(board)),
        FabLayer("silkscreen-CPL.csv", cpl_csv(board)),
    ]
