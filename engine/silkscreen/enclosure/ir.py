"""Validated intermediate representation for an enclosure.

The ``netlist.py`` founding lesson, applied to 3D: a model proposes a JSON
:class:`EnclosureSpec`; nothing emits OpenSCAD until it validates. The model
chooses *style within bounds* — lid type, wall thickness, which connectors get
cutouts — and never types a board millimetre; every measured dimension is
injected by the deterministic emitter from the ``.kicad_pcb``.

Dimensions are **integer nanometres** everywhere inside the IR. The JSON at
the model boundary uses mm floats; the conversion happens exactly once, here,
in :func:`parse_enclosure_spec`.

Ref *existence* is deliberately not checked here — the IR does not know the
board. ``verify.py``'s ``verify_fit`` owns that check (a cutout naming an
absent ref is a hard ``CutoutError`` there, per the ``edge_refs`` convention).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from silkscreen.units import mm

from .errors import EnclosureValidationError

__all__ = [
    "MIN_WALL_NM",
    "DEFAULT_WALL_NM",
    "DEFAULT_CLEARANCE_NM",
    "FACES",
    "LIDS",
    "Cutout",
    "EnclosureSpec",
    "parse_enclosure_spec",
]

#: Printable FDM minimum wall. Anything thinner prints as lace.
MIN_WALL_NM: int = mm(1.2)

#: Default wall thickness when the model does not choose one.
DEFAULT_WALL_NM: int = mm(2.0)

#: Default board-to-cavity clearance.
DEFAULT_CLEARANCE_NM: int = mm(1.0)

#: Faces a cutout may sit on. ``bottom`` is absent on purpose: the board rests
#: on the base and a bottom opening would be under it.
FACES: tuple[str, ...] = ("left", "right", "front", "back", "top")

#: Lid styles the emitter knows how to draw.
LIDS: tuple[str, ...] = ("friction", "screw", "none")

#: Board refs look like ``J1``/``USB3`` — letters then digits, same shape the
#: rest of the engine assigns via ``CircuitSpec.assign_refs``.
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*[0-9]$")

#: Cutout ids: a plain identifier, unique within the spec.
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Characters allowed in an embossed lid label after sanitisation.
_LABEL_OK_RE = re.compile(r"[^A-Za-z0-9 ._+-]")

_MAX_LABEL_LEN = 32


@dataclass(frozen=True)
class Cutout:
    """A rectangular opening for one board part.

    The model names the part and the face; the engine resolves the actual
    geometry from the part's courtyard, so the opening can never disagree with
    the board.
    """

    id: str          # unique within the spec
    ref: str         # board ref, e.g. "J1" — engine resolves geometry
    face: str        # member of FACES
    margin_nm: int   # opening margin around the resolved courtyard interval


@dataclass(frozen=True)
class EnclosureSpec:
    """A validated two-piece case description. All dimensions integer nm."""

    wall_nm: int
    clearance_nm: int
    lid: str                      # member of LIDS
    corner_radius_nm: int         # 0 = square
    cutouts: tuple[Cutout, ...]
    standoffs: bool               # auto-placed by the emitter, not the model
    vents: bool
    label: str | None             # embossed text on the lid, sanitised


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _sanitise_label(value: object, errors: list[str]) -> str | None:
    """Emboss-safe lid text, or ``None``.

    Sanitisation is a transform, not a rejection: strip disallowed characters,
    collapse whitespace, cap the length. Only a non-string non-null value is a
    validation error — a label that sanitises to nothing becomes ``None``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(
            f"'label' must be a string or null, got {type(value).__name__}"
        )
        return None
    cleaned = " ".join(_LABEL_OK_RE.sub("", value).split())
    cleaned = cleaned[:_MAX_LABEL_LEN].rstrip()
    return cleaned or None


def _dim_nm(
    data: dict,
    key: str,
    errors: list[str],
    *,
    default_nm: int,
    minimum_nm: int,
    what: str,
) -> int:
    """Read one mm-float dimension from the JSON, converting to nm once.

    Missing key -> ``default_nm``. A non-numeric value or one below
    ``minimum_nm`` appends to ``errors`` and returns the default so the other
    checks still run and the batch stays complete.
    """
    if key not in data or data[key] is None:
        return default_nm
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{key!r} must be a number (mm), got {value!r}")
        return default_nm
    value_nm = mm(float(value))
    if value_nm < minimum_nm:
        if minimum_nm == 0:
            limit = "not negative"
        elif minimum_nm == 1:
            limit = "positive"
        else:
            limit = f"at least {minimum_nm / 1_000_000} mm"
        errors.append(f"{key!r} is {float(value)} mm; {what} must be {limit}")
        return default_nm
    return value_nm


