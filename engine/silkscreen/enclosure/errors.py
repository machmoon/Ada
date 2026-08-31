"""Error taxonomy for the enclosure feature.

Frozen in docs/ai-cad-plan.md. Every failure path in the enclosure packages
raises one of these — nothing returns a quiet zero, because an agent that gets
an empty result concludes the case fits. The render errors live here too (they
are raised by ``enclosure/render.py``) so the whole taxonomy has one home.
"""

from __future__ import annotations

__all__ = [
    "EnclosureError",
    "EnclosureValidationError",
    "CavityFitError",
    "CutoutError",
    "WallError",
    "RenderUnavailable",
    "RenderFailed",
    "EmptyGeometryError",
]


class EnclosureError(Exception):
    """Base class for every enclosure failure."""


class EnclosureValidationError(EnclosureError):
    """A proposed spec is invalid.

    ``errors`` holds one human-readable message per problem so the whole batch
    goes back to the model as a single repair prompt — the ``netlist.py``
    ``ValidationError`` convention.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in enclosure spec:\n  - "
            + "\n  - ".join(errors)
        )


class CavityFitError(EnclosureError):
    """The board does not fit the cavity.

    ``margins_nm`` is signed, keyed ``"x"``/``"y"``/``"z"``; a negative margin
    is a collision, and the number says by how much.
    """

    def __init__(self, message: str, margins_nm: dict[str, int]):
        self.margins_nm = margins_nm
        super().__init__(message)


class CutoutError(EnclosureError):
    """A cutout cannot be realised: bad ref, bad face, or overlap."""


class WallError(EnclosureError):
    """A wall violates a physical limit (e.g. below ``MIN_WALL_NM``)."""


class RenderUnavailable(EnclosureError):
    """The OpenSCAD binary was not found.

    ``executable`` names what was searched for (``"openscad"``) so the CLI can
    tell the user exactly what to install.
    """

    def __init__(self, executable: str):
        self.executable = executable
        super().__init__(f"{executable!r} not found on PATH")


class RenderFailed(EnclosureError):
    """OpenSCAD ran and failed (non-zero exit, or errors on stderr)."""


class EmptyGeometryError(RenderFailed):
    """OpenSCAD warned and emitted nothing — never a vacuous pass."""
