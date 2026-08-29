# Silkscreen

**Reads and writes KiCad boards directly. No KiCad install, no plugin, no mouse automation.**

Silkscreen places components on a PCB with a CP-SAT solver and writes the result
straight into a `.kicad_pcb` file. The board file *is* the API — Silkscreen parses the
same S-expression format KiCad does, so it runs headless on any OS, in CI, with no
KiCad process alive anywhere.

```
170 passed — no network, no API key, no KiCad install
```

---

## Status

| Component | State |
|---|---|
| `kicad.py` — `.kicad_pcb` read/write | **Working** · 13 tests |
| `packing.py` — CP-SAT placer | **Working** · 36 tests |
| `netlist.py` — validated circuit IR | **Working** · 15 tests |
| `footprints.py` + `board.py` — land patterns, board emission | **Working** · 16 tests |
| `agents/` — datasheet, propose, review, pipeline | **Working** · 22 tests |
| `agents/retrieval.py` — page-cited datasheet retrieval | **Working** · 15 tests |
| `agents/resilience.py` — provider failover | **Working** · 14 tests |
| `mcp/` — MCP server over stdio | **Working** · 23 tests |
| `service/` — Cloud Run + Firestore cache | **Working** · 16 tests |
| Overlay UI, guided cursor | Not built (mockups only) |

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
motor-driver fixture in `engine/tests/fixtures/`:

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

## Prompt to PCB

```bash
echo 'GOOGLE_API_KEY=...' >> .env
python -m silkscreen "a 3.3V motor driver board around an STM32F030" \
    --datasheet "AMS1117-3.3=https://.../ams1117.pdf" \
    -o board.kicad_pcb
```

```
intent ──► datasheets ──► propose ──► validate/repair ──► place ──► .kicad_pcb
                                          │                            │
                                          └────────► review ───────────┘
```

Two gates sit between the model and the board.

**Structural.** The proposal goes through the circuit IR before anything is built.
Every validation error is collected and fed back as one repair prompt; the loop is
bounded and `result.repair_rounds` reports how many corrections it took.

**Semantic.** A reviewer re-reads the datasheets and is prompted to *refute* the
design — an agent asked "is this correct?" says yes. Findings are graded
blocker / marginal / note and cite the datasheet page. Findings naming parts that
aren't on the board are dropped rather than surfaced.

Everything below `agents/` is model-free and network-free, so the whole pipeline —
including its failure paths — is tested against a scripted model with no API key.

### Generating footprints

Emitting a board means generating real land patterns: pads at real coordinates, a
courtyard, silkscreen. `footprints.py` builds them parametrically — chip passives
(0402–1210), SOT-23, SOT-223, SOIC, LQFP — so a board can be written with no KiCad
install and no footprint library on disk. Courtyards are fitted to enclose every pad
*and* the body, which is what makes the placer's clearance guarantee mean anything.

Coverage is narrow on purpose and it **raises rather than guessing**. A wrong footprint
is the most common cause of a dead first-spin board; inventing a land pattern for an
unrecognised package would be worse than refusing. Capacitor packages widen with value
(a 22 µF part does not fit an 0603), and output is byte-identical across runs, so a
regenerated board diffs cleanly in git.

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
| Testable without KiCad | No | **Yes, all 170 tests** |

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

## Prompt to PCB

```bash
cp .env.example .env          # add your GOOGLE_API_KEY
python -m silkscreen "a 3.3V motor driver with an STM32F103" -o board.kicad_pcb
```

Reads any datasheets you point it at, proposes a circuit into the validated IR,
repairs it against the validation errors until it passes, places it with CP-SAT,
writes the `.kicad_pcb`, then reviews its own work and cites the page.

| Stage | Module |
|---|---|
| Datasheet reading (Gemini native PDF vision) | `agents/datasheet.py` |
| Retrieval over datasheet text, page-cited | `agents/retrieval.py` |
| Circuit proposal into the IR | `agents/propose.py` |
| Validation + bounded repair loop | `netlist.py` |
| Footprint generation, board emission | `footprints.py`, `board.py` |
| Placement | `packing.py` |
| Adversarial review | `agents/review.py` |

Every model call goes through a provider chain that validates the response
before accepting it, so a rate limit or a malformed reply falls through to the
next tier instead of failing the request.

### As a service

```bash
gcloud run deploy silkscreen --source . --region us-central1 \
  --set-env-vars GOOGLE_API_KEY=...,GOOGLE_CLOUD_PROJECT=your-project
```

`POST /generate` with `{"intent": "...", "datasheets": {"PART": "url"}}` returns
the board plus the emitted `.kicad_pcb`. Extracted datasheet facts persist to
Firestore, so the second request for a part skips the most expensive stage.
`GET /healthz` is the readiness probe.

### As an MCP server

```bash
silkscreen-mcp        # JSON-RPC 2.0 over stdio
```