def parse_enclosure_spec(text: str | dict) -> EnclosureSpec:
    """Parse and validate model output into an :class:`EnclosureSpec`.

    Accepts raw model text (a Markdown code fence is tolerated, dimensions are
    mm floats) or an already-decoded dict. **Collects every failure** into one
    :class:`EnclosureValidationError` so the whole batch goes back to the model
    as a single repair prompt.
    """
    if isinstance(text, str):
        try:
            data = json.loads(_strip_code_fence(text))
        except json.JSONDecodeError as exc:
            raise EnclosureValidationError(
                [f"response is not valid JSON: {exc}"]
            ) from exc
    else:
        data = text

    if not isinstance(data, dict):
        raise EnclosureValidationError(
            [f"expected a JSON object, got {type(data).__name__}"]
        )

    errors: list[str] = []

    wall_nm = _dim_nm(
        data, "wall_mm", errors,
        default_nm=DEFAULT_WALL_NM, minimum_nm=1,
        what="wall thickness",
    )
    # A positive-but-thin wall is its own message so the model learns the
    # actual limit, not just "positive".
    if wall_nm < MIN_WALL_NM:
        errors.append(
            f"'wall_mm' is {wall_nm / 1_000_000} mm, below the printable FDM "
            f"minimum of {MIN_WALL_NM / 1_000_000} mm"
        )
        wall_nm = DEFAULT_WALL_NM

    clearance_nm = _dim_nm(
        data, "clearance_mm", errors,
        default_nm=DEFAULT_CLEARANCE_NM, minimum_nm=1,
        what="board-to-cavity clearance",
    )

    corner_radius_nm = _dim_nm(
        data, "corner_radius_mm", errors,
        default_nm=0, minimum_nm=0,
        what="corner radius",
    )

    lid = str(data.get("lid", "friction"))
    if lid not in LIDS:
        errors.append(f"'lid' is {lid!r}; allowed: {list(LIDS)}")
        lid = "friction"

    raw_cutouts = data.get("cutouts", [])
    if raw_cutouts is None:
        raw_cutouts = []
    if not isinstance(raw_cutouts, list):
        errors.append(
            f"'cutouts' must be a list, got {type(raw_cutouts).__name__}"
        )
        raw_cutouts = []

    cutouts: list[Cutout] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cutouts):
        where = f"cutout[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object, got {item!r}")
            continue

        cid = str(item.get("id", "")).strip()
        if not _ID_RE.match(cid):
            errors.append(
                f"{where}: 'id' {cid!r} is not a valid identifier"
            )
        elif cid in seen_ids:
            errors.append(f"{where}: duplicate cutout id {cid!r}")
        seen_ids.add(cid)

        ref = str(item.get("ref", "")).strip()
        if not _REF_RE.match(ref):
            errors.append(
                f"{where}: 'ref' {ref!r} is not a reference designator "
                f"(expected e.g. 'J1', 'U3')"
            )

        face = str(item.get("face", ""))
        if face not in FACES:
            errors.append(
                f"{where}: 'face' is {face!r}; allowed: {list(FACES)}"
            )

        margin_nm = _dim_nm(
            item, "margin_mm", errors,
            default_nm=mm(0.5), minimum_nm=0,
            what=f"{where} opening margin",
        )

        cutouts.append(
            Cutout(id=cid, ref=ref, face=face, margin_nm=margin_nm)
        )

    standoffs = data.get("standoffs", True)
    if not isinstance(standoffs, bool):
        errors.append(f"'standoffs' must be a boolean, got {standoffs!r}")
        standoffs = True

    vents = data.get("vents", False)
    if not isinstance(vents, bool):
        errors.append(f"'vents' must be a boolean, got {vents!r}")
        vents = False

    # A screw lid's pilot holes bite into the standoff bosses; without
    # standoffs they open onto an empty floor and the screws hold nothing.
    # Model-fixable, so it joins the batch rather than raising later.
    if lid == "screw" and not standoffs:
        errors.append(
            "'lid' is 'screw' but 'standoffs' is false: screw pilot holes "
            "need standoff bosses to bite into; set 'standoffs' to true or "
            "choose a different lid"
        )

    label = _sanitise_label(data.get("label"), errors)

    known = {
        "wall_mm", "clearance_mm", "corner_radius_mm", "lid", "cutouts",
        "standoffs", "vents", "label",
    }
    for key in sorted(set(data) - known):
        errors.append(f"unknown field {key!r}; allowed: {sorted(known)}")

    if errors:
        raise EnclosureValidationError(errors)

    return EnclosureSpec(
        wall_nm=wall_nm,
        clearance_nm=clearance_nm,
        lid=lid,
        corner_radius_nm=corner_radius_nm,
        cutouts=tuple(cutouts),
        standoffs=standoffs,
        vents=vents,
        label=label,
    )
