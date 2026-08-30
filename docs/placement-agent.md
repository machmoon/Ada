# Silkscreen Placement Agent

Silkscreen repairs PCB placement faults while following an explicit company
profile. The same damaged board can produce two different legal layouts because
compact-control and thermal-first teams optimize different preferences.

The end-user entry point is simpler than the verifier lab: name a chip and the
job the board should do. Silkscreen turns that request into a circuit proposal,
retrieves any supplied manufacturer evidence, validates the circuit, places the
parts, runs the independent review, and returns a placed KiCad board. The
placement lab remains the inspectable proof surface underneath that flow.

## What runs today

The demo exposes two modes. Verified fast policy is the product path. It picks
the best configured small-policy backend and falls back to deterministic repair
when none is available. Gemini directly is the comparison demo. Backend names
remain visible in the run receipt, not as separate product choices.

On the private-GPU stopgap, base Gemma 3 4B runs through Ollama on the 5090.
Deterministic search produces a bounded candidate set and Gemma selects a short
ordered batch. The geometry verifier evaluates each action and commits only the
longest improving prefix. This is speculative placement execution, not
token-level speculative decoding.

The visible score has two axes. `H` is hard penetration in millimetres from
overlap, clearance, boundary, and keepout faults. A legal placement has `H = 0`.
`P` is the weighted company-preference cost for grouping, connector access,
compactness, and thermal separation. Lower is better. The old combined internal
ranking scalar is deliberately not exposed.

## Agent, policy, verifier, and training

The **agent** owns the multi-turn loop. It assembles the board and company
profile, asks a policy for candidate actions, applies verifier feedback, and
decides whether another turn is needed.

The **policy** proposes actions. Today that can be deterministic search, Gemini,
or base Gemma on the private 5090. Hybrid mode tries the fast policy first and
uses Gemini only when no proposed action is accepted.

The **verifier** is authoritative. It recomputes `(H, P)` after each action and
accepts an action only if that pair improves lexicographically. Hard geometry
therefore always outranks preference. Neither Gemini nor the small policy can
override it.

**Supervised fine-tuning** teaches a small policy the action grammar and repair
patterns from deterministic traces plus accepted engineer corrections. Tinker
does not currently list Gemma as a supported training model, so the included
Tinker recipe targets Qwen 3.5 4B. The branch ships the data exporter, training
recipe, and checkpoint loader. No hosted checkpoint is claimed without a
successful training run and held-out promotion gate.

**Reinforcement learning** comes only after SFT. Its portable reward is outcome
for a legal board, normalized progress in `H`, then a small preference reward
for legal boards. RL may improve the policy, but it never replaces the runtime
verifier. A promoted policy must beat the SFT checkpoint on a frozen held-out
set without reducing legality.

## Failure memory for post-training

Every rejected Qwen or local-policy proposal can become a verifier-backed
training example. The trace stores the exact board, company profile, prompt,
raw proposal, accepted prefix, per-action verifier receipts, and the run's
reward. Its preferred target is the successful Gemini recovery when one
exists, otherwise the deterministic repair oracle. Terminal boards that remain
illegal at the turn limit are recorded too.

Local runs append JSONL under `artifacts/`; Cloud Run uses a separate Firestore
collection. Demo boards record by default. Uploaded boards never record unless
the request explicitly sets `record_trace` to true. The export script produces
both chat messages for SFT and chosen/rejected pairs for later preference
training. Collection does not mean automatic training or promotion. A new
checkpoint still has to beat the frozen held-out legality and preference gates.

Hard rules include boundaries, clearance, keepouts, and fixed components. Soft
preferences include grouping, connector access, compactness, and thermal
separation. Engineer corrections are structured profile updates. In Cloud Run,
the service stores those updates in Firestore; local and test runs use memory.

## Honest claim

The shipped proof is placement repair against deterministic rectangle geometry.
It is not routing, electrical validation, fabrication readiness, or proof that
an SFT or RL policy outperforms the deterministic baseline.
