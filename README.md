# Silkscreen

**Agentic PCB design that shows its work.**

Silkscreen is the layer of a circuit board that carries the human-readable marks —
reference designators, polarity dots, the annotations that tell a person what the
board is doing. That is what this project is for. Not "AI designs your board while
you watch," but "AI designs your board and *explains every decision*, on the board
itself and on your screen."

---

## Status

Early. The layout engine is real, tested, and runs. The agent layer and the UI are
being built. This README distinguishes the two throughout — nothing here claims a
capability that isn't in the repo.

| Component | State |
|---|---|
| `engine/silkscreen/packing.py` — CP-SAT placer | **Working**, 26 tests |
| `engine/silkscreen/netlist.py` — validated circuit IR | **Working**, 15 tests |
| `engine/silkscreen/kicad.py` — `.kicad_pcb` read/write | **Working**, 13 tests |
| Datasheet understanding agents | Not built |
| Overlay assistant | Not built |
| Guided cursor ("show me where") | Not built |
| Web UI | Not built |

```
54 passed
```

---

## What works today

Give it a KiCad board with footprints and nets, and it will place the components:

```python
from silkscreen import pack
from silkscreen.kicad import (
    load_board, extract_parts, extract_nets, to_parts,
    apply_placements, set_board_outline, save_board,
)

board = load_board("my_board.kicad_pcb")
infos = extract_parts(board)

result = pack(
    to_parts(infos, edge_refs={"J1"}),   # connector pinned to a board edge
    nets=extract_nets(infos),            # power rails down-weighted, not dropped
    clearance_nm=250_000,                # 0.25 mm between courtyards
    time_limit_s=20.0,
)

apply_placements(board, infos, result.placements, result.board_height_nm)
set_board_outline(board, result.board_width_nm, result.board_height_nm)
save_board(board, "placed.kicad_pcb")
```

Measured on an 11-footprint STM32 + regulator + motor-driver board:

```
11 footprints, 6 nets
status     : feasible
board size : 19.60 x 15.05 mm  (295.0 mm²)
HPWL       : 52.4 mm
placed 11/11 -> placed.kicad_pcb  (43,925 bytes, reparses clean)
```

Identical across three consecutive runs. Reproducibility requires `workers=1`,
which is the default — CP-SAT's multi-worker portfolio interleaves results
non-deterministically regardless of the seed.

One caveat about that number, since it is the only measured result here: the
fixture's nets are named `0_device_pin_N` by the pipeline that generated it, so
**none of them match the power-rail heuristic** and the power-net weighting
described below does not fire on this particular board. It is exercised by unit
tests, not by the figure above.

**No KiCad installation is required.** Board files are parsed and written directly
through `kiutils`, which is pure Python. It runs the same on macOS, Linux, and
Windows.

---

## The layout engine

The placer is a CP-SAT model. Decision variables are the bottom-left corner of each
part on an integer grid; `AddNoOverlap2D` enforces disjointness over fixed-size
intervals; `AddMaxEquality` derives the board bounding box; `AddAbsEquality`
linearises Manhattan wirelength. The objective minimises a weighted sum of board
half-perimeter and total wirelength.

What it does that a naive version of this doesn't:

- **Real clearance.** Every part is inflated by `clearance_nm / 2` on each side before
  the no-overlap constraint, so neighbouring courtyards end up a specified distance
  apart. Packing parts flush at 0 mm produces boards nobody can assemble.
- **Correct units.** Everything is nanometres — KiCad's internal unit — as integers,
  end to end. `pcbnew`'s `GetBoundingBox()` values go in without conversion. The solver
  quantises to a configurable grid (default 0.05 mm) rather than solving at 1 nm.
- **Optional 90° rotation.** Modelled exactly: a boolean per part switches the interval
  sizes, and pin offsets are rotated with two implications rather than approximated by
  the part centre.
- **Edge constraints.** USB connectors, antennas, and mounting holes can be pinned to a
  board edge via a disjunction over four half-reified literals.
