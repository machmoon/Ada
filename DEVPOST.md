
# Silkscreen

**Agentic PCB design that shows its work.**

---

## Inspiration

Every device in your life runs on a printed circuit board, and designing even a simple
one still takes days of work that is mostly *lookup*, not *thought*. You find a chip.
You open a 300-page datasheet. You hunt for the one table that tells you which pin is
AVDD. You find the reference schematic buried on page 214 and copy the decoupling
network by hand. Then you do it again for the next chip. Then you place everything,
route it, and hope.

We know this because we built a tool for it before and got it wrong in an instructive
way.

Our previous attempt won a prize and could not survive contact with a second user. The
"KiCad integration" was a script that moved the operator's mouse to screen coordinate
(1600, 590) and pressed Ctrl+V, pasting in a board file a human had already laid out by
hand. The datasheet cache was twelve committed JSON files, so the demo never called a
model at all. The netlist generator picked the main IC's schematic symbol by fuzzy-
searching the *last word* of whatever the user typed — ask for "STM32F103C8T6
microcontroller" and it searched for "microcontroller" and wired the datasheet's pin
numbers onto whatever came back first. It looked spectacular for four minutes.

That experience produced the only opinion we actually trust: **in hardware, the demo is
not the hard part. Being right is the hard part, and being *checkably* right is the
whole game.**

So we went looking for where "checkably right" is worth the most. It is not layout.
Layout automation is crowded — Quilter, Cadence Allegro X AI, Zuken, DeepPCB — and
practitioners are openly hostile to it; a working RF designer put the objection best,
that with enough automation "the designers will be clueless." The genuinely underserved
job is one step earlier and one step less glamorous: **checking that a schematic
actually matches the datasheets it was drawn from.** Every EDA tool on the market
enforces structural connectivity — this wire reaches that pin — and none of them
verifies *semantic* correctness: that the pin was the right pin, that the regulator's
feedback divider produces the voltage you asked for, that the capacitor on the enable
line isn't ten times too large.

That check is what a beginner needs before their first board comes back dead. It is
also what a senior engineer wants before releasing to fab. It is the rare feature where
the novice product and the expert product are the same product.

And there is prior art for the demand, running on unpaid human labour: the "roast my
board" ritual, where people post schematics and wait days for a stranger to spot the
swapped pin. We want to be that stranger, in ninety seconds, with the datasheet page
cited.

---

## What it does

Silkscreen takes a plain-language description of what you want to build and produces a
schematic, a placed board, and — the part we care about — **a review of its own work,
with citations.**

Each stage below is tagged **[built]** or **[not yet built]** against the code in this
repository today.

**1. Understand the parts. [not yet built]**
Point Silkscreen at a component and it reads the actual datasheet. Gemini's native PDF
vision matters here in a way that text extraction does not: pinout tables, package
drawings, and reference schematics are *pictures*, and the numbers we need live inside
them. Every extracted fact carries the page it came from.

**2. Propose a circuit. [not yet built]**
The model emits a `CircuitSpec` — devices, passives, and nets — into a validated
intermediate representation.

**3. Refuse to build a broken circuit. [built]**
This is the load-bearing piece. Nothing reaches KiCad until it validates. The IR
rejects a net referencing a pin the device doesn't have, a part that doesn't exist, a
capacitor wired on only one leg, a bare part name where a specific terminal is required.
All failures are collected at once and handed back to the model as a single repair
prompt, so the loop converges instead of retrying blindly.

That last check is subtler than it sounds. Connecting *one specific leg* of a decoupling
capacitor to a specific pin is the most common operation in this entire domain — and an
IR that can only join whole parts to nets, as ours previously did, cannot express it at
all. Making that unrepresentable-by-construction is most of the value.

**4. Place the board. [built]**
A CP-SAT model places components to minimise board size and total wirelength, with real
courtyard clearance, optional 90° rotation, edge constraints for connectors and
antennas, and symmetry breaking over interchangeable passives. On the 11-footprint STM32 +
regulator + motor-driver board in `engine/tests/fixtures/` it returns a 19.60 × 15.05 mm
placement with 52.4 mm of total half-perimeter wirelength, reported as `FEASIBLE`
rather than `OPTIMAL` because that is what the solver proved in 20 s. The run is
reproducible at `workers=1`.

**5. Write a real file. [built]**
Silkscreen reads and writes KiCad files directly. No KiCad installation, no `pcbnew`
DLLs, no platform lock, and — emphatically — no controlling the user's mouse. It runs
identically on macOS, Linux, and Windows, which is the difference between a demo and a
tool.

A run leaves a whole project, one file per stage: the `.kicad_pro`, the `.kicad_sch`
schematic, the placed board before any copper, and the routed board. Symbols and
footprints are both generated and embedded, so the project opens on a machine with no
KiCad libraries installed and cannot silently resolve to a different part than the one
it was drawn for. The schematic and the board number parts from one shared call, so
`C3` on the drawing is `C3` on the board — numbered separately, the two files would each
be self-consistent and describe different circuits.

