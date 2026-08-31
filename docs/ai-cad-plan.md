# AI CAD — final plan (CEO decision, 2026-08-31)

Feature: AI-generated OpenSCAD enclosures for the KiCad PCBs the pipeline already
produces. This document is the contract. Five engineers build the five workstreams
below concurrently in one tree; **no two workstreams touch the same file**, and the
public interfaces in "Frozen contracts" are pinned — anyone who needs a contract
change edits this file first, in its own commit, and tells the other four.

Read CLAUDE.md first. Everything there applies: integer nanometres everywhere,
no quiet zeros, all validation failures batched into one error, gated-binary
convention copied from `test_spice.py`, offline suite green with no keys and no
new required binaries.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **No OpenSCAD in the Docker image. v1 ships `.scad` text + code view + download; PNG/STL render only via the locally-gated CLI.** | Infra/Engineering are right: 350–800 MB and cold-start CPU spikes in a stdlib single-process Cloud Run server, to produce a preview that is not the product. Product's real differentiator — the verified-fit receipt with signed margins — is computed by the *offline* verifier and ships in v1 untouched. |
| 2 | v2 preview candidate is an `openscad-wasm` lazy chunk in the SPA, not three.js and not server render. Decided later; v1 leaves the `find_openscad()` seam and the additive response shape it would need. | Keeps the server stdlib-only forever; client-side render scales with users, not instances. |
| 3 | Model emits validated JSON `EnclosureSpec`, never raw SCAD. Deterministic code injects every measured dimension; the model never types a board millimetre. | The `netlist.py` founding lesson. The model chooses *style within bounds* (lid, wall, cutout selection); geometry comes from the `.kicad_pcb`. |
| 4 | Pure-Python `.scad` emission. The C++ source at `vendor/openscad` is never built, linked, or imported — it is a read-only reference, gitignored, disclosed in DEVPOST. The only interaction with OpenSCAD is exec-ing a user-installed binary, same arms-length boundary as ngspice. | GPL-2.0 stays outside the codebase; MIT project stays MIT. |
| 5 | Enclosure failure never fails the run: exhausted repair budget → `enclosure: null` + a visible `enclosure.failed` event, board still delivered. The repair rounds themselves are visible events (Product's fix-it loop), the degradation is honest (Infra's requirement). | The board is still the product. |
| 6 | Naming: code says `enclosure` everywhere (package, stage, events, response key, artifact `enclosure.scad`); user-facing surfaces say **Case** (SPA tab label, CLI `--case`). | One code vocabulary, one product vocabulary; no half-renamed modules. |
| 7 | CLI v1 is `--case` / `--case-style` / `--case-render` on the existing generate command. The standalone `silkscreen case board.kicad_pcb` subcommand and `--no-model` deterministic default case are deferred to v2. | Keeps workstream D small and the v1 surface reviewable. |
| 8 | CI: `apt-get install openscad` on the **Linux job only**, mirroring ngspice; macOS brew skipped (slow/flaky), Windows skips. Python/web/docker jobs otherwise unchanged. Gated tests skip cleanly everywhere else. | Same convention as every other external verifier. |
| 9 | Heights: `.kicad_pcb` carries no Z, so component heights come from a table keyed by footprint class, with an explicit default that lands as a **warning in the fit report**, never a silent guess. | No quiet zeros. |
| 10 | Coordinate frames: `BoardEnvelope` stays in the KiCad Y-down frame — **no new flip**. The one place enclosure geometry changes frame is inside `emit.py`, which maps the envelope into OpenSCAD's frame. | The project has exactly one Y flip (placer boundary) and this feature does not add a second wandering one. |
| 11 | Hackathon constraints intact: proposals go through the existing `Model` protocol (Gemini via GenAI SDK, `CHEAP_MODEL = gemini-3.5-flash-lite`), the pipeline gains one ADK `@node`, both drivers stay event-identical, service stays on Cloud Run. No new required binary, no new infra dependency. | Non-negotiable submission requirements. |
| 12 | DEVPOST gets the feature tagged `[not yet built]` until merged, plus the optional-openscad-CLI disclosure under Third-party code. `check_docs.py --fix` before merge; never quote a frontend test count. | Documentation discipline section of CLAUDE.md. |

