# Silkscreen

**End-to-end PCB design. Describe a board in plain language, get a placed KiCad
layout back — with the reasoning shown and every claim cited.**

Silkscreen reads the datasheets, proposes a circuit, refuses to build it if it does
not validate, generates the footprints, places the board with a CP-SAT solver, writes
a real `.kicad_pcb`, and then argues against its own design and tells you what it
thinks is wrong.

```bash
python -m silkscreen "a 3.3V motor driver around an STM32F103" -o board.kicad_pcb
```

```
419 passed — no network, no API key, no KiCad install
```

---

## Do I need KiCad?

**Not to run Silkscreen. Yes to open what it makes.**

Silkscreen writes the `.kicad_pcb` format itself, so nothing in the pipeline shells
out to KiCad, imports `pcbnew`, or touches your mouse. But a board file is not much
use if you cannot look at it — so install KiCad unless you have a reason not to.

| | Without KiCad | With KiCad *(recommended)* |
|---|---|---|
| Generate a board from a prompt | ✅ | ✅ |
| Run the test suite | ✅ | ✅ |
| Deploy the service, use the MCP server | ✅ | ✅ |
| **See the board** | ❌ | ✅ |
| **Edit, route, and run DRC on it** | ❌ | ✅ |
| **Export Gerbers and get it fabricated** | ❌ | ✅ |

Skip KiCad if you are running Silkscreen in CI, on a server, or inside another tool
that consumes the file. Install it if you are a person who wants to see a board.

---

## Status

| Component | State |
|---|---|
| `kicad.py` — `.kicad_pcb` read/write | **Working** · 28 tests |
| `packing.py` — CP-SAT placer | **Working** · 43 tests |
| `netlist.py` — validated circuit IR | **Working** · 15 tests |
| `footprints.py` + `board.py` — land patterns, board emission | **Working** · 20 tests |
| `agents/` — datasheet, propose, review, pipeline | **Working** · 30 tests |
| `agents/retrieval.py` — page-cited datasheet retrieval | **Working** · 15 tests |
| `agents/resilience.py` — provider failover | **Working** · 14 tests |
| `fab.py` — Gerber, Excellon, BOM, pick-and-place | **Working** · fab package export |
| `order.py` — order options, manufacturability preflight | **Working** · blocks an unroutable board |
| `mcp/` — MCP server over stdio | **Working** · 23 tests |
| `service/` — Cloud Run + Firestore cache | **Working** · 97 tests |
| `frontend/` — Svelte review UI, served by the service | **Working** · review and board tabs, with an in-app debug console for log export |
| Overlay UI, guided cursor | Not built (mockups only) |

---

## Install

**1. Silkscreen itself** — Python 3.11+, no KiCad required:

```bash
git clone https://github.com/machmoon/silkscreen && cd silkscreen
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev,agents]"
```

**2. A Gemini key**, for the prompt-to-PCB path. The engine and the whole test suite
run without one; only the agents need it.

```bash
cp .env.example .env      # then put your GOOGLE_API_KEY in it
```

**3. KiCad** — recommended, so you can open the result:

| Platform | Command |
|---|---|
| macOS | `brew install --cask kicad` |
| Windows | `winget install KiCad.KiCad` |
| Debian/Ubuntu | `sudo add-apt-repository ppa:kicad/kicad-8.0-releases && sudo apt install kicad` |
| Fedora / any Linux | `flatpak install flathub org.kicad.KiCad` |

