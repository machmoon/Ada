"""Demo-scale single-layer autorouter.

This is a **demo** router. It is not a production autorouter, it is not
competitive with one, and it must not be described as one. What it does is
small and checkable: it lays a uniform grid over the board, snaps every pad to
a grid node, and asks CP-SAT for a minimum-copper set of grid edges that
connects each net's nodes to each other while no two nets ever touch the same
node. Everything it returns is a path the solver actually found between real
pad locations, so a human can measure it against the board file.

What it deliberately does **not** do:

* **No second layer and no vias.** Everything is top copper. A board that
  needs a crossing needs a via, and a via is not modelled, so such a board is
  reported as partially routed rather than routed with an invented jumper.
* **No diagonal or any-angle traces.** Only the four orthogonal grid
  directions, so copper is longer than a real router's would be.
* **No design-rule checking.** Two nets can never share a grid node, and on a
  4-connected grid that is enough to prove that no two traces cross or
  overlap -- two edges of different nets can only intersect at a shared node.
  But the only clearance between two *parallel* traces is
  ``grid_nm - trace_width_nm``, and pad clearance is enforced only to the
  point where copper would touch. Run a real DRC before fabricating anything.
* **No footprint bodies as obstacles.** Only pads block. A trace may pass
  under a package body, which is fine for a chip resistor and wrong under a
  QFN.
* **No differential pairs, length matching, impedance control, copper pours,
  thermal reliefs, teardrops, net classes, or rotated/bottom-side parts.**
  Rotated or bottom-side parts are refused outright rather than routed from
  pad coordinates this module cannot verify.
* **No large boards.** The grid is refused above ``max_cells`` nodes instead
  of being attempted and hanging. On the default 0.5 mm grid that is roughly
  a 30 x 30 mm board.

What it will never do is invent a trace. If the solver finds nothing in the
time budget, or a net's pads cannot be reached on this grid, that net comes
back in :attr:`RouteResult.unrouted_nets` with a warning saying why. A
truthful failure is the intended output; decorative geometry is not an output
at all.

Nets are routed **one at a time**, in a fixed order, each net blocked by the
copper the previous ones already laid. That is what enforces "one net per grid
node", and it is a deliberate choice over one simultaneous model: measured on
the three-net regulator board in this repo, solving all nets together left
CP-SAT at a FEASIBLE solution of 159 grid edges after 20 s (best bound 63),
while routing the same nets in sequence finished in 10.4 s at 86 edges with
two of the three proven optimal. Sequential routing is order-dependent -- an
early net can wall off a later one -- and there is no rip-up and retry, so a
board that needs one is reported partial rather than rerouted.

Determinism: one worker, a fixed seed, sorted iteration everywhere, integer
nanometres throughout, and a fixed per-net time slice that does not depend on
how fast earlier nets finished. A net whose solve *completes* inside its slice
is reproducible anywhere. A net whose solve hits the time limit returns
CP-SAT's incumbent, which is reproducible on the same machine but, like
``packing.pack``, is not guaranteed identical on a slower one.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .board import BoardResult
from .packing import Layer
from .units import DEFAULT_CLEARANCE_NM, mm

__all__ = [
    "Segment",
    "RouteResult",
    "route_board",
]

#: CP-SAT seed. Reproducibility also needs ``num_workers = 1``; a multi-worker
#: portfolio search interleaves results non-deterministically whatever the seed
#: is. This is the same rule ``packing.pack`` follows.
_SEED = 0

#: Never hand CP-SAT a budget so small that a solve is pointless.
_MIN_SOLVE_S = 0.5

#: A pad with no net is still copper, so it blocks. Sentinel owner for those.
_NO_NET = ""


@dataclass(frozen=True)
class Segment:
    """One straight run of top-layer copper, in integer nanometres.

    Both endpoints are grid nodes and every segment is axis-aligned: the
    router works on a 4-connected grid, so a diagonal segment would mean a
    bug, not a shortcut.
    """

    net: str
    x1_nm: int
    y1_nm: int
    x2_nm: int
    y2_nm: int
    width_nm: int

    @property
    def length_nm(self) -> int:
        """Centreline length. Axis-aligned, so Manhattan distance is exact."""
        return abs(self.x2_nm - self.x1_nm) + abs(self.y2_nm - self.y1_nm)


@dataclass(frozen=True)
class RouteResult:
    """What the router managed, and what it did not.

    ``routed_nets`` and ``unrouted_nets`` together cover every net that had at
    least two pads to connect. A net with fewer than two pads needs no copper
    and appears in neither; a warning names those so a silently netless
    footprint cannot be mistaken for a routed one.
    """

    segments: tuple[Segment, ...]
    routed_nets: tuple[str, ...]
    unrouted_nets: tuple[str, ...]
    status: str
    grid_nm: int
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True only when every net that needed routing got routed."""
        return not self.unrouted_nets

    @property
    def total_length_nm(self) -> int:
        """Total centreline copper laid, in nanometres."""
        return sum(s.length_nm for s in self.segments)


