# Architecture

How the system is built and — more usefully — *why* each piece is shaped the way it
is. Decisions with alternatives worth remembering live in `adr/`. Measurements and
the running build history live in `BUILD-LOG.md`.

---

## 1. The problem

An Obsidian vault holding ~51,600 markdown files (5.2 GB) was serving two jobs at
once: authoring surface and retrieval layer. Both failed.

| symptom | cause |
|---|---|
| Obsidian unstable | 51k files in one vault; metadata cache, backlink graph and Smart Connections embeddings all in-process |
| Token burn | Agent navigation via `INDEX.md` + grep + whole-file reads to find one paragraph |
| Weak semantic search | Smart Connections is built for personal vaults, not a 51k-doc technical corpus |

**Both problems share one root cause: retrieval and authoring running in the same
process over the same 51k files.** Splitting them fixes both.

The corpus moves to a dedicated service. The vault keeps only human-authored notes.

---

## 2. Shape

```
Claude Code  ──MCP (capped)──>  agent loop  ──>  retrieval  ──>  Qdrant
                                                     │              (dense+sparse)
                                                     ├──>  parent store (SQLite)
                                                     └──>  ripgrep (exact symbols)

ingest:  corpus ──> normalize ──> chunk ──> embed ──> Qdrant + manifest
```

**The cost boundary is the point of the whole design.** Retrieval runs entirely on
local/free models. Claude is invoked only on the compressed result. If retrieval
ever calls an expensive model, the project has failed at its stated purpose.

---

## 3. Ingest

### Corpus classification (`ingest/normalize.py`)

Every top-level directory maps to a `source` class in `config.SOURCE_BY_TOP_DIR`.
An unmapped directory raises rather than being silently skipped — a silently
skipped directory is an invisible hole in the index.

| source | files | what |
|---|---|---|
| `official` | 51,251 | vendor documentation |
| `personal` | 325 | Notion migration, inbox, dashboards |
| `wiki` | 39 | Karpathy-pattern LLM wiki layer |
| `custom-app` | 25 | app notes, agents, skills |
| `code-graph` | 2 | Graphify output |

`index.md` files are excluded: they are 500 KB–2 MB navigation link-dumps, not
retrievable content. They still get a manifest row with `status='skipped'`, so file
counts reconcile exactly rather than needing an explanation.

### Manifest (`ingest/manifest.py`, SQLite)

`rel_path -> sha256 -> chunk_ids`, plus the parent store and a `pending_deletes`
queue. Without content hashing, every reindex is a full reindex.

**Content hash decides staleness — not indexing status.** Comparing the desired
status (`pending`) against the stored status (`indexed`) reports every indexed file
as changed on every run. That bug shipped briefly and is now covered by the
"re-running normalization is a no-op" acceptance test.

### Chunking (`ingest/chunker.py`, pure functions)

Parents = H1–H3 sections merged to 2,000–4,000 chars. Children ≈ 500 chars.
Small-to-search, large-to-read.

Four invariants, all test-enforced:

1. Parents tile the document body **exactly** — concatenating them reproduces it
   byte-for-byte.
2. **No chunk boundary ever falls inside a fenced code block, pipe table, or HTML
   `<table>`.** These are atomic.
3. An atomic block larger than the cap is emitted **oversized rather than split**.
   A bisected `GlideRecord` example is worse than useless — it is wrong.
4. Children carry the breadcrumb of the block they came from, which may be deeper
   than their merged parent's.

`h_path` (the heading breadcrumb) is prepended to the embedded text. A 500-char
fragment of a ServiceNow doc is frequently meaningless without it. Breadcrumbs are
markdown-unescaped, because `addQuery\(String\)` is embedding noise.

Measured projection: **~117k parents, ~525k children**, mean child 483 chars,
~0.25 GB of text. That is +31% on the original ~400k estimate and drives every RAM
and throughput figure downstream.

### Embedding (`ingest/embed.py`)

**Length-sorted batching is load-bearing, not an optimisation.** Transformer
batches pad every sequence to the longest member, and this corpus mixes 200-char
prose with 30,000-char code blocks (the oversized atomic chunks invariant 3
produces). Sorting by length before batching measured **5.4x** throughput —
16.9 → 92.4 chunks/sec. Smaller batches beat larger ones, which is backwards from
the usual advice and only makes sense once padding is understood as the bottleneck.

Qdrant: one collection, named `dense` + `sparse` vectors, int8 scalar quantization
(`always_ram`), original vectors on disk. Payload indexes on `source`, `doc_type`,
`rel_path`, `api_symbols`, and `facets.*` — unindexed filters over ~500k points are
slow.

