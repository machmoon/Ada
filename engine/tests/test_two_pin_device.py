"""Two-pin devices declared under ``devices`` rather than ``passives``.

The failure these guard against was nondeterministic and expensive: the model
sometimes declares a two-terminal part -- an LED one run, a two-pin input
connector the next -- under ``devices``. Validation rightly accepts that (the
IR does not forbid it), so the repair loop never fired, and the run crashed in
``_footprint_for_device`` with ``UnsupportedPackage`` only after the paid model
calls had already happened. Twice on demo day, on the golden-path LDO intent.

The fix routes 2-pin devices onto the proven 1206 chip land pattern. Following
the house discipline, the round-trip checks here re-read the written file with
``kiutils`` and compute overlap with independent math rather than asking
``board.py`` where anything is.
"""

from __future__ import annotations

import itertools

from kiutils.board import Board
from kiutils.schematic import Schematic
from silkscreen.board import build_board, route_board, write_board
from silkscreen.netlist import parse_circuit_spec
from silkscreen.schematic import build_schematic, emit_kicad_sch


def _spec():
    """The demo's golden-path shape: an LDO plus the two 2-pin devices that
    actually crashed real runs -- an LED (D-ish) and a connector (J-ish).
    Refs are assigned by ``assign_refs`` (all devices become U*), so nothing
    here may depend on name prefixes.
    """
    return parse_circuit_spec({
        "devices": {
            "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
            "LED_STATUS": {"pins": {"A": "1", "K": "2"}},
            "J_IN": {"pins": {"VIN": "1", "GND": "2"}},
        },
        "passives": {
            "r_led": {"type": "resistor", "value": "330"},
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["J_IN.VIN", "AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["J_IN.GND", "AMS1117-3.3.GND", "c_in.2", "c_out.2",
                    "LED_STATUS.K"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1", "r_led.1"],
            "LED_A": ["r_led.2", "LED_STATUS.A"],
        },
    })


def test_two_pin_device_places_instead_of_raising():
    """The crash itself: this call used to raise UnsupportedPackage."""
    board = build_board(_spec(), time_limit_s=10.0)
    assert len(board.parts) == 6
    by_value = {p.value: p for p in board.parts}
    for name in ("LED_STATUS", "J_IN"):
        fp = by_value[name].footprint
        assert len(fp.pads) == 2, name
        # Pad numbers must be exactly the "1"/"2" the connections reference.
        assert {p.number for p in fp.pads} == {"1", "2"}, name
        # A courtyard must exist, or the placer's overlap guarantee is blind
        # to this part.
        assert fp.courtyard_w_nm > 0 and fp.courtyard_h_nm > 0, name


def test_two_pin_device_pads_carry_their_nets():
    board = build_board(_spec(), time_limit_s=10.0)
    fp = next(p for p in board.parts if p.value == "J_IN").footprint
    assert fp.pad_by_number("1").net == "VIN"
    assert fp.pad_by_number("2").net == "GND"
    fp = next(p for p in board.parts if p.value == "LED_STATUS").footprint
    assert fp.pad_by_number("1").net == "LED_A"
    assert fp.pad_by_number("2").net == "GND"


def test_two_pin_device_board_round_trips_without_overlap(tmp_path):
    """Emit, reparse with KiCad's own parser, and measure courtyard overlap
    with independent math (anchor + local courtyard bbox, the same truth
    function discipline as ``test_kicad.py``) -- never by asking board.py.
    """
    from silkscreen.kicad import extract_parts, footprint_ref

    board = build_board(_spec(), time_limit_s=10.0)
    path = write_board(board, tmp_path / "twopin.kicad_pcb")

    reloaded = Board.from_file(str(path))
    assert len(reloaded.footprints) == len(board.parts)
    assert {footprint_ref(f) for f in reloaded.footprints} == {
        p.ref for p in board.parts
    }

    infos = extract_parts(reloaded)
    boxes = []
    for info, fp in zip(infos, reloaded.footprints, strict=True):
        boxes.append((
            info.ref,
            fp.position.X + info.min_x_nm / 1e6,
            fp.position.Y + info.min_y_nm / 1e6,
            fp.position.X + info.max_x_nm / 1e6,
            fp.position.Y + info.max_y_nm / 1e6,
        ))
    for a, b in itertools.combinations(boxes, 2):
        dx = min(a[3], b[3]) - max(a[1], b[1])
        dy = min(a[4], b[4]) - max(a[2], b[2])
        assert not (dx > 0.01 and dy > 0.01), (
            f"courtyard overlap {a[0]}/{b[0]}: {dx:.2f} x {dy:.2f} mm"
        )


def test_two_pin_device_board_routes_instead_of_refusing():
    """Every net on this board reaches ordinary chip pads; nothing about a
    2-pin device may make the router refuse or leave its nets unrouted.
    """
    board = build_board(_spec(), time_limit_s=10.0)
    result = route_board(board)
    assert result.unrouted == {}, result.unrouted
    assert set(result.routed) == {"VIN", "GND", "+3V3", "LED_A"}


def test_two_pin_device_schematic_emits_and_reparses(tmp_path):
    """The schematic emitter draws a 2-pin device as a small IC rectangle;
    it must produce a sheet KiCad's parser accepts, with the same refs the
    board used.
    """
    spec = _spec()
    board = build_board(spec, time_limit_s=10.0)
    text = emit_kicad_sch(build_schematic(
        spec, footprints={p.ref: p.footprint.name for p in board.parts}
    ))
    path = tmp_path / "twopin.kicad_sch"
    path.write_text(text, encoding="utf-8")
    sch = Schematic.from_file(str(path))
    refs = {
        prop.value
        for sym in sch.schematicSymbols
        for prop in sym.properties
        if prop.key == "Reference"
    }
    assert refs == {p.ref for p in board.parts}


def test_output_stays_byte_identical_across_runs():
    """The determinism property, extended over the new package rule."""
    from silkscreen.board import emit_kicad_pcb

    a = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    b = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    assert a == b
