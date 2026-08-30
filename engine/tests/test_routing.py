"""Routing tests.

The claim under test is not "the router emitted some segments" -- it is that
the copper in the written file actually joins the pads it says it joins. So the
truth function here, :func:`connected_nets`, re-derives connectivity from the
emitted ``.kicad_pcb`` parsed by ``kiutils``, touching nothing in
:mod:`silkscreen.routing`. A check written in terms of the router would share
whatever blind spot the router has, which is the same reasoning
``test_kicad.py`` uses for courtyard overlap.

The bug class this guards is the quiet one: a run reports "4/4 nets routed",
the file opens, the tracks look plausible, and one of them ends a hair short of
its pad. Nothing raises. The board comes back from the fab dead.
"""

from __future__ import annotations

import pytest
from kiutils.board import Board
from kiutils.items.brditems import Segment, Via
from silkscreen.board import (
    build_board,
    emit_kicad_pcb,
    route_board,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.packing import Layer
from silkscreen.routing import RoutePad, route
from silkscreen.units import mm

#: Coordinates in the file are millimetre decimals; compare at 1 nm.
EPS = 1e-6


REGULATOR = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rled": {"type": "resistor", "value": "1k"},
        "D1": {"type": "diode", "value": "LED"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "D1.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "Cout.1", "Rled.1"],
        "LED_A": ["Rled.2", "D1.1"],
    },
}


@pytest.fixture(scope="module")
def routed():
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    result = route_board(board)
    return board, result


# --------------------------------------------------------------------------
# The independent truth function
# --------------------------------------------------------------------------


def _on_segment(px, py, ax, ay, bx, by) -> bool:
    """Is (px,py) on the axis-aligned segment a-b, endpoints included?"""
    if abs(ax - bx) < EPS:  # vertical
        return abs(px - ax) < EPS and min(ay, by) - EPS <= py <= max(ay, by) + EPS
    if abs(ay - by) < EPS:  # horizontal
        return abs(py - ay) < EPS and min(ax, bx) - EPS <= px <= max(ax, bx) + EPS
    return False


class _Union:
    def __init__(self):
        self.parent: dict[object, object] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def board_pads_from_file(brd: Board):
    """Absolute pad rectangles, read back out of the written file.

    Deliberately re-derived from the parsed s-expressions rather than from the
    ``BoardResult`` the router saw, so a placement written to the file
    differently from how the router imagined it shows up as a disconnection.
    """
    pads = []
    for fp in brd.footprints:
        ref = fp.properties.get("Reference", fp.libraryNickname)
        assert fp.position.angle in (None, 0), "fixture must not be rotated"
        for pad in fp.pads:
            layer = "B.Cu" if any("B.Cu" in v for v in pad.layers) else "F.Cu"
            pads.append(
                {
                    "id": ("pad", ref, pad.number),
                    "net": pad.net.name if pad.net else "",
                    "layer": layer,
                    "x": fp.position.X + pad.position.X,
                    "y": fp.position.Y + pad.position.Y,
                    "w": pad.size.X,
                    "h": pad.size.Y,
                }
            )
    return pads


