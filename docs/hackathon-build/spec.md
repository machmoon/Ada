# Placement Agent Build Spec

The portable core uses only the Python standard library. It represents boards,
components, keepouts, company profiles, placement actions, violations, and
weighted scores. Its public reward functions stay independent of Gemini,
Verifiers, or any training backend.

The runtime agent receives the current board, profile, and verifier feedback.
It emits absolute PLACE or relative MOVE actions. Unknown references and
malformed text do not crash a run. Only score-improving actions are accepted.
A deterministic repair policy provides reproducible offline operation and
synthetic SFT traces.

Cloud Run exposes a same-origin placement repair endpoint. The existing Svelte
frontend adds a focused placement lab showing before/after geometry, violations,
profile terms, action history, and reward. Gemini is the live policy when
requested; deterministic mode remains available for tests and the fallback demo.
