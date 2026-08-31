# The 3-minute demo

> **QA-verified model-tier warning (added after live testing 2026-08-31):** `gemini-3.7-flash`
> (the default primary) was measured degraded on demo eve — the default CLI hung
> silently for 11+ minutes, and the service took ~5.5 min to fail over. With
> `--model gemini-3.5-flash-lite` the same intent completed in **6 seconds**, end to
> end, zero findings deferred. Until the request-timeout fix merges, EVERY live run
> in this script should pass the model explicitly (CLI: `--model gemini-3.5-flash-lite`;
> Kaleo/web: pick it in the model selector). Also: do not ad-lib intents containing
> LEDs/diodes — a 2-pin semiconductor deterministically fails at board emission
> (no package rule); stick to the scripted intents.


The judge-facing live demo, timed. Every click here exists in the shipped code; every
line you say is backed by a file. The disaster plan for each step is at the bottom —
read it once before you go on stage, because the recovery moves only work if you
rehearsed them.

**One-line pitch:** describe a board out loud, watch an agent pipeline read the
datasheet, design it, place it, route it, and then argue against its own work —
and walk away with a real KiCad project.

---

## Setup checklist (T minus 30 minutes)

Do these in order. The demo has no live-fixable failure mode for a skipped step.

1. `git fetch` and confirm you are on the demo commit. Four people push here.
2. `cd frontend && npm run build` — demo the **built** bundle, never `npm run dev`.
   The Vite dev server has a known split-module failure mode after heavy churn
   (documented in CLAUDE.md); the built bundle served by the service is immune.
3. Start the engine with the key loaded: `./.venv/bin/silkscreen serve` (it reads
   `.env`; bare `python -m service.app` does not). Confirm `http://localhost:8081/healthz`.
4. Launch Kaleo (installed from `Kaleo_*_aarch64.dmg` on the GitHub release —
   **Apple silicon only**; do not plan this demo on an Intel Mac). In its Engine
   page, set the base URL to the local service and watch the health check go green.
5. Voice out: flip the voice toggle on. With an ElevenLabs key in Kaleo's voice
   settings you get the good voice; with no key it uses the system voice
   (`app/src/lib/speech/backends.ts` — the fallback is automatic). Play one test run
   at venue volume.
6. Voice in (optional): spoken intent needs a speech-to-text key configured in the
   shell (Whisper/Groq/ElevenLabs/Google — `app/src/config/stt.constants.ts`).
   **If you have not tested STT in this room, type the intent instead.** Typing is
   not a downgrade; the pipeline is the show.
7. **Record the golden session now.** Run the golden intent once in the web SPA,
   let it finish, click **Save session**. That JSON file is your no-network,
   no-model, no-luck fallback — **Open session** restores the full transcript,
   trace, findings, and board artifact locally.
8. Pre-generate the KiCad beat: run the CLI once and keep the output directory:
   `./.venv/bin/silkscreen "<golden intent>" -o demo/board.kicad_pcb`. You get
   `board.kicad_pro`, `board.kicad_sch`, `board.placed.kicad_pcb`, `board.kicad_pcb`.
   Open `board.kicad_pro` in KiCad now and leave it in the Dock.
9. macOS Do Not Disturb on. Close Slack.

## The golden intent