@dataclass(frozen=True)
class _Grid:
    """Uniform routing grid. Node ``(ix, iy)`` sits at a cell centre."""

    x0_nm: int
    y0_nm: int
    nx: int
    ny: int
    pitch_nm: int

    @property
    def size(self) -> int:
        return self.nx * self.ny

    def node(self, ix: int, iy: int) -> int:
        return iy * self.nx + ix

    def coords(self, node: int) -> tuple[int, int]:
        return node % self.nx, node // self.nx

    def nm(self, node: int) -> tuple[int, int]:
        ix, iy = self.coords(node)
        return self.x0_nm + ix * self.pitch_nm, self.y0_nm + iy * self.pitch_nm

    def snap(self, x_nm: int, y_nm: int) -> int:
        """Nearest node, clamped into the grid. Integer arithmetic only."""
        ix = (x_nm - self.x0_nm + self.pitch_nm // 2) // self.pitch_nm
        iy = (y_nm - self.y0_nm + self.pitch_nm // 2) // self.pitch_nm
        ix = min(self.nx - 1, max(0, ix))
        iy = min(self.ny - 1, max(0, iy))
        return self.node(ix, iy)


@dataclass(frozen=True)
class _PadSite:
    """One pad's absolute copper rectangle in the Y-up solver frame."""

    net: str
    ref: str
    number: str
    x_nm: int
    y_nm: int
    half_w_nm: int
    half_h_nm: int

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"


@dataclass(frozen=True)
class _NetJob:
    """A net to route: its name and its distinct terminal nodes."""

    net: str
    terminals: tuple[int, ...]


def _ceil_div(a: int, b: int) -> int:
    """Ceiling division for possibly-negative ``a``, positive ``b``."""
    return -((-a) // b)


def _pad_sites(board: BoardResult) -> list[_PadSite]:
    """Absolute pad centres, in the Y-up frame ``PlacedPart`` uses.

    A footprint's pads are stored as offsets from its anchor, and the anchor
    sits at the courtyard centre, so the absolute centre is the part's
    bottom-left corner plus the courtyard half-extent plus the pad offset --
    exactly the arithmetic ``board.build_board`` uses to hand pin offsets to
    the placer.
    """
    sites: list[_PadSite] = []
    for part in sorted(board.parts, key=lambda p: p.ref):
        fp = part.footprint
        for pad in sorted(fp.pads, key=lambda p: (p.number, p.x_nm, p.y_nm)):
            sites.append(
                _PadSite(
                    net=pad.net,
                    ref=part.ref,
                    number=pad.number,
                    x_nm=part.x_nm + fp.courtyard_w_nm + pad.x_nm,
                    y_nm=part.y_nm + fp.courtyard_h_nm + pad.y_nm,
                    half_w_nm=pad.w_nm // 2,
                    half_h_nm=pad.h_nm // 2,
                )
            )
    return sites


def _build_grid(
    board: BoardResult, sites: list[_PadSite], grid_nm: int
) -> _Grid:
    """Grid spanning the board outline and every pad, whichever is wider."""
    xs = [0, board.width_nm]
    ys = [0, board.height_nm]
    for site in sites:
        xs += [site.x_nm - site.half_w_nm, site.x_nm + site.half_w_nm]
        ys += [site.y_nm - site.half_h_nm, site.y_nm + site.half_h_nm]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return _Grid(
        x0_nm=x0,
        y0_nm=y0,
        nx=(x1 - x0) // grid_nm + 1,
        ny=(y1 - y0) // grid_nm + 1,
        pitch_nm=grid_nm,
    )


def _grid_edges(grid: _Grid) -> list[tuple[int, int]]:
    """Every 4-connected grid edge, in a fixed order."""
    edges: list[tuple[int, int]] = []
    for iy in range(grid.ny):
        for ix in range(grid.nx):
            here = grid.node(ix, iy)
            if ix + 1 < grid.nx:
                edges.append((here, grid.node(ix + 1, iy)))
            if iy + 1 < grid.ny:
                edges.append((here, grid.node(ix, iy + 1)))
    return edges


def _pad_footprint_nodes(
    grid: _Grid, site: _PadSite, keepout_nm: int
) -> list[int]:
    """Nodes a trace centred there would drive copper into this pad."""
    half_w = site.half_w_nm + keepout_nm
    half_h = site.half_h_nm + keepout_nm
    ix_lo = max(0, _ceil_div(site.x_nm - half_w - grid.x0_nm, grid.pitch_nm))
    ix_hi = min(grid.nx - 1, (site.x_nm + half_w - grid.x0_nm) // grid.pitch_nm)
    iy_lo = max(0, _ceil_div(site.y_nm - half_h - grid.y0_nm, grid.pitch_nm))
    iy_hi = min(grid.ny - 1, (site.y_nm + half_h - grid.y0_nm) // grid.pitch_nm)
    return [
        grid.node(ix, iy)
        for iy in range(iy_lo, iy_hi + 1)
        for ix in range(ix_lo, ix_hi + 1)
    ]


def _reaches_pad(
    grid: _Grid, site: _PadSite, node: int, trace_width_nm: int
) -> bool:
    """Would copper at ``node`` actually overlap this pad?

    Snapping moves a terminal by up to half a grid pitch. If the pad is small
    enough that the nearest node lands outside it, a segment ending there does
    not connect to anything -- so the net is reported unrouted instead of
    being handed a trace that stops short of its pad.
    """
    nx_nm, ny_nm = grid.nm(node)
    reach = trace_width_nm // 2
    return (
        abs(nx_nm - site.x_nm) <= site.half_w_nm + reach
        and abs(ny_nm - site.y_nm) <= site.half_h_nm + reach
    )


def _spans_terminals(
    edges: list[tuple[int, int]],
    edge_ids: tuple[int, ...],
    terminals: tuple[int, ...],
) -> bool:
    """Is this edge set one connected component covering every terminal?

    The final gate before any segment is emitted. :func:`_trim_to_tree` should
    already guarantee it; this re-derives it from scratch so that a net whose
    copper does not actually join its pads is reported unrouted rather than
    drawn.
    """
    if not edge_ids:
        return False
    adjacency: dict[int, list[int]] = {}
    for edge_id in edge_ids:
        a, b = edges[edge_id]
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    start = terminals[0]
    if start not in adjacency:
        return False
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for neighbour in adjacency[node]:
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return set(terminals) <= seen and set(adjacency) <= seen


def _trim_to_tree(
    edges: list[tuple[int, int]],
    edge_ids: tuple[int, ...],
    terminals: tuple[int, ...],
) -> tuple[int, ...] | None:
    """Keep only the source-to-terminal paths inside the solver's own edges.

    A solve stopped by the time limit returns CP-SAT's incumbent, which can
    carry copper that is redundant (a loop back onto the same net) or, in
    principle, a circulation not attached to the source at all. This walks the
    solver's edge set breadth-first from the source and keeps the union of the
    paths to each other terminal.

    Every edge kept is an edge CP-SAT chose; nothing is added, only dropped.
    Returns ``None`` when the solver's edges do not in fact reach every
    terminal, which is the case that must never be papered over.
    """
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for edge_id in sorted(edge_ids):
        a, b = edges[edge_id]
        adjacency.setdefault(a, []).append((b, edge_id))
        adjacency.setdefault(b, []).append((a, edge_id))
    source = terminals[0]
    if source not in adjacency:
        return None

    parent: dict[int, tuple[int, int]] = {}
    seen = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for neighbour, edge_id in sorted(adjacency[node]):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            parent[neighbour] = (node, edge_id)
            queue.append(neighbour)
    if not set(terminals) <= seen:
        return None

    keep: set[int] = set()
    for terminal in terminals[1:]:
        node = terminal
        while node != source:
            previous, edge_id = parent[node]
            keep.add(edge_id)
            node = previous
    return tuple(sorted(keep)) if keep else None


def _solve_net(
    grid: _Grid,
    edges: list[tuple[int, int]],
    job: _NetJob,
    blocked: frozenset[int],
    time_limit_s: float,
) -> tuple[int, ...] | None:
    """Route one net around ``blocked`` nodes, or return ``None``.

    The formulation is a single-commodity flow over the grid graph, with ``k``
    the number of sinks (terminals other than the source):

    * ``node[c]`` -- the net occupies grid node ``c``. Nodes in ``blocked`` --
      another net's pad copper, or a node an earlier net already took -- get
      no variable at all, which is how "at most one net per grid node" is
      enforced.
    * ``use[e]`` -- the net uses undirected grid edge ``e``.
    * ``f[e]`` -- **signed** integer flow on ``e``, in ``[-k, k]``, positive
      meaning "towards the second endpoint". One signed variable rather than
      two directed ones is what rules out a two-cycle: a pair of opposing
      directed flows on one edge satisfies conservation at both ends, so it
      survives in a feasible solution as an edge attached to nothing.
    * ``use[e] <= |f[e]| <= k * use[e]`` -- flow only on used edges, and no
      used edge without flow. The lower bound is what stops the solver
      "using" an edge it never routes anything through.
    * ``inflow - outflow == demand`` at every node: ``-k`` at the source (the
      terminal with the smallest ``(x, y)``), ``+1`` at every other terminal,
      ``0`` elsewhere. Delivering one unit to each sink is what makes the
      answer a connected tree instead of a set of unrelated lines.
    * ``use[e] => node[a]`` and ``node[b]``; and for a non-terminal node,
      ``node[c] <= sum(incident use)``. Together these pin ``node`` to
      "touched by a used edge", so the net cannot squat on nodes it does not
      need and wall off the nets routed after it. Terminals are pinned to 1.
    * Minimise ``sum(use)``: fewest grid edges, and every edge is exactly one
      grid pitch of copper, so that is shortest total copper for this net.
    """
    if any(t in blocked for t in job.terminals):
        return None

    model = cp_model.CpModel()
    allowed = [node for node in range(grid.size) if node not in blocked]
    nodes = {n: model.NewBoolVar(f"n{n}") for n in allowed}
    incident: dict[int, list[cp_model.IntVar]] = {n: [] for n in allowed}
    balance: dict[int, list[cp_model.LinearExpr]] = {n: [] for n in allowed}
    used: dict[int, cp_model.IntVar] = {}
    terminal_set = set(job.terminals)
    sinks = set(job.terminals[1:])
    capacity = len(sinks)

    for edge_id, (a, b) in enumerate(edges):
        if a not in nodes or b not in nodes:
            continue
        use = model.NewBoolVar(f"e{edge_id}")
        flow = model.NewIntVar(-capacity, capacity, f"f{edge_id}")
        magnitude = model.NewIntVar(0, capacity, f"m{edge_id}")
        model.AddAbsEquality(magnitude, flow)
        model.Add(magnitude <= capacity * use)
        model.Add(magnitude >= use)
        model.AddImplication(use, nodes[a])
        model.AddImplication(use, nodes[b])
        incident[a].append(use)
        incident[b].append(use)
        balance[a].append(-flow)
        balance[b].append(flow)
        used[edge_id] = use

    for node in allowed:
        demand = 0
        if node == job.terminals[0]:
            demand = -capacity
        elif node in sinks:
            demand = 1
        if balance[node]:
            model.Add(sum(balance[node]) == demand)
        elif demand != 0:
            # An isolated terminal cannot be fed. Report it by hand rather
            # than through an empty constraint the solver would reject.
            return None
        if node in terminal_set:
            model.Add(nodes[node] == 1)
        elif incident[node]:
            model.Add(nodes[node] <= sum(incident[node]))
        else:
            model.Add(nodes[node] == 0)

    if not used:
        return None
    model.Minimize(sum(used.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.random_seed = _SEED
    solver.parameters.num_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return tuple(
        sorted(edge_id for edge_id, use in used.items() if solver.Value(use))
    )


def _runs(values: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted index list into inclusive consecutive runs."""
    runs: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        runs.append((start, previous))
        start = previous = value
    runs.append((start, previous))
    return runs


def _to_segments(
    grid: _Grid,
    edges: list[tuple[int, int]],
    net: str,
    edge_ids: tuple[int, ...],
    width_nm: int,
) -> list[Segment]:
    """Merge collinear adjacent grid edges into maximal straight runs."""
    horizontal: dict[int, list[int]] = {}
    vertical: dict[int, list[int]] = {}
    for edge_id in edge_ids:
        a, b = edges[edge_id]
        ax, ay = grid.coords(a)
        bx, by = grid.coords(b)
        if ay == by:
            horizontal.setdefault(ay, []).append(min(ax, bx))
        else:
            vertical.setdefault(ax, []).append(min(ay, by))

    segments: list[Segment] = []
    for iy in sorted(horizontal):
        for first, last in _runs(sorted(set(horizontal[iy]))):
            x1, y1 = grid.nm(grid.node(first, iy))
            x2, y2 = grid.nm(grid.node(last + 1, iy))
            segments.append(Segment(net, x1, y1, x2, y2, width_nm))
    for ix in sorted(vertical):
        for first, last in _runs(sorted(set(vertical[ix]))):
            x1, y1 = grid.nm(grid.node(ix, first))
            x2, y2 = grid.nm(grid.node(ix, last + 1))
            segments.append(Segment(net, x1, y1, x2, y2, width_nm))
    return segments


def _order_key(grid: _Grid, job: _NetJob) -> tuple[int, int, str]:
    """Routing order: most terminals first, then widest, then by name.

    Sequential routing is order-dependent, so the order has to be both fixed
    and sensible. Big, wide nets are the ones an already-crowded board most
    easily walls off, so they go first.
    """
    xs = [grid.coords(t)[0] for t in job.terminals]
    ys = [grid.coords(t)[1] for t in job.terminals]
    span = (max(xs) - min(xs)) + (max(ys) - min(ys))
    return (-len(job.terminals), -span, job.net)


def _status_for(routed: list[str], unrouted: list[str]) -> str:
    if not unrouted:
        return "routed"
    if routed:
        return "partial"
    return "failed"


def route_board(
    board: BoardResult,
    *,
    grid_nm: int = mm(0.5),
    trace_width_nm: int = mm(0.25),
    time_limit_s: float = 20.0,
    max_cells: int = 4000,
) -> RouteResult:
    """Route ``board``'s nets on a single top-copper grid.

    Args:
        board: A placed board. Pad positions are read from the placed
            footprints, so this must be the result of a real placement.
        grid_nm: Routing grid pitch. Coarser is faster and blockier; finer
            multiplies the model size by the square of the change.
        trace_width_nm: Emitted trace width. Also sets how far a pad blocks
            neighbouring nodes, since a trace centred one node away must not
            drive copper into another net's pad.
        time_limit_s: Total wall-clock budget, split evenly across the nets
            that need routing, with a 0.5 s floor per net -- so a budget below
            0.5 s per net is overrun rather than honoured. The split is fixed
            rather than rolled over from nets that finished early, so the
            budget a given net gets does not depend on machine speed.
        max_cells: Hard cap on grid nodes. Over this the board is refused with
            a warning rather than attempted -- a demo router that hangs is
            worse than one that says no.

    Returns:
        A :class:`RouteResult`. ``status`` is ``"routed"`` when every net that
        needed copper got it, ``"partial"`` when some did, ``"failed"`` when
        none did. Segments are only ever produced for nets in
        ``routed_nets``.

    Raises:
        ValueError: for a non-positive grid, width, budget or cell cap.
    """
    if grid_nm <= 0:
        raise ValueError("grid_nm must be positive")
    if trace_width_nm <= 0:
        raise ValueError("trace_width_nm must be positive")
    if time_limit_s <= 0:
        raise ValueError("time_limit_s must be positive")
    if max_cells <= 0:
        raise ValueError("max_cells must be positive")

    warnings: list[str] = []
    if grid_nm - trace_width_nm < DEFAULT_CLEARANCE_NM:
        warnings.append(
            f"Edge-to-edge gap between traces on adjacent grid lines is "
            f"{grid_nm - trace_width_nm} nm, below the "
            f"{DEFAULT_CLEARANCE_NM} nm default clearance."
        )

    sites = _pad_sites(board)
    by_net: dict[str, list[_PadSite]] = {}
    for site in sites:
        if site.net:
            by_net.setdefault(site.net, []).append(site)

    lonely = sorted(n for n, s in by_net.items() if len(s) < 2)
    if lonely:
        warnings.append(
            f"Nets with fewer than two pads need no copper and were skipped: "
            f"{lonely}."
        )
    candidates = sorted(n for n, s in by_net.items() if len(s) >= 2)
    if not candidates:
        warnings.append("No net has two or more pads; nothing to route.")
        return RouteResult((), (), (), "routed", grid_nm, tuple(warnings))

    def refuse(reasons: list[str]) -> RouteResult:
        """Give back the nets that needed copper, and say why none got it."""
        return RouteResult(
            segments=(),
            routed_nets=(),
            unrouted_nets=tuple(candidates),
            status="failed",
            grid_nm=grid_nm,
            warnings=tuple(warnings + reasons),
        )

    # Two nets may sit on adjacent grid lines, one pitch apart. A trace at
    # least as wide as the pitch would therefore overlap its neighbour: the
    # geometry would be a short, so it is refused rather than drawn.
    if trace_width_nm >= grid_nm:
        return refuse(
            [
                f"Refusing to route: trace_width_nm ({trace_width_nm}) is not "
                f"smaller than grid_nm ({grid_nm}), so two nets on adjacent "
                f"grid lines would overlap. Widen the grid or thin the trace."
            ]
        )

    rotated = sorted(p.ref for p in board.parts if p.rotated)
    off_top = sorted(p.ref for p in board.parts if p.layer is not Layer.TOP)
    if rotated or off_top:
        reasons = []
        if rotated:
            reasons.append(
                f"Refusing to route: {rotated} are rotated, and this router "
                f"does not transform pad offsets, so their pad coordinates "
                f"would be wrong."
            )
        if off_top:
            reasons.append(
                f"Refusing to route: {off_top} are not on the top layer, and "
                f"reaching them needs a via, which is not modelled."
            )
        return refuse(reasons)

    grid = _build_grid(board, sites, grid_nm)
    if grid.size > max_cells:
        return refuse(
            [
                f"Refusing to route: a {grid.nx} x {grid.ny} grid is "
                f"{grid.size} cells, over the {max_cells} cell limit. Use a "
                f"coarser grid_nm or raise max_cells."
            ]
        )

    edges = _grid_edges(grid)
    unrouted: list[str] = []

    # Snap terminals, refusing any pad the grid cannot actually reach.
    terminals: dict[str, tuple[int, ...]] = {}
    for net in candidates:
        nodes: list[int] = []
        missed: list[str] = []
        for site in by_net[net]:
            node = grid.snap(site.x_nm, site.y_nm)
            if not _reaches_pad(grid, site, node, trace_width_nm):
                missed.append(site.label)
                continue
            nodes.append(node)
        if missed:
            warnings.append(
                f"Net {net!r} not routed: pad(s) {missed} are smaller than "
                f"the {grid_nm} nm grid, so the nearest node misses the pad."
            )
            unrouted.append(net)
            continue
        distinct = sorted(set(nodes), key=grid.coords)
        if len(distinct) < 2:
            warnings.append(
                f"Net {net!r} not routed: all of its pads snap to one grid "
                f"node, so no copper would be proven to join them. Use a "
                f"finer grid_nm."
            )
            unrouted.append(net)
            continue
        terminals[net] = tuple(distinct)

    # Node ownership. A pad's copper belongs to its net and blocks every other
    # net; a node two different pads both claim is blocked for everyone,
    # unless exactly one net has a terminal there and therefore must have it.
    terminal_nets: dict[int, set[str]] = {}
    for net in sorted(terminals):
        for node in terminals[net]:
            terminal_nets.setdefault(node, set()).add(net)
    claim_nets: dict[int, set[str]] = {}
    keepout_nm = trace_width_nm // 2
    for site in sites:
        for node in _pad_footprint_nodes(grid, site, keepout_nm):
            claim_nets.setdefault(node, set()).add(site.net or _NO_NET)

    owner: dict[int, str | None] = {}
    for node in sorted(set(terminal_nets) | set(claim_nets)):
        claiming = terminal_nets.get(node, set())
        if len(claiming) == 1:
            net = next(iter(claiming))
            # A terminal does not override foreign copper on the same node.
            # Granting it would let this net's trace run through another net's
            # pad -- a short in copper that the preflight would then wave
            # through as "routed". Blocking the node instead leaves the
            # terminal unreachable, so the net is reported unrouted, which is
            # the honest answer.
            contested = {other for other in claim_nets.get(node, set())
                         if other != net}
            if contested:
                owner[node] = None
                if net not in unrouted:
                    named = ", ".join(repr(o) if o else "an unnetted pad"
                                      for o in sorted(contested))
                    warnings.append(
                        f"Net {net!r} not routed: one of its pads shares a "
                        f"grid node with copper belonging to {named}. Routing "
                        f"through it would short them."
                    )
                    unrouted.append(net)
                continue
            owner[node] = net
            continue
        if len(claiming) > 1:
            winner = sorted(claiming)[0]
            owner[node] = winner
            for loser in sorted(claiming)[1:]:
                if loser not in unrouted:
                    warnings.append(
                        f"Net {loser!r} not routed: one of its pads snaps to "
                        f"the same grid node as net {winner!r}. Use a finer "
                        f"grid_nm."
                    )
                    unrouted.append(loser)
            continue
        pads = claim_nets.get(node, set())
        real = sorted(n for n in pads if n)
        owner[node] = real[0] if len(pads) == 1 and real else None

    jobs = [
        _NetJob(net, terminals[net])
        for net in sorted(terminals)
        if net not in unrouted
    ]
    forbidden = {
        job.net: {
            node for node, own in owner.items() if own != job.net
        }
        for job in jobs
    }

    # Hardest first: most terminals, then widest span, then name. Fixed order
    # and a fixed per-net budget, so the sequence does not depend on how fast
    # any earlier net happened to solve.
    jobs.sort(key=lambda job: _order_key(grid, job))
    routed_edges: dict[str, tuple[int, ...]] = {}
    if jobs:
        slice_s = max(_MIN_SOLVE_S, time_limit_s / len(jobs))
        taken: set[int] = set()
        for job in jobs:
            blocked = frozenset(forbidden[job.net] | taken)
            found = _solve_net(grid, edges, job, blocked, slice_s)
            if found is not None:
                found = _trim_to_tree(edges, found, job.terminals)
            if not found or not _spans_terminals(edges, found, job.terminals):
                warnings.append(
                    f"Net {job.net!r} not routed: no path was found on this "
                    f"grid within {slice_s:.1f}s, around the pads and the "
                    f"nets already routed. No copper was invented for it."
                )
                unrouted.append(job.net)
                continue
            routed_edges[job.net] = found
            for edge_id in found:
                taken.update(edges[edge_id])

    segments: list[Segment] = []
    for net in sorted(routed_edges):
        segments += _to_segments(
            grid, edges, net, routed_edges[net], trace_width_nm
        )
    segments.sort(key=lambda s: (s.net, s.x1_nm, s.y1_nm, s.x2_nm, s.y2_nm))

    routed = sorted(routed_edges)
    return RouteResult(
        segments=tuple(segments),
        routed_nets=tuple(routed),
        unrouted_nets=tuple(sorted(set(unrouted))),
        status=_status_for(routed, unrouted),
        grid_nm=grid_nm,
        warnings=tuple(warnings),
    )
