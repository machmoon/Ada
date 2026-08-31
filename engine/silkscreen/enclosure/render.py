"""The gated OpenSCAD CLI half. Never on the service path.

Mirrors :mod:`silkscreen.spice.simulators`: the only interaction with OpenSCAD
is exec-ing a user-installed binary at arm's length (plan decision 4), the
binary is located via ``shutil.which`` with an environment override, and its
absence is a specific error -- :class:`RenderUnavailable` naming what was
searched for -- rather than a quiet no-op.

The one failure mode peculiar to OpenSCAD is the vacuous pass: it can warn
``No top level geometry to render`` and still exit zero, leaving a well-formed
STL containing zero facets. An agent that gets an empty mesh concludes the
enclosure exists, so an empty output raises :class:`EmptyGeometryError`
(a :class:`RenderFailed`) instead of returning the path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import EmptyGeometryError, RenderFailed, RenderUnavailable

__all__ = [
    "ENV_OPENSCAD",
    "DEFAULT_TIMEOUT_S",
    "find_openscad",
    "available",
    "render_stl",
    "render_png",
]

#: Environment override, checked before anything on PATH -- the
#: ``SILKSCREEN_SPICE`` convention.
ENV_OPENSCAD = "SILKSCREEN_OPENSCAD"

#: Wall clock a single render may take before it is killed. A pathological
#: ``$fn`` is not a hang; it is bounded here rather than discovered later.
DEFAULT_TIMEOUT_S = 60.0

#: What the binary is called when it has to be described to a human. The
#: :class:`RenderUnavailable` message tells the user what to install.
_EXECUTABLE_NAME = "openscad"

#: Where OpenSCAD installs itself when it is not on PATH, per platform --
#: the ``LTspiceSimulator.CANDIDATE_PATHS`` convention.
_CANDIDATE_PATHS = (
    "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    r"C:\Program Files\OpenSCAD\openscad.exe",
    r"C:\Program Files (x86)\OpenSCAD\openscad.exe",
)

#: stderr lines that are fatal even when the exit code is zero.
_ERROR_PATTERNS = (
    re.compile(r"^ERROR:", re.MULTILINE),
    re.compile(r"^\s*Parser error", re.IGNORECASE | re.MULTILINE),
    re.compile(r"Can't open file", re.IGNORECASE),
)

#: stderr lines that mean the model produced nothing to export.
_EMPTY_PATTERNS = (
    re.compile(r"No top level geometry to render", re.IGNORECASE),
    re.compile(r"Current top level object is empty", re.IGNORECASE),
)


def find_openscad() -> str | None:
    """The OpenSCAD executable, or ``None``. The seam a v2 preview replaces.

    ``SILKSCREEN_OPENSCAD`` wins outright when set -- as a path that exists or
    a name ``shutil.which`` resolves. Otherwise ``openscad`` on PATH, then the
    per-platform install locations.
    """
    override = os.environ.get(ENV_OPENSCAD)
    if override:
        if Path(override).exists():
            return override
        return shutil.which(override)
    found = shutil.which(_EXECUTABLE_NAME)
    if found:
        return found
    for candidate in _CANDIDATE_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def available() -> bool:
    """Whether this machine can render at all. The test-gating probe."""
    return find_openscad() is not None


def _stl_facet_count(path: Path) -> int:
    """How many triangles an STL file carries, for both dialects.

    Binary STL: 80-byte header then a little-endian uint32 facet count.
    ASCII STL: starts with ``solid`` and carries one ``facet`` block per
    triangle. A file too short to be either counts as zero.
    """
    blob = path.read_bytes()
    if len(blob) < 15:
        return 0
    if blob.lstrip()[:5] == b"solid" and b"facet" in blob:
        return blob.count(b"facet normal")
    if len(blob) < 84:
        return 0
    return int.from_bytes(blob[80:84], "little")


def _run(scad: str, out_path: Path, *, timeout_s: float) -> tuple[Path, str]:
    """Exec OpenSCAD on ``scad`` text, writing ``out_path``. Shared plumbing.

    Returns the output path and the captured stderr (OpenSCAD reports on
    stderr, exit codes second). Raises :class:`RenderUnavailable` when there
    is no binary, :class:`EmptyGeometryError` when the log says the model
    produced nothing, and :class:`RenderFailed` for everything else.
    """
    executable = find_openscad()
    if executable is None:
        raise RenderUnavailable(_EXECUTABLE_NAME)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="silkscreen-scad-") as tmp:
        source = Path(tmp) / "enclosure.scad"
        source.write_text(scad, encoding="utf-8")
        try:
            proc = subprocess.run(
                [executable, "-o", str(out_path), str(source)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderFailed(
                f"openscad did not finish within {timeout_s:g} s and was "
                f"killed. This is usually a pathological $fn or an enormous "
                f"model, not a hang worth waiting out."
            ) from exc

    log = (proc.stderr or "") + (proc.stdout or "")
    if any(p.search(log) for p in _EMPTY_PATTERNS):
        raise EmptyGeometryError(
            "openscad reported no top-level geometry; the model rendered "
            "to nothing. An empty enclosure must never pass as a rendered "
            f"one. Log:\n{log}"
        )
    if proc.returncode != 0 or any(p.search(log) for p in _ERROR_PATTERNS):
        raise RenderFailed(
            f"openscad failed (exit {proc.returncode}):\n{log}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise EmptyGeometryError(
            f"openscad exited cleanly but wrote no output at {out_path}; "
            "an empty render must never pass as a finished one."
        )
    return out_path, log


def render_stl(
    scad: str, out_path: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> Path:
    """Render ``scad`` source to an STL mesh at ``out_path``.

    A well-formed STL with zero facets raises :class:`EmptyGeometryError` --
    OpenSCAD can warn-and-emit-nothing, and empty must raise, never pass
    vacuously.
    """
    path, _log = _run(scad, Path(out_path), timeout_s=timeout_s)
    if _stl_facet_count(path) == 0:
        raise EmptyGeometryError(
            f"{path} is a well-formed STL containing zero facets; the model "
            "rendered to nothing."
        )
    return path


def render_png(
    scad: str, out_path: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> Path:
    """Render ``scad`` source to a PNG preview at ``out_path``."""
    path, _log = _run(scad, Path(out_path), timeout_s=timeout_s)
    return path