Or download from [kicad.org/download](https://www.kicad.org/download/). Then open the
output with **File → Open** in the PCB Editor, or `pcbnew placed.kicad_pcb`.

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

| Stage | Module |
|---|---|
| Datasheet reading (Gemini native PDF vision) | `agents/datasheet.py` |
| Retrieval over datasheet text, page-cited | `agents/retrieval.py` |
| Circuit proposal into the IR | `agents/propose.py` |
| Validation + bounded repair loop | `netlist.py` |
| Footprint generation, board emission | `footprints.py`, `board.py` |
| Placement | `packing.py` |
| Adversarial review | `agents/review.py` |

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
| Testable without KiCad | No | **Yes, all 419 tests** |

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

## Placing a board you already have

Silkscreen can also be used as a library on an existing `.kicad_pcb`, with no
model involved at all — read it, re-place it, write it back:

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

## Running it other ways

### As a service

```bash
gcloud run deploy silkscreen --source . --region us-central1 \
  --set-env-vars GOOGLE_API_KEY=...,GOOGLE_CLOUD_PROJECT=your-project
```

`POST /generate` with `{"intent": "...", "datasheets": {"PART": "url"}}` returns
the board plus the emitted `.kicad_pcb`. Extracted datasheet facts persist to
Firestore, so the second request for a part skips the most expensive stage.
`GET /healthz` is the readiness probe. The container also serves the built review
UI at `/`, same origin as `/generate`, so there is no CORS anywhere.

### Running the web UI

The UI is a Svelte SPA in `frontend/`, and it needs Node 22 or newer
(`node --version`). In development it runs on Vite's dev server, which proxies
`/generate` and `/healthz` to the Python service — two terminals:

```bash
PORT=8081 python -m service.app            # terminal 1: the API
cd frontend && npm install && npm run dev  # terminal 2: http://localhost:5173
```

For the production path, build the bundle and let the service serve it itself:

```bash
cd frontend && npm run build  # writes frontend/dist/
python -m service.app         # http://localhost:8080 serves the UI and the API
```

The UI has its own suite, which CI runs before the build:

```bash
cd frontend && npm test  # Vitest over frontend/src/lib
```

A run lands on the review, and the **Board** tab draws the board the placer
actually produced — courtyard outlines, copper pads, and part refs, straight
from the `placements` the service returns. Selecting a finding highlights the
parts it names on that board, and either pane will hand you the emitted
`.kicad_pcb` as a download.

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
| **Pinned parts** | `Part(fixed_at_nm=(x, y))` holds a part where you put it. Without this every re-solve reshuffles the board, so you can't keep a placement you like and let the solver work around it. |
| **Keepouts** | `Keepout(x, y, w, h)` reserves a region — mounting holes, a connector's mating envelope, a mechanical boss. Modelled as an immovable participant in the same no-overlap constraint as the parts, because that's what it is. |
| **Degrades, doesn't fail** | If CP-SAT finds nothing in budget, a deterministic shelf packer returns a valid layout flagged `FALLBACK`, warning about every constraint it couldn't honour — including the pins and keepouts it can't. |

### Iterating on a placement

Keep what you like and re-solve the rest:

```python
from silkscreen import Keepout, Part

first = pack(parts, nets=nets)
good  = {p.ref: p for p in first.placements}

parts = [
    Part(..., ref="J1", fixed_at_nm=(good["J1"].x_nm, good["J1"].y_nm))
    if p.ref == "J1" else p
    for p in parts
]
second = pack(
    parts,
    nets=nets,
    keepouts=[Keepout(mm(3), mm(3), mm(3.2), mm(3.2), name="MH1")],  # M3 hole
)
```

`fixed_at_nm` names where the **part** goes, not its clearance-inflated box, and is
snapped to the solver grid. Pinning closer to the origin than `clearance_nm/2` raises,
because the clearance ring has to exist.

### Limits

2D packing with a wirelength objective is NP-hard. On real boards the solver returns
`FEASIBLE`, not `OPTIMAL`, inside 20 s — and Silkscreen reports it as `FEASIBLE`.
Coarsening the grid from 0.025 mm to 0.5 mm barely moves the result, so the bottleneck
is combinatorial, not resolution. **Treat the output as a strong starting placement,
not a proof.**

No support for: **two-sided placement** (everything is one layer), **connector
orientation** (`must_be_on_edge` puts a part on an edge but says nothing about which
way it faces), **thermal relief**, or **differential pairs**. Two-sided placement is
the one that most limits real use.

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
  tests/          419 tests — no network, no API keys, no KiCad
    fixtures/     ref.kicad_pcb -- 11-footprint board fixture
scripts/
  demo.py         end-to-end: read -> place -> write -> verify
  check_docs.py   fails CI if a quoted test count goes stale
frontend/
  src/
    lib/          api client, run store, severity + format helpers
    components/   title bar, intent form, progress, findings, side rail
    styles/       the Drafting Table design tokens
  dist/           built bundle -- service/app.py serves it at /
vendor/
  mudriknow/      third-party (MIT), reference only -- not imported, not tested
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
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev,agents,cloud]"
```

Then run the same four Python checks CI runs, in the same order:

```bash
./.venv/bin/python -m pytest -q                            # 1. tests     (~2 min)
./.venv/bin/python -m ruff check engine service scripts    # 2. lint      (~1 s)
./.venv/bin/python scripts/check_docs.py                   # 3. doc drift (~5 s)
./.venv/bin/python scripts/demo.py                         # 4. end-to-end(~20 s)
```

Check 3 re-counts the suite and fails if any number quoted in this README or in
`DEVPOST.md` has gone stale, so the figures below cannot drift from the code.

On Windows, use `.venv\Scripts\python.exe` in place of `./.venv/bin/python`.

CI runs two more jobs that need Node 22 and Docker rather than Python, so they
are not in the list above:

```bash
cd frontend && npm ci && npm test && npm run build   # the `web` job
docker build .                                      # the `docker` job
```

### Expected output

**1. Test suite** — 419 tests, no warnings (four key-gated live-model tests skip unless `GOOGLE_API_KEY` is set):

```
419 passed in 190.85s
```

The suite is dominated by the 20-second solver budget in a handful of placement
tests; the rest run in milliseconds.

| File | Tests | Covers |
|---|---:|---|
| `test_packing.py` | 43 | CP-SAT model: no-overlap, clearance, edge pinning, rotation, symmetry breaking, keepouts, pinned parts, fallback, determinism |
| `test_app.py` | 31 | Cloud Run HTTP surface and the served UI bundle, over a real socket |
| `test_mcp.py` | 23 | MCP protocol — initialize, tools/list, tools/call, stdio transport, every tool |
| `test_agents.py` | 22 | Datasheet extraction, proposal repair loop, review — against a scripted model |
| `test_board.py` | 20 | Footprint generation and emitting a `.kicad_pcb` from a circuit spec |
| `test_netlist.py` | 15 | Circuit IR validation — every rejection rule |
| `test_retrieval.py` | 15 | Datasheet chunking, embedding, cosine ranking, page citations |
| `test_resilience.py` | 14 | Provider failover — every fallback path, forced |
| `test_kicad.py` | 13 | Board read/write, coordinate conversion, round-trip |
| `test_cache.py` | 7 | Firestore fact cache, via a fake client |
| **Total** | **203** | |

**2. Lint:**

```
All checks passed!
```

**3. Doc drift** — re-counts the suite and checks every figure quoted in the docs:

```
docs ok: 16 claim(s) across 2 files match a suite of 183
```

**4. End-to-end demo** — reads the 11-footprint fixture board, places it, writes a
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
mask the others. Two further jobs run on Ubuntu only: `web` builds the UI bundle,
and `docker` builds the container image, which is the only thing that exercises the
`Dockerfile`.

---

## License

MIT — see [LICENSE](LICENSE).

Dependencies: `kiutils` (MIT), OR-Tools (Apache-2.0).