## v1 scope

**In:** two-piece case (base + lid) derived from actual `.kicad_pcb` geometry;
natural-language case intent ("rounded corners, USB cutout left"); JSON spec →
validate → batched repair loop (≤3 repairs); offline fit verification with signed
per-axis margins; `enclosure.scad` written beside the project and returned in the
API/SPA; hash-addressed **Case** tab with code view, parameters, fit receipt,
download; `--case` on the CLI with locally-gated STL/PNG render; ScriptedModel
offline tests throughout.

**Out (v1):** server-side rendering of any kind; in-browser 3D viewer;
arbitrary 3D modeling; real connector-geometry cutouts (the footprint set has no
connectors — cutouts are rectangular openings sized from courtyard extents);
print-readiness claims; standalone `case` subcommand; conversational revision of
an existing case (the chat root can simply re-run generation).

## Layout

New package `engine/silkscreen/enclosure/` (installed package — **not** a
top-level directory, which is the retired-code graveyard):

```
engine/silkscreen/enclosure/
  __init__.py      A   (docstring + re-exports of A's names only; everyone else
                        imports submodules directly, so B–D never edit it)
  errors.py        A   whole error taxonomy, incl. render errors (frozen here)
  ir.py            A   EnclosureSpec / Cutout, parse_enclosure_spec
  board_shape.py   B   BoardEnvelope extraction
  heights.py       B   footprint-class → height table
  emit.py          B   deterministic .scad emitter
  verify.py        B   offline fit checks, FitReport
  render.py        C   gated OpenSCAD CLI wrapper
engine/silkscreen/agents/enclosure.py  C   propose_enclosure + prompt
```

## Frozen contracts

All dimensions are **integer nanometres** (`silkscreen.units`). JSON at the
model boundary and `params` at the API boundary use mm floats; the conversion
happens once in `parse_enclosure_spec` (in) and once in the formatters (out).

### errors.py (owner A — includes the render errors C raises)

```python
class EnclosureError(Exception): ...
class EnclosureValidationError(EnclosureError):
    errors: list[str]          # every failure, batched — one repair prompt
class CavityFitError(EnclosureError):
    margins_nm: dict[str, int] # signed, keys "x","y","z"; negative = collision
class CutoutError(EnclosureError): ...      # bad ref, bad face, overlap
class WallError(EnclosureError): ...        # below MIN_WALL_NM etc.
class RenderUnavailable(EnclosureError):
    executable: str            # "openscad" — names what was searched for
class RenderFailed(EnclosureError): ...
class EmptyGeometryError(RenderFailed): ... # OpenSCAD warned-and-emitted-nothing
```

### ir.py (owner A)

```python
MIN_WALL_NM: int          # mm(1.2) — printable FDM minimum
DEFAULT_WALL_NM: int      # mm(2.0)
DEFAULT_CLEARANCE_NM: int # mm(1.0) board-to-cavity
FACES: tuple[str, ...]    # ("left", "right", "front", "back", "top")
LIDS: tuple[str, ...]     # ("friction", "screw", "none")

@dataclass(frozen=True)
class Cutout:
    id: str          # unique within the spec
    ref: str         # board ref, e.g. "J1" — engine resolves geometry
    face: str        # member of FACES
    margin_nm: int   # opening margin around the resolved courtyard interval

@dataclass(frozen=True)
class EnclosureSpec:
    wall_nm: int
    clearance_nm: int
    lid: str                      # member of LIDS
    corner_radius_nm: int         # 0 = square
    cutouts: tuple[Cutout, ...]
    standoffs: bool               # auto-placed by the emitter, not positioned by the model
    vents: bool
    label: str | None             # embossed text on the lid, sanitised

def parse_enclosure_spec(text: str) -> EnclosureSpec
```

`parse_enclosure_spec` accepts raw model output (fenced JSON tolerated, mm
floats in the JSON) and **collects every failure into one
`EnclosureValidationError`** — negative/zero dims, wall below `MIN_WALL_NM`,
unknown face/lid, duplicate cutout ids, malformed refs. Ref *existence* is not
checked here (the IR does not know the board); that is `verify_fit`'s job.