Five tools: `validate_circuit`, `build_board`, `emit_kicad_pcb`, `place_parts`,
`generate_footprint`.

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
| **Symmetry breaking** | 24 identical caps admit 24! relabelings. Forcing a lexicographic order collapses each orbit to one representative — on a 27-part board, optimality in 0.72 s instead of 16.7 s. |
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
    footprints.py parametric IPC-7351 land patterns
    board.py      emit a .kicad_pcb from a circuit
    kicad.py      read/modify an existing .kicad_pcb via kiutils
    cli.py        python -m silkscreen "..."
    agents/       the only place a model call happens
      model.py      provider seam + scripted stand-in for tests
      datasheet.py  PDF -> structured facts, with page citations
      propose.py    intent -> circuit, with a bounded repair loop
      review.py     adversarial design review
      pipeline.py   prompt -> PCB
  tests/          170 tests — no network, no API keys, no KiCad
    fixtures/     ref.kicad_pcb -- 11-footprint board fixture
scripts/
  demo.py         end-to-end: read -> place -> write -> verify
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

## Reproducible testing

Every result in this README can be reproduced from a clean clone in about four
minutes. **No KiCad install, no network access, and no API keys are required** —
the test suite and the demo both run fully offline.

### Requirements

| | |
|---|---|
| Python | 3.11 or newer (`python3 -V`) |
| OS | macOS, Linux, or Windows — all three run in CI |
| Network | Not needed after `pip install` |
| API keys | None. `agents/` tests use a scripted stand-in model, not a live provider |
| KiCad | Not needed. Board files are parsed by `kiutils`, which is pure Python |
| Disk | ~400 MB, almost all of it OR-Tools |

### From a clean clone

```bash
git clone https://github.com/machmoon/silkscreen.git
cd silkscreen
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
```

Then run all three checks:

```bash
./.venv/bin/python -m pytest -q           # 1. test suite   (~2 min)
./.venv/bin/python -m ruff check engine   # 2. lint         (~1 s)
./.venv/bin/python scripts/demo.py        # 3. end-to-end   (~20 s)
```

On Windows, use `.venv\Scripts\python.exe` in place of `./.venv/bin/python`.

### Expected output

**1. Test suite** — 170 tests, no skips, no warnings:

```
170 passed in 123.92s
```

The suite is dominated by the 20-second solver budget in a handful of placement
tests; the rest run in milliseconds.

| File | Tests | Covers |
|---|---:|---|
| `test_packing.py` | 26 | CP-SAT model: no-overlap, clearance, edge pinning, rotation, symmetry breaking, fallback, determinism |
| `test_mcp.py` | 23 | MCP tool surface and schemas |
| `test_agents.py` | 21 | Datasheet extraction, proposal repair loop, review — against a scripted model |
| `test_board.py` | 16 | Emitting a `.kicad_pcb` from a circuit spec |
| `test_retrieval.py` | 15 | Symbol and footprint lookup |
| `test_netlist.py` | 15 | Circuit IR validation — every rejection rule |
| `test_resilience.py` | 14 | Malformed input, degenerate footprints, timeout paths |
| `test_kicad.py` | 13 | Board read/write, coordinate conversion, round-trip |

**2. Lint:**

```
All checks passed!
```

**3. End-to-end demo** — reads the 11-footprint fixture board, places it, writes a
real `.kicad_pcb`, and re-parses it to prove the round-trip:

```
3. Solve (OR-Tools CP-SAT)
--------------------------------------------------------------
  status     : feasible
  board size : 19.60 x 15.05 mm  (295.0 mm^2)
  HPWL       : 52.4 mm
  solve time : 20.00 s
  warning    : Time limit reached; solution is feasible but not proven
               optimal (gap bound 669000 vs 1740000).

4. Write a real .kicad_pcb
--------------------------------------------------------------
  placed 11/11 -> placed.kicad_pcb  (43,933 bytes)

5. Prove the round-trip
--------------------------------------------------------------
  reparsed OK, 11 footprints preserved
```

These are the exact figures quoted in [The placer](#the-placer). To inspect the
result, open `placed.kicad_pcb` in KiCad's PCB Editor — but note that installing
KiCad is only ever needed to *look* at the output, never to produce it.

### On determinism

Placement is reproducible **only with `workers=1`**, which is the default. CP-SAT's
multi-worker portfolio search interleaves results non-deterministically regardless
of `seed`, so raising `workers` trades byte-identical output for speed. The
determinism test in `test_packing.py` asserts this contract by solving the same
model twice and comparing placements exactly.

Two caveats worth stating plainly:

- **`status: feasible` is expected, not a failure.** 2D packing with a wirelength
  objective is NP-hard; inside a 20-second budget the solver returns a valid
  solution plus a bound rather than a proof. The reported gap is genuine.
- **Solve *time* varies with your machine**, even though the *result* does not. The
  20-second figure is the budget, not a benchmark.

### Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `ruff check engine` and
`pytest -q` on **Ubuntu, macOS, and Windows** against Python 3.11, on every push to
`main` and every pull request. `fail-fast` is off, so one platform failing does not
mask the others.

---

## License

MIT — see [LICENSE](LICENSE).

Dependencies: `kiutils` (MIT), OR-Tools (Apache-2.0).
