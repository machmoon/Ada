# Silkscreen

**Reads and writes KiCad boards directly. No KiCad install, no plugin, no mouse automation.**

Silkscreen places components on a PCB with a CP-SAT solver and writes the result
straight into a `.kicad_pcb` file. The board file *is* the API — Silkscreen parses the
same S-expression format KiCad does, so it runs headless on any OS, in CI, with no
KiCad process alive anywhere.

```
54 passed — no network, no API key, no KiCad install
```

---

## Status

| Component | State |
|---|---|
| `kicad.py` — `.kicad_pcb` read/write | **Working** · 13 tests |
| `packing.py` — CP-SAT placer | **Working** · 26 tests |
| `netlist.py` — validated circuit IR | **Working** · 15 tests |
| Datasheet agents, overlay UI, guided cursor | Not built |

---

## Install

The engine needs **no KiCad**:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
```

Install KiCad only if you want to *look* at the boards Silkscreen writes:

| Platform | Command |
|---|---|
| macOS | `brew install --cask kicad` |
| Windows | `winget install KiCad.KiCad` |
| Debian/Ubuntu | `sudo add-apt-repository ppa:kicad/kicad-8.0-releases && sudo apt install kicad` |
| Fedora / any Linux | `flatpak install flathub org.kicad.KiCad` |

Or download from [kicad.org/download](https://www.kicad.org/download/). Then open the
output with **File → Open** in the PCB Editor, or `pcbnew placed.kicad_pcb`.

---

## Quickstart

```python
from silkscreen.kicad import (
    load_board, extract_parts, extract_nets, to_parts,
    apply_placements, set_board_outline, save_board,
)
from silkscreen.packing import pack

board  = load_board("my_board.kicad_pcb")
infos  = extract_parts(board)              # courtyard extents + pad offsets, in nm

result = pack(
    to_parts(infos, edge_refs={"J1"}),     # connector pinned to a board edge
    nets=extract_nets(infos),              # power rails down-weighted, not dropped
    clearance_nm=250_000,                  # 0.25 mm between courtyards
    time_limit_s=20.0,
)

