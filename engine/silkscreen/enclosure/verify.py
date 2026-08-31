"""Offline fit verification -- the receipt behind every generated enclosure.

``verify_fit`` never renders anything and never calls a model: it re-derives
the same dimensions :mod:`.emit` will write and checks them against the
measured board. Design rules copied from :mod:`silkscreen.spice`:

* **Nothing returns a quiet zero.** Every failure raises a specific error --
  :class:`CavityFitError` carrying signed per-axis margins,
  :class:`CutoutError` naming the offending cutout (an absent ref is a hard
  error per the ``edge_refs`` convention), :class:`WallError` for
  unprintable walls.
* **Warnings are visible, and ``strict=True`` promotes them to errors** (the
  ``Testbench(strict=True)`` precedent) -- that is what the agent's repair
  loop runs, so a defaulted component height or a tight clearance feeds the
  loop instead of shipping silently.

All checks are in integer nanometres; ``params_mm`` is display-only and comes
from :func:`silkscreen.enclosure.emit.scad_params` so the receipt shows the
exact numbers the ``.scad`` file carries.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..units import mm, to_mm
from .board_shape import BoardEnvelope
from .emit import STANDOFF_HEIGHT_NM, opening_extent, scad_params
from .errors import (
    CavityFitError,
    CutoutError,
    EnclosureValidationError,
    WallError,
)
from .heights import DEFAULT_HEIGHT_NM
from .ir import MIN_WALL_NM, EnclosureSpec

__all__ = ["FitReport", "verify_fit"]

#: Below this board-to-cavity clearance the fit report warns: real boards
#: have routing tolerance and the printer has shrinkage.
TIGHT_CLEARANCE_NM: int = mm(0.5)

#: A side cutout whose part sits further than this from the named wall gets a
#: warning -- the opening will be a tunnel to nowhere.
FAR_FROM_FACE_NM: int = mm(5.0)


@dataclass(frozen=True)
class FitReport:
    margins_nm: dict[str, int]  # signed, per axis -- the receipt
    warnings: tuple[str, ...]
    params_mm: dict[str, float]  # the emitted parameters, for display


def _overhang_margins(spec: EnclosureSpec, envelope: BoardEnvelope) -> dict[str, int]:
    """Signed margin per axis between the cavity and what it must contain.

    The cavity is sized from the *outline* bbox plus clearance, but parts may
    overhang the outline; the margin is the clearance minus the worst
    overhang, so a part reaching past the cavity wall goes negative.
    """
    over_x = 0
    over_y = 0
    over_z = 0
    for part in envelope.parts:
        over_x = max(
            over_x,
            envelope.x_min_nm - part.x_min_nm,
            part.x_max_nm - envelope.x_max_nm,
        )
        over_y = max(
            over_y,
            envelope.y_min_nm - part.y_min_nm,
            part.y_max_nm - envelope.y_max_nm,
        )
        # Cavity height budgets for max_height_nm; any single part cannot
        # exceed it by construction, but a caller-supplied envelope might.
        over_z = max(over_z, part.height_nm - envelope.max_height_nm)
    return {
        "x": spec.clearance_nm - over_x,
        "y": spec.clearance_nm - over_y,
        "z": spec.clearance_nm - over_z,
    }


def _check_cutouts(
    spec: EnclosureSpec, envelope: BoardEnvelope, warnings: list[str]
) -> None:
    extents: list[tuple[str, str, tuple[tuple[int, int], tuple[int, int]]]] = []
    for cutout in spec.cutouts:
        # Unknown ref or face raises CutoutError inside opening_extent.
        extent = opening_extent(spec, envelope, cutout)
        extents.append((cutout.id, cutout.face, extent))

        part = next(p for p in envelope.parts if p.ref == cutout.ref)
        distance = {
            "left": part.x_min_nm - envelope.x_min_nm,
            "right": envelope.x_max_nm - part.x_max_nm,
            # OpenSCAD front = KiCad max-Y edge (the emit.py frame map).
            "front": envelope.y_max_nm - part.y_max_nm,
            "back": part.y_min_nm - envelope.y_min_nm,
            "top": 0,
        }[cutout.face]
        if distance > FAR_FROM_FACE_NM:
            warnings.append(
                f"cutout {cutout.id}: {cutout.ref} sits {to_mm(distance):.1f} mm "
                f"from the {cutout.face} face; the opening may not reach it"
            )

    for i, (id_a, face_a, ((ax0, ax1), (ay0, ay1))) in enumerate(extents):
        for id_b, face_b, ((bx0, bx1), (by0, by1)) in extents[i + 1:]:
            if face_a != face_b:
                continue
            if min(ax1, bx1) > max(ax0, bx0) and min(ay1, by1) > max(ay0, by0):
                raise CutoutError(
                    f"cutouts {id_a!r} and {id_b!r} overlap on the "
                    f"{face_a} face; merge them or widen one"
                )


def verify_fit(
    spec: EnclosureSpec, envelope: BoardEnvelope, *, strict: bool = False
) -> FitReport:
    """Check that the enclosure ``spec`` actually fits ``envelope``.

    Returns the :class:`FitReport` receipt on success. Raises
    :class:`WallError`, :class:`CutoutError`, or :class:`CavityFitError` on a
    hard failure, and with ``strict=True`` additionally raises
    :class:`EnclosureValidationError` batching every warning (so one repair
    prompt sees them all, the ``parse_circuit_spec`` convention).
    """
    if spec.wall_nm < MIN_WALL_NM:
        raise WallError(
            f"wall {to_mm(spec.wall_nm):.2f} mm is below the printable "
            f"minimum {to_mm(MIN_WALL_NM):.2f} mm"
        )
    if spec.corner_radius_nm > 0 and spec.corner_radius_nm > min(
        spec.wall_nm + spec.clearance_nm + (envelope.x_max_nm - envelope.x_min_nm) // 2,
        spec.wall_nm + spec.clearance_nm + (envelope.y_max_nm - envelope.y_min_nm) // 2,
    ):
        raise WallError(
            f"corner radius {to_mm(spec.corner_radius_nm):.2f} mm exceeds "
            "half the enclosure's smaller side"
        )

    warnings: list[str] = []

    margins = _overhang_margins(spec, envelope)
    if min(margins.values()) < 0:
        pretty = {axis: f"{to_mm(v):+.3f} mm" for axis, v in margins.items()}
        raise CavityFitError(
            f"board does not fit the cavity; per-axis margins {pretty} "
            "(negative = collision)",
            dict(margins),
        )

    _check_cutouts(spec, envelope, warnings)

    if spec.clearance_nm < TIGHT_CLEARANCE_NM:
        warnings.append(
            f"clearance under {to_mm(TIGHT_CLEARANCE_NM):.1f} mm "
            f"({to_mm(spec.clearance_nm):.2f} mm): the board may bind"
        )
    for part in envelope.parts:
        if part.height_default:
            warnings.append(
                f"{part.ref} height defaulted to "
                f"{to_mm(DEFAULT_HEIGHT_NM):.1f} mm (no table entry)"
            )
    if spec.standoffs and spec.lid == "none":
        warnings.append(
            "standoffs raise the board by "
            f"{to_mm(STANDOFF_HEIGHT_NM):.1f} mm in an open case"
        )

    if strict and warnings:
        raise EnclosureValidationError(list(warnings))

    return FitReport(
        margins_nm=margins,
        warnings=tuple(warnings),
        params_mm=scad_params(spec, envelope),
    )
