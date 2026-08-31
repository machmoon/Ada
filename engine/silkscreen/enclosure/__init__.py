"""AI-generated OpenSCAD enclosures for the boards the pipeline produces.

The package mirrors the engine's layering: this IR layer (``ir.py``,
``errors.py``) validates what a model proposes and knows nothing about any
board; ``board_shape.py``/``heights.py``/``emit.py``/``verify.py`` are the
deterministic geometry half; ``render.py`` is the gated OpenSCAD CLI wrapper,
never on the service path. Contracts are frozen in ``docs/ai-cad-plan.md``.

This ``__init__`` re-exports the IR and error names only; everything else is
imported from its submodule directly, so the other workstreams never edit
this file.
"""

from .errors import (
    CavityFitError,
    CutoutError,
    EmptyGeometryError,
    EnclosureError,
    EnclosureValidationError,
    RenderFailed,
    RenderUnavailable,
    WallError,
)
from .ir import (
    DEFAULT_CLEARANCE_NM,
    DEFAULT_WALL_NM,
    FACES,
    LIDS,
    MIN_WALL_NM,
    Cutout,
    EnclosureSpec,
    parse_enclosure_spec,
)

__all__ = [
    "EnclosureError",
    "EnclosureValidationError",
    "CavityFitError",
    "CutoutError",
    "WallError",
    "RenderUnavailable",
    "RenderFailed",
    "EmptyGeometryError",
    "MIN_WALL_NM",
    "DEFAULT_WALL_NM",
    "DEFAULT_CLEARANCE_NM",
    "FACES",
    "LIDS",
    "Cutout",
    "EnclosureSpec",
    "parse_enclosure_spec",
]
