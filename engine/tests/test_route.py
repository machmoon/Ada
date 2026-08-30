"""Tests for the demo-scale single-layer autorouter.

The claim ``route.py`` makes is narrow and checkable: everything it returns is
copper the solver actually found between real pad locations, and it never
invents a trace. So the tests that matter most here are geometric, and they are
computed from ``PlacedPart``/``Pad`` rather than from the router's own helpers
-- a check expressed in terms of the code under test shares its blind spot.

Two rectangle models are used, deliberately in opposite directions:

* the **core** rectangle is the stroke without its round end caps, so it
  under-states the copper. Connectivity is proven with it: if two runs of
  copper overlap even under the pessimistic model, they really are joined.
* the **capped** rectangle is the bounding box of the stroke including the
  caps, so it over-states the copper. Shorts are ruled out with it: if nothing
  touches under the generous model, nothing touches.

Solver cost is kept to two ``route_board`` calls on one small board (a
module-scoped fixture plus one repeat for the determinism check). Every
contract and refusal test uses a hand-built ``BoardResult``, all of which
return before CP-SAT is ever constructed.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.board import BoardResult, PlacedPart, build_board
from silkscreen.fab import fab_files, gerber_copper
from silkscreen.footprints import Footprint, Pad
from silkscreen.netlist import parse_circuit_spec
from silkscreen.order import OrderOptions, order_manifest, preflight
from silkscreen.packing import Layer
from silkscreen.route import RouteResult, Segment, route_board
from silkscreen.units import mm

#: The outline margin the Gerber writers shift every coordinate by. Written out
#: here rather than imported from ``fab`` for the same reason ``test_fab.py``
#: does it: the point is to check the module against an independent number.
MARGIN_NM = mm(2.0)

#: Budget for both the placement and the routing. The board is small enough
#: that CP-SAT finishes well inside this; it is a ceiling, not a target.
TIME_LIMIT_S = 8.0

#: A three-net regulator: one SOT-223 and two 1206 capacitors. Small enough to
#: route in seconds, and its GND net has three pads, so the router's
#: multi-terminal flow model is actually exercised.
DEMO_SPEC = {
    "devices": {"U1": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "22uF"},
        "C2": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["U1.VIN", "C1.1"],
        "+3V3": ["U1.VOUT", "C2.1"],
        "GND": ["U1.GND", "C1.2", "C2.2"],
    },
}


@pytest.fixture(scope="module")
def spec():
    return parse_circuit_spec(json.dumps(DEMO_SPEC))


@pytest.fixture(scope="module")
def board(spec):
    return build_board(spec, time_limit_s=TIME_LIMIT_S)


@pytest.fixture(scope="module")
def routes(board):
    """One routing run, shared by every test that reads real segments."""
    return route_board(board, time_limit_s=TIME_LIMIT_S)


# ------------------------------------------------------------------- geometry


def _pad_rects(board: BoardResult) -> list[tuple[str, str, tuple[int, ...]]]:
    """``(net, label, rect)`` for every pad, in the Y-up solver frame.

    A pad's absolute centre is the part's bottom-left corner plus the courtyard
    half-extent plus the pad offset -- the same arithmetic ``build_board`` uses
    to hand pin offsets to the placer, spelled out here rather than borrowed.
    """
    out = []
    for part in board.parts:
        fp = part.footprint
        for pad in fp.pads:
            cx = part.x_nm + fp.courtyard_w_nm + pad.x_nm
            cy = part.y_nm + fp.courtyard_h_nm + pad.y_nm
            out.append(
                (
                    pad.net,
                    f"{part.ref}.{pad.number}",
                    (
                        cx - pad.w_nm // 2,
                        cy - pad.h_nm // 2,
                        cx + pad.w_nm // 2,
                        cy + pad.h_nm // 2,
                    ),
                )
            )
    return out


def _core_rect(seg: Segment) -> tuple[int, int, int, int]:
    """The stroke widened only across its own axis: copper minus the end caps.

    Pessimistic on purpose. Anything this model says is joined is joined.
    """
    half = seg.width_nm // 2
    grow_x = half if seg.x1_nm == seg.x2_nm else 0
    grow_y = half if seg.y1_nm == seg.y2_nm else 0
    return (
        min(seg.x1_nm, seg.x2_nm) - grow_x,
        min(seg.y1_nm, seg.y2_nm) - grow_y,
        max(seg.x1_nm, seg.x2_nm) + grow_x,
        max(seg.y1_nm, seg.y2_nm) + grow_y,
    )


def _capped_rect(seg: Segment) -> tuple[int, int, int, int]:
    """Bounding box of the stroke including its round end caps.

    Optimistic on purpose. Anything this model says is clear is clear.
    """
    half = seg.width_nm // 2
    return (
        min(seg.x1_nm, seg.x2_nm) - half,
        min(seg.y1_nm, seg.y2_nm) - half,
        max(seg.x1_nm, seg.x2_nm) + half,
        max(seg.y1_nm, seg.y2_nm) + half,
    )


def _overlaps(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """Positive-area overlap. Two rectangles that merely abut do not count."""
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _components(rects: list[tuple[int, ...]]) -> int:
    """How many connected components ``rects`` forms, joined by overlap."""
    if not rects:
        return 0
    adjacency: dict[int, list[int]] = {i: [] for i in range(len(rects))}
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _overlaps(rects[i], rects[j]):
                adjacency[i].append(j)
                adjacency[j].append(i)

    unseen = set(range(len(rects)))
    count = 0
    while unseen:
        count += 1
        stack = [unseen.pop()]
        while stack:
            node = stack.pop()
            for neighbour in adjacency[node]:
                if neighbour in unseen:
                    unseen.discard(neighbour)
                    stack.append(neighbour)
    return count


# ------------------------------------------------------------ hand-built boards


def _part(ref: str, pads: list[Pad], **kwargs) -> PlacedPart:
    """A placed part around a throwaway footprint, with no solver involved."""
    footprint = Footprint(
        name=f"FP_{ref}",
        pads=pads,
        courtyard_w_nm=kwargs.pop("courtyard_w_nm", mm(1.0)),
        courtyard_h_nm=kwargs.pop("courtyard_h_nm", mm(1.0)),
    )
    return PlacedPart(ref=ref, footprint=footprint, **kwargs)


def _hand_board(
    parts: list[PlacedPart], *, width_nm: int = mm(10), height_nm: int = mm(10)
) -> BoardResult:
    return BoardResult(
        parts=parts,
        nets=sorted({pad.net for p in parts for pad in p.footprint.pads if pad.net}),
        width_nm=width_nm,
        height_nm=height_nm,
        solver_status="optimal",
    )


def _pad(net: str = "N1") -> list[Pad]:
    return [Pad("1", 0, 0, mm(1.0), mm(1.0), net=net)]


def _two_pads_one_net(**kwargs) -> BoardResult:
    """Two parts sharing net ``N1``, far enough apart that copper is needed.

    ``kwargs`` are applied to the first part, which is how the rotated and
    off-top refusals are provoked without a placement.
    """
    return _hand_board(
        [
            _part("R1", _pad(), x_nm=0, y_nm=0, **kwargs),
            _part("R2", _pad(), x_nm=mm(6), y_nm=0),
        ]
    )


# ============================================================== routing itself


def test_demo_board_routes_every_net(routes):
    """The headline claim, measured rather than assumed.

    If this ever comes back "partial", the tests below still hold -- they are
    written against ``routed_nets`` -- but the router stopped doing the thing
    the rest of this file is checking, so it is asserted up front.
    """
    assert routes.status == "routed", (
        f"expected a complete route, got {routes.status!r} with "
        f"unrouted_nets={routes.unrouted_nets} and warnings={routes.warnings}"
    )
    assert routes.routed_nets == ("+3V3", "GND", "VIN")
    assert routes.unrouted_nets == ()
    assert routes.complete is True
    assert routes.segments, "a routed board must carry copper"
    assert routes.grid_nm == mm(0.5)


def test_every_segment_is_axis_aligned_and_has_length(routes):
    """The router works a 4-connected grid, so a diagonal segment is a bug,
    and a zero-length one is a dot of copper that connects nothing."""
    for seg in routes.segments:
        assert seg.x1_nm == seg.x2_nm or seg.y1_nm == seg.y2_nm, (
            f"{seg} is diagonal; a 4-connected grid cannot produce one"
        )
        assert seg.length_nm > 0, f"{seg} has no length"
        assert seg.width_nm == mm(0.25)


def test_total_length_is_the_sum_of_its_segments(routes):
    assert routes.total_length_nm == sum(s.length_nm for s in routes.segments)
    assert routes.total_length_nm > 0


def test_each_routed_net_is_one_connected_component_with_its_pads(routes, board):
    """The property that makes a route a route.

    Segment copper *plus* the net's own pad copper must form a single blob.
    Endpoint-matching would be the wrong test: two runs that meet in the middle
    of a third, or meet inside a pad, are connected copper even though no pair
    of endpoints coincides.
    """
    pads = _pad_rects(board)
    for net in routes.routed_nets:
        segments = [s for s in routes.segments if s.net == net]
        net_pads = [rect for pad_net, _, rect in pads if pad_net == net]
        assert segments, f"net {net!r} is reported routed but has no copper"
        assert len(net_pads) >= 2, f"net {net!r} should have needed routing"

        rects = [_core_rect(s) for s in segments] + net_pads
        assert _components(rects) == 1, (
            f"net {net!r} is reported routed but its {len(segments)} "
            f"segment(s) and {len(net_pads)} pad(s) fall into "
            f"{_components(rects)} disconnected islands of copper"
        )


def test_every_pad_of_a_routed_net_is_touched_by_copper(routes, board):
    """A trace that stops one grid pitch short of its pad connects nothing.

    Checked under the pessimistic core model, so an overlap here is real copper
    on real copper, not the corner of an end cap.
    """
    pads = _pad_rects(board)
    for net in routes.routed_nets:
        segments = [s for s in routes.segments if s.net == net]
        for pad_net, label, rect in pads:
            if pad_net != net:
                continue
            assert any(_overlaps(_core_rect(s), rect) for s in segments), (
                f"pad {label} on net {net!r} is not overlapped by any of the "
                f"{len(segments)} segments routed for it"
            )


def test_no_segment_shorts_a_different_net(routes, board):
    """THE SAFETY PROPERTY. A short here is a dead board, not a cosmetic bug.

    Both halves are checked under the generous capped model: copper of one net
    must not touch copper of another, whether that copper is a trace or a pad.
    """
    segments = list(routes.segments)
    for i, first in enumerate(segments):
        for second in segments[i + 1 :]:
            if first.net == second.net:
                continue
            assert not _overlaps(_capped_rect(first), _capped_rect(second)), (
                f"SHORT: trace on net {first.net!r} {first} overlaps trace on "
                f"net {second.net!r} {second}"
            )

    for seg in segments:
        for pad_net, label, rect in _pad_rects(board):
            if not pad_net or pad_net == seg.net:
                continue
            assert not _overlaps(_capped_rect(seg), rect), (
                f"SHORT: trace on net {seg.net!r} {seg} runs over pad {label}, "
                f"which belongs to net {pad_net!r}"
            )


def test_routing_the_same_board_twice_gives_identical_segments(board, routes):
    """One worker, a fixed seed, sorted iteration, a fixed per-net budget.

    Same machine and same budget, so a solve that completes and a solve that
    returns an incumbent are both expected to reproduce exactly.
    """
    again = route_board(board, time_limit_s=TIME_LIMIT_S)
    assert again.segments == routes.segments
    assert again.routed_nets == routes.routed_nets
    assert again.unrouted_nets == routes.unrouted_nets
    assert again.status == routes.status
    assert again.total_length_nm == routes.total_length_nm


# ======================================================= contract and refusals


def test_a_board_whose_nets_all_have_one_pad_needs_no_routing():
    """Nothing to connect is not a failure; it is a board with no copper due."""
    result = route_board(
        _hand_board(
            [
                _part("R1", _pad("N1"), x_nm=0),
                _part("R2", _pad("N2"), x_nm=mm(5)),
            ]
        ),
        time_limit_s=2.0,
    )
    assert result.status == "routed"
    assert result.segments == ()
    assert result.routed_nets == ()
    assert result.unrouted_nets == ()
    assert result.complete is True
    assert any("fewer than two pads" in w for w in result.warnings), (
        "a netless-looking footprint must be named, not silently skipped"
    )


def test_a_grid_over_max_cells_is_refused_without_inventing_copper():
    """A demo router that hangs is worse than one that says no."""
    board = _hand_board(
        [
            _part("R1", _pad(), x_nm=0, y_nm=0),
            _part("R2", _pad(), x_nm=mm(90), y_nm=mm(90)),
        ],
        width_nm=mm(100),
        height_nm=mm(100),
    )
    result = route_board(board, time_limit_s=2.0)

    assert result.status == "failed"
    assert result.segments == (), "a refused board must not come back with copper"
    assert result.routed_nets == ()
    assert result.unrouted_nets == ("N1",), "every net that needed copper is named"
    assert result.complete is False
    assert any(
        "over the 4000 cell limit" in w for w in result.warnings
    ), f"no warning explains the refusal: {result.warnings}"


def test_a_trace_as_wide_as_the_grid_is_refused_as_a_drawn_short():
    """PINNED BEHAVIOUR: this is a returned ``failed`` result, not a raise.

    ``grid_nm`` and ``trace_width_nm`` are the two arguments a caller is most
    likely to get wrong together, and the module treats their collision as a
    board condition rather than a programming error. Recorded here so a change
    of mind about that is a visible test change.
    """
    board = _two_pads_one_net()
    for width_nm in (mm(0.5), mm(0.7)):
        result = route_board(
            board, grid_nm=mm(0.5), trace_width_nm=width_nm, time_limit_s=2.0
        )
        assert result.status == "failed", f"trace_width_nm={width_nm} was accepted"
        assert result.segments == ()
        assert result.unrouted_nets == ("N1",)
        assert any(
            "is not smaller than grid_nm" in w for w in result.warnings
        ), f"no warning explains the refusal: {result.warnings}"
        assert any(
            "below the" in w and "default clearance" in w for w in result.warnings
        ), "the clearance the geometry would violate should be stated too"


def test_a_rotated_part_is_refused_rather_than_routed():
    """The router does not transform pad offsets, so a rotated part's pad
    coordinates would be wrong -- and wrong coordinates are worse than none."""
    result = route_board(_two_pads_one_net(rotated=True), time_limit_s=2.0)
    assert result.status == "failed"
    assert result.segments == ()
    assert result.unrouted_nets == ("N1",)
    assert any("are rotated" in w for w in result.warnings), result.warnings


def test_a_bottom_layer_part_is_refused_rather_than_routed():
    """Reaching the underside needs a via, and a via is not modelled."""
    result = route_board(_two_pads_one_net(layer=Layer.BOTTOM), time_limit_s=2.0)
    assert result.status == "failed"
    assert result.segments == ()
    assert result.unrouted_nets == ("N1",)
    assert any("not on the top layer" in w for w in result.warnings), result.warnings


def test_a_part_the_solver_never_sided_is_also_refused():
    """PINNED BEHAVIOUR, and it disagrees with ``fab.py`` on purpose or by
    accident -- worth having written down either way.

    ``route.py`` tests ``layer is not Layer.TOP``, so a part left on
    ``Layer.EITHER`` is refused as if it were on the bottom. ``fab.py`` tests
    ``layer is Layer.BOTTOM`` instead, and so puts the same part's copper on
    the top layer. Nothing in the pipeline produces ``EITHER`` today.
    """
    result = route_board(_two_pads_one_net(layer=Layer.EITHER), time_limit_s=2.0)
    assert result.status == "failed"
    assert any("not on the top layer" in w for w in result.warnings), result.warnings


def test_pads_that_all_snap_to_one_grid_node_are_reported_not_drawn():
    """Two pads inside one grid cell cannot be *proven* joined by grid copper,
    so the net is reported unrouted rather than handed a zero-length trace."""
    board = _hand_board(
        [
            _part(
                "R1",
                [
                    Pad("1", -50_000, 0, mm(1.0), mm(1.0), net="N1"),
                    Pad("2", 50_000, 0, mm(1.0), mm(1.0), net="N1"),
                ],
            )
        ]
    )
    result = route_board(board, time_limit_s=2.0)
    assert result.status == "failed"
    assert result.segments == ()
    assert result.unrouted_nets == ("N1",)
    assert any("snap to one grid node" in w for w in result.warnings), result.warnings


def test_pads_smaller_than_the_grid_are_reported_not_drawn():
    """Snapping moves a terminal by up to half a pitch. If the nearest node
    lands outside a small pad, a trace ending there connects to nothing."""
    tiny = [Pad("1", -2_250_000, 0, 100_000, 100_000, net="N1")]
    board = _hand_board(
        [
            _part("R1", tiny, x_nm=0, courtyard_w_nm=mm(3), courtyard_h_nm=mm(3)),
            _part("R2", tiny, x_nm=mm(5), courtyard_w_nm=mm(3), courtyard_h_nm=mm(3)),
        ]
    )
    result = route_board(board, time_limit_s=2.0)
    assert result.status == "failed"
    assert result.segments == ()
    assert result.unrouted_nets == ("N1",)
    assert any(
        "smaller than the" in w and "grid" in w for w in result.warnings
    ), result.warnings


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"grid_nm": 0}, "grid_nm must be positive"),
        ({"grid_nm": -mm(1)}, "grid_nm must be positive"),
        ({"trace_width_nm": 0}, "trace_width_nm must be positive"),
        ({"trace_width_nm": -1}, "trace_width_nm must be positive"),
        ({"time_limit_s": 0.0}, "time_limit_s must be positive"),
        ({"time_limit_s": -1.0}, "time_limit_s must be positive"),
        ({"max_cells": 0}, "max_cells must be positive"),
        ({"max_cells": -3}, "max_cells must be positive"),
    ],
)
def test_non_positive_arguments_raise_valueerror(kwargs, message):
    """A caller error is a raise; a board condition is a ``failed`` result."""
    with pytest.raises(ValueError, match=message):
        route_board(_two_pads_one_net(), **{"time_limit_s": 2.0, **kwargs})


def test_complete_is_true_exactly_when_nothing_is_unrouted():
    """``complete`` is derived, so it cannot disagree with the net lists."""
    base = {"segments": (), "grid_nm": mm(0.5)}
    assert RouteResult(
        routed_nets=("A", "B"), unrouted_nets=(), status="routed", **base
    ).complete
    assert not RouteResult(
        routed_nets=("A",), unrouted_nets=("B",), status="partial", **base
    ).complete
    assert not RouteResult(
        routed_nets=(), unrouted_nets=("A",), status="failed", **base
    ).complete
    # An empty board needed no copper, so nothing is outstanding.
    assert RouteResult(
        routed_nets=(), unrouted_nets=(), status="routed", **base
    ).complete


# =============================================================== the fab wiring


def _draws(gerber: str) -> list[str]:
    return [ln for ln in gerber.splitlines() if ln.endswith("D01*")]


def _flashes(gerber: str) -> list[str]:
    return [ln for ln in gerber.splitlines() if ln.endswith("D03*")]


def test_copper_without_routes_is_pads_only(board):
    """Routing is a separate, later step: unrouted copper is still valid, it
    just has no connections in it."""
    copper = gerber_copper(board)
    assert _flashes(copper), "the pads must still be there"
    assert _draws(copper) == [], "an unrouted board has no traces to stroke"


def test_copper_with_routes_strokes_one_line_per_segment(board, routes):
    plain = gerber_copper(board)
    wired = gerber_copper(board, routes=routes)

    assert len(_draws(wired)) == len(routes.segments)
    assert _flashes(wired) == _flashes(plain), "the pads must not move or vanish"

    lines = wired.splitlines()
    assert lines[0] == "%FSLAX46Y46*%", "the format spec must still come first"
    assert lines[-1] == "M02*", "the stream must still terminate"
    for line in lines:
        if line.startswith("X"):
            assert "-" not in line, f"negative coordinate {line!r}; fabs reject these"


def test_bottom_copper_never_carries_traces(board, routes):
    """The router is top-side only, so its segments belong on F.Cu alone."""
    bottom = gerber_copper(board, routes=routes, bottom=True)
    assert _draws(bottom) == [], "a single-layer router must not write B.Cu traces"


def test_fab_files_with_routes_still_returns_twelve_layers(board, routes):
    files = fab_files(board, routes=routes)
    assert len(files) == 12
    assert len({f.filename for f in files}) == 12

    top = next(f for f in files if f.filename == "silkscreen-F_Cu.GTL")
    assert top.content == gerber_copper(board, routes=routes)

    bottom = next(f for f in files if f.filename == "silkscreen-B_Cu.GBL")
    assert _draws(bottom.content) == []


def test_trace_coordinates_are_the_segment_shifted_by_the_outline_margin(routes, board):
    """One Gerber unit is one nanometre, so the shifted integers appear
    verbatim. The expected literals are built from the ``Segment`` here, not
    from ``fab``'s own arithmetic."""
    wired = gerber_copper(board, routes=routes)
    seg = routes.segments[0]
    move = f"X{seg.x1_nm + MARGIN_NM}Y{seg.y1_nm + MARGIN_NM}D02*"
    draw = f"X{seg.x2_nm + MARGIN_NM}Y{seg.y2_nm + MARGIN_NM}D01*"
    assert move in wired, f"segment {seg} should open with the literal {move!r}"
    assert draw in wired, f"segment {seg} should stroke to the literal {draw!r}"

    # Every segment, not just the first.
    for other in routes.segments:
        assert (
            f"X{other.x2_nm + MARGIN_NM}Y{other.y2_nm + MARGIN_NM}D01*" in wired
        ), other