### Crash safety

Qdrant upsert happens **before** the manifest commit. A crash between them leaves
surplus vectors for a file still marked pending, which the next run overwrites
idempotently (point IDs are deterministic sha1-derived UUIDs).

The reverse order would leave a file marked `indexed` with no vectors — a silent
recall hole nothing would ever repair. `index.py status` reconciles manifest against
Qdrant and exits non-zero on drift.

Deletions are queued in `pending_deletes` and drained on the next embed, so a
removal detected while Qdrant is down is not lost.

An exclusive lock lives in `index.py` itself, not only in the sync wrapper, so a
manual run and a timer-triggered run cannot interleave over the same manifest.

---

## 4. Retrieval

| stage | module | purpose |
|---|---|---|
| hybrid search | `retrieval/hybrid.py` | dense + BM25 sparse, RRF fusion, payload pre-filter |
| rerank | `retrieval/rerank.py` | cross-encoder, top-30 → top-8 |
| lexical | `retrieval/lexical.py` | ripgrep for exact identifiers |
| parent expansion | `retrieval/parents.py` | child hit → surrounding section |
| agents | `retrieval/profiles.py` | scoping and routing policy |

Sparse is not optional: dense embeddings smear exact identifiers like
`sys_user_grmember` or `GlideRecord.addQuery`, which is precisely what technical
lookups depend on.

### Search agents

Agents are retrieval **policy** over one shared index — not separate indexes.

| agent | sources | behaviour |
|---|---|---|
| `general` | all | whole second brain |
| `servicenow` | `official` | vendor docs only; boosts `api`/`reference`; accepts `release`/`product`/`classification` facets |
| `personal` | everything else | your own material only |

`personal` exists because filtering *to* ServiceNow implies its complement, and
without it "search only my notes" is impossible — 51,251 official docs bury 391
personal ones sharing the same vocabulary.

Profiles **reject** unsupported facets (`personal` has no `release`) rather than
silently ignoring them and returning wrong-scoped results.

Code-like queries (dotted calls, CamelCase, `sys_*`, `gs.`/`gr.` prefixes) route
through ripgrep and promote exact matches above vector hits.

---

## 5. Evaluation

`eval/run_eval.py` measures recall@5, recall@10, MRR and p50/p95 latency across
`dense | sparse | hybrid | hybrid+rerank`, per profile.

**Gate: hybrid+rerank recall@10 >= 0.85.** Nothing gets built on top of retrieval
until it clears.

The set itself (`eval/golden.yaml`) is human-authored and cannot be automated — see
`GOLDEN-SET-GUIDE.md`. The authoring tool's `find` uses filename and ripgrep only,
never the vector search under evaluation, because choosing expected paths from the
system's own output makes the measurement circular.

---

## 6. Operations

Both run as **user** systemd units — no root, running as the account that owns the
corpus and model cache.

| unit | purpose |
|---|---|
| `qdrant.service` | `Restart=always`. An unsupervised Qdrant died mid-session and every retrieval failed until noticed by hand |
| `sn-rag-sync.timer` | nightly 03:00 corpus sync + incremental reindex |

### Missed-run catch-up

Plain cron silently skips a window if the machine was off. Two independent
mechanisms prevent that:

1. **`Persistent=true`** — systemd stamps each fire in
   `~/.local/share/systemd/timers/`; at boot it compares that against the schedule,
   sees the missed window, and runs. `OnBootSec=2min` stops a catch-up run racing
   Qdrant's startup.
2. **A staleness check inside the script** — if the last *successful* run is older
   than `MAX_AGE_HOURS` (20), it proceeds regardless of caller. This makes the
   script safe on hosts without systemd.

`install.sh` seeds the stamp, because `Persistent=true` treats a never-run timer as
having missed its window and would otherwise fire a multi-hour index on install.

The `git pull` is **best effort**. The corpus is a live Obsidian vault, so a dirty
working tree is the normal state and `git pull` refuses to run. Indexing is driven
by on-disk sha256, so local edits index correctly either way. Aborting here would
have failed the sync every night for anyone who edits their own notes.

For a headless server with nobody logged in, user timers need
`sudo loginctl enable-linger $USER`.

---

## 7. Deliberate constraints

- **No silent fallback.** If the planner route is unavailable, tools return
  `PLANNER_UNAVAILABLE`. Failing over to an expensive model would silently destroy
  the cost boundary the project exists to create.
- **Output caps in code, never in prompts.** A prompt instruction is a suggestion.
- **No tool returns a whole file.** Full reads are an explicit, visible escalation.
- **Fabrication is a build failure.** Hence negative cases in the golden set.