The copper is laid by a two-layer A* grid maze router. **It is not a competitive
autorouter and the output says so:** a uniform 0.25 mm grid cannot reach every pin of a
fine-pitch package, and a sequential router paints itself into corners a rip-up-and-retry
router escapes. On a dense LQFP board it finishes 6 of 50 nets. Every net it cannot
finish is named, with the reason, and left as ratsnest for a human — a router that
silently dropped a connection would be worse than no router at all.

Both emitters are checked against KiCad itself, not only against a parser: `kicad-cli
sch erc` and `pcb drc` are run on the output. That is how we found a via shorting a
foreign track on a board the entire test suite passed.

**6. Review it, and say why. [built]**
An adversarial reviewer re-reads the datasheets and argues against the design: this pin
is an input, you drove it; this cap is on the wrong side of the regulator; this part is
end-of-life. Findings cite the datasheet page, and each one is checked against the spec
before it is shown. **[not yet built]** The approval gate that would let you accept a
suggested fix and have it applied: the fix buttons in the review UI are deliberately
inert until that exists.

**7. Show, don't tell. [not yet built]**
Professional EDA tools are dense — KiCad has dozens of panels, and knowing *where to
click* is a real barrier that no chatbot removes. Silkscreen drives an animated cursor
across the interface to the exact control it means, so "add a net class" becomes
something you watch once and can then do yourself. The tool teaches its own UI. This is
the difference between automating a beginner out of the loop and bringing them into it.

---

## How we built it

**STATUS:** the ADK driver is the default engine; `SILKSCREEN_ENGINE=sdk` keeps the
straight-line driver one environment variable away.

The agent layer is Google's Agent Development Kit. The pipeline — read → propose →
validate → place → review — is an ADK dynamic **Workflow** in
`engine/silkscreen/agents/adk/`, where each stage is a node that calls the same stage
body the plain SDK path calls. `generate_pcb(engine=...)` chooses the driver, and both
drivers emit the same events from inside those shared bodies, so which one ran is not
something a client can observe. The topology is deliberate rather than a flat pile of
prompts:

- an **orchestrator node** for the main pipeline, running the stages as successive
  `await ctx.run_node(...)` calls, so the order is ordinary program text and a stage
  that fails comes back out of the run as the original exception
- a **bounded repair cycle** inside the propose node: every IR failure in a batch goes
  back to the model as one repair prompt, and the loop ends when the IR validates
- a dedicated **adversarial reviewer** node, prompted to *refute* the design rather than
  confirm it, because an agent asked "is this correct?" will say yes — and its findings
  are filtered against the spec, so a part reference the circuit does not contain is
  stripped out of the finding that named it, while the finding itself is still shown
- **[not yet built]** a **parallel fan-out** over datasheets, one reader per component,
  since parts are independent: an `asyncio.gather` inside the read node. Today parts are
  read one after another.

Model tiering: `gemini-3.7-flash` for datasheet vision and reasoning, dropping to
`gemini-3.5-flash-lite` behind it. It is a failover chain rather than per-task routing,
and every provider's output is checked for usable text before it is accepted, because a
fallback path nobody has exercised is a second bug and not a backup. Deployment is
Cloud Run; extracted datasheet facts persist to Firestore so the second run on a part is
free.

**[not yet built]** Tool confirmation gates any step that writes a file.

**[built]** The engine underneath is deliberately boring and has no network in it at all:

- **OR-Tools CP-SAT** for placement
- **kiutils** for `.kicad_pcb` I/O — pure Python, no KiCad install
- Pure-integer nanometre arithmetic end to end, because unit confusion between
  millimetres, mils, and KiCad's internal nanometres is a silent, board-destroying class
  of bug
- 817 tests that run with no network, no API key, and no KiCad installed

Splitting it this way is the point. The parts that must be *correct* are testable
offline. The parts that must be *smart* are the ones talking to a model.

---

## Challenges we ran into

**The solver was lying about being optimal.** Our earlier CP-SAT model derived the
board's vertical domain from a sum over `for h, _ in rects` — which binds `h` to the
*width*. Any component taller than the total width of the board was declared infeasible.
It also packed parts flush at 0 mm clearance, producing layouts no assembler could
build, and raised an exception on timeout, throwing away a perfectly good feasible
solution. Each of those is now a named regression test.

**Optimality is not available, and pretending otherwise is a lie.** 2D packing with a
wirelength objective is NP-hard. On real boards the solver returns `FEASIBLE`, not
`OPTIMAL`, inside a 20-second budget. We tried coarsening the grid from 0.025 mm to
0.5 mm; it barely moved the result, which told us the bottleneck is combinatorial, not
resolution. So we invested in symmetry breaking instead — a board with 24 identical
capacitors admits 24! relabelings of the same physical layout, and collapsing those
orbits proved optimality 3× faster on a 27-part test. And we made the failure mode
honest: when the solver finds nothing in time, a deterministic shelf packer returns a
valid layout flagged `FALLBACK`, rather than crashing.