def connected_nets(text: str) -> dict[str, list[set]]:
    """``{net: [component, ...]}`` over the copper in ``text``.

    A net that came out fully routed has exactly one component containing all
    of its pads.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "b.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        brd = Board().from_file(str(path))

    pads = board_pads_from_file(brd)
    segs = [t for t in brd.traceItems if isinstance(t, Segment)]
    vias = [t for t in brd.traceItems if isinstance(t, Via)]

    uf = _Union()
    for index, seg in enumerate(segs):
        sid = ("seg", index)
        uf.find(sid)
        for pad in pads:
            if pad["layer"] != seg.layer:
                continue
            for x, y in ((seg.start.X, seg.start.Y), (seg.end.X, seg.end.Y)):
                inside = (
                    abs(x - pad["x"]) <= pad["w"] / 2 + EPS
                    and abs(y - pad["y"]) <= pad["h"] / 2 + EPS
                )
                if inside:
                    uf.union(sid, pad["id"])
        for other, seg2 in enumerate(segs):
            if other <= index or seg2.layer != seg.layer:
                continue
            touch = any(
                _on_segment(px, py, a.X, a.Y, b.X, b.Y)
                for (px, py), (a, b) in (
                    ((seg2.start.X, seg2.start.Y), (seg.start, seg.end)),
                    ((seg2.end.X, seg2.end.Y), (seg.start, seg.end)),
                    ((seg.start.X, seg.start.Y), (seg2.start, seg2.end)),
                    ((seg.end.X, seg.end.Y), (seg2.start, seg2.end)),
                )
            )
            if touch:
                uf.union(sid, ("seg", other))
    for vindex, via in enumerate(vias):
        vid = ("via", vindex)
        for index, seg in enumerate(segs):
            if _on_segment(
                via.position.X, via.position.Y, seg.start.X, seg.start.Y,
                seg.end.X, seg.end.Y,
            ):
                uf.union(vid, ("seg", index))

    by_net: dict[str, list[set]] = {}
    for pad in pads:
        if not pad["net"]:
            continue
        by_net.setdefault(pad["net"], [])
    for net in by_net:
        groups: dict[object, set] = {}
        for pad in pads:
            if pad["net"] != net:
                continue
            groups.setdefault(uf.find(pad["id"]), set()).add(pad["id"])
        by_net[net] = list(groups.values())
    return by_net


# --------------------------------------------------------------------------


def test_the_regulator_board_routes_every_net(routed):
    board, result = routed
    assert result.unrouted == {}, result.unrouted
    assert result.tracks, "a routed board must have copper"
    assert board.is_routed


def test_the_emitted_copper_actually_joins_each_net(routed):
    """The claim the router makes, checked against the file it produced."""
    board, result = routed
    components = connected_nets(emit_kicad_pcb(board))
    assert set(components) == set(result.routed)
    for net, groups in components.items():
        assert len(groups) == 1, (
            f"net {net} is in {len(groups)} disconnected pieces: {groups}"
        )


def test_a_placed_board_has_no_copper_at_all():
    """Placement alone must not look routed. This was every run before now."""
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    text = emit_kicad_pcb(board)
    assert "(segment" not in text
    assert "(via " not in text
    assert not board.is_routed
    # ...and the pads still carry their nets, which is what KiCad draws as the
    # ratsnest that made an unrouted board look finished.
    assert '(net 1 "' in text


def test_routing_is_deterministic():
    spec = parse_circuit_spec(REGULATOR)
    outputs = []
    for _ in range(2):
        board = build_board(spec, time_limit_s=5.0)
        route_board(board)
        outputs.append(emit_kicad_pcb(board))
    assert outputs[0] == outputs[1]


def test_every_track_is_axis_aligned(routed):
    board, _ = routed
    for t in board.tracks:
        assert t.start_x_nm == t.end_x_nm or t.start_y_nm == t.end_y_nm, t


def test_no_copper_leaves_the_board_outline(routed):
    board, _ = routed
    margin = mm(2.0)
    lo_x, lo_y = -margin, -margin
    hi_x, hi_y = board.width_nm + margin, board.height_nm + margin
    for t in board.tracks:
        for x, y in (
            (t.start_x_nm, t.start_y_nm),
            (t.end_x_nm, t.end_y_nm),
        ):
            assert lo_x <= x <= hi_x and lo_y <= y <= hi_y, t
    for v in board.vias:
        assert lo_x <= v.x_nm <= hi_x and lo_y <= v.y_nm <= hi_y, v


#: A board with two layers in play, so vias exist to be checked. The regulator
#: fixture above routes entirely on the front, so every via rule it claims to
#: enforce went untested until this was added.
VIA_BOARD = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rled": {"type": "resistor", "value": "1k"},
        "D1": {"type": "diode", "value": "LED"},
        "L1": {"type": "inductor", "value": "10uH"},
        "Y1": {"type": "crystal", "value": "8MHz"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1", "L1.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "D1.2", "Y1.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "Cout.1", "Rled.1"],
        "LED_A": ["Rled.2", "D1.1"],
        "XTAL": ["L1.2", "Y1.1"],
    },
}


@pytest.fixture(scope="module")
def via_routed():
    board = build_board(parse_circuit_spec(VIA_BOARD), time_limit_s=10.0)
    result = route_board(board)
    assert result.vias, "fixture is meant to exercise vias and produced none"
    return board, result


def _copper_discs(board):
    """Every piece of copper as (net, layer, x, y, radius), in millimetres.

    A disc per sample point, so one distance rule covers track-to-track,
    track-to-via and via-to-via without three separate geometries. Sampled
    rather than solved analytically for the same reason the original did it:
    the point is to catch copper that is too close.
    """
    step = mm(0.05)
    out = []
    for tr in board.tracks:
        r = tr.width_nm / 2
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        for k in range(0, length + 1, step):
            out.append(
                (tr.net, tr.layer, tr.start_x_nm + dx * k, tr.start_y_nm + dy * k, r)
            )
    for via in board.vias:
        # A barrel pierces both layers, so it is copper on each of them.
        for layer in (Layer.TOP, Layer.BOTTOM):
            out.append((via.net, layer, via.x_nm, via.y_nm, via.diameter_nm / 2))
    return out


def test_copper_of_different_nets_keeps_its_clearance(via_routed):
    """The rule the router claims to enforce, on a board that has vias.

    The original check sampled tracks only, on a fixture with no vias at all,
    so nothing tested the via rules -- and KiCad's own DRC found a via shorting
    a foreign track plus two sub-clearance gaps on a board this suite passed.
    The clearance is a property of the search now rather than of whatever the
    clearance halo happened to win, and this is what says so.
    """
    required = mm(0.2)  # netclass default, the same number KiCad checks
    discs = _copper_discs(via_routed[0])
    for a in range(len(discs)):
        net_a, layer_a, ax, ay, ra = discs[a]
        for b in range(a + 1, len(discs)):
            net_b, layer_b, bx, by, rb = discs[b]
            if net_a == net_b or layer_a is not layer_b:
                continue
            gap = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 - ra - rb
            assert gap >= required - 1, (
                f"{net_a} and {net_b} are {gap / 1e6:.3f} mm apart on "
                f"{layer_a}, under the {required / 1e6:.2f} mm rule"
            )


def test_copper_keeps_its_clearance_from_foreign_pads(via_routed):
    """Copper must clear a pad it does not belong to, not only other copper."""
    from silkscreen.board import board_pads

    required = mm(0.2)
    board = via_routed[0]
    pads = [p for p in board_pads(board) if p.net]
    for net, layer, x, y, r in _copper_discs(board):
        for pad in pads:
            if pad.net == net or pad.layer is not layer:
                continue
            dx = max(abs(x - pad.x_nm) - pad.w_nm / 2, 0)
            dy = max(abs(y - pad.y_nm) - pad.h_nm / 2, 0)
            gap = (dx * dx + dy * dy) ** 0.5 - r
            assert gap >= required - 1, (
                f"{net} copper is {gap / 1e6:.3f} mm from {pad.ref} pad "
                f"{pad.number} [{pad.net}], under the {required / 1e6:.2f} mm rule"
            )


def test_the_via_defaults_clear_kicads_own_minimums():
    """KiCad 8 defaults to a 0.5 mm via and a 0.3 mm hole.

    The emitted board carries no design-settings block, so a reader gets those
    defaults -- and every via written at 0.4/0.2 came back a DRC error.
    """
    from silkscreen.routing import DEFAULT_VIA_DIAMETER_NM, DEFAULT_VIA_DRILL_NM

    # Read as "KiCad's minimum is at most ours". Ruff treats the SCREAMING_CASE
    # module constants as the constant side, so they go on the right.
    assert mm(0.5) <= DEFAULT_VIA_DIAMETER_NM
    assert mm(0.3) <= DEFAULT_VIA_DRILL_NM


def test_an_unroutable_net_is_reported_and_not_half_laid():
    """A net with no path must be named, and must contribute no copper.

    Half a net's tracks is the worst outcome available: the board looks routed
    everywhere a person happens to look.
    """
    # Two pads on opposite sides of a wall of a third net's pads, on a
    # single-layer board so there is no way around or under it.
    pads = [RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
            RoutePad(net="A", x_nm=mm(19.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0))]
    for k in range(0, 21):
        pads.append(
            RoutePad(
                net="WALL",
                x_nm=mm(10.0),
                y_nm=mm(0.5) * k,
                w_nm=mm(1.0),
                h_nm=mm(1.0),
            )
        )
    result = route(
        pads,
        min_x_nm=0,
        min_y_nm=0,
        max_x_nm=mm(20.0),
        max_y_nm=mm(10.0),
        two_layer=False,
    )
    assert "A" in result.unrouted
    assert "A" not in result.routed
    assert not [t for t in result.tracks if t.net == "A"]
    assert any("unrouted" in w for w in result.warnings)


def test_the_back_layer_lets_a_blocked_net_through():
    """The same wall, with two layers available, is routable via a via pair."""
    pads = [RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
            RoutePad(net="A", x_nm=mm(19.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0))]
    for k in range(0, 21):
        pads.append(
            RoutePad(net="WALL", x_nm=mm(10.0), y_nm=mm(0.5) * k,
                     w_nm=mm(1.0), h_nm=mm(1.0))
        )
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(20.0), max_y_nm=mm(10.0)
    )
    assert result.unrouted.get("A") is None
    assert len([v for v in result.vias if v.net == "A"]) == 2


def test_a_rotated_part_refuses_to_route_rather_than_guessing():
    """Rotation has a known anchor bug in the emitter; routing to it would
    turn a latent placement bug into copper that lands on bare laminate."""
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    board.parts[0].rotated = True
    result = route_board(board)
    assert result.tracks == []
    assert any("rotated" in w for w in result.warnings)
    assert any("rotated" in w for w in board.warnings)
    # And the refusal names the nets it covers, on the board as well as in the
    # result -- otherwise route_completion reads a board with no copper as 100%.
    assert result.unrouted and set(board.unrouted_nets) == set(result.unrouted)
    assert board.route_completion == 0.0
    assert not board.is_routed


def test_a_single_terminal_net_is_reported_not_silently_skipped():
    result = route(
        [RoutePad(net="LONE", x_nm=mm(2.0), y_nm=mm(2.0), w_nm=mm(1.0), h_nm=mm(1.0))],
        min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
    )
    assert "LONE" in result.unrouted
    assert result.completion == 0.0


def test_pads_with_no_net_are_obstacles_only():
    """An unnetted pad must block copper without becoming something to route."""
    pads = [
        RoutePad(net="", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(2.0), h_nm=mm(2.0)),
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(9.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0)
    )
    assert "" not in result.routed and "" not in result.unrouted
    assert "A" in result.routed


def test_a_grid_too_coarse_for_the_pads_says_so_instead_of_shorting():
    """Two pads collapsing onto one lattice node is reported, not routed."""
    pads = [
        RoutePad(net="A", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(0.2), h_nm=mm(0.2)),
        RoutePad(net="A", x_nm=mm(5.05), y_nm=mm(5.0), w_nm=mm(0.2), h_nm=mm(0.2)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
        grid_nm=mm(1.0),
    )
    assert "too coarse" in result.unrouted["A"]


def test_a_refused_route_names_the_nets_it_gave_up_on():
    """A refusal is still a result about those nets.

    Naming none of them left routed and unrouted both empty, which
    RouteResult.completion reads as 1.0 -- a board with no copper at all
    reporting as fully routed, the exact class this module exists to prevent.
    """
    pads = [
        RoutePad(net="A", x_nm=mm(10.0), y_nm=mm(10.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(300.0), y_nm=mm(300.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="B", x_nm=mm(20.0), y_nm=mm(20.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="B", x_nm=mm(280.0), y_nm=mm(280.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        # One terminal only: nothing to route, so nothing to report.
        RoutePad(net="LONE", x_nm=mm(50.0), y_nm=mm(50.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(400.0), max_y_nm=mm(400.0)
    )

    assert not result.tracks
    assert sorted(result.unrouted) == ["A", "B"]
    assert all("node budget" in reason for reason in result.unrouted.values())
    assert result.completion == 0.0


def test_an_empty_board_area_names_its_nets_too():
    pads = [
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(1.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(2.0), y_nm=mm(2.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=0, max_y_nm=0)
    assert list(result.unrouted) == ["A"]
    assert result.completion == 0.0