apply_placements(board, infos, result.placements, result.board_height_nm)
set_board_outline(board, result.board_width_nm, result.board_height_nm)
save_board(board, "placed.kicad_pcb")
```

Reproduce it with `python scripts/demo.py`, on the 11-footprint STM32 + regulator +
motor-driver fixture in `backend/ref.txt`:

```
11 footprints, 6 nets
status     : feasible
board size : 19.60 x 15.05 mm  (295.0 mm²)
HPWL       : 52.4 mm
placed 11/11 -> placed.kicad_pcb  (~43.9 kB, reparses clean)
```

Identical across consecutive runs. Reproducibility requires `workers=1` (the default) —
CP-SAT's multi-worker portfolio interleaves results non-deterministically regardless of seed.
(That run omits `edge_refs`: the fixture has no connector, and naming a ref that isn't on the
board is an error, not a no-op.)

One caveat, since this is the only measured figure here: the fixture's nets are named
`0_device_pin_N` by the pipeline that generated it, so none match the power-rail heuristic
and the power-net weighting described below **does not fire on this board**. It is exercised
by unit tests, not by the number above.

---

## KiCad integration

Most AI-and-KiCad tools are plugins: they live inside KiCad's Python environment and
drive the IPC API, so they need KiCad running, a supported KiCad version, and a
platform KiCad's plugin loader is happy on. Silkscreen takes the other route — it
treats the board file as the interface.

| | Plugin / IPC approach | Silkscreen |
|---|---|---|
| Requires KiCad installed | Yes | **No** |
| Requires KiCad running | Yes | **No** |
| Headless / CI | Hard | **Native** |
| Platform lock | KiCad's plugin loader | **None — pure Python** |
| Testable without KiCad | No | **Yes, all 54 tests** |

### What it reads

`load_board()` → `extract_parts()` returns a `FootprintInfo` per footprint:

| Field | Source |
|---|---|
| `width_nm` / `height_nm` | `F.CrtYd` courtyard, falling back to the pad bounding box |
| `pad_offsets` | Per-pad offsets from the part's bottom-left, **flipped into a Y-up frame** |
| `pad_nets` | Net name per pad, used to build the wirelength objective |
| `library_id` | Footprint library nickname |

`extract_nets()` turns shared nets into HPWL nets; `extract_wires()` emits pad-pairs.

### What it writes

- **Footprint positions** — `apply_placements()` moves every footprint, converting the
  solver's Y-up frame back to KiCad's Y-down, anchoring on the courtyard, not the origin.
- **`Edge.Cuts` outline** — `set_board_outline()` draws the board rectangle. Without it
  the file has no boundary at all, and `Edge.Cuts` is both what KiCad measures edge
  clearance against and the only representation of the edge that `must_be_on_edge` was
  solved against.
- Pads, silkscreen, `F.Fab`, courtyards, nets, and zones pass through untouched.

### Units and compatibility

Everything is **integer nanometres**, KiCad's own internal unit, end to end. Unit
confusion between mm, mils, and nm is a silent, board-destroying class of bug, so there
are no floats in the pipeline; the solver quantises to a configurable grid (default
0.05 mm) rather than solving at 1 nm.

| | |
|---|---|
| Board format | `kicad_pcb` version `20240108` (KiCad 7–8) |
| Parser | `kiutils` 1.4.8 — pure Python |
| Solver | OR-Tools CP-SAT 9.15 |
| Python | 3.11+ |
| OS | macOS, Linux, Windows — identical behaviour |

Round-trip is verified by test: a written board reparses, preserves every footprint,
and has no two overlapping courtyards.

---

## The placer

CP-SAT. Variables are each part's bottom-left corner on an integer grid;
`AddNoOverlap2D` enforces disjointness, `AddMaxEquality` derives the bounding box,
`AddAbsEquality` linearises wirelength. The objective minimises board half-perimeter
plus total HPWL.

| Feature | Why it's there |
|---|---|
| **Real clearance** | Parts inflate by `clearance_nm/2` per side before no-overlap. Flush-packed boards can't be assembled. |
| **90° rotation** | A boolean per part swaps interval sizes; pin offsets rotate with two implications, not a centre approximation. |
| **Edge constraints** | Connectors and antennas pin to an edge via a disjunction over four half-reified literals. |
| **Symmetry breaking** | 24 identical caps admit 24! relabelings. Forcing a lexicographic order collapses each orbit to one representative — on a 27-part board, optimality in 0.1 s instead of 0.3 s. |
| **HPWL, one box per net** | A pairwise clique makes a 50-pad ground net contribute 1,225 terms that swamp every signal. A star overestimates length and makes layout depend on footprint order in the file. |
| **Power rails down-weighted, not dropped** | A decap's only connections are VCC and GND — drop power nets and it has no objective term and drifts. Measured: excluding power put a cap 9.65 mm from its IC pin; weighting at 0.25 brings it to 5.15 mm. |
| **Degrades, doesn't fail** | If CP-SAT finds nothing in budget, a deterministic shelf packer returns a valid layout flagged `FALLBACK`, warning about every constraint it couldn't honour. |

### Limits

2D packing with a wirelength objective is NP-hard. On real boards the solver returns
`FEASIBLE`, not `OPTIMAL`, inside 20 s — and Silkscreen reports it as `FEASIBLE`.
Coarsening the grid from 0.025 mm to 0.5 mm barely moves the result, so the bottleneck
is combinatorial, not resolution. **Treat the output as a strong starting placement,
not a proof.**

No support for: two-sided placement, locked/pre-placed parts, keepouts, connector
orientation, thermal relief, or differential pairs. The first two are what stop it
being usable iteratively on a real design.

---

## The circuit IR

`silkscreen.netlist` is the contract between a model and anything that touches KiCad.
A model proposes a `CircuitSpec`; nothing is instantiated until it validates, and *all*
failures are collected so the batch goes back as one repair prompt.

Rejects: a pin the device doesn't have · a part that doesn't exist · a bare part name
where a terminal is required (`C1` not `C1.1`) · a passive wired on one leg · a net with
fewer than two endpoints · unsupported passive types.

That fourth check matters most. Connecting *one specific leg* of a decoupling capacitor
to a specific pin is the most common operation in this domain, and an IR that can only
join whole parts to nets cannot express it at all.

---

## Layout

```
engine/
  silkscreen/
    units.py      nm/mm/mil conversion, grid quantisation
    packing.py    CP-SAT placer
    netlist.py    validated circuit IR
    kicad.py      .kicad_pcb read/write via kiutils
  tests/          54 tests — no network, no API keys, no KiCad
scripts/
  demo.py         end-to-end: read -> place -> write -> verify
backend/
  ref.txt         11-footprint board fixture
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `edge_refs names refs not on this board` | A ref in `edge_refs`/`rotatable_refs` matches no footprint. The error lists valid refs — a typo would otherwise become a silently missing constraint. |
| `status` is `FALLBACK` | CP-SAT found nothing in budget. Raise `time_limit_s`, coarsen `grid_nm`, or relax `max_board_nm`. Check `result.warnings`. |
| `status` is `FEASIBLE`, not `OPTIMAL` | Expected on real boards. The solution is valid but unproven. |
| Results differ between runs | You set `workers > 1`. Use `workers=1` for determinism. |
| KiCad won't open the output | Confirm the board is format `20240108` (KiCad 7–8). Older KiCad won't read it. |
| Parts overlap in KiCad's DRC | DRC measures pad/copper clearance; `clearance_nm` is *courtyard* clearance. Raise it. |

---

## Development

```bash
./.venv/bin/python -m pytest -q          # 54 tests
./.venv/bin/python -m ruff check engine  # lint
./.venv/bin/python scripts/demo.py       # end-to-end run
```

CI runs all three on Linux, macOS, and Windows.

## License

TBD.
