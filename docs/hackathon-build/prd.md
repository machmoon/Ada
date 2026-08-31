# Placement Agent Product

Hardware engineers inherit crowded boards and team-specific layout habits that
generic placers do not understand. The placement agent lets an engineer select
a named company profile, inspect each proposed move, correct the policy, and
download the repaired placement.

Hard rules cover board boundaries, component clearance, fixed parts, and
keepouts. Soft preferences cover functional grouping, connector access,
compactness, and thermal separation. A deterministic verifier owns legality.
Gemini chooses actions and explains them, but cannot override the verifier.
