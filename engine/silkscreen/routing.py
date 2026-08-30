"""Copper routing: turn placed pads into tracks and vias.

Before this module the pipeline emitted a board whose pads carried net numbers
and whose copper was empty. KiCad opens such a file and draws a ratsnest --
thin lines showing what *should* connect -- which looks like a routed board at
a glance and is not one. Nothing in the project routed anything; placement was
the end of the line.

This is a two-layer grid maze router: A* over a uniform lattice with an
explicit via cost, nets routed one at a time, each net grown from its first
terminal outward so later terminals connect to the nearest point of the tree
already laid rather than back to the first pad.

**It is not a competitive autorouter and does not pretend to be.** A uniform
grid cannot reach every pin of a fine-pitch package, and a sequential router
paints itself into corners that a rip-up-and-retry router escapes. So the
contract here is honesty, not completeness: every net it cannot route is named
in :attr:`RouteResult.unrouted` and left as ratsnest for a human to finish. A
router that silently dropped a connection would be worse than no router --
that is the same bug class :mod:`silkscreen.kicad` guards against, where the
run reports success and the board is wrong.

**Coordinate frame.** This module works entirely in the solver's Y-up frame,
like :mod:`silkscreen.packing`. The flip to KiCad's Y-down frame happens once,
in the emitter, alongside the flip already applied to footprint anchors.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .packing import Layer
from .units import mm

__all__ = [
    "RoutePad",
    "Track",
    "Via",
    "RouteResult",
    "route",
    "DEFAULT_ROUTE_GRID_NM",
    "DEFAULT_TRACK_WIDTH_NM",
    "DEFAULT_ROUTE_CLEARANCE_NM",
]

#: Routing lattice pitch. 0.25 mm divides the 0.5 mm pitch of the finest
#: package these footprints generate (LQFP), so adjacent pins land on distinct
#: nodes instead of collapsing onto one.
DEFAULT_ROUTE_GRID_NM = mm(0.25)

#: Track width. 0.2 mm carries a few hundred milliamps on 1 oz copper and is
#: above every mainstream fab's minimum, so a board using it is orderable.
DEFAULT_TRACK_WIDTH_NM = mm(0.2)

#: Copper-to-copper clearance the router keeps. Deliberately equal to the
#: track width so the two together fit inside twice the grid pitch.
DEFAULT_ROUTE_CLEARANCE_NM = mm(0.2)

DEFAULT_VIA_DIAMETER_NM = mm(0.4)
DEFAULT_VIA_DRILL_NM = mm(0.2)

#: What a layer change costs, in grid steps. High enough that the router keeps
#: a net on one layer when it can, low enough that it will hop to get through.
_VIA_COST_STEPS = 12

#: Refuse rather than grind: a lattice this size means the grid is far too fine
#: for the board, and searching it would take minutes for a worse result.
_MAX_NODES = 4_000_000

#: The two layers this router uses, in the order it prefers them.
_LAYERS: tuple[Layer, ...] = (Layer.TOP, Layer.BOTTOM)

#: Marks a node no net may use, where two nets' pad clearances overlap.
_CONTESTED = "\x00contested"


@dataclass(frozen=True)
class RoutePad:
    """One pad to route to, in absolute Y-up board coordinates."""

    net: str
    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int
    layer: Layer = Layer.TOP
    #: For error messages only; never used in geometry.
    ref: str = ""
    number: str = ""


@dataclass(frozen=True)
class Track:
    """A straight copper segment on one layer, in the Y-up frame."""

    start_x_nm: int
    start_y_nm: int
    end_x_nm: int
    end_y_nm: int
    layer: Layer
    net: str
    width_nm: int

    @property
    def length_nm(self) -> int:
        return abs(self.end_x_nm - self.start_x_nm) + abs(
            self.end_y_nm - self.start_y_nm
        )


@dataclass(frozen=True)
class Via:
    """A through via joining the two copper layers."""

    x_nm: int
    y_nm: int
    net: str
    diameter_nm: int
    drill_nm: int


@dataclass
class RouteResult:
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    #: Nets fully connected by the tracks above.
    routed: list[str] = field(default_factory=list)
    #: Nets left as ratsnest, each with the reason it could not be finished.
    unrouted: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM

    @property
    def routed_length_nm(self) -> int:
        return sum(t.length_nm for t in self.tracks)

    @property
    def completion(self) -> float:
        """Fraction of routable nets that came out fully connected."""
        total = len(self.routed) + len(self.unrouted)
        return 1.0 if total == 0 else len(self.routed) / total

    def summary(self) -> str:
        return (
            f"{len(self.routed)}/{len(self.routed) + len(self.unrouted)} nets routed, "
            f"{len(self.tracks)} tracks, {len(self.vias)} vias, "
            f"{self.routed_length_nm / 1_000_000:.1f} mm of copper"
        )


def _disc(radius_nm: int, grid_nm: int) -> tuple[tuple[int, int], ...]:
    """Grid offsets strictly inside ``radius_nm`` of the origin node.

    Strictly inside, not within: a neighbour at exactly the required clearance
    is legal, and rounding it out would cost real routing channels.
    """
    reach = int(radius_nm // grid_nm)
    offsets = [
        (dx, dy)
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
        if math.hypot(dx * grid_nm, dy * grid_nm) < radius_nm
    ]
    return tuple(sorted(offsets))


def route(
    pads: list[RoutePad],
    *,
    min_x_nm: int,
    min_y_nm: int,
    max_x_nm: int,
    max_y_nm: int,
    grid_nm: int = DEFAULT_ROUTE_GRID_NM,
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM,
    clearance_nm: int = DEFAULT_ROUTE_CLEARANCE_NM,
    via_diameter_nm: int = DEFAULT_VIA_DIAMETER_NM,
    via_drill_nm: int = DEFAULT_VIA_DRILL_NM,
    two_layer: bool = True,
) -> RouteResult:
    """Route every multi-terminal net over the placed pads.

    Args:
        pads: Every pad on the board, absolute, in the solver's Y-up frame.
            Pads whose net is empty are obstacles and nothing else.
        min_x_nm..max_y_nm: The rectangle copper may occupy, normally the board
            outline. Nodes outside it are unusable, so no track leaves the
            board.
        two_layer: Allow the back copper layer and vias. With this off the
            router is single-layer and will leave more nets unrouted, which is
            the honest outcome rather than a crossing short.

    Returns:
        A :class:`RouteResult` whose ``unrouted`` names every net that did not
        come out fully connected, with the reason. Callers must surface that;
        a partially routed board reported as routed is the failure this whole
        module is written to avoid.
    """
    result = RouteResult(track_width_nm=track_width_nm)
    if max_x_nm <= min_x_nm or max_y_nm <= min_y_nm:
        result.warnings.append("board area is empty; nothing routed")
        return result

    nx = int((max_x_nm - min_x_nm) // grid_nm) + 1
    ny = int((max_y_nm - min_y_nm) // grid_nm) + 1
    layers = _LAYERS if two_layer else (Layer.TOP,)
    if nx * ny * len(layers) > _MAX_NODES:
        result.warnings.append(
            f"routing grid would be {nx}x{ny} nodes, over the {_MAX_NODES} node "
            f"budget; skipped routing (raise grid_nm to route this board)"
        )
        return result

    def node_x(i: int) -> int:
        return min_x_nm + i * grid_nm

    def node_y(j: int) -> int:
        return min_y_nm + j * grid_nm

    # ---- obstacles -------------------------------------------------------
    # A node is reserved for at most one net. Reserved-for-me is free; anything
    # else is a wall. Two nets' pad clearances overlapping leaves the node
    # contested, and no net may cross it.
    reserved: dict[Layer, dict[tuple[int, int], str]] = {
        layer: {} for layer in layers
    }
    pad_margin = clearance_nm + track_width_nm // 2

    def reserve(layer: Layer, key: tuple[int, int], net: str) -> None:
        if layer not in reserved:
            return
        held = reserved[layer].get(key)
        if held is None:
            reserved[layer][key] = net
        elif held != net:
            reserved[layer][key] = _CONTESTED

    for pad in sorted(pads, key=lambda p: (p.net, p.ref, p.number, p.x_nm, p.y_nm)):
        layer = Layer.TOP if pad.layer is not Layer.BOTTOM else Layer.BOTTOM
        lo_i = int(math.floor((pad.x_nm - pad.w_nm / 2 - pad_margin - min_x_nm) / grid_nm))
        hi_i = int(math.ceil((pad.x_nm + pad.w_nm / 2 + pad_margin - min_x_nm) / grid_nm))
        lo_j = int(math.floor((pad.y_nm - pad.h_nm / 2 - pad_margin - min_y_nm) / grid_nm))
        hi_j = int(math.ceil((pad.y_nm + pad.h_nm / 2 + pad_margin - min_y_nm) / grid_nm))
        for i in range(max(0, lo_i), min(nx - 1, hi_i) + 1):
            for j in range(max(0, lo_j), min(ny - 1, hi_j) + 1):
                reserve(layer, (i, j), pad.net or _CONTESTED)

    # ---- ports -----------------------------------------------------------
    # Each pad's port is the lattice node nearest its centre, which lies inside
    # its own copper. Forcing it back to the pad's own net undoes any contest
    # written above: a track ending on the pad it belongs to is not a
    # violation, whatever else crowds that node.
    ports: dict[str, list[tuple[Layer, int, int]]] = {}
    for pad in pads:
        if not pad.net:
            continue
        layer = Layer.TOP if pad.layer is not Layer.BOTTOM else Layer.BOTTOM
        if layer not in reserved:
            continue
        i = min(nx - 1, max(0, int(round((pad.x_nm - min_x_nm) / grid_nm))))
        j = min(ny - 1, max(0, int(round((pad.y_nm - min_y_nm) / grid_nm))))
        reserved[layer][(i, j)] = pad.net
        ports.setdefault(pad.net, [])
        if (layer, i, j) not in ports[pad.net]:
            ports[pad.net].append((layer, i, j))

    track_disc = _disc(track_width_nm + clearance_nm, grid_nm)
    via_disc = _disc(via_diameter_nm // 2 + clearance_nm + track_width_nm // 2, grid_nm)

    # ---- net order -------------------------------------------------------
    # Shortest and simplest first. Sequential routers are order-dependent and
    # this order is a heuristic, not an optimum -- but it is deterministic,
    # which matters more here: the same design must route the same way twice.
    def net_key(net: str) -> tuple[int, int, str]:
        nodes = ports[net]
        span = max(n[1] for n in nodes) - min(n[1] for n in nodes) + (
            max(n[2] for n in nodes) - min(n[2] for n in nodes)
        )
        return (len(nodes), span, net)

    routable = sorted((n for n, p in ports.items() if len(p) >= 2), key=net_key)
    for net, nodes in sorted(ports.items()):
        if len(nodes) < 2:
            # One reachable terminal is not a connection to make. This is
            # normal for a net whose other pads all collapsed onto the same
            # lattice node, which is worth saying out loud.
            result.unrouted[net] = (
                f"only {len(nodes)} distinct grid node(s) among its pads; "
                f"the routing grid is too coarse for this footprint"
            )

    for net in routable:
        paths, failure = _route_net(
            ports[net],
            net=net,
            nx=nx,
            ny=ny,
            layers=layers,
            reserved=reserved,
            via_cost=_VIA_COST_STEPS,
        )
        if failure is not None:
            result.unrouted[net] = failure
            continue
        result.routed.append(net)
        for path in paths:
            _commit(
                path,
                net=net,
                reserved=reserved,
                track_disc=track_disc,
                via_disc=via_disc,
                nx=nx,
                ny=ny,
            )
            result.tracks.extend(
                _to_tracks(path, net, node_x, node_y, track_width_nm)
            )
            result.vias.extend(
                _to_vias(path, net, node_x, node_y, via_diameter_nm, via_drill_nm)
            )

    if result.unrouted:
        result.warnings.append(
            f"{len(result.unrouted)} of "
            f"{len(result.unrouted) + len(result.routed)} nets left unrouted: "
            + ", ".join(sorted(result.unrouted))
        )
    return result


_Node = tuple[Layer, int, int]


def _route_net(
    terminals: list[_Node],
    *,
    net: str,
    nx: int,
    ny: int,
    layers: tuple[Layer, ...],
    reserved: dict[Layer, dict[tuple[int, int], str]],
    via_cost: int,
) -> tuple[list[list[_Node]], str | None]:
    """Grow one net's tree, terminal by terminal.

    Returns the paths found and ``None``, or ``([], reason)`` if any terminal
    could not be reached. A net is all-or-nothing on purpose: half a net's
    tracks laid down is a board that looks routed in the places you happen to
    look at.
    """
    tree: set[_Node] = {terminals[0]}
    paths: list[list[_Node]] = []
    for target in terminals[1:]:
        if target in tree:
            continue
        path = _astar(
            sources=tree,
            goal=target,
            net=net,
            nx=nx,
            ny=ny,
            layers=layers,
            reserved=reserved,
            via_cost=via_cost,
            # A path may run along copper this net already owns; that is a
            # T-junction, which is exactly what a multi-pin net wants.
            owned=tree,
        )
        if path is None:
            return [], (
                f"no clear path to one of its {len(terminals)} pads; "
                f"the channel is blocked by earlier nets or by pad clearance"
            )
        paths.append(path)
        tree.update(path)
    return paths, None


def _astar(
    *,
    sources: set[_Node],
    goal: _Node,
    net: str,
    nx: int,
    ny: int,
    layers: tuple[Layer, ...],
    reserved: dict[Layer, dict[tuple[int, int], str]],
    via_cost: int,
    owned: set[_Node],
) -> list[_Node] | None:
    """Shortest path from any node in ``sources`` to ``goal``.

    Costs are in grid steps; a layer change costs ``via_cost`` of them. The
    heuristic is Manhattan distance in steps, which never overestimates because
    every move costs at least one step and a via costs more.
    """
    _, gi, gj = goal

    def passable(node: _Node) -> bool:
        layer, i, j = node
        if not (0 <= i < nx and 0 <= j < ny):
            return False
        if node in owned:
            return True
        held = reserved[layer].get((i, j))
        return held is None or held == net

    def h(i: int, j: int) -> int:
        return abs(i - gi) + abs(j - gj)

    open_heap: list[tuple[int, int, int, _Node]] = []
    best: dict[_Node, int] = {}
    came: dict[_Node, _Node] = {}
    tie = 0
    for src in sorted(sources, key=lambda n: (n[0].value, n[1], n[2])):
        if not passable(src):
            continue
        best[src] = 0
        tie += 1
        heapq.heappush(open_heap, (h(src[1], src[2]), tie, 0, src))
    if not open_heap:
        return None

    while open_heap:
        _, _, cost, node = heapq.heappop(open_heap)
        if cost > best.get(node, cost):
            continue
        if node == goal:
            path = [node]
            while path[-1] in came:
                path.append(came[path[-1]])
            path.reverse()
            return path
        layer, i, j = node
        moves: list[tuple[_Node, int]] = [
            ((layer, i + 1, j), 1),
            ((layer, i - 1, j), 1),
            ((layer, i, j + 1), 1),
            ((layer, i, j - 1), 1),
        ]
        for other in layers:
            if other is not layer:
                moves.append(((other, i, j), via_cost))
        for nxt, step in moves:
            if not passable(nxt):
                continue
            new_cost = cost + step
            if new_cost >= best.get(nxt, new_cost + 1):
                continue
            best[nxt] = new_cost
            came[nxt] = node
            tie += 1
            heapq.heappush(
                open_heap, (new_cost + h(nxt[1], nxt[2]), tie, new_cost, nxt)
            )
    return None


def _commit(
    path: list[_Node],
    *,
    net: str,
    reserved: dict[Layer, dict[tuple[int, int], str]],
    track_disc: tuple[tuple[int, int], ...],
    via_disc: tuple[tuple[int, int], ...],
    nx: int,
    ny: int,
) -> None:
    """Reserve the copper a routed path occupies, plus its clearance halo.

    The halo is what keeps the next net from being laid one grid step away
    from this one. Without it the router would produce tracks that pass DRC
    node-by-node and short in the real world.
    """

    def mark(layer: Layer, i: int, j: int) -> None:
        if 0 <= i < nx and 0 <= j < ny and reserved[layer].get((i, j)) is None:
            reserved[layer][(i, j)] = net

    for index, (layer, i, j) in enumerate(path):
        reserved[layer][(i, j)] = net
        for dx, dy in track_disc:
            mark(layer, i + dx, j + dy)
        changes_layer = (index and path[index - 1][0] is not layer) or (
            index + 1 < len(path) and path[index + 1][0] is not layer
        )
        if changes_layer:
            # A via is a barrel through both layers: it has to clear copper on
            # the side the path never touches, too.
            for other in reserved:
                reserved[other][(i, j)] = net
                for dx, dy in via_disc:
                    mark(other, i + dx, j + dy)


def _to_tracks(
    path: list[_Node], net: str, node_x, node_y, width_nm: int
) -> list[Track]:
    """Collapse a node path into the fewest straight segments that draw it.

    One ``segment`` per grid step would be electrically identical and
    unreadable -- a 40 mm track as 160 objects that a human cannot select or
    drag. Splitting only at layer changes and corners gives the same copper in
    the shape someone can edit.
    """
    tracks: list[Track] = []

    def segment(start: _Node, end: _Node) -> None:
        if (start[1], start[2]) == (end[1], end[2]):
            return
        tracks.append(
            Track(
                start_x_nm=node_x(start[1]),
                start_y_nm=node_y(start[2]),
                end_x_nm=node_x(end[1]),
                end_y_nm=node_y(end[2]),
                layer=start[0],
                net=net,
                width_nm=width_nm,
            )
        )

    runs: list[list[_Node]] = [[path[0]]]
    for node in path[1:]:
        if node[0] is runs[-1][-1][0]:
            runs[-1].append(node)
        else:
            runs.append([node])

    for run in runs:
        start = run[0]
        heading: tuple[int, int] | None = None
        for prev, node in zip(run, run[1:]):
            step = (_sign(node[1] - prev[1]), _sign(node[2] - prev[2]))
            if heading is not None and step != heading:
                segment(start, prev)
                start = prev
            heading = step
        segment(start, run[-1])
    return tracks


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _to_vias(
    path: list[_Node], net: str, node_x, node_y, diameter_nm: int, drill_nm: int
) -> list[Via]:
    """One via wherever the path changes layer."""
    vias: list[Via] = []
    for index in range(1, len(path)):
        if path[index][0] is not path[index - 1][0]:
            _, i, j = path[index]
            vias.append(
                Via(
                    x_nm=node_x(i),
                    y_nm=node_y(j),
                    net=net,
                    diameter_nm=diameter_nm,
                    drill_nm=drill_nm,
                )
            )
    return vias