### board_shape.py / heights.py (owner B)

```python
@dataclass(frozen=True)
class PartExtent:
    ref: str
    x_min_nm: int; y_min_nm: int; x_max_nm: int; y_max_nm: int  # KiCad Y-down, absolute
    height_nm: int
    height_default: bool   # True when the heights table had no entry (surfaces as a warning)

@dataclass(frozen=True)
class BoardEnvelope:
    outline_nm: tuple[tuple[int, int], ...]  # Edge.Cuts polygon, KiCad Y-down
    x_min_nm: int; y_min_nm: int; x_max_nm: int; y_max_nm: int
    thickness_nm: int                        # board substrate, default mm(1.6)
    parts: tuple[PartExtent, ...]
    max_height_nm: int

def board_envelope(path: str | Path,
                   *, heights: Mapping[str, int] | None = None) -> BoardEnvelope
# raises ValueError when the board has no Edge.Cuts outline — a board without a
# boundary cannot be encased, per the set_board_outline convention.

# heights.py
DEFAULT_HEIGHT_NM: int                      # mm(3.0)
HEIGHTS_NM: dict[str, int]                  # footprint-class key → height
def height_for(footprint_name: str) -> tuple[int, bool]   # (height, was_default)
```

No new Y flip: everything stays in the KiCad frame.

### emit.py (owner B)

```python
def emit_scad(spec: EnclosureSpec, envelope: BoardEnvelope) -> str
```

Deterministic (same inputs → byte-identical output). nm→mm **only** inside the
formatter via `units.to_mm` with fixed precision. Emits named parameters at the
top (`board_x`, `board_y`, `wall`, `clearance`, `cavity_z`, …) and named modules
`base()`, `lid()`, `standoffs()`. This is the one place geometry crosses into
OpenSCAD's frame. Structural invariants (tier-1 tests read these back out of
the text): `cavity_x == board_x + 2*clearance`, `outer − cavity == 2*wall` per
axis, each cutout opening covers its connector's courtyard interval + margin.

### verify.py (owner B)

```python
@dataclass(frozen=True)
class FitReport:
    margins_nm: dict[str, int]     # signed, per axis — the receipt
    warnings: tuple[str, ...]      # e.g. "clearance under 0.5 mm", "U1 height defaulted"
    params_mm: dict[str, float]    # the emitted parameters, for display

def verify_fit(spec: EnclosureSpec, envelope: BoardEnvelope,
               *, strict: bool = False) -> FitReport
```

Every failure raises a specific error (`CavityFitError` with signed margins,
`CutoutError` naming an absent ref — hard error per the `edge_refs` convention —
`WallError`); nothing returns a quiet zero. `strict=True` promotes warnings to
errors (the `Testbench(strict=True)` precedent) and is what the agent loop uses.

### render.py (owner C — gated CLI half, never on the service path)

```python
def find_openscad() -> str | None          # shutil.which seam
def available() -> bool
def render_stl(scad: str, out_path: Path, *, timeout_s: float = 60.0) -> Path
def render_png(scad: str, out_path: Path, *, timeout_s: float = 60.0) -> Path
```

Absent binary → `RenderUnavailable("openscad")`. Non-zero exit or stderr errors
→ `RenderFailed`. A well-formed but empty STL → `EmptyGeometryError`, never a
vacuous pass. Test gating copies `test_spice.py` exactly (`HAS_OPENSCAD`,
`needs_openscad` skipif).

### agents/enclosure.py (owner C)

```python
ENCLOSURE_PROMPT: str   # must contain the literal marker "ENCLOSURE-SPEC v1"
                        # (frozen so any workstream's ScriptedModel.by_marker can key on it)

class EnclosureProposalError(EnclosureError):
    attempts: int

def propose_enclosure(model: Model, envelope: BoardEnvelope,
                      *, style_hint: str = "", max_repairs: int = 3,
                      on_event: Callable[[dict], None] | None = None,
                      ) -> tuple[EnclosureSpec, int]   # (spec, repair_rounds)
```

