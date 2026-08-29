"""Footprint generation and board emission.

The end-to-end test here is the one that matters: a circuit spec becomes a
``.kicad_pcb`` that KiCad's own parser reads back, with pads on the right nets
and no overlapping courtyards.
"""

from __future__ import annotations

import itertools

import pytest
from silkscreen.board import build_board, emit_kicad_pcb, write_board
from silkscreen.footprints import (
    CHIP_SIZES,
    UnsupportedPackage,
    chip_passive,
    for_passive,
    lqfp,
    soic,
    sot223,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.units import to_mm


def _spec():
    return parse_circuit_spec({
        "devices": {
            "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
            "DRV8837": {"pins": {"IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                                 "OUT1": "5", "OUT2": "6", "VCC": "7",
                                 "nSLEEP": "8"}},
        },
        "passives": {
            "c_in": {"type": "capacitor", "value": "22uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
            "c_dec": {"type": "capacitor", "value": "100nF"},
            "r_sleep": {"type": "resistor", "value": "10k"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1", "DRV8837.VM"],
            "GND": ["AMS1117-3.3.GND", "DRV8837.GND", "c_in.2", "c_out.2",
                    "c_dec.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "DRV8837.VCC", "c_out.1", "c_dec.1",
                     "r_sleep.1"],
            "SLEEP": ["DRV8837.nSLEEP", "r_sleep.2"],
            "MOT": ["DRV8837.OUT1", "DRV8837.IN1"],
        },
    })


# ---------------------------------------------------------------- footprints


def test_chip_passive_sizes_are_physically_right():
    fp = chip_passive("0603")
    assert len(fp.pads) == 2
    # An 0603 land pattern is about 3mm across including both pads.
    assert 2.5 <= to_mm(fp.courtyard_w_nm * 2) <= 3.5
    assert 1.0 <= to_mm(fp.courtyard_h_nm * 2) <= 2.0


def test_every_chip_size_generates():
    for size in CHIP_SIZES:
        fp = chip_passive(size)
        assert len(fp.pads) == 2
        assert fp.courtyard_w_nm > 0 and fp.courtyard_h_nm > 0


def test_pad_count_matches_package():
    assert len(sot223().pads) == 4          # 3 pins + tab
    assert len(soic(8).pads) == 8
    assert len(lqfp(48).pads) == 48
    assert len(lqfp(100).pads) == 100


def test_lqfp_pins_are_unique_and_contiguous():
    fp = lqfp(48)
    numbers = sorted(int(p.number) for p in fp.pads)
    assert numbers == list(range(1, 49))


def test_lqfp_courtyard_matches_a_real_footprint():
    """A real LQFP-48 7x7mm courtyard is ~10.3mm. Ours should be close."""
    fp = lqfp(48, body_mm=7.0)
    assert 9.5 <= to_mm(fp.courtyard_w_nm * 2) <= 11.5


def test_no_two_pads_overlap_in_a_generated_footprint():
    for fp in (sot223(), soic(8), soic(16), lqfp(32), lqfp(48), chip_passive("0805")):
        boxes = [
            (p.x_nm - p.w_nm // 2, p.y_nm - p.h_nm // 2,
             p.x_nm + p.w_nm // 2, p.y_nm + p.h_nm // 2)
            for p in fp.pads
        ]
        for a, b in itertools.combinations(boxes, 2):
            dx = min(a[2], b[2]) - max(a[0], b[0])
            dy = min(a[3], b[3]) - max(a[1], b[1])
            assert not (dx > 0 and dy > 0), f"{fp.name} has overlapping pads"


def test_pads_are_inside_their_courtyard():
    for fp in (sot223(), soic(14), lqfp(44), chip_passive("1206")):
        for p in fp.pads:
            assert abs(p.x_nm) + p.w_nm // 2 <= fp.courtyard_w_nm + 1, fp.name
            assert abs(p.y_nm) + p.h_nm // 2 <= fp.courtyard_h_nm + 1, fp.name


def test_large_capacitor_gets_a_larger_package():
    """A 22uF part does not fit an 0603, and pretending it does is a dead board."""
    small = for_passive("capacitor", "100nF")
    large = for_passive("capacitor", "22uF")
    assert large.courtyard_w_nm > small.courtyard_w_nm


def test_unsupported_package_raises_rather_than_guessing():
    with pytest.raises(UnsupportedPackage):
        lqfp(47)
    with pytest.raises(UnsupportedPackage):
        soic(7)
    with pytest.raises(UnsupportedPackage):
        chip_passive("0201")


# ---------------------------------------------------------------- board


def test_build_board_places_every_part():
    board = build_board(_spec(), time_limit_s=15.0)
    assert len(board.parts) == 6
    refs = {p.ref for p in board.parts}
    assert refs == {"U1", "U2", "C1", "C2", "C3", "R1"}


def test_reference_designators_follow_kicad_convention():
    board = build_board(_spec(), time_limit_s=15.0)
    for part in board.parts:
        assert part.ref[0] in "URCLDY"


def test_emitted_board_reparses_and_is_geometrically_valid(tmp_path):
    """The one test that proves 'generate a PCB' actually happened."""
    from kiutils.board import Board
    from silkscreen.kicad import extract_parts, footprint_ref

    board = build_board(_spec(), time_limit_s=15.0)
    path = write_board(board, tmp_path / "out.kicad_pcb")
    assert path.stat().st_size > 2000

    reloaded = Board.from_file(str(path))
    assert len(reloaded.footprints) == len(board.parts)

    # Every reference survived the round trip.
    assert {footprint_ref(f) for f in reloaded.footprints} == {
        p.ref for p in board.parts
    }

    # Pads carry their nets.
    pad_nets = {
        pad.net.name
        for fp in reloaded.footprints
        for pad in fp.pads
        if pad.net and pad.net.name
    }
    assert {"GND", "+3V3", "VIN"} <= pad_nets

    # A board outline exists.
    edges = [
        g for g in reloaded.graphicItems if getattr(g, "layer", None) == "Edge.Cuts"
    ]
    assert len(edges) == 4

    # No two courtyards overlap, measured on the written geometry.
    infos = extract_parts(reloaded)
    boxes = [
        (i.ref,
         fp.position.X + i.min_x_nm / 1e6, fp.position.Y + i.min_y_nm / 1e6,
         fp.position.X + i.max_x_nm / 1e6, fp.position.Y + i.max_y_nm / 1e6)
        for i, fp in zip(infos, reloaded.footprints, strict=True)
    ]
    for a, b in itertools.combinations(boxes, 2):
        dx = min(a[3], b[3]) - max(a[1], b[1])
        dy = min(a[4], b[4]) - max(a[2], b[2])
        assert not (dx > 0.01 and dy > 0.01), f"courtyard overlap {a[0]}/{b[0]}"


def test_net_zero_exists_for_kicad():
    """KiCad requires net 0 (the unconnected net) to be declared."""
    board = build_board(_spec(), time_limit_s=10.0)
    text = emit_kicad_pcb(board)
    assert '(net 0 "")' in text


def test_output_is_byte_identical_across_runs():
    """UUIDs are seeded, so a regenerated board diffs cleanly in git."""
    a = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    b = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    assert a == b


def test_part_name_containing_a_dot_is_handled():
    """Regression: 'AMS1117-3.3' split on the first dot, losing the part."""
    spec = parse_circuit_spec({
        "devices": {"LM317-2.5": {"pins": {"ADJ": "1", "OUT": "2", "IN": "3"}}},
        "passives": {"c1": {"type": "capacitor", "value": "10uF"}},
        "nets": {
            "VIN": ["LM317-2.5.IN", "c1.1"],
            "GND": ["LM317-2.5.ADJ", "c1.2"],
            "VOUT": ["LM317-2.5.OUT", "LM317-2.5.ADJ"],
        },
    })
    board = build_board(spec, time_limit_s=10.0)
    assert len(board.parts) == 2


def test_unknown_pin_count_refuses_rather_than_guessing():
    spec = parse_circuit_spec({
        "devices": {"WEIRD": {"pins": {f"P{i}": str(i) for i in range(1, 38)}}},
        "passives": {"c1": {"type": "capacitor", "value": "1uF"}},
        "nets": {"A": ["WEIRD.P1", "c1.1"], "B": ["WEIRD.P2", "c1.2"]},
    })
    with pytest.raises(UnsupportedPackage, match="No package rule"):
        build_board(spec, time_limit_s=5.0)


def test_two_sided_board_emits_footprints_on_both_copper_layers(tmp_path):
    from kiutils.board import Board

    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    path = write_board(board, tmp_path / "two.kicad_pcb")
    reloaded = Board.from_file(str(path))
    layers = {fp.layer for fp in reloaded.footprints}
    assert layers == {"F.Cu", "B.Cu"}


def test_bottom_side_pads_are_on_bottom_layers(tmp_path):
    from kiutils.board import Board

    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    path = write_board(board, tmp_path / "two.kicad_pcb")
    reloaded = Board.from_file(str(path))
    for fp in reloaded.footprints:
        if fp.layer != "B.Cu":
            continue
        for pad in fp.pads:
            assert "B.Cu" in pad.layers, "a bottom footprint's pads must be on B.Cu"
            assert "F.Cu" not in pad.layers


def test_two_sided_is_smaller_than_single_sided():
    single = build_board(_spec(), two_sided=False, time_limit_s=15.0)
    both = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    assert both.width_nm * both.height_nm < single.width_nm * single.height_nm


def test_ics_stay_on_top_even_when_two_sided():
    """An IC underneath complicates assembly and rework for little area saved."""
    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    from silkscreen.packing import Layer

    for part in board.parts:
        if part.ref.startswith("U"):
            assert part.layer is Layer.TOP
