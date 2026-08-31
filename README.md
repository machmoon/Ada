# Silkscreen

[![CI](https://github.com/machmoon/silkscreen/actions/workflows/ci.yml/badge.svg)](https://github.com/machmoon/silkscreen/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![KiCad 7–8](https://img.shields.io/badge/KiCad-7--8%20file%20format-brightgreen)](https://www.kicad.org/download/)
[![License: MIT](https://img.shields.io/badge/license-MIT-black)](LICENSE)

**End-to-end PCB design. Describe a board in plain language, get a placed KiCad
layout back — with the reasoning shown and every claim cited.**

Silkscreen reads the datasheets, proposes a circuit, refuses to build it if it does
not validate, **draws a schematic**, generates the footprints, places the board with a
CP-SAT solver, **routes the copper**, and then argues against its own design and tells
you what it thinks is wrong.

**Silkscreen is a Python program you run.** The command line is the product; the web UI
is a viewer for what it produced, and is the less-supported path — see
[Which interface](#which-interface).

<!-- SCREENSHOTS WANTED — do not uncomment until the files exist under docs/img/:
     docs/img/review.png    the review pane with findings and citations
     docs/img/board.png     the Board tab: courtyards, pads, silkscreen refs
     docs/img/schematic.png the Schematic tab
-->

---

## Quickstart

Python 3.11 or newer. **No KiCad install, no API key, and no network access are needed
to install it or to run the whole test suite.**

**One command.** It finds a Python 3.11+, creates `.venv`, installs the engine editable
with its extras, and builds the web UI if Node 22+ is on your PATH (skipped, not fatal,
if it isn't). Nothing is written outside the repo and it never uses `sudo`:

```bash
git clone https://github.com/machmoon/silkscreen && cd silkscreen
./scripts/install.sh                                           # macOS / Linux
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # Windows
```

Then, optionally, one command to configure a key and one to open the app:

```bash
./.venv/bin/silkscreen setup    # writes .env; never echoes the key back
./.venv/bin/silkscreen serve    # starts the API + UI and opens a browser
```

<details>
<summary><b>Or install by hand</b> (three lines, same result)</summary>

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"   # Windows: .venv\Scripts\pip
./.venv/bin/python -m pytest -q                    # offline; no keys required
```
</details>

<details>
<summary><b>Or run the web UI in Docker</b> (no Python or Node on your machine)</summary>

```bash
docker build -t silkscreen .
docker run -p 8080:8080 -e GOOGLE_API_KEY=... silkscreen   # http://localhost:8080
```

The image builds the Svelte bundle in a Node stage and serves it from the Python
service, same origin as the API.
</details>

Then place a board — no API key, nothing to configure, and it finishes in about
20 seconds:

```bash
./.venv/bin/python scripts/demo.py
```

To go from a prompt to a board you need a Gemini key (`GOOGLE_API_KEY`); everything
else works without one:

```bash
./.venv/bin/silkscreen setup    # or: cp .env.example .env, and edit it
./.venv/bin/silkscreen "a 3.3V motor driver around an STM32F103" -o board.kicad_pcb
```

`setup` and `serve` are the only subcommands; any other argument is the plain-language
intent, so `silkscreen "..."` and `python -m silkscreen "..."` are the same generator.

One run leaves you a KiCad project, one file per stage:

```
wrote board.kicad_pro          ← open this in KiCad
wrote board.kicad_sch          ← the schematic
wrote board.placed.kicad_pcb   ← after placement, before any copper
wrote board.kicad_pcb          ← routed
```

Every stage is a real KiCad file you can open and inspect on its own, so you can see
where a design went wrong instead of only seeing the last artifact.

```
802 tests collected — no network, no API key, no KiCad install
```

**Next:** [full install guide and troubleshooting](docs/install.md) ·
[contributing](CONTRIBUTING.md) · [how it works](#prompt-to-pcb)

---

## Which interface

| | `python -m silkscreen` *(recommended)* | Web UI |
|---|---|---|
| Schematic (`.kicad_sch`) | ✅ | ❌ not surfaced |
| Routed copper | ✅ | ✅ in the downloaded file only — the board well draws placement, not tracks |
| Per-stage files you can open | ✅ | ❌ final board only |
| KiCad project (`.kicad_pro`) | ✅ | ❌ |
| Adversarial review, findings, citations | ✅ text | ✅ nicer to read |

**The CLI is still the complete project-output path.** The web UI (`service/` +
`frontend/`) now starts with a persistent orchestrator chat, shows the observable model,
tool, validation, and retry activity, and hands you compact cards for the schematic,
placement diagram, review, and final `.kicad_pcb`. It still does not return the native
`.kicad_sch`/`.kicad_pro` or draw routed tracks, so use the CLI when those files matter.

---

## Do I need KiCad?

**Not to run Silkscreen. Yes, strongly recommended, to do anything with what it makes.**

Silkscreen writes the KiCad formats itself, so nothing in the pipeline shells out to
KiCad, imports `pcbnew`, or touches your mouse. But the output *is* a KiCad project,
and without KiCad you have files you cannot open, check, or fabricate. Silkscreen's
router leaves hard nets unrouted on purpose and tells you which — finishing them is
work you do in KiCad. Install it unless you have a specific reason not to.

| | Without KiCad | With KiCad *(strongly recommended)* |
|---|---|---|
| Generate a schematic and a routed board from a prompt | ✅ | ✅ |
| Run the test suite | ✅ | ✅ |
| Deploy the service, use the MCP server | ✅ | ✅ |
| **See the schematic and the board** | ❌ | ✅ |
| **Finish the nets the router left unrouted** | ❌ | ✅ |
| **Run DRC and electrical rules check** | ❌ | ✅ |
| **Export Gerbers and get it fabricated** | ❌ | ✅ |

Skip KiCad if you are running Silkscreen in CI, on a server, or inside another tool
that consumes the file. Install it if you are a person who wants to see a board.
Platform-by-platform commands are in [docs/install.md](docs/install.md#kicad-optional-but-recommended).

---

## Status

| Component | State |
|---|---|
| `kicad.py` — `.kicad_pcb` read/write | **Working** · 33 tests |
| `packing.py` — CP-SAT placer | **Working** · 44 tests |
| `netlist.py` — validated circuit IR | **Working** · 21 tests |
| `schematic.py` — `.kicad_sch` + `.kicad_pro` emission | **Working** · 22 tests · KiCad ERC clean |
| `routing.py` — two-layer grid autorouter | **Working, partial by design** · 20 tests — see below |
| `footprints.py` + `board.py` — land patterns, board emission | **Working** · 20 tests |
| `agents/` — datasheet, propose, review, pipeline | **Working** · 34 tests |
| `agents/adk/` — ADK dynamic-workflow driver for the pipeline | **Working** · 18 tests |
| `agents/retrieval.py` — page-cited datasheet retrieval | **Working** · 15 tests |
| `agents/resilience.py` — provider failover | **Working** · 15 tests |
| `fab.py` — Gerber, Excellon, BOM, pick-and-place | **Working** · fab package export |
| `order.py` — order options, manufacturability preflight | **Working** · blocks an unrouted board |
| `mcp/` — MCP server over stdio | **Working** · 43 tests |
| `audit/` — optional visual design review | **Working** · 52 tests |
| `constraints/` — datasheet PDF → checkable, provenance-carrying constraints | **Working** · 79 tests |
| `service/` — Cloud Run + Firestore cache | **Working** · 108 tests · not deployed anywhere yet; no live URL |
| `frontend/` — Svelte review UI, served by the service | **Working** · persistent orchestrator chat, expandable model/tool traces, session JSON, review, schematic and board tabs |
| Voice / talk input | Not built |
| Overlay UI, guided cursor | Not built (mockups only) |

Every module above is covered by tests that run with no network, no API key, and no
KiCad install. The count is deliberately not quoted here — it drifts, and
`scripts/check_docs.py` fails CI when a quoted one goes stale.

---

## Prompt to PCB

```bash
echo 'GOOGLE_API_KEY=...' >> .env
python -m silkscreen "a 3.3V motor driver board around an STM32F030" \
    --datasheet "AMS1117-3.3=https://.../ams1117.pdf" \
    -o board.kicad_pcb
```

```
intent ─► datasheets ─► propose ─► validate/repair ─► .kicad_sch ─► place ─► route ─► .kicad_pcb
                                        │                                               │
                                        └──────────────► review ────────────────────────┘
```

| Stage | Module | Artifact |
|---|---|---|
| Datasheet reading (Gemini native PDF vision) | `agents/datasheet.py` | |
| Retrieval over datasheet text, page-cited | `agents/retrieval.py` | |
| Circuit proposal into the IR | `agents/propose.py` | |
| Validation + bounded repair loop | `netlist.py` | |
| Schematic drawing | `schematic.py` | `.kicad_sch`, `.kicad_pro` |
| Footprint generation, board emission | `footprints.py`, `board.py` | |
| Placement | `packing.py` | `.placed.kicad_pcb` |
| Copper routing | `routing.py` | `.kicad_pcb` |
| Adversarial review | `agents/review.py` | |

Useful flags: `--no-route` stops after placement, `--no-review` skips the adversarial
pass, `--board-only` writes just the routed `.kicad_pcb`, `--time-limit` sets the
solver budget, `--repairs` how many times a bad proposal goes back to the model.

The schematic and the board are drawn by two emitters from one `CircuitSpec`, and both
take their reference designators from `CircuitSpec.assign_refs()` — so `C1` on the
drawing is `C1` on the board. Numbering them separately would give two files that are
each internally consistent and describe different circuits.

Two gates sit between the model and the board.

**Structural.** The proposal goes through the circuit IR before anything is built.
Every validation error is collected and fed back as one repair prompt; the loop is
bounded and `result.repair_rounds` reports how many corrections it took.

**Semantic.** A reviewer re-reads the datasheets and is prompted to *refute* the
design — an agent asked "is this correct?" says yes. Findings are graded
blocker / marginal / note and cite the datasheet page. A part reference the circuit
does not contain is stripped out of the finding that named it; the finding itself
is still shown.

Everything below `agents/` is model-free and network-free, so the whole pipeline —
including its failure paths — is tested against a scripted model with no API key.

### Drawing the schematic

`schematic.py` renders the validated `CircuitSpec` as a KiCad 8 `.kicad_sch`, plus the
`.kicad_pro` that ties the schematic and the board together as one project.

Symbols are **generated, not looked up**. The file carries its own `lib_symbols` block,
the same way `footprints.py` generates land patterns rather than reading a library — so
it opens on a machine with no KiCad symbol libraries installed, and cannot silently
resolve to a different part than the one it was drawn for. Each passive type gets its
own body: a schematic whose crystals are drawn as capacitors reads as correct and
is not.

Connections are a short wire stub from each pin to a **net label**, which is ordinary
KiCad practice and electrically identical to point-to-point wires. The netlist KiCad
extracts from the sheet is the netlist the board was built from, and a pin on no net
gets no stub and no label rather than a wire to nowhere.

### Routing the copper

`routing.py` is a two-layer grid maze router: A* over a uniform lattice with an explicit
via cost, nets routed one at a time, each net grown outward from its first terminal so
later pins join the nearest point of the tree already laid.

**It is not a competitive autorouter, and the output says so.** A uniform grid cannot
reach every pin of a fine-pitch package, and a sequential router paints itself into
corners a rip-up-and-retry router escapes. So the contract is honesty rather than
completeness — every net it cannot finish is **named**, with the reason:

```
Routing: 11/13 nets routed, 47 tracks, 6 vias, 214.3 mm of copper
  unrouted SPI1_SCK: no clear path to one of its 3 pads; the channel is blocked
  unrouted VDDA: only 1 distinct grid node(s) among its pads; the routing grid is
                 too coarse for this footprint
```

Unrouted nets stay as ratsnest in KiCad, for you to finish. A net is all-or-nothing:
half a net's tracks laid down would give a board that looks routed everywhere you
happen to look. Defaults are 0.2 mm tracks, 0.2 mm clearance, 0.4/0.2 mm vias, on a
0.25 mm lattice.

Rotated footprints are **refused** rather than approximated, because `board.py` has a
recorded, unfixed bug in the anchor it writes for a rotated part — routing to those
coordinates would turn a latent placement bug into copper landing on bare laminate.
Nothing sets rotation today.

`--no-route` stops after placement, which is what every run produced before this
existed.

### Simulating the circuit

DRC answers whether a board can be *made*. Nothing answered whether the circuit
*works* — and that gap is why a design loop cannot close itself: it can produce a
plausible schematic and a manufacturable board with no evidence the thing does what it
was asked for. `spice/` is that missing check, shaped like a test runner rather than a
waveform viewer.

```python
from silkscreen.spice import Assertion, Measurement, Source, Testbench, Transient, verify

bench = Testbench(
    analysis=Transient(step=1e-6, stop=2e-3),
    sources=[Source.pulse("V1", "VIN", "GND",
                          initial=0, pulsed=5, width=1e-3, period=2e-3)],
)
report = verify(spec, bench, [
    Assertion(name="rise time under 250 us",
              measurement=Measurement(kind="rise_time", signal="VOUT",
                                      window=(0, 1e-3)),
              op="<", value=250e-6, unit="s"),
])
report.passed        # bool
report.summary()     # which clause failed, and by how much
```

`python scripts/simulate_demo.py` runs it end to end against an RC low-pass, checking
every result against closed-form circuit theory:

```
  PASS  10-90% rise time is tau*ln(9)
        measured 0.00021946s, expected within 0.000219503s (margin -4.28e-08)
  PASS  -3 dB corner is 1/(2*pi*R*C)
        measured 1593.23Hz, expected within 1593.14Hz (margin -0.0889)
```

**Nothing here can return a quiet zero.** An agent that gets an empty result reads it as
a circuit that behaves. So a missing model, a probe on a net that does not exist, a
signal with no rising edge, and a solver that will not converge each raise a distinct,
self-describing error. A measurement that cannot be taken fails its clause rather than
passing it vacuously.

**Where it stops, plainly.** A device (an IC) in the circuit IR is a pin map with no
behaviour attached, and no netlist generator can invent one. Trusted Python code can
supply a `SubcircuitModel` — the part's own SPICE model — directly on `Testbench`.
The MCP/JSON tool deliberately does not accept raw SPICE programs; IC simulation there
waits for a trusted server-side model registry. Without a model the run raises and names
the part rather than quietly leaving it out. Passive networks need nothing extra. A
diode with no model gets a generic silicon stand-in and a warning saying so;
`Testbench(strict=True)` turns that warning into an error, which is what you want when
the verdict has to be about the specified part.

ngspice and LTspice sit behind one interface, selected automatically. ngspice is what CI
installs and what this is verified against; LTspice discovery and batch invocation are
implemented but have not been run end to end — see the note in
`spice/simulators.py`. Install ngspice with `brew install ngspice` or
`apt-get install ngspice`; without a simulator the simulation tests skip and the rest of
the suite is unaffected.

---

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
| Testable without KiCad | No | **Yes, all 802 tests** |

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
board size : 18.25 x 18.00 mm  (328.5 mm²)
HPWL       : 53.0 mm
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

Nothing in this repo performs a deploy and no instance is running anywhere — that
command is the recipe, not a description of something live.

`POST /generate` with `{"intent": "...", "datasheets": {"PART": "url"}}` returns
the board, the emitted `.kicad_pcb`, and a versioned `schematic` topology block
with stable part ids, board refs, pins and structured net endpoints. Extracted
datasheet facts persist to Firestore, so the second request for a part skips the most expensive stage.
`POST /chat/stream` is the presentation path: a genuine ADK `LlmAgent` may ask one
essential clarification and otherwise calls the validated generator as its
`generate_board` tool. It streams versioned NDJSON events for the orchestrator, tool,
worker calls, and final result. `GET /models` discovers the current key's
`generateContent`-capable Gemini models, with a short server cache and configured fallback
catalog. `GET /healthz` is the readiness probe. The container serves the built UI at `/`,
same origin as all of these routes, so there is no CORS anywhere.

### Running the web UI

The UI is a Svelte SPA in `frontend/`, and it needs Node 22 or newer
(`node --version`). In development it runs on Vite's dev server, which proxies
`/generate`, `/chat`, `/models`, and `/healthz` to the Python service — two terminals:

```bash
PORT=8081 python -m service.app            # terminal 1: the API
cd frontend && npm install && npm run dev  # terminal 2: http://localhost:5173
```

For the production path, build the bundle and let the service serve it itself:

```bash
cd frontend && npm run build  # writes frontend/dist/
python -m service.app         # http://localhost:8080 serves the UI and the API
```

The UI has its own Vitest suite, which CI runs before the build:

```bash
cd frontend && npm test
```

A run stays in the **Chat** tab as a persistent transcript. Friendly activity summaries
are shown by default; raw orchestrator and worker prompts/responses are expandable for a
demo or debugging. Before submitting, the orchestrator panel selects Gemini 3.7 Flash or
Gemini 3.1 Pro Preview and an Auto/Fast/Standard/Deep thinking effort. Gemini 3 cannot
disable thinking completely, so Fast maps to the supported `low` level. A separate
request-pace control can space every explicit orchestrator and worker attempt at 15, 6,
or 3 RPM across one service instance; Auto preserves the provider default. This mitigates
RPM bursts but cannot raise Google's per-project token or daily quotas. Clarification,
retry, edit, copy-error, and discovered-model retry
controls remain beside the failed or incomplete turn. **Save session** exports a versioned
JSON snapshot containing the transcript, trace, result, and board artifact; **Open session**
restores it locally. Treat that debug export as sensitive if a prompt contains private
design information.

Appearance stays local to the browser. **Glass** in the title bar switches the whole
rendered interface from the opaque Drafting Table skin to a translucent material, while
**Night** independently selects its light or dark reading. Both choices persist across
reloads; reduced-transparency system settings replace blur with opaque surfaces, and the
PCB canvas keeps its fixed KiCad colours in every combination.

The compact artifact cards open the existing views. **Schematic** draws the validated
circuit as generic symbols with physical pin numbers and net-labelled connections; it does
not claim to be a native `.kicad_sch` or a library-accurate sheet. **Board** draws the
placement the service actually produced — courtyard outlines, pads, and part refs — while
**Review** shows the grounded findings. Selecting a finding highlights the parts it names,
and the board and review panes offer the emitted `.kicad_pcb` as a download.

`silkscreen serve` does both of those for you — it loads `.env`, starts the service on
`--port` (or `PORT`), and opens the browser. Running `python -m service.app` directly
does **not** read `.env`; only the CLIs do, so export the key first. See
[docs/install.md](docs/install.md#the-env-caveat).

### As an MCP server

```bash
silkscreen-mcp        # JSON-RPC 2.0 over stdio
```

Tools: `validate_circuit`, `build_board`, `emit_kicad_pcb`, `place_parts`,
`generate_footprint`, `simulate_circuit`, `spice_capabilities`.

`simulate_circuit` accepts typed sources (`dc`, `ac`, `pulse`, `sine`) and typed
analyses only. Unknown fields and raw model/directive text are rejected before a
simulator starts; runtime is capped at 120 seconds and returned waveforms at 2,000
points per signal. `spice_capabilities` reports simulator names without exposing local
executable paths.

---

## Reviewing a board

Optional, and separate from generating one. `silkscreen-review` reviews any
`.kicad_pcb` — one this project emitted, or one you laid out yourself — and
marks what it finds **on the board**, not just in a list.

```bash
silkscreen-review board.kicad_pcb                       # standard effort
silkscreen-review board.kicad_pcb -e deep -o review/    # deeper, write reports
silkscreen-review board.kicad_pcb -e quick --no-model   # offline, no API key
silkscreen-review board.kicad_pcb --fail-on-blocker     # exit 1 for CI
```

`-o` writes three files: `review.html` (board render beside the findings,
click either to highlight the other), `review.svg` (the annotated board on its
own, for a PR comment or a slide) and `review.json`.

### The thinking slider

```
quick     ●───○───○   geometry and connectivity. No model call at all.
standard  ○───●───○   + clearance sweeps, decoupling distance, track widths,
                      and one model pass.
deep      ○───○───●   + manufacturing rules, tighter thresholds, a per-part
                      model pass for every IC, and every model finding must
                      survive a refutation prompt before it is reported.
```

Each level runs a strict superset of the level below it — enforced by test, so
"deeper" cannot quietly become "different". Higher levels are slower and, above
`quick`, cost model calls.

### Proven and suggested

The report keeps two kinds of finding apart, because they are not equally
trustworthy:

- **Proven** — measured by a deterministic checker in `audit/rules.py`, and
  shown with the measurement that proves it (`gap 0.100 mm, clearance
  0.300 mm`). Outlined solid on the render.
- **Suggested** — argued by the model: wrong capacitor value, floating mode
  pin, a topology mistake. No measurement, dashed on the render, and dropped
  entirely if it names no part the board contains.

Nothing the model returns can delete, downgrade or reword a proven finding,
and a model failure loses only the suggested half — the report then says why
the model did not run rather than showing a shorter list.

The report also states what was *checked*, so an empty finding list cannot be
mistaken for a clean board when it only means a rule never ran.

---

## Datasheet constraints

Most of the requirements that decide whether a board works — decoupling,
strap-pin states, maximum ratings, power sequencing — have exactly one right
answer, and that answer is locked in a PDF. `silkscreen-constraints` converts
a datasheet into a versioned, machine-readable constraint file that the board
checker can enforce:

```bash
silkscreen-constraints stm32f030f4.pdf --part STM32F030F4 -o stm32f030f4.constraints.json
silkscreen-constraints stm32f030f4.pdf --part STM32F030F4 --report   # human-review worksheet
```

Extraction is a model transcribing tables, so the pipeline distrusts it by
construction. Every constraint carries provenance — page, section, and the
verbatim source text — and each quote is mechanically searched for on its
claimed page in the PDF's extracted text. A constraint whose quote cannot be
found, or whose extraction confidence is low, is marked `needs_review` with
the reason spelled out; it is never silently presented as fact. A separate
`confirmed` flag records that a human checked the constraint against the PDF,
and only humans set it.

`silkscreen.constraints.check_board()` then turns a constraint set into
review findings against a real board, in the same shape `silkscreen-review`
renders: decoupling count, per-pin coverage, capacitor value and placement
distance; strap pins tied to defined levels. Findings from constraints a
human has confirmed are **proven**; the rest stay **suggested**. Constraints
the checker cannot enforce on a board file (operating voltages, temperatures)
are reported as unchecked rather than dropped, so the file always says more
than the checker — never less than it claims.

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
    schematic.py  emit a .kicad_sch and the .kicad_pro that ties them
    routing.py    two-layer A* copper router
    kicad.py      read/modify an existing .kicad_pcb via kiutils
    ids.py        stable UUIDs, so two runs diff cleanly
    cli.py        python -m silkscreen "..."
    spice/        typed testbenches, deck building, simulators, measurements
    mcp/          JSON-RPC tools, including the bounded simulation verifier
    agents/       the only place a model call happens
      model.py      provider seam + scripted stand-in for tests
      datasheet.py  PDF -> structured facts, with page citations
      propose.py    intent -> circuit, with a bounded repair loop
      review.py     adversarial design review
      stages.py     the six stage bodies, shared by both drivers
      pipeline.py   prompt -> PCB
      adk/          ADK dynamic workflow over the same stage bodies
    audit/        optional visual review of a finished board
    constraints/  datasheet PDF -> versioned, provenance-carrying constraints
  tests/          802 tests — no network, no API keys, no KiCad
    fixtures/     ref.kicad_pcb -- 11-footprint board fixture
scripts/
  demo.py         end-to-end: read -> place -> write -> verify
  simulate_demo.py closed-form RC checks through a real ngspice run
  check_docs.py   fails CI if a quoted test count goes stale
frontend/
  src/
    lib/          api client, run store, severity + format helpers
    components/   title bar, intent form, progress, findings, side rail
    styles/       paper/glass material and light/dark design tokens
  dist/           built bundle -- service/app.py serves it at /
vendor/
  mudriknow/      third-party (MIT), reference only -- not imported, not tested
```

Top-level `mcp/`, `pcb/`, `packing/`, `footprint/`, `frontend-archive/`, `lcsc.py` and
`test_skidl.py` are pre-rewrite hackathon code, kept for reference and not part of the
layout above. They are name-collision traps — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Troubleshooting

Install and environment problems are in [docs/install.md](docs/install.md#troubleshooting).
Problems with a run:

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
./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"
```

Then run the same four Python checks CI runs, in the same order:

```bash
./.venv/bin/python -m pytest -q                            # 1. tests     (~2 min)
./.venv/bin/python -m ruff check engine service scripts    # 2. lint      (~1 s)
./.venv/bin/python scripts/check_docs.py                   # 3. doc drift (~5 s)
./.venv/bin/python scripts/demo.py                         # 4. end-to-end(~20 s)
```

Check 3 re-counts the suite and fails if any number quoted in this README or in
`DEVPOST.md` has gone stale, so those figures cannot drift from the code.

On Windows, use `.venv\Scripts\python.exe` in place of `./.venv/bin/python`.

CI runs two more jobs that need Node 22 and Docker rather than Python, so they
are not in the list above:

```bash
cd frontend && npm ci && npm test && npm run build   # the `web` job
docker build .                                      # the `docker` job
```

### Expected output

**1. Test suite** — 802 tests (live-model and local-simulator cases skip when
their optional dependency is unavailable):

```
669 successful, 14 skipped
```

The suite is dominated by the 20-second solver budget in a handful of placement
tests; the rest run in milliseconds. Google ADK currently emits one warning for
its experimental JSON-schema function-declaration feature.

| File | Tests | Covers |
|---|---:|---|
| `test_spice.py` | 99 | Deck construction, rawfile parsing, measurements, assertions, and closed-form ngspice checks |
| `test_app.py` | 99 | Cloud Run HTTP surface, the NDJSON stream, and the served UI bundle, over a real socket |
| `test_grounding.py` | 73 | Datasheet grounding — SSRF-guarded PDF fetch, page extraction, page-cache sharding, citation corroboration |
| `test_audit.py` | 52 | Deterministic and model-assisted design review |
| `test_packing.py` | 43 | CP-SAT model: no-overlap, clearance, edge pinning, rotation, symmetry breaking, keepouts, pinned parts, fallback, determinism |
| `test_mcp.py` | 43 | MCP protocol and tools, including the bounded SPICE boundary |
| `test_agents.py` | 34 | Datasheet extraction, proposal repair loop, review — against a scripted model |
| `test_order.py` | 30 | Order options and manufacturability preflight |
| `test_fab.py` | 29 | Gerber, drill, BOM, and pick-and-place export |
| `test_kicad.py` | 28 | Board read/write, coordinate conversion, round-trip |
| `test_schematic.py` | 22 | Schematic generation and KiCad validation |
| `test_netlist.py` | 21 | Circuit IR validation — every rejection rule |
| `test_routing.py` | 20 | Grid routing, vias, keepouts, and honest unrouted results |
| `test_board.py` | 20 | Footprint generation and emitting a `.kicad_pcb` from a circuit spec |
| `test_adk.py` | 18 | Parity between the SDK and ADK drivers — same events, same result, same exceptions |
| `test_retrieval.py` | 15 | Datasheet chunking, embedding, cosine ranking, page citations |
| `test_resilience.py` | 15 | Provider failover — every fallback path, forced |
| `test_cache.py` | 7 | Firestore fact cache, via a fake client |
| `test_live_model.py` | 4 | The live Gemini path, behind an API-key gate that skips it by default |
| `test_models.py` | 22 | Gemini discovery, model/thinking selection, and request-pace policy |
| `test_orchestrator.py` | 4 | Root clarification, tool dispatch, ADK thinking, and pre-call pacing hook |
| `test_quota.py` | 5 | Shared request spacing, Auto behavior, and invalid pace rejection |
| **Total** | **703** | |

**2. Lint:**

```
All checks passed!
```

**3. Doc drift** — re-counts the suite and checks every figure quoted in the docs:

```
docs ok: 20 claim(s) across 2 files match a suite of 706
```

**4. End-to-end demo** — reads the 11-footprint fixture board, places it, writes a
real `.kicad_pcb`, and re-parses it to prove the round-trip:

```
3. Solve (OR-Tools CP-SAT)
--------------------------------------------------------------
  status     : feasible
  board size : 18.25 x 18.00 mm  (328.5 mm^2)
  HPWL       : 53.0 mm
  solve time : 20.00 s
  warning    : Time limit reached; solution is feasible but not proven
               optimal (gap bound 696000 vs 1785000).

4. Write a real .kicad_pcb
--------------------------------------------------------------
  placed 11/11 -> placed.kicad_pcb  (43,936 bytes)

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

## Contributing

`CONTRIBUTING.md` has the setup, the checks to run before opening a PR, and the
conventions that are easy to violate by accident — the integer-nanometre rule, the
Y-up/Y-down coordinate boundary, and which top-level directories are dead code.

## License

MIT — see [LICENSE](LICENSE).

Dependencies: `kiutils` (MIT), OR-Tools (Apache-2.0).
