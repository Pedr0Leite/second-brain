# ADR-0004: The local model plans and selects; it never writes the answer

**Date:** 2026-08-04
**Status:** Accepted
**Phase:** 5

## Context

Phase 5 needs a local planner. Until now `PLANNER_BASE_URL` and `PLANNER_MODEL`
were unset by design, so `sn_research` returned `PLANNER_UNAVAILABLE` rather than
silently failing over to a paid route.

A planner model now exists locally:

```
$ ollama list
qwen2.5:3b-instruct    357c53fb659c    1.9 GB
```

Measured on this box (CPU only — `nvidia-smi` absent, 12 cores):

```
prompt  50 tok in 0.38s = 132.5 tok/s
gen     35 tok in 1.69s =  20.7 tok/s
total   5.04s  (including cold model load)
```

That immediately constrains what the local model may be asked to do. `sn_research`
carries a 900-word output cap. 900 words is roughly 1,200 tokens, and at **20.7
tok/s that is ~58 seconds of generation** — for a single call, against a 15 s p95
budget. Local generation is not marginally over budget; it is roughly 4x over on
its own, before retrieval or reranking is counted.

The obvious reactions are both wrong:

- *Buy a bigger box / use the GPU.* There is no GPU, and the project's premise is
  that this runs on ordinary local hardware.
- *Call a hosted model for synthesis.* This is precisely the failure the whole
  project exists to prevent. It would work, appear fast, and quietly reintroduce
  the cost the system was built to remove.

## Decision

**The local model plans and selects. It never generates prose for the user.**

Concretely, the local planner is permitted only short, structured outputs:

| allowed | typical size | why it fits |
|---|---|---|
| decompose a question into sub-queries | ~40 tok | ~2 s |
| choose an agent profile / facets | ~20 tok | ~1 s |
| judge whether a candidate chunk is relevant | ~5 tok | sub-second |
| decide "enough evidence, stop" | ~5 tok | sub-second |

Evidence compression is **extractive, not generative**: the tools already return
chunk text with breadcrumbs and citations, and `mcp_server/caps.py` already
enforces the size limits in code. Selecting which spans to keep needs no
generation at all.

Synthesis — turning cited evidence into an answer — is Claude's job. It already
has the question, the conversation, and the user's intent. Duplicating that
locally at 20 tok/s buys nothing and costs a minute.

This is not a compromise forced by weak hardware. It is what the build spec
described from the start: *retrieval runs on free local models; Claude only sees
compressed, cited evidence*. The measurement simply makes the boundary
non-negotiable rather than aspirational.

## Consequences

- `sn_research` returns **selected, cited evidence with a reasoning trace**, not a
  written answer. Its 900-word cap now bounds *evidence*, which is what the cap
  was always meant to protect.
- The latency budget becomes achievable: ~2 s planning + ~0.9 s vector retrieval +
  sub-second selection, rather than ~60 s of local generation.
- `PLANNER_UNAVAILABLE` remains the behaviour when no local route is configured.
  **No fallback to a hosted model is added, ever** — that is the one change that
  would invalidate the project while making every metric look better.
- Swapping in a larger local model later changes throughput, not this boundary.
  A 7B model at ~8 tok/s would make local synthesis worse, not better.
- The planner's output is structured and short enough to validate. Malformed JSON
  is a retryable structured error, not a prose apology.

## Rejected alternatives

**Local synthesis with a raised latency budget.** Moving p95 from 15 s to 90 s to
accommodate CPU generation trades the system's responsiveness for a capability
Claude already provides for free. A tool that takes a minute stops being used.

**Streaming local synthesis so it "feels" faster.** MCP tool calls are not
streamed to the user; the total is what Claude waits for. This would hide the cost
from no one.

**Hosted planner "just for planning".** Planning is small, but a configured hosted
route is a configured hosted route: the next change extends it to synthesis. The
architectural guarantee is worth more than the quality difference on a 40-token
JSON emission.