**Ground nets destroy the objective.** Expanding every net into a clique of pairwise
connections means a ground net touching 50 pads contributes 1,225 edges and swamps every
signal net in the design — which, in our previous version, collapsed the entire board
into a single placement group and silently disabled the hierarchical layout we thought we
had built. Silkscreen excludes power rails by name and by fan-out, and connects the
remaining multi-pad nets as a star rather than a clique: linear instead of quadratic.

**Y is down.** KiCad's Y axis points down; a bottom-left-origin packer's points up. Mixing
them mirrors every layout vertically, and it is invisible until you look at a rendering
and something feels subtly wrong. There is now exactly one line in the codebase that does
that conversion, and it is commented.

**Nobody's PDF is standard.** Datasheets are inconsistent, frequently multilingual, and
hundreds of pages long. Native PDF vision — reading the pinout *table* as a table and the
package drawing as a drawing — is what makes this tractable at all.

---

## Accomplishments that we're proud of

Mostly, that we deleted things.

The version this replaces had two live API keys committed to a public repository, an
`eval()` on raw model output, a main server that raised `NameError` on import and could
not start, a frontend that failed to build, and a "layout engine" that was three
`pyautogui` clicks at hardcoded screen coordinates. We know all of this precisely
because we went back and audited it line by line before writing anything new. The most
valuable engineering artifact we produced was an honest list of what was actually true.

What we're proud of in the new one:

- **The engine has no network calls.** Every correctness-critical path is tested offline.
- **817 tests, and the interesting ones are regressions** — each pins down a specific bug
  that shipped in the previous version and can never ship again.
- **A validation layer whose job is to say no.** The IR makes a floating capacitor and a
  hallucinated pin unrepresentable rather than merely unlikely.
- **The KiCad integration is a file format, not a robot arm.** Cross-platform, headless,
  testable, and it does not seize the user's mouse.
- **We can state what doesn't work.** `FEASIBLE` is reported as `FEASIBLE`.

---

## What we learned

That in a domain with no cheap oracle, verification *is* the product. LLM coding works
because compiling and running the tests is nearly free. In hardware, ground truth is a
fabricated board four weeks and several thousand dollars away, and design-rule checking
is a weak proxy — it validates geometry, not intent. A board can pass every automated
check and still have a regulator wired to the wrong pin. That asymmetry is why the
category is hard, and it is also why the most valuable thing an agent can do here is not
*generate faster* but *catch what a human would have missed*, with a citation.

We also learned how easy it is to build something that demos beautifully and is hollow.
Our previous project's README claimed a RAG pipeline (there was no retrieval, no
chunking, no embeddings — one model call with a PDF URL), an MCP integration (there was
no MCP anywhere; a directory was named `mcp/`), and "multiple backups to ensure zero
single points of failure" (every fallback path was broken — one returned a streaming
iterator where the caller expected text, another parsed a field that never existed in the
response). None of that was dishonesty. It was a team at hour 22 describing what they
meant to build. The lesson we took is that the README should be written from the tests.

---

## Third-party code

`vendor/mudriknow/` is not our code. It is [MudrikNow](https://github.com/abdallahmagdy15/mudriknow)
at revision `ad58192`, MIT licensed, included unmodified with its licence file
intact as a working reference for the guided-cursor overlay we have not built
yet. Nothing in `engine/`, `service/`, or `scripts/` imports from it, it is
excluded from lint and tests, and it contributes nothing to the 817 tests or to
any figure quoted in this document. Everything else in the repository was
written during the submission period.

---

## What's next

**Footprint generation from datasheets.** Wrong footprints are the most common cause of a
dead first-spin board, and unlike layout, correctness is objectively checkable against the
package drawing in the PDF. It is a bounded, verifiable, high-value task that every tool in
this category quietly depends on and almost none of them own.

**Part availability as a design-time constraint.** A perfect design specifying an
end-of-life part is worthless. The existing sourcing-aware tools have a neutrality problem
— one sells design-intent data to component manufacturers, another is owned by a chipmaker.
There is no trusted, neutral, AI-native sourcing layer.

**Manufacturability as distinct from design rules.** A board can pass DRC and still fail
DFM: an acute-angle trace within spec creates an acid trap that over-etches. Every fab has
slightly different capabilities, published as PDFs that differ per vendor and change over
time — unstructured, per-vendor knowledge, which is precisely the shape of problem language
models are good at.

**Teaching as the product, not a mode.** The guided cursor is the piece we most want to get
right. Hardware has a brutal on-ramp, and the honest risk of every tool in this category is
that it produces engineers who can accept a suggestion and cannot evaluate one. A tool that
shows you where it clicked and why is a different bet: not automating the beginner out of
the loop, but making the loop learnable.