Mirrors `propose.py`: deterministic facts injected into the prompt (outline
size, part rects, per-zone max height, edge-adjacent refs with their faces);
repair rounds resend the full prompt + the batched errors + the previous
proposal; `ModelError` propagates unwrapped (so `FallbackModel` wraps
unchanged); budget exhaustion raises `EnclosureProposalError(attempts=…)`.
Each round also runs `verify_fit(strict=True)` so fit failures feed the repair
loop, not just JSON-shape failures. Uses `CHEAP_MODEL`.

### Pipeline / events (owner C)

`generate_pcb` gains `enclosure: bool = False` and `enclosure_style: str = ""`
(opt-in, so both drivers stay event-identical by default). `enclosure_stage`
lands in `stages.py` **after `route_stage`, before `review_stage`**, no-ops
silently when `enclosure` is falsy (the `route_stage` pattern), and writes
`enclosure.scad` beside the project only when `output` is set (the
`schematic_stage` filesystem rule). The ADK workflow gains one always-run
`@node(name="enclosure")`; `test_adk.py`-style parity applies.

`PipelineResult` gains:

```python
class EnclosureResult(NamedTuple):
    spec: EnclosureSpec
    scad: str
    fit: FitReport
    repair_rounds: int
    rendered: bool          # always False on the service path in v1
# PipelineResult.enclosure: EnclosureResult | None
```

Event names (frozen — D passes them through, E renders them):

```
{"event": "stage.start", "stage": "enclosure"}
{"event": "enclosure.round", "round": <int>, "errors": <int>, "first_error": <str ≤160>}
{"event": "stage.done", "stage": "enclosure", "cutouts": <int>, "lid": <str>,
 "wall_mm": <float>, "repair_rounds": <int>, "rendered": false}
{"event": "enclosure.failed", "error": <str ≤160>}   # run continues, enclosure=None
```

Any `EnclosureError`/`EnclosureProposalError` inside the stage is caught by the
stage body itself → `enclosure.failed` + `None`. Everything else (including
callback exceptions) propagates as today.

### Service response (owner D — additive)

```json
"enclosure": {
  "scad": "<full text>",
  "params": {"board_x": 48.2, "wall": 2.0, ...},
  "fit": {"margins_mm": {"x": 1.0, "y": 1.0, "z": 0.55}},
  "warnings": ["U1 height defaulted to 3.0 mm"],
  "repair_rounds": 1
}
```

or `"enclosure": null`. Request opt-in: `"enclosure": true`,
`"enclosure_style": "<free text ≤500 chars>"` on `POST /generate` and
`/generate/stream`; the stream passes the events above through unchanged.
Invalid `enclosure_style` type/length is a plain pre-stream 400 like every
other field. The `.scad` rides the JSON exactly as `kicad_pcb` does.

### CLI (owner D)

```
--case                 generate an enclosure; writes enclosure.scad beside -o
--case-style TEXT      natural-language case intent
--case-render          additionally render enclosure.stl + enclosure.png via the
                       local openscad binary; exits with a clear message naming
                       the executable when it is absent (RenderUnavailable)
```

## Workstreams

Interfaces above are the build-against contract; C (and D, E) start immediately
against them without waiting for A/B code. File ownership is exclusive —
if you need a file another workstream owns, you need a contract conversation,
not a merge conflict.

### A — Enclosure IR + errors
**Owns:** `engine/silkscreen/enclosure/__init__.py`, `enclosure/errors.py`,
`enclosure/ir.py`, `engine/tests/test_enclosure_ir.py`.
Frozen dataclasses, all-integer-nm, `parse_enclosure_spec` with batched
`EnclosureValidationError`. Tests: reject negative/zero dims, walls below
`MIN_WALL_NM`, unknown face/lid, duplicate cutout ids; assert a multi-error
input reports *all* errors in one exception; fenced-JSON tolerance.
`__init__.py` re-exports A's names only — nobody else edits it.