- **Symmetry breaking.** A board with 24 identical capacitors admits 24! relabelings of
  the same physical layout. Interchangeable, unwired parts are forced into a fixed
  lexicographic order, which collapses each orbit to one representative. On a
  27-part board this proved optimality in 0.1 s instead of 0.3 s.
- **Half-perimeter wirelength, one bounding box per net.** HPWL is the standard
  placement proxy, and it replaces two worse models. A *clique* of pairwise
  distances makes a 50-pad ground net contribute 1,225 terms that swamp every
  signal net. A *star* to one arbitrary terminal overestimates length, turns the
  hub into a gravity well carrying n−1 terms against each spoke's one, and — since
  the hub is whichever pad was read first — makes the layout depend on footprint
  ordering inside the `.kicad_pcb` text.
- **Power rails are down-weighted, not dropped.** Because HPWL costs one box per
  net regardless of pad count, rails can stay in the objective at reduced weight.
  This matters more than it sounds: a decoupling capacitor's *only* connections to
  the IC it bypasses are VCC and GND, so excluding power nets leaves it with no
  objective term at all and it drifts wherever the packer finds room. Measured on
  a decap test board, excluding power put the cap 9.65 mm from its IC pin;
  weighting it at 0.25 brings it to 5.15 mm — sitting against the IC edge.
- **It degrades instead of failing.** If CP-SAT finds nothing inside the time limit,
  a deterministic shelf packer returns a valid — if unlovely — layout, flagged as
  `PackStatus.FALLBACK`. Raising an exception and losing the work is not an option a
  layout tool gets to take. The fallback packs by size alone, so it also warns
  explicitly about every hard constraint it could not honour rather than returning a
  quietly invalid board.
- **It writes a board outline.** `set_board_outline` emits `Edge.Cuts`. Without it the
  file has no boundary at all — and `Edge.Cuts` is both what KiCad measures edge
  clearance against and the only representation of the edge that `must_be_on_edge`
  was solved against.

### What it does not do

2D packing with a wirelength objective is NP-hard, and on real boards the solver
returns `FEASIBLE`, not `OPTIMAL`, inside a 20 s budget. Coarsening the grid from
0.025 mm to 0.5 mm barely changes the result, so the bottleneck is combinatorial,
not resolution. Treat the output as a strong starting placement, not a proof.

Beyond that, the placer has no concept of: **two-sided placement** (everything is
treated as one layer), **locked or pre-placed parts** (you cannot fix a connector at
a known coordinate and re-solve the rest), **keepouts and mounting holes**,
**connector orientation** (`must_be_on_edge` puts a part on an edge but says nothing
about which way it faces), **thermal relief**, or **differential pairs**. Of these,
the first two are what stop it being usable iteratively on a real design.

---

## The circuit IR

`silkscreen.netlist` is the contract between a language model and anything that
touches KiCad. A model proposes a `CircuitSpec`; nothing is instantiated until it
validates. Failures are collected — all of them, not the first — so the whole batch
can go back to the model as a single repair prompt.

It rejects, among other things:

- endpoints naming a pin the device doesn't have
- endpoints naming a part that doesn't exist
- a bare part name where a terminal is required (`C1` instead of `C1.1`)
- a passive wired on only one leg, which would sit floating on the board
- a net with fewer than two endpoints
- passive types outside the supported set

That fifth check matters more than it looks. Connecting *a specific leg* of a
decoupling capacitor to a specific pin is the single most common operation in this
entire domain, and an IR that can only join whole parts to nets cannot express it.

---

## Layout

```
engine/
  silkscreen/
    units.py      nm/mm/mil conversion, grid quantisation
    packing.py    CP-SAT placer
    netlist.py    validated circuit IR
    kicad.py      .kicad_pcb read/write via kiutils
  tests/          40 tests, no network, no API keys, no KiCad install
```

The engine has no network calls and no model calls by design, so the parts that need
to be correct can be tested without either.

---

## Development

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest -q
```

---

## License

TBD.