# ============================================================= the order wiring


def test_preflight_without_routes_still_blocks_on_unrouted_nets(board, spec):
    """Unchanged behaviour: a placed but unrouted board is not orderable."""
    pre = preflight(board, spec=spec)
    assert pre.orderable is False
    blocker = next(i for i in pre.issues if i.code == "unrouted-nets")
    assert blocker.title == "3 net(s) have no copper connecting them"


def test_preflight_with_a_fully_routed_board_is_orderable(board, spec, routes):
    """The gate stops firing on its own once a router lands, which is what
    ``order.py``'s docstring promises."""
    assert routes.complete, "this test is only meaningful on a complete route"
    pre = preflight(board, spec=spec, routes=routes)
    assert "unrouted-nets" not in {i.code for i in pre.issues}
    assert pre.blockers == ()
    assert pre.orderable is True, f"still blocked by {[i.code for i in pre.issues]}"


def test_a_partial_route_clears_only_the_nets_it_actually_routed(board, spec):
    """The whole point of the gate: a net the router gave up on keeps
    blocking, and the blocker counts what is left rather than what was tried."""
    partial = RouteResult(
        segments=(Segment("GND", 0, 0, mm(1), 0, mm(0.25)),),
        routed_nets=("GND",),
        unrouted_nets=("+3V3", "VIN"),
        status="partial",
        grid_nm=mm(0.5),
    )
    pre = preflight(board, spec=spec, routes=partial)
    assert pre.orderable is False

    blocker = next(i for i in pre.issues if i.code == "unrouted-nets")
    assert blocker.title == "2 net(s) have no copper connecting them", (
        "the blocker should count only the nets still open, not all three"
    )
    assert "+3V3" in blocker.detail and "VIN" in blocker.detail
    assert "GND" not in blocker.detail, "the routed net must not be listed as open"


