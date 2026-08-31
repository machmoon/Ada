"""Component heights for enclosure cavity sizing.

A ``.kicad_pcb`` carries no Z at all, so the third dimension has to come from
somewhere else. This table maps *footprint classes* (substrings of the
footprint's library id, e.g. ``LQFP`` or ``C_0805``) to nominal maximum body
heights in **integer nanometres**. The values are deliberately conservative --
a cavity slightly taller than the part is a working enclosure, one slightly
shorter is scrap plastic.

A footprint whose name matches no class gets :data:`DEFAULT_HEIGHT_NM` and the
lookup says so (``was_default=True``), which :mod:`.board_shape` records on the
part and :mod:`.verify` surfaces as a warning in the fit report. A silent
guess would be a quiet zero in disguise (plan decision 9).
"""

from __future__ import annotations

from ..units import mm

__all__ = ["DEFAULT_HEIGHT_NM", "HEIGHTS_NM", "height_for"]

#: Height used when no class matches. Tall enough for most SMD parts, and it
#: always arrives paired with ``was_default=True`` so the caller can warn.
DEFAULT_HEIGHT_NM: int = mm(3.0)

#: Footprint class -> nominal maximum height. Keys are matched
#: case-insensitively as substrings of the footprint's library id; when
#: several keys match, the longest one wins (``SOT-223`` beats ``SOT-2``),
#: with the alphabetically first breaking any remaining tie so the lookup is
#: deterministic.
HEIGHTS_NM: dict[str, int] = {
    # IC packages.
    "LQFP": mm(1.6),
    "TQFP": mm(1.2),
    "QFN": mm(1.0),
    "DFN": mm(0.8),
    "WSON": mm(0.8),
    "SOIC": mm(1.75),
    "SSOP": mm(2.0),
    "TSSOP": mm(1.2),
    "SOT-23": mm(1.45),
    "SOT-223": mm(1.8),
    "SOT-89": mm(1.6),
    "TO-252": mm(2.4),
    "TO-263": mm(4.6),
    # Chip passives (body height per EIA size).
    "C_0402": mm(0.6),
    "C_0603": mm(0.9),
    "C_0805": mm(1.4),
    "C_1206": mm(1.8),
    "R_0402": mm(0.4),
    "R_0603": mm(0.55),
    "R_0805": mm(0.7),
    "R_1206": mm(0.7),
    "L_0805": mm(1.1),
    "L_1206": mm(1.4),
    # Discretes and misc.
    "SOD-123": mm(1.35),
    "SOD-323": mm(1.1),
    "LED_0603": mm(0.8),
    "LED_0805": mm(1.1),
    "Crystal_SMD": mm(1.3),
    # Things that poke up. Connectors are the usual cutout candidates, so
    # their heights matter most.
    "PinHeader": mm(8.5),
    "PinSocket": mm(8.5),
    "USB_C": mm(3.2),
    "USB_Micro": mm(2.9),
    "Barrel_Jack": mm(11.0),
    "SW_SPST": mm(3.5),
}


def height_for(
    footprint_name: str, table: dict[str, int] | None = None
) -> tuple[int, bool]:
    """Height for a footprint name, and whether it fell back to the default.

    ``footprint_name`` is the footprint's library id (``Package_QFP:LQFP-48``)
    or any string containing the class token. ``table`` substitutes the whole
    class table (used by :func:`.board_shape.board_envelope` when a caller
    supplies overrides); ``None`` means :data:`HEIGHTS_NM`.
    """
    entries = HEIGHTS_NM if table is None else table
    haystack = footprint_name.lower()
    # Longest key first so the most specific class wins; alphabetical second
    # so ties cannot depend on dict insertion order.
    for key in sorted(entries, key=lambda k: (-len(k), k)):
        if key.lower() in haystack:
            return entries[key], False
    return DEFAULT_HEIGHT_NM, True
