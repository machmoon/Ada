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
        ref = next(
            (p.value for p in fp.properties if p.key == "Reference"), fp.libraryNickname
        )
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


def test_tracks_of_different_nets_keep_their_clearance(routed):
    """Two nets one grid step apart would pass a node-by-node check and short.

    Sampled along each track rather than solved analytically: the point is to
    catch copper that is too close, and a dense sample of an axis-aligned
    segment finds that without reimplementing segment-to-segment distance.
    """
    board, _ = routed
    step = mm(0.05)
    points: dict[str, list[tuple[int, int, Layer]]] = {}
    for t in board.tracks:
        dx = (t.end_x_nm > t.start_x_nm) - (t.end_x_nm < t.start_x_nm)
        dy = (t.end_y_nm > t.start_y_nm) - (t.end_y_nm < t.start_y_nm)
        length = abs(t.end_x_nm - t.start_x_nm) + abs(t.end_y_nm - t.start_y_nm)
        for k in range(0, length + 1, step):
            points.setdefault(t.net, []).append(
                (t.start_x_nm + dx * k, t.start_y_nm + dy * k, t.layer)
            )

    # width + clearance, minus a nanometre of slack for integer rounding.
    required = mm(0.2) + mm(0.2) - 1
    nets = sorted(points)
    for i, a in enumerate(nets):
        for b in nets[i + 1 :]:
            for ax, ay, al in points[a]:
                for bx, by, bl in points[b]:
                    if al is not bl:
                        continue
                    d2 = (ax - bx) ** 2 + (ay - by) ** 2
                    assert d2 >= required**2, (
                        f"{a} and {b} come within "
                        f"{d2 ** 0.5 / 1e6:.3f} mm on {al}"
                    )


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
