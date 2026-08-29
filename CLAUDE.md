# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev,agents,cloud]"  # setup (Windows: .venv/Scripts/pip)
python -m pytest -q                                  # full suite: engine/tests + service/tests (no network, no API keys, no KiCad)
python -m pytest engine/tests/test_packing.py -q     # one test file
python -m pytest -k test_name -q                     # one test by name
python -m ruff check engine service scripts          # lint (exactly what CI lints)
python scripts/demo.py                               # engine end-to-end: read, place, write, verify round-trip
python scripts/check_docs.py                         # fails if README/DEVPOST quote a stale test count (CI runs this)
```

Pytest config lives in `pyproject.toml` (`testpaths = engine/tests, service/tests`; `pythonpath = engine, .`). CI (`.github/workflows/ci.yml`) runs install + ruff + pytest + check_docs on Linux/macOS/Windows, Python 3.11. Do not quote test counts in prose anywhere — the number has drifted repeatedly (40, 54, 143, 160) and `check_docs.py` only guards README and DEVPOST.

## Team workflow

Four people push to `main` concurrently — the local checkout goes stale fast. Run `git fetch` and check `git status` / `git log origin/main` before starting work and again before any commit or push, and rebase rather than letting the histories diverge. During long working sessions, poll the remote every few minutes (e.g. `/loop 5m`) and surface new teammate commits to the user as they land.

## Architecture

The installed `silkscreen` package lives under `engine/` (setuptools packages only `engine/`), with the Cloud Run surface in `service/`. The core pipeline is: parse a `.kicad_pcb` file, solve placement with CP-SAT, write the placements and board outline back into the file. The board file is the API — there is no KiCad process, plugin, or IPC anywhere. The design is layered: the deterministic engine makes no network calls; `agents/` is the only place a model call lives; `service/` is the only place GCP lives. Each layer keeps an offline stand-in (`ScriptedModel`, `MemoryFactStore`) so the whole suite runs with no keys.

Core engine modules, in dependency order:

- **`units.py`** — every dimension in the codebase is an **integer nanometre** (KiCad's internal unit). No floats flow through the pipeline; unit confusion (mm/mil/nm) is treated as a board-destroying bug class. Provides `mm()`/`mil()`/`to_mm()` and grid quantisation (`DEFAULT_GRID_NM` = 0.05 mm, `DEFAULT_CLEARANCE_NM` = 0.25 mm).
- **`packing.py`** — the OR-Tools CP-SAT placer. `pack(parts, nets=..., clearance_nm=..., time_limit_s=...)` minimises HPWL wirelength plus board area and returns a `PackResult` with status `OPTIMAL` / `FEASIBLE` / `FALLBACK` (a shelf-pack fallback fires when CP-SAT finds nothing in budget). `FEASIBLE` is the normal outcome on real boards. Determinism requires `workers=1` (the default) — multi-worker CP-SAT is non-deterministic regardless of seed. Power nets are down-weighted (not dropped) via the `is_power_net` heuristic in kicad.py. `edge_refs`/`rotatable_refs` naming a ref not on the board is a hard error, not a no-op.
- **`kicad.py`** — `.kicad_pcb` read/write via `kiutils` (pure Python). `load_board` then `extract_parts` yields `FootprintInfo` (courtyard extents from `F.CrtYd`, falling back to the pad bounding box; per-pad offsets; pad-to-net map). `extract_nets`/`extract_wires`/`to_parts` build solver input. After solving, `apply_placements` moves footprints and `set_board_outline` draws `Edge.Cuts` (without it the board has no boundary and edge constraints mean nothing), then `save_board`. **Coordinate frames:** the solver works Y-up; KiCad is Y-down. kicad.py owns the flip at both boundaries (pad offsets in, placements out) — never convert anywhere else. Output format is `20240108` (KiCad 7–8).
- **`netlist.py`** — validated circuit IR, the contract between an LLM and anything touching KiCad. `parse_circuit_spec` accepts raw model output (fenced JSON tolerated), and **collects all validation failures into one `ValidationError`** so the whole batch can go back to the model as a single repair prompt. Connections require pin-level terminals (`C1.1`, not `C1`) because "one leg of this capacitor to this pin" must be expressible.

Layers above the core:

- **`agents/`** — the Gemini-backed layer, behind a `Model` protocol: `GeminiModel` calls `google-genai` (`generate_content` with document parts); `ScriptedModel` keeps every test offline; `FallbackModel` (resilience.py) does provider failover. `pipeline.py` runs read, propose, validate, place, review; `propose.py` drives the batched repair loop against `parse_circuit_spec`; `review.py` is an adversarial critic whose findings are filtered against the actual spec; `retrieval.py` does embedding retrieval with asymmetric task types. `board.py`, `footprints.py`, and `cli.py` (`python -m silkscreen "..."`) turn a validated spec into a board.
- **`mcp/`** (inside `engine/silkscreen/`) — a real MCP server: JSON-RPC 2.0 over stdio, exposing engine operations as tools. `handle()` maps one request dict to one response dict and never touches a stream, so the protocol is testable without spawning a process. Console script: `silkscreen-mcp`.
- **`service/`** — the Cloud Run surface: `app.py` is a stdlib-only HTTP server (`POST /generate`, `GET /healthz`) over `silkscreen.agents.generate_pcb`; `cache.py` is a Firestore-backed datasheet-fact cache behind the `FactStore` protocol. Deployment uses the root `Dockerfile`.

Tests live in `engine/tests/` and `service/tests/`; the shared fixture is `engine/tests/fixtures/ref.kicad_pcb` (an 11-footprint STM32 board). The round-trip property — written board reparses, keeps every footprint, no overlapping courtyards — is enforced by test. The fixture's nets are named `0_device_pin_N`, so the power-net heuristic does not fire on it; that path is covered by unit tests only.

## Retired pre-rewrite code

Top-level `mcp/`, `pcb/`, `packing/`, `footprint/`, `frontend/`, `lcsc.py`, and `test_skidl.py` are hackathon-era code from before the engine rewrite (a FastAPI datasheet-to-SKiDL server, KiCad-9-DLL scripts, a Next.js frontend). Nothing in `engine/`, `service/`, `scripts/`, or the tests imports them; they are not linted, not packaged, and not part of the documented layout. Name collisions are a real trap: top-level `mcp/` is not `engine/silkscreen/mcp/`, top-level `packing/` is not `engine/silkscreen/packing.py`, and top-level `footprint/` is not `engine/silkscreen/footprints.py`. Do not add new code to the retired directories.

## Hackathon requirements

The submission must satisfy three Google-stack constraints; design any new AI or agent work against them:

- Models: Gemini 3.5 or newer, called through the Gemini API or Vertex AI.
- Frameworks: at least one Google agent framework — Google ADK, GenAI SDK, Antigravity SDK, or Genkit.
- Infrastructure: at least one Google Cloud infrastructure service.

All three are currently met — Gemini 3.7/3.5 models via the Gemini Developer API, the Google GenAI SDK as the framework, Cloud Run plus Firestore as the infrastructure. Per-requirement analysis with citations lives in `docs/gemini.md`, `docs/agent-framework.md`, and `docs/cloud-infrastructure.md`. The engine itself stays key-free and offline-testable; `GOOGLE_API_KEY` in `.env.example` serves the agents layer only.

## Known issues (recorded 2026-08-29 — deliberately not fixed)

Found in a verification pass over the docs and recent commits; left open on purpose. Do not fix these as drive-bys — when one is addressed, do it deliberately, with tests, and remove it from this list.

1. `engine/silkscreen/agents/model.py:111` passes `media_resolution="high"`, which matches no member of the SDK's `MediaResolution` enum (values are `MEDIA_RESOLUTION_HIGH` etc.), so high-resolution datasheet reading is likely silently not applied.
2. The Firestore fact cache is a placeholder: `service/app.py` writes only `{"part_number": part}` and a cache hit drops that part's datasheet without feeding cached facts back into `generate_pcb` (TODO.txt item 8).
3. `.env.example` documents `GOOGLE_CLOUD_LOCATION`, which nothing reads (no Vertex AI path exists); `USE_FIRESTORE` is read by `service/app.py` but documented nowhere.
4. No test, even a key-gated one, exercises the live `GeminiModel`.
5. `build_store()` constructs a fresh Firestore client per request in production.
6. The `google-genai>=1.0` pin is unbounded upward; PyPI is at 2.x and a breaking 3.0 has been announced.
7. `engine/silkscreen/mcp/server.py`'s docstring says "four useful operations"; `TOOLS` defines five.
8. Nothing in the repo performs a deploy (CI only lints and tests) and no deployed URL is recorded, so a live Cloud Run instance cannot be verified from the repo.

## Documentation discipline

The README's measured figures (board size, HPWL) are reproduced exactly by `scripts/demo.py`; if a change shifts them, rerun the demo and update the README rather than leaving stale numbers. Test counts in README/DEVPOST are enforced by `scripts/check_docs.py` in CI. Unbuilt features are tagged `[not yet built]` in DEVPOST.md — keep unbuilt work visibly unbuilt.