Type or speak exactly this (it is the reviewer-demo prompt from TODO.txt, and it
stays inside the footprint generator's supported set — no connectors, no headers):

> An AMS1117-3.3 regulator in SOT-223 with a 10 uF input capacitor and a 22 uF
> ceramic output capacitor. Use only the regulator and passive components, and
> preserve the ceramic output-capacitor choice so the adversarial review can
> check it against the selected AMS1117 datasheet.

Add the AMS1117-3.3 datasheet URL in the datasheets field and switch grounding on
(both the SPA form and Kaleo's run form take part-to-URL rows; grounding only does
anything when a URL is present).

Why this intent: the AMS1117 is an old bipolar LDO that needs output-capacitor ESR
in a stability band — a low-ESR ceramic is the classic subtle mistake. The
adversarial reviewer is explicitly prompted to probe regulator output capacitors
and ESR (`engine/silkscreen/agents/review.py`), so this run reliably produces a
finding class a hardware judge will recognize as *real*, not staged. The exact
wording varies run to run because it is a live model — say so if asked; it is a
strength.

---

## The 3:00 script

**0:00 — Cold open, Kaleo on top of the desktop.**
Kaleo is already floating. Say:

> "This is Kaleo — a desktop copilot for a PCB engine called Silkscreen. I'm going
> to describe a power supply, and it's going to read the datasheet, design the
> circuit, refuse to build it until it validates, place it, route copper, and then
> — the part we care about — argue against its own design."

**0:15 — Enter the intent, start the run.**
Type (or speak) the golden intent, datasheet URL filled, grounding on. Hit run.
The stream starts immediately (`run.accepted`, then live stage events over NDJSON).

**0:25 — Narrate the stream while it works.** The stages tick as real events
arrive — read, propose, validate/repair, schematic, place, route, review. Talking
points, in the order the stages light up:

> "It's reading the actual PDF — Gemini's native document vision, page numbers kept
> for citations." (read)
>
> "Now it proposes a circuit into a validated intermediate representation. If the
> model wires a capacitor by one leg or names a pin the part doesn't have, every
> error goes back in one batch as a repair prompt. Nothing reaches a board file
> until this validates." (propose / repair rounds)
>
> "Placement is not the model — it's Google OR-Tools CP-SAT minimizing wirelength
> and board area with real courtyard clearance. And routing is an A* maze router
> that names any net it can't finish instead of pretending. The honesty is the
> feature." (place / route)
>
> One sentence on the stack while review runs: "Orchestration is Google's Agent
> Development Kit — the pipeline is an ADK dynamic workflow, and that's the
> default engine, not a wrapper."

**1:15 — WOW BEAT 1: the voice.** The run completes and Kaleo *speaks the digest
of the review out loud* while the findings render. Shut up and let it talk.
Expected content: the finding about the ceramic output capacitor on the AMS1117,
against the datasheet. Then:

> "It just told me my output capacitor choice can make this regulator oscillate —
> and it cites the datasheet. That's the product: not drawing faster, catching
> what you'd have missed."

**1:35 — Switch to the web SPA (already open in a tab).** Show the finished run:
review findings on the left, click the ceramic-cap finding — the parts it names
**cross-highlight on the board well**. Flick through the Schematic tab. One line:

> "Same engine, same run. Findings, schematic, and the placed board — click a
> finding and it lights up the parts it's about."

**1:55 — WOW BEAT 2: the guided pointer.** On the selected finding card, click
**Guide me**. A consent card appears ("Walk through …?"); click **Start guide**.
An animated pointer lands on "Show on board"; click **Next**; the tab switches and
the pointer lands on the actual capacitor on the board. Say:

> "And for someone new to EDA, it doesn't just tell you — it walks you there. The
> pointer only advances when you say so, and if it can't find its target it says
> that instead of pointing at the wrong thing."

**2:20 — WOW BEAT 3: KiCad.** Cmd-Tab to the already-open KiCad project
(the pre-generated one — do not download-and-open live, it burns 30 seconds):

> "Everything you saw is a real KiCad project — schematic, placed board, routed
> copper. KiCad's own ERC passes on the schematic. No KiCad was involved in
> making it: Silkscreen writes the file format directly, pure Python, so it runs
> headless, in CI, anywhere. Any net the router couldn't finish is left as
> ratsnest and named, with the reason — you finish it here, in the tool you'd
> use anyway."

**2:45 — Close.**

> "Gemini through the GenAI SDK, orchestrated by ADK dynamic workflows as the
> default engine, Cloud Run service with a Firestore datasheet cache. The
> deterministic engine underneath runs entirely offline — the whole test suite
> needs no network, no key, and no KiCad. Describe a board; get a project back;
> and it tells you what's wrong with it before your fab does."

---

## The 90-second cut

Drop the SPA and KiCad. One surface, one artifact, one argument.

- **0:00** Cold open line, type the golden intent in Kaleo, run. (15 s)
- **0:15** Narrate the stream, compressed: "reads the real datasheet with page
  citations — validates the circuit before anything is built — CP-SAT placement —
  A* routing that names what it can't finish — then an adversarial review." (45 s)
- **1:00** Voice beat: Kaleo speaks the ceramic-cap finding. (15 s)
- **1:15** Close: "That's a real KiCad project on disk, and the review just caught
  a stability bug with a datasheet citation. Gemini + ADK dynamic workflows +
  Cloud Run and Firestore." (15 s)

If the live run has not finished by 1:00, **Open session** the golden JSON in the
SPA and voice-beat becomes show-the-finding; the close is unchanged.

## The 30-second cut

No live run. SPA with the golden session already open, KiCad behind it.

> "You describe a board in plain language. Silkscreen reads the datasheet, designs
> and validates the circuit, places it with CP-SAT, routes it, and then argues
> against its own design — this finding says my output capacitor can make the
> regulator oscillate, with the datasheet page cited. [click finding — board
> highlights] And it's a real KiCad project [Cmd-Tab] — schematic, routed board,
> ERC clean. Gemini, ADK dynamic workflows as the default engine, Cloud Run,
> Firestore."

---

## Fallback plan, per failure

| Risk | What actually happens | Recovery — rehearse it |
|---|---|---|
| **Model hangs mid-run** | Known open issue: `GeminiModel` sets no request deadline, so a hung primary can sit there and failover never fires (TODO.txt feature 5, observed live 2026-08-30). | Do not stare at it past ~20 s of a silent stage. In the SPA the failed/stalled turn keeps retry / edit / switch-model controls — retry once on the cheaper model. If that stalls too: **Open session** with the pre-saved golden JSON and narrate it as "a run from this morning" — say it plainly; judges respect it. |
| **No wifi / venue network dies** | Model calls are the only network in the product. | Everything else is genuinely live offline: **Open session** restores the full golden run in the SPA (local file, no server round-trip for the transcript and findings); `python scripts/demo.py` does a real CP-SAT solve in front of them in ~20 s, deterministically; the pre-generated KiCad project carries the KiCad beat. The demo loses "watch the model think" and keeps everything else. |
| **Audio out fails / room is loud** | The speech layer never throws — a failed TTS call degrades to silence with one warning (`app/src/lib/speech/speaker.ts`), and the findings render on screen regardless. | The findings *are* the captions; read the top one aloud yourself. If ElevenLabs specifically fails, the built-in system voice is the automatic fallback — no action needed. |
| **Spoken intent misfires** | STT is bring-your-own-key through the shell and depends on room noise. | Type it. The script above already treats typing as the default; speaking is a garnish, not a beat. |
| **Kaleo won't launch or won't connect** | The .dmg is aarch64-only; the engine URL is configured in its Engine page with a live health check. | The web SPA at the service URL does the entire demo except the spoken digest — run the same script there and read the digest yourself. Check the health light *before* going on stage. |
| **SPA looks broken (empty feed, stuck form)** | This is the Vite dev-server split-module trap. | You should never be on the dev server on stage. If you somehow are: kill it, use the built bundle the service serves at `/`. |
| **KiCad missing or won't open the file** | — | The SPA's Schematic tab and board well cover the visuals; skip the KiCad beat and say the files are on disk. Never try to install anything live. |
| **Run finishes but the reviewer misses the capacitor finding** | It is a live model; the class is reliably elicited but not contractual. | Whatever findings it did produce are real — present those, with their citations. If you need the ceramic-cap beat specifically, the golden session has it. |
| **Service 502 mentioning GOOGLE_API_KEY** | You launched the service without the key exported (`python -m service.app` does not read `.env`). | The SPA renders this as setup instructions, not an outage — fix is `silkscreen serve`, which loads `.env`, or export the key and restart. Ten seconds if you know it; that is why it is on the T-30 checklist. |