def test_order_manifest_from_a_routed_preflight_reports_orderable(board, spec, routes):
    pre = preflight(board, spec=spec, routes=routes)
    manifest = order_manifest(board, OrderOptions(), pre)
    assert manifest["orderable"] is True
    assert manifest["blocker_count"] == 0
    assert json.loads(json.dumps(manifest))["orderable"] is True
    assert manifest["requires_human_approval"] is True, (
        "a routed board is orderable, which is still not the same as ordered"
    )


def _single_pad_part(ref, net, x_mm, y_mm, size_mm=1.0):
    """One square pad on its own net, centred at ``(x_mm, y_mm)``."""
    fp = Footprint(
        name="P",
        pads=[
            Pad(
                number="1",
                x_nm=0,
                y_nm=0,
                w_nm=mm(size_mm),
                h_nm=mm(size_mm),
                net=net,
            )
        ],
        courtyard_w_nm=mm(size_mm / 2),
        courtyard_h_nm=mm(size_mm / 2),
    )
    return PlacedPart(
        ref=ref,
        footprint=fp,
        x_nm=mm(x_mm) - mm(size_mm / 2),
        y_nm=mm(y_mm) - mm(size_mm / 2),
    )

def test_a_terminal_does_not_override_a_foreign_pads_copper():
    """A pad sharing a grid node with another net's copper is refused.

    Granting the node to the terminal's net would let its trace run through
    the other net's pad, which is a short in copper that preflight would then
    accept as a routed net. Refusing the net is the honest outcome.
    """
    parts = [
        _single_pad_part("A1", "NETA", 3.0, 3.0),
        _single_pad_part("A2", "NETA", 8.0, 8.0),
        _single_pad_part("B1", "NETB", 8.3, 8.0),
        _single_pad_part("B2", "NETB", 3.0, 8.0),
    ]
    board = BoardResult(
        parts=parts,
        nets=["NETA", "NETB"],
        width_nm=mm(14),
        height_nm=mm(14),
        solver_status="optimal",
    )
    result = route_board(board, time_limit_s=5.0)

    assert set(result.unrouted_nets) == {"NETA", "NETB"}
    assert not result.segments, "a contested node must not be routed through"
    assert any("short" in w for w in result.warnings), (
        "the caller must be told why the net was refused"
    )