### B — Board envelope + emitter + offline verify
**Owns:** `enclosure/board_shape.py`, `enclosure/heights.py`, `enclosure/emit.py`,
`enclosure/verify.py`, `engine/tests/test_enclosure_geometry.py`,
`engine/tests/test_enclosure_emit.py`, `engine/tests/test_enclosure_verify.py`.
Geometry tests compute expected bboxes with inline math over the raw
`ref.kicad_pcb` fixture, never by calling the extractor (the `test_kicad.py`
oracle discipline), and include a rotated-footprint case (the issue-9 bug
class). Emitter tests are tier-1 always-on: an independent `.scad` reader in
the test file (regex/token extraction of numeric literals, importing no emitter
constants) asserts the structural invariants listed under emit.py, plus the
round-trip property (emitted `.scad` → tier-1 reparse → dims equal the IR's).
Fully offline; B ships no gated tests.

### C — Agent stage + repair loop + render gate + pipeline wiring
**Owns:** `engine/silkscreen/agents/enclosure.py`, `enclosure/render.py`,
`engine/silkscreen/agents/stages.py`, `agents/pipeline.py`,
`agents/adk/workflow.py`, `.github/workflows/ci.yml` (the one-line Linux
openscad install), `engine/tests/test_enclosure_agent.py`,
`engine/tests/test_enclosure_render.py`.
`propose_enclosure` per contract; `ScriptedModel.by_marker` keyed on the
`"ENCLOSURE-SPEC v1"` marker keeps tests offline; tests assert the repair
prompt contains the batched validation errors and that `enclosure.failed`
degrades without killing the run. `test_enclosure_render.py` holds **all**
gated tests (tier-2): render an STL, parse its bounding box with inline
vertex min/max, assert the cavity contains the board box and the mesh is
non-empty. Until A/B land, C develops against local stubs matching the frozen
signatures and swaps to real imports at integration — the stubs never merge.

### D — Service + CLI surface
**Owns:** `service/app.py`, `engine/silkscreen/cli.py`,
`service/tests/test_enclosure_service.py`, `engine/tests/test_enclosure_cli.py`.
Additive `enclosure` response key and request opt-in per contract; stream
pass-through; the field-validation 400 must not regress known issue 10's
pattern (validate `enclosure`/`enclosure_style` before the pipeline runs).
CLI flags per contract. Service tests monkeypatch `generate_pcb` (existing
convention) and assert the additive key, the `null` degradation, and that the
one-shot response never grows raw model output. CLI tests cover `--case`
writing `enclosure.scad` and `--case-render` failing with the executable's
name when openscad is absent (ungated: assert the message, mocking
`find_openscad` to return `None`).

### E — Frontend Case tab + chores
**Owns:** `frontend/src/lib/enclosure.js`, `frontend/src/lib/enclosure.test.js`,
`frontend/src/lib/tabs.js`, `frontend/src/lib/tabs.test.js`,
`frontend/src/lib/run.js`, `frontend/src/lib/run.test.js`,
`frontend/src/App.svelte`, `frontend/src/components/CaseTab.svelte`,
plus chores: `.gitignore`, `vendor/README.md`, `DEVPOST.md`,
`docs/ai-cad-plan.md` (this file's upkeep), and the CLAUDE.md gated-tools
paragraph at merge time.
Fifth hash-addressed **Case** tab, peer in `tabs.js`: code view + copy +
download (`enclosure.scad` via the existing `download.js` — reused, not
modified), parameter table, and the fit receipt (signed margins + warnings).
Chat artifact card follows the existing compact-card pattern. `data-testid`s
on intrinsic elements only, disambiguated by identity attributes.
Chores: gitignore `vendor/openscad/` and `*.stl` (NOT `*.scad` wholesale —
fixtures must stay trackable); vendor/README.md entry with the shallow-clone
command and the do-not-import rule; DEVPOST `[not yet built]` tag until merged
+ optional-openscad disclosure; `check_docs.py --fix` before merge; never a
frontend test count in README/DEVPOST.

## Integration order

A and B merge first (independently). C merges once both are in and its stubs
are deleted. D and E merge in any order after C (both depend only on frozen
event/response shapes, so they can be reviewed in parallel). Every PR passes
the offline suite with no openscad installed; the Linux CI job additionally
exercises the gated tier. After merge, remove the `[not yet built]` tag and
add openscad to CLAUDE.md's gated-tools paragraph (E owns both edits).
