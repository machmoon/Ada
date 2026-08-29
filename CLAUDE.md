# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"   # setup (Windows: .venv/Scripts/pip)
python -m pytest -q                                            # all 54 tests (~seconds; no network, no API keys, no KiCad)
python -m pytest engine/tests/test_packing.py -q               # one test file
python -m pytest -k test_name -q                               # one test by name
python -m ruff check engine                                    # lint (CI lints only engine/)
python scripts/demo.py                                         # end-to-end: read → place → write → verify round-trip
```

Pytest config lives in `pyproject.toml` (`testpaths = engine/tests`, `pythonpath = engine`). CI (`.github/workflows/ci.yml`) runs install + ruff + pytest on Linux/macOS/Windows, Python 3.11.

## Architecture

The active code is the `silkscreen` package under `engine/` (setuptools only packages `engine/`); everything else at the top level is retired pre-rewrite code (see below). The pipeline is: parse a `.kicad_pcb` file → solve placement with CP-SAT → write the placements and board outline back into the file. The board file is the API — there is no KiCad process, plugin, or IPC anywhere.

Four modules, in dependency order:

- **`units.py`** — every dimension in the codebase is an **integer nanometre** (KiCad's internal unit). No floats flow through the pipeline; unit confusion (mm/mil/nm) is treated as a board-destroying bug class. Provides `mm()`/`mil()`/`to_mm()` and grid quantisation (`DEFAULT_GRID_NM` = 0.05 mm, `DEFAULT_CLEARANCE_NM` = 0.25 mm).
- **`packing.py`** — the OR-Tools CP-SAT placer. `pack(parts, nets=…, clearance_nm=…, time_limit_s=…)` minimises HPWL wirelength plus board area and returns a `PackResult` with status `OPTIMAL` / `FEASIBLE` / `FALLBACK` (a shelf-pack fallback fires when CP-SAT finds nothing in budget). `FEASIBLE` is the normal outcome on real boards. Determinism requires `workers=1` (the default) — multi-worker CP-SAT is non-deterministic regardless of seed. Power nets are down-weighted (not dropped) via the `is_power_net` heuristic in kicad.py. `edge_refs`/`rotatable_refs` naming a ref not on the board is a hard error, not a no-op.
- **`kicad.py`** — `.kicad_pcb` read/write via `kiutils` (pure Python). `load_board` → `extract_parts` yields `FootprintInfo` (courtyard extents from `F.CrtYd`, falling back to the pad bounding box; per-pad offsets; pad→net map). `extract_nets`/`extract_wires`/`to_parts` build solver input. After solving, `apply_placements` moves footprints and `set_board_outline` draws `Edge.Cuts` (without it the board has no boundary and edge constraints mean nothing), then `save_board`. **Coordinate frames:** the solver works Y-up; KiCad is Y-down. kicad.py owns the flip at both boundaries (pad offsets in, placements out) — never convert anywhere else. Output format is `20240108` (KiCad 7–8).
- **`netlist.py`** — validated circuit IR, the contract between an LLM and anything touching KiCad. `parse_circuit_spec` accepts raw model output (fenced JSON tolerated), and **collects all validation failures into one `ValidationError`** so the whole batch can go back to the model as a single repair prompt. Connections require pin-level terminals (`C1.1`, not `C1`) because "one leg of this capacitor to this pin" must be expressible.

Tests mirror the modules in `engine/tests/`; the shared fixture is `engine/tests/fixtures/ref.kicad_pcb` (an 11-footprint STM32 board). The round-trip property — written board reparses, keeps every footprint, no overlapping courtyards — is enforced by test. Its nets are named `0_device_pin_N`, so the power-net heuristic does not fire on the fixture; it is covered by unit tests only.

## Retired pre-rewrite code

Top-level `mcp/`, `pcb/`, `packing/`, `footprint/`, `frontend/`, `lcsc.py`, and `test_skidl.py` are hackathon-era code from before the engine rewrite (a FastAPI datasheet→SKiDL server, KiCad-9-DLL scripts, a Next.js frontend). Nothing in `engine/`, `scripts/`, or the tests imports them; they are not linted, not packaged, and not part of the documented layout. `backend/` was already deleted for the same reason. Do not confuse top-level `packing/` with `engine/silkscreen/packing.py`, and do not add new code to these directories. The engine itself needs no keys; the Google credentials in `.env.example` serve the model-calling layer (see Hackathon requirements below).

## Hackathon requirements

The submission must satisfy three Google-stack constraints; design any new AI or agent work against them:

- Models: Gemini 3.5 or newer, called through the Gemini API or Vertex AI.
- Frameworks: at least one Google agent framework — Google ADK, GenAI SDK, Antigravity SDK, or Genkit.
- Infrastructure: at least one Google Cloud infrastructure service.

These apply to the agentic layer (datasheet agents, model calls, deployment), not to the deterministic engine: `engine/` stays key-free and offline-testable by design, so Gemini and agent-framework code belongs in a separate package, per the split described in `engine/silkscreen/__init__.py`. `GOOGLE_API_KEY` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` in `.env.example` are where those credentials go.

## Documentation discipline

The README's measured figures (board size, HPWL, test count) are reproduced exactly by `scripts/demo.py`; if a change shifts them, rerun the demo and update the README rather than leaving stale numbers. Unbuilt features are tagged `[not yet built]` in DEVPOST.md — keep unbuilt work visibly unbuilt.
