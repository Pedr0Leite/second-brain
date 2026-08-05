# ADR-0007: Dense embedding model — bge-small-en-v1.5, not bge-base or MiniLM

**Date:** 2026-08-05
**Status:** Accepted
**Phase:** 3 (embedding) / 7 (operations)
**Follows:** ADR-0005 (index is built on the workstation and copied to the server)

## Context

The full-corpus embed had never actually been run. Building it revealed the cost:
`BAAI/bge-base-en-v1.5` sustains **3.3 chunks/s** through the shipped pipeline, and
the corpus is ~51,588 files ≈ 490,000 chunks — a **~41 hour** run.

```bash
$ python3 ingest/index.py embed --limit 500 --shuffle
DONE files=500 chunks=4801 elapsed=1473.7s rate=3.3 chunks/s
```

That is a one-time cost on a 99.3%-static corpus, so it is not by itself a reason
to change anything. Two other facts made the model worth revisiting:

1. **The target server is an N100.** ADR-0005 measured typical NiPoGi hardware at
   4 Alder Lake-N E-cores with no SMT, and sized the index snapshot at 2-3 GB.
   Vector dimension drives that snapshot directly.
2. **Retrieval quality is the known weak point.** `servicenow` recall@10 sits at
   0.455 on the full golden set, and ADR-0006 is explicitly gated on it. Any model
   change must be evaluated on recall, not on speed.

## Decision

**Use `BAAI/bge-small-en-v1.5` (384-dim) as the default dense model.**

## Evidence

A/B over an **identical haystack** — the same 500 shuffled files plus the 23
golden expected documents (523 files, 5,276 chunks in every collection) — with
only `DENSE_MODEL` varying. Separate collections and manifests so no run
contaminated another:

```bash
$ MANIFEST_DB_PATH=$S/minilm-manifest.db QDRANT_COLLECTION=knowledge_minilm \
  DENSE_MODEL=sentence-transformers/all-MiniLM-L6-v2 \
  python3 ingest/index.py embed --paths $S/sample500.txt --recreate
DONE files=500 chunks=4801 elapsed=140.2s rate=34.2 chunks/s

$ ... DENSE_MODEL=BAAI/bge-small-en-v1.5 ...
DONE files=500 chunks=4801 elapsed=543.8s rate=8.8 chunks/s

$ ... DENSE_MODEL=BAAI/bge-base-en-v1.5 ...      # baseline, first run
DONE files=500 chunks=4801 elapsed=1473.7s rate=3.3 chunks/s
```

Identical chunk counts (4,801) across all three confirm the chunker is
model-independent and the comparison is clean.

`python3 eval/run_eval.py`, **`servicenow` profile, 13 cases:**

| model | dim | index rate | dense r@10 | hybrid+rerank r@10 | **dense MRR** | dense p50 |
|---|---|---|---|---|---|---|
| bge-base-en-v1.5 | 768 | 3.3 ch/s | 0.909 | 0.818 | **0.795** | 204 ms |
| **bge-small-en-v1.5** | **384** | **8.8 ch/s** | **0.909** | **0.909** | **0.705** | **90 ms** |
| all-MiniLM-L6-v2 | 384 | 34.2 ch/s | 0.909 | 0.909 | **0.615** | 167 ms |

Constructed-case `servicenow` recall@10: base 0.818, small **0.909**, MiniLM 0.909.

**recall@10 ties at 0.909 across all three because it has saturated** — a 523-file
haystack is ~100x smaller than the real corpus and too easy to discriminate. MRR
has not saturated, and it degrades monotonically with model capacity
(0.795 → 0.705 → 0.615). MRR measures *where* the right answer lands; on a
full-size haystack a ranking deficit converts into outright recall misses.

**These numbers cannot pass the Phase 4 gate and are not offered as if they
could.** All are constructed cases (3 real, need 20 — `run_eval.py` exits
INCONCLUSIVE). `personal` scores 1.000 for every model, which is the known
circularity showing. They are a *model-selection* comparison on a reduced
haystack, nothing more.

## Why not MiniLM, despite being 10x faster

MiniLM indexes the full corpus in ~4h versus bge-small's ~15.5h. That saving is
real but it is **paid once, on the workstation**, and ADR-0005 already decided the
server never rebuilds the index — so the server never experiences the difference.

On the NiPoGi the two models are **indistinguishable in resource terms**: both are
384-dim, so identical vector storage, identical Qdrant memory, identical query
cost. bge-small is even smaller on disk (0.067 GB vs 0.09 GB). The only thing
separating them on the server is ranking quality, where bge-small leads by ~15%
MRR.

Trading permanent ranking quality for a one-time 11-hour saving, on a system whose
acknowledged problem is relevance, is the wrong exchange.

## Consequences

**Good**

- Full-corpus embed drops from ~41h to ~15.5h (2.7x).
- Query-time dense latency drops 204ms → 90ms p50 (2.3x).
- 384-dim **halves vector storage**, directly shrinking the ADR-0005 snapshot
  that has to be copied to and held by an N100.
- `hybrid+rerank` on `servicenow` improved 0.818 → 0.909 on this haystack — the
  smaller model was not worse where it was measured.

**Bad / accepted**

- **Dimension change forces a full rebuild.** 768-dim and 384-dim points cannot
  coexist; the collection must be recreated (`embed --recreate`) and the 4,801
  bge-base vectors already built were discarded.
- **MRR is genuinely lower than bge-base** (0.705 vs 0.795). Accepted because
  recall@10 and hybrid+rerank did not regress, and because bge-base's advantage
  was not large enough to justify 41h and double the server-side storage. This
  should be re-measured once the golden set has 20 real cases.
- **The comparison ran on a 523-file haystack.** Relative ordering is the usable
  signal; absolute numbers are inflated and must not be quoted as corpus-level
  recall.

## Alternatives rejected

**Keep bge-base-en-v1.5.** Best MRR of the three and no rebuild needed. Rejected
on total cost: 41h to index, 768-dim storage on a RAM-constrained N100, and 2.3x
query latency — for an MRR edge measured on a saturated haystack that no longer
showed up in recall@10 or in hybrid+rerank, where it was actually *worse* (0.818
vs 0.909).

**all-MiniLM-L6-v2.** 10x faster indexing, ~4h full corpus. Rejected: worst MRR of
the three (0.615), and its sole advantage is an operation the target server never
performs. See above.

**bge-large-en-v1.5 (1024-dim).** Not benchmarked. Rejected without measurement on
the ADR-0005 constraint alone: 1.2 GB model and 1024-dim vectors on a 4-core N100
is the opposite direction from where the deployment target points.

**Change nothing until the golden set has 20 real cases.** The disciplined option,
and genuinely tempting given the gate is INCONCLUSIVE. Rejected because the full
index had to be built either way, and building 490,000 vectors at 768-dim only to
rebuild them later is the single most expensive way to defer the decision. The
rebuild cost is what forced the choice now.

## Follow-up

Re-run this comparison once `eval/golden.yaml` reaches `MIN_REAL_CASES`. If
bge-base proves materially better on real cases, the decision is reversible at the
cost of one re-embed — which is exactly the cost this ADR chose to pay once rather
than twice.
