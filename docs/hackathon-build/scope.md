# Placement Agent Scope

Silkscreen repairs component-placement faults on a bounded PCB and applies an
explicit company placement profile. The demo begins with an overlapping board,
runs a multi-turn repair, exposes the score change after every action, and ends
with a legal placement. Re-running the same board under a different profile
must produce a different legal result.

The protected claim is geometry-grounded placement repair. Routing, fabrication,
electrical correctness, and a trained production policy are outside this branch.
