# Autonomous Placement Agent Checklist

- [ ] Implement portable geometry, company profiles, action parsing, scoring, and repair.
  Verify: focused Python tests cover legal, corrupted, fixed, keepout, and profile cases.
- [ ] Implement the Gemini-compatible multi-turn runtime and synthetic trajectory export.
  Verify: scripted-model tests prove accepted moves, ignored hallucinations, and reward traces.
- [ ] Add a placement repair service endpoint with deterministic and Gemini policies.
  Verify: socket tests cover success, invalid input, and model-free operation.
- [ ] Add the same-origin placement lab to the current Svelte frontend.
  Verify: frontend tests and production build pass with no fabricated actions or scores.
- [ ] Document the shipped claim and the agent, SFT, RL, verifier boundary.
  Verify: documentation drift check passes.
- [ ] Run the full Python, lint, frontend, and deterministic demo gates.
  Verify: preserve exact command output in build notes.

Build mode: autonomous. Pause only for external credentials, deployment authority,
or a real training run that would consume paid compute.
