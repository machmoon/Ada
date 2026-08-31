# Speculative Placement Evaluation

## Frozen protocol

The benchmark compares the same three policy calls in two schedules:

1. Sequential: run all three isolated lanes one after another.
2. Speculative: run all three together, verifier-score arrivals, and cancel
   losing work after a safe early commit.

Both schedules receive the same corrupted board, profile, candidate actions,
lane prompts, turn budget, and verifier. The fixed workload is identified by
`speculative-placement-v1` and SHA-256
`fc202655337a1ef6e08665aee683d9702aeae2b11403f3b9207cab030cbafe05`.

Promotion requires all of the following:

- 100% legality on tune, with no regression against sequential execution
- at least 20% lower tune p50 latency
- no more than 5% p95 regression on the unopened holdout
- no legality regression on stress cases

## Live tune result

These are one-repeat engineering smokes, not a model-quality claim.

| Backend | Parallel p50 | Sequential p50 | Parallel legality | Decision |
| --- | ---: | ---: | ---: | --- |
| Gemma 3 4B, RTX 5090 | 2715 ms | 3663 ms | 2/3 | Reject |
| Qwen 2.5 3B, RTX 5090 | 2044 ms | 3199 ms | 1/3 | Reject |
| GLM 5.3 Flash, OpenCode | 21362 ms | 61785 ms | 0/3 | Reject |

Gemma and Qwen showed lower median latency, but both failed the absolute
legality gate. OpenCode proved the fallback wiring and usage accounting, but
all speculative runs hit a lane timeout and none completed the one-turn repair.
The holdout therefore remains unopened for every live backend.

The runtime safety claim is narrower and independently tested: model actions
cannot bypass the deterministic verifier, slow lanes return at their deadline,
winning legal prefixes can cancel losing lanes, and the service can finish with
deterministic repair when an experimental proposer is incomplete.
