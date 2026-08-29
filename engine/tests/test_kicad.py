"""KiCad interop tests, run against a real board file.

The fixture is ``tests/fixtures/ref.kicad_pcb`` -- the hand-placed board the original
pipeline pasted into KiCad through the clipboard. Using it here is deliberate:
it proves the same board can be placed by writing a file instead of by driving
a GUI.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from silkscreen import pack
from silkscreen.kicad import (
    apply_placements,
    extract_nets,
    extract_parts,
    extract_wires,
    is_power_net,
    load_board,
    save_board,
    to_parts,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ref.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="board fixture not present"
)


@pytest.fixture(scope="module")
def board():
    return load_board(FIXTURE)


@pytest.fixture(scope="module")
def infos(board):
    return extract_parts(board)


def test_reads_every_footprint(infos):
    assert len(infos) == 11
    assert all(i.ref for i in infos), "every footprint should have a reference"


def test_footprint_extents_are_physically_plausible(infos):
    for info in infos:
        # Nothing on a board this size is smaller than 0.3 mm or larger than 30 mm.
        assert 300_000 <= info.width_nm <= 30_000_000, info.ref
        assert 300_000 <= info.height_nm <= 30_000_000, info.ref


def test_lqfp48_is_about_9mm_including_courtyard(infos):
    """An LQFP-48 7x7mm body has ~9mm courtyard including leads and margin."""
    big = max(infos, key=lambda i: i.width_nm * i.height_nm)
    assert 8_000_000 <= big.width_nm <= 11_000_000
    assert 8_000_000 <= big.height_nm <= 11_000_000


def test_pads_carry_net_names(infos):
    assert any(info.pad_nets for info in infos), "expected nets on pads"


def test_power_net_detection():
    assert is_power_net("GND", 2, 6)
    assert is_power_net("+3V3", 2, 6)
    assert is_power_net("VCC", 2, 6)
    assert is_power_net("/AVDD", 2, 6)
    assert not is_power_net("SWDIO", 2, 6)
    # Fans out too far to be a signal net, whatever it is called.
    assert is_power_net("SOME_BUS", 40, 6)


def test_power_nets_are_excluded_from_wires(infos):
    """Regression: the original expanded every net, ground included, into a
    clique, which swamped the objective and merged the whole board."""
    wires = extract_wires(infos)
    all_pairs = sum(
        len(v) * (len(v) - 1) // 2
        for v in _nets_by_name(infos).values()
        if len(v) > 1
    )
    assert len(wires) < all_pairs, "star + power exclusion should shrink the graph"
    assert len(wires) > 0, "but some signal nets should survive"


def _nets_by_name(infos):
    out: dict[str, list] = {}
    for idx, info in enumerate(infos):
        for pad, net in info.pad_nets.items():
            out.setdefault(net, []).append((idx, pad))
    return out


def test_end_to_end_replace_produces_a_valid_board(board, infos, tmp_path):
    """Load a real board, re-place it with the solver, write it back out."""
    parts = to_parts(infos)
    wires = extract_wires(infos)

    result = pack(parts, wires, time_limit_s=20.0)
    assert len(result.placements) == len(parts)

    moved = apply_placements(board, infos, result.placements, result.board_height_nm)
    assert moved == len(infos), "every footprint should have been placed"

    out = save_board(board, tmp_path / "placed.kicad_pcb")
    assert out.exists() and out.stat().st_size > 1000

    # And it round-trips: KiCad's own parser should accept what we wrote.
    reloaded = load_board(out)
    assert len(reloaded.footprints) == len(board.footprints)

    # No two placed courtyards overlap on the written board.
    #
    # This must be computed as anchor + LOCAL courtyard bbox. Deriving the box
    # as (fp.position, fp.position + size) assumes fp.position is the bbox
    # corner -- it is the anchor -- and a test that makes that assumption is
    # self-consistently wrong: it passes while the written board overlaps.
    new_infos = extract_parts(reloaded)
    boxes = []
    for info, fp in zip(new_infos, reloaded.footprints, strict=True):
        x0 = fp.position.X + info.min_x_nm / 1e6
        y0 = fp.position.Y + info.min_y_nm / 1e6
        x1 = fp.position.X + info.max_x_nm / 1e6
        y1 = fp.position.Y + info.max_y_nm / 1e6
        boxes.append((info.ref, x0, y0, x1, y1))
    for (ra, ax0, ay0, ax1, ay1), (rb, bx0, by0, bx1, by1) in itertools.combinations(
        boxes, 2
    ):
        dx = min(ax1, bx1) - max(ax0, bx0)
        dy = min(ay1, by1) - max(ay0, by0)
        assert not (dx > 0.01 and dy > 0.01), (
            f"courtyard overlap {ra}/{rb}: {dx:.2f} x {dy:.2f} mm"
        )

    # And the board stays in the positive quadrant it reported.
    min_x = min(b[1] for b in boxes)
    min_y = min(b[2] for b in boxes)
    assert min_x >= -0.01, f"board spills to x={min_x:.2f} mm"
    assert min_y >= -0.01, f"board spills to y={min_y:.2f} mm"


def test_courtyard_is_anchor_centred_in_the_fixture(infos):
    """Guards the assumption the placement math depends on.

    If a footprint's courtyard started at its anchor, min_x would be ~0 and the
    anchor/bbox distinction would be invisible. It is not: courtyards straddle
    the anchor, which is why apply_placements must subtract the local corner.
    """
    for info in infos:
        assert info.min_x_nm < 0, f"{info.ref} courtyard does not straddle anchor"
        assert info.max_x_nm > 0, f"{info.ref} courtyard does not straddle anchor"


def test_mixed_valid_and_missing_refs_land_correctly(board, infos):
    """A bogus ref must not shift the valid ones onto the wrong footprints."""
    from silkscreen.packing import Placement

    target = infos[0]
    placements = [
        Placement(ref="NOT_ON_BOARD", x_nm=0, y_nm=0),
        Placement(ref=target.ref, x_nm=5_000_000, y_nm=3_000_000),
    ]
    moved = apply_placements(board, infos, placements, 40_000_000)
    assert moved == 1

    from silkscreen.kicad import footprint_ref
    fp = next(f for f in board.footprints if footprint_ref(f) == target.ref)
    expected_x = (5_000_000 - target.min_x_nm) / 1e6
    assert pytest.approx(expected_x, abs=1e-6) == fp.position.X


def test_missing_ref_is_skipped_not_misaligned(board, infos):
    """Regression: a missing footprint used to desync the size and ref lists,
    shifting every subsequent placement onto the wrong part."""
    from silkscreen.packing import Placement

    bogus = [Placement(ref="NOT_ON_BOARD", x_nm=0, y_nm=0)]
    moved = apply_placements(board, infos, bogus, 10_000_000)
    assert moved == 0


def test_board_outline_is_written_and_encloses_every_part(board, infos, tmp_path):
    """Without an outline, must_be_on_edge is solved against nothing.

    Edge.Cuts is what KiCad measures board-edge clearance against, so a file
    with no outline cannot be meaningfully design-rule checked.
    """
    from silkscreen.kicad import set_board_outline

    parts = to_parts(infos)
    result = pack(parts, nets=extract_nets(infos), time_limit_s=15.0)
    apply_placements(board, infos, result.placements, result.board_height_nm)
    set_board_outline(board, result.board_width_nm, result.board_height_nm)

    out = save_board(board, tmp_path / "outlined.kicad_pcb")
    reloaded = load_board(out)

    edges = [
        g for g in reloaded.graphicItems
        if getattr(g, "layer", None) == "Edge.Cuts"
    ]
    assert len(edges) == 4, f"expected a 4-sided outline, got {len(edges)}"

    xs = [p.X for g in edges for p in (g.start, g.end)]
    ys = [p.Y for g in edges for p in (g.start, g.end)]
    ex0, ex1, ey0, ey1 = min(xs), max(xs), min(ys), max(ys)

    # Every courtyard must sit inside the outline we drew.
    new_infos = extract_parts(reloaded)
    for info, fp in zip(new_infos, reloaded.footprints, strict=True):
        assert fp.position.X + info.min_x_nm / 1e6 >= ex0 - 0.01, info.ref
        assert fp.position.X + info.max_x_nm / 1e6 <= ex1 + 0.01, info.ref
        assert fp.position.Y + info.min_y_nm / 1e6 >= ey0 - 0.01, info.ref
        assert fp.position.Y + info.max_y_nm / 1e6 <= ey1 + 0.01, info.ref


def test_extract_nets_keeps_power_rails_at_reduced_weight():
    """Power nets must stay in the objective, not be dropped."""
    from silkscreen.kicad import FootprintInfo, extract_nets

    infos = [
        FootprintInfo("U1", 9_000_000, 9_000_000,
                      {"1": (0, 0), "2": (10, 10)}, {"1": "GND", "2": "SDA"}),
        FootprintInfo("C1", 1_600_000, 800_000,
                      {"1": (0, 0), "2": (10, 10)}, {"1": "GND", "2": "SDA"}),
    ]
    nets = extract_nets(infos)
    by_name = {n.name: n for n in nets}
    assert "GND" in by_name, "power net was dropped instead of down-weighted"
    assert by_name["GND"].weight < by_name["SDA"].weight


def test_unknown_edge_ref_raises_instead_of_silently_doing_nothing(infos):
    """A typo'd ref used to produce no constraint and no signal."""
    with pytest.raises(ValueError, match="not on this board"):
        to_parts(infos, edge_refs={"J1"})
