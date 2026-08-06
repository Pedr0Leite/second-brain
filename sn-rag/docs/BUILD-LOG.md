# BUILD-LOG

## 2026-08-04 — Phase 0: Recon & baseline decisions

### Corpus stats (measured)

Commands run against `/mnt/c/Users/pedro/Documents/Programacao/Github/obsidian-servicenow-docs`:

```
find . -name '*.md' | wc -l
=> 51642

du -sh .
=> 5.2G

find . -name '*.md' -printf '%s\n' | sort -n | awk '{a[NR]=$1} END {print "n",NR; print "p50",a[int(NR*0.5)]; print "p90",a[int(NR*0.9)]; print "p99",a[int(NR*0.99)]; print "max",a[NR]}'
=> n 51642
   p50 3329
   p90 8702
   p99 29265
   max 2230795
```

Per-top-level-dir `.md` counts:

```
ServiceNowOfficialDocs: 51251
Notion: 287
wiki: 39
Clippings: 1
Applications: 14
ClaudeAgents: 9
ClaudeSkills: 2
chats: 0
raw: 33
graphify: 2
logs: 0
scripts: 0
Dashboards: 1
```

`.smart-env` (Smart Connections store) and `.obsidian` also present — vault has been running Smart Connections against the full 51k-file corpus.

Repo already git-tracked: `git remote -v` → `origin git@github.com:Pedr0Leite/obsidian-servicenow-docs.git`. Confirmed **private**.

**Outlier finding:** the size-percentile max (2.2MB) and several files 500KB–2MB are not real content — they are per-category `index.md` nav/link-dump files (e.g. `ServiceNowOfficialDocs/employee-service-management/index.md` at 2,060,209 bytes). Verified by reading `api-client-next.md` (normal, 49 lines) vs. these index files (pure link lists). **Decision: exclude `index.md` files from the RAG index entirely** — they are navigation, not retrievable content; the docs they link to are indexed individually.

### Sample documents (Phase 0 fixtures, read in full)

1. **API reference (list)**: `ServiceNowOfficialDocs/api-reference/api-client-next.md` — frontmatter has `release`, `product`, `classification`, `topic_type`, `breadcrumb`, `tags`. Wikilink-heavy index of API classes.
2. **API reference (method detail)**: `ServiceNowOfficialDocs/api-reference/GlideFormAPINX.md` — 46min reading time, dozens of `## Heading` per-method sections, code fences (some unlabeled ``` blocks), pipe tables, one raw HTML `<table>`. This is the hard case for the chunker (§9 Phase 2 code-fence property test).
3. **Task-type doc**: `ServiceNowOfficialDocs/it-service-management/accept-chat-ai-native-itsm.md` — `topic_type: task`, numbered procedure, `[Omitted image ...]` placeholders, `Role required:` line.
4. **Release note**: `ServiceNowOfficialDocs/release-notes/australia-all-other-fixes.md` — `topic_type: reference`, 144min reading time, large HTML `<table>` of fixed PRBs.
5. **Personal note (Notion export)**: `Notion/ServiceNow/AI & VA/50+ (Un)documented Virtual Agent variables....md` — `source: notion-export` frontmatter, no `release`/`product` fields (confirms personal-source docs need a different, simpler metadata schema than official docs).

Frontmatter across official docs is rich and deterministic → confirms DECISION-4 answer (directory + frontmatter, no inference job needed for `ServiceNowOfficialDocs/*`). Personal/Notion notes carry `source: notion-export`/`area` instead — `source` field itself is directly usable for the `source` payload classification.

### Hardware facts (measured, this WSL machine — not yet Nipogi)

```
lscpu: AMD Ryzen 5 9600X, 6 cores / 12 threads, AMD-V
free -h: 25Gi total, 21Gi free (WSL-allocated)
df -h: / has 931G avail; C:\ mount 548G avail; D:\ 252G avail
nvidia-smi: command not found — GPU passthrough not configured in this WSL shell
```

User confirmed an NVIDIA GPU is physically available, but WSL/CUDA passthrough is **not yet set up** — treat as CPU-only until that's installed; revisit DECISION-2 embedding model choice if/when GPU becomes available.

Build target: **generic (any local PC/server)**, not Nipogi-specific — user wants this deployable anywhere, not hard-coded to one box.

### Decisions locked (§11)

| # | Decision | Answer |
|---|---|---|
| 1 | Corpus transport | Git clone/pull of existing private repo (already git-tracked) |
| 2 | Embedding wall-clock budget | Overnight — model choice still gated on Phase 3 benchmark |
| 3 | Qdrant collections | One collection, `source` payload filter |
| 4 | product_family/release metadata | Directory + frontmatter, deterministic (confirmed feasible above) |
| 5 | Graphify/claude-memory-compiler outputs | Index them too (not deferred — adds schema work in Phase 2/3) |
| 6 | Smart Connections plugin | Retire |
| 7 | Observability | Langfuse (self-hosted, always-on) |
| 8 | Tolerable p95 query latency | ~15s or more (loose — prioritize retrieval quality over speed) |
| — | Governance | Repo private, user confirmed compliant with employer policy |
| — | Obsidian endgame | Vault keeps human-authored notes only; full corpus leaves vault |
| — | New `index.md` nav-dump handling | Exclude from index (found during recon, not in original open-questions list) |
| — | Project location | `sn-rag/` inside `second-brain` repo (not a standalone repo) |

### Deferred (not blocking Phase 1)

- Blocker #9 (golden eval set, ≥30 Q&A pairs) and #10 (baseline token measurement on current Obsidian workflow) — explicitly deferred by user. Required before Phase 4 gate and Phase 6 headline measurement; must be done before those phases start.
- GPU/CUDA passthrough setup for WSL — optional, revisit at Phase 3 if user wants faster indexing than CPU-only overnight budget allows.

### Rejected alternatives

- NFS/SMB mount and rsync for corpus transport — rejected in favor of git, since repo already exists and is already the sync mechanism between Obsidian and elsewhere.
- Separate Qdrant collections per source — rejected, single collection + filter is simpler ops and still supports personal-only search.
- Metadata inference batch job (DECISION-4) — rejected as unnecessary; frontmatter already carries the needed fields.

## 2026-08-04 — Phase 1: Normalizer + manifest (no ML)

Repo scaffolded at `sn-rag/` inside `second-brain` per §8 (`config.py`, `ingest/`, `retrieval/`, `agent/`, `mcp_server/`, `eval/smoke/`, `docs/adr/` — layout only; retrieval/agent/mcp_server are empty until their phases).

`config.py` encodes `SOURCE_BY_TOP_DIR` as the deterministic classification table derived from the Phase 0 dir breakdown, plus `EXCLUDED_DIRS` (`.git`, `.obsidian`, `.smart-env`, `.claude`) and `EXCLUDED_FILENAMES` (`index.md`, the nav-dump files found in Phase 0).

`ingest/normalize.py` — pure functions: `iter_corpus_files`, `classify_source` (raises on unknown top-level dir), `skip_reason`, `sha256_of`. `ingest/manifest.py` — SQLite schema exactly per §5.2 (`files`/`chunks`/`errors`), connection context manager, upsert/query helpers. `ingest/index.py` — CLI `full` (walk + classify + hash + upsert, deletes manifest rows for files no longer on disk) and `verify`.

### Acceptance evidence

```
$ CORPUS_PATH=<vault> MANIFEST_DB_PATH=./manifest.db python3 ingest/index.py full
pending=51588 skipped=54 unchanged=0 errors=0 deleted=0

$ python3 ingest/index.py verify
filesystem_md_count=51642
manifest_rows=51642
match=True
by_status={'pending': 51588, 'skipped': 54}
by_source={'code-graph': 2, 'custom-app': 25, 'official': 51251, 'personal': 325, 'wiki': 39}

$ python3 ingest/index.py full   # second run, nothing changed on disk
pending=0 skipped=0 unchanged=51642 errors=0 deleted=0
```

- [x] `manifest_rows == filesystem_md_count` — exact match, 51642 == 51642.
- [x] Re-running normalization is a no-op — second `full` run: 0 pending, 0 skipped, 0 errors, 51642 unchanged.
- [x] Source classification 100% deterministic, covers every file — 0 classification errors across 51642 files; every top-level dir mapped in `SOURCE_BY_TOP_DIR` (unmapped dir raises `ValueError` and is caught into `errors` table, not silently skipped — untested in this run since no file triggered it, all 13 top-level dirs with `.md` content are covered).
- 54 files skipped = the `index.md` nav-dump files found in Phase 0 (still get a manifest row with `status=skipped`, so `manifest_rows` stays equal to filesystem count — satisfies the "exact equality or explained skip list" acceptance clause literally, via the `status` column being the explanation).

Next: Phase 2 (pure, unit-tested chunker) against the 5 Phase-0 fixture docs.

## 2026-08-04 — Phase 2: Chunker (pure, unit-tested)

Decided first: **ADR-0001** (generic core, ServiceNow as first corpus profile). Prompted by
the direct question "is this for ServiceNow or a real second brain?" — answer is second brain,
and Phase 2 is where the metadata schema gets fixed, so genericizing had to happen now rather
than after ~500k chunks are embedded. Chunk metadata carries a generic `facets` dict instead of
typed `product_family`/`release` fields; `api_symbols` is redefined as domain-agnostic code
identifier extraction.

`ingest/chunker.py` — pure functions, zero I/O. Four stated invariants, all test-enforced:
parents tile the body exactly; no boundary ever falls inside a fenced code block, pipe table,
or HTML `<table>`; an oversized atomic block is emitted oversized rather than split; children
carry the breadcrumb of the block they came from (which may be deeper than their merged
parent's).

### Acceptance evidence

```
$ CORPUS_PATH=<vault> python3 -m pytest tests/test_chunker.py -q
63 passed in 0.46s
```

- [x] **Golden-fixture tests over the 5 Phase-0 documents** — `tests/test_chunker.py::FIXTURES`
      pins the exact five files; every property test is parametrized across all of them.
- [x] **Property test: parent concat reproduces the original modulo whitespace** — asserted both
      ways: `test_parents_reproduce_body_modulo_whitespace` (the spec's wording) and
      `test_parents_reproduce_body_verbatim` (stronger: byte-exact tiling, which the chunker
      actually achieves).
- [x] **Property test: no chunk boundary inside a fenced code block or table** —
      `test_no_parent_boundary_inside_atomic_block` + `test_no_child_splits_an_atomic_block`
      over all 5 fixtures, plus `test_giant_code_block_is_never_split` with a 6,000+ char code
      block as the spec requires, plus giant-pipe-table and giant-HTML-table cases.
- [x] **`api_symbols` extraction verified against the API-reference fixture** —
      `test_api_symbols_from_api_reference_fixture` asserts `g_form.addChoice`, `addChoice`,
      `GlideForm`, `addDecoration` are extracted from `GlideFormAPINX.md`. Negative tests confirm
      URLs/filenames are rejected and that extraction is language-agnostic (Python fixture).

### Two defects found by eyeballing real output, not by the tests

Tests passed 57/57 on first run; spot-checking an actual chunk found both of these anyway.
Recorded here because "tests are green" was not sufficient evidence:

1. **Breadcrumb off-by-one section.** A child whose text opened with the `addDecoration`
   heading was labelled `... > addChoice(...)`. Cause: a blank-line text remnant belonging to
   the *previous* section entered the child buffer first and pinned `buf_h_path`. Fix: adopt the
   breadcrumb of the first *substantive* unit. Regression test:
   `test_child_breadcrumb_matches_its_own_heading`, parametrized over all fixtures.
2. **Markdown escapes leaking into `h_path`.** Breadcrumbs read
   `addQuery\(String name\)`. Since `h_path` is prepended to the embedded text (§5.1), those
   backslashes are pure embedding noise. Fix: `unescape_markdown()` applied to heading titles.
   Regression test: `test_h_path_is_unescaped`.

Neither fix changes chunk boundaries — only labels — so the projection below is unaffected.

### Chunk-count projection (arithmetic shown)

```
$ python3 scripts/project_chunks.py --sample 2000
population (status=pending)   = 51588
sample size (seed=20260804) = 2000   [failures: 0]
chunking wall-clock           = 5.35s  (374 files/sec, chunking only)

sample parents                = 4552
sample children               = 20362
parents/file                  = 4552 / 2000 = 2.276
children/file                 = 20362 / 2000 = 10.181
children/file median          = 7.0
children/file p90             = 21
children/file max             = 217

PROJECTED parents  = 2.276 x 51588 = 117,414
PROJECTED children = 10.181 x 51588 = 525,217
  (scale factor = 51588 / 2000 = 25.79)

mean child chunk size = 9825960 / 20362 = 482.6 chars
PROJECTED text to embed = 525,217 x 482.6 chars = 0.25 GB

by source (sample):
  code-graph   files=    1 parents/file=  5.00 children/file= 28.00
  custom-app   files=    1 parents/file= 32.00 children/file=217.00
  official     files= 1981 parents/file=  2.25 children/file=  9.99
  personal     files=   15 parents/file=  3.53 children/file= 21.40
  wiki         files=    2 parents/file=  1.00 children/file=  4.00
```

Mean child size 482.6 chars against a 500-char target — packing is working, not degenerating
into many tiny chunks.

**Carry into Phase 3:** the projection is **~525k child vectors, not the ~400k the spec's §5.1
assumed**. That is +31% on every Qdrant RAM/disk estimate, and it moves the embedding-throughput
requirement for an overnight budget (DECISION-2) to roughly `525,217 / (10 hours x 3600s) ≈ 15
chunks/sec` sustained as the floor. Benchmark against that floor, not against the spec's number.

Chunking itself is cheap (374 files/sec → full corpus in ~2.3 min), so it is not a bottleneck
and can be re-run freely when chunk parameters are tuned.

Next: Phase 3 (embedding benchmark on DECISION-2 candidates, then 500-doc sample index).

## 2026-08-04 — Phase 3: Embedding + index

Environment gaps found first: no `docker`, no `nvidia-smi`, no embedding packages, and
`python3 -m venv` fails (`ensurepip is not available`). Installed `fastembed` + `qdrant-client`
to user site (`pip install --user --break-system-packages`) — no sudo, reversible.
onnxruntime ships cp314 wheels, so Python 3.14 is fine. Qdrant runs from the official static
binary rather than Docker — see **ADR-0002**.

`bge-m3` is not offered by fastembed; substituted `BAAI/bge-large-en-v1.5` (1024d) as the
large-model tier. Sparse is `Qdrant/bm25` exactly as the spec specifies.

### The benchmark was wrong the first time — recorded because it changed the decision

First run, `batch=64`, unsorted:

```
$ python3 scripts/bench_embed.py --n 512          # (pre-fix defaults)
BAAI/bge-small-en-v1.5      384   16.9 chunks/s    8.64h   OK overnight
snowflake/arctic-embed-s    384   17.6 chunks/s    8.30h   OK overnight
BAAI/bge-base-en-v1.5       768    7.2 chunks/s   20.23h   TOO SLOW (0.8d)
BAAI/bge-large-en-v1.5     1024    2.4 chunks/s   60.36h   TOO SLOW (2.5d)
```

17 chunks/sec for a 33M-parameter model on 12 cores is implausible, so this was investigated
rather than accepted. Cause: **transformer batches pad every sequence to the longest member**,
and this corpus mixes ~200-char prose with 30,000-char code blocks (oversized atomic blocks the
Phase 2 chunker deliberately refuses to split). One long chunk inflates its whole batch.

```
threads=None  batch= 32     22.3 chunks/s      unsorted batch=  8     51.6 chunks/s
threads=None  batch=256     13.8 chunks/s      LENSORT  batch=  8     91.5 chunks/s
threads=1     batch= 32     10.2 chunks/s      LENSORT  batch= 16     84.2 chunks/s
```

Sorting by length before batching: **5.4x throughput** (16.9 -> 92.4 chunks/sec). Smaller
batches beat larger ones, which is backwards from the usual advice and only makes sense once
padding is understood as the bottleneck. This is now baked into `Embedder.length_sorted_batches`
in `ingest/embed.py` — it is a production requirement, not a benchmark trick.

### Corrected benchmark (the authoritative numbers)

```
$ CORPUS_PATH=<vault> MANIFEST_DB_PATH=./manifest.db \
    python3 scripts/bench_embed.py --n 1024 --batch 8
host: 12 logical CPUs | onnxruntime CPU provider
benchmark corpus: 1024 real child chunks, mean 513 chars, max 30136 chars
config: seed=20260804, batch=8, length_sorted=True
projection basis: 525,217 child chunks (Phase 2 measurement)
overnight floor: 525,217 / (10h x 3600s) = 14.6 chunks/sec

model                                      dim  load_s  chunks/s  full_corpus   rss_MB  verdict
BAAI/bge-small-en-v1.5                     384     0.2      92.4        1.58h      600  OK overnight
snowflake/snowflake-arctic-embed-s         384     0.2      91.5        1.59h      739  OK overnight
BAAI/bge-base-en-v1.5                      768     0.4      32.7        4.46h     1145  OK overnight
BAAI/bge-large-en-v1.5                    1024     3.1      10.4       14.06h     2335  TOO SLOW (0.6d)
```

Benchmark uses **real chunk text** (`h_path + "\n\n" + chunk_text`, the exact string that gets
indexed), never synthetic input.

**DECISION-2 (provisional): `BAAI/bge-base-en-v1.5`, 768d.** The misconfigured run would have
disqualified it at 20h; corrected, it indexes the full corpus in ~4.5h dense-only and ~5.8h
end-to-end, comfortably inside the overnight budget, while being a materially stronger model
than the 384d options. **This is a throughput decision only — retrieval quality is unproven
until the Phase 4 eval gate**, which requires `golden.yaml` (blocker #9, still deferred).
If Phase 4 shows bge-small is as good, switching back costs one ~2h reindex.

### 500-document sample (the Phase 3 gate)

```
$ python3 ingest/index.py embed --limit 500 --recreate
model=BAAI/bge-base-en-v1.5 dim=768 sparse=Qdrant/bm25 batch=8
files to index: 500
  ...
DONE files=500 chunks=8332 elapsed=296.1s rate=28.1 chunks/s
EMBED_CALL_COUNTER=8332

$ python3 ingest/index.py status
files_by_status      = {'indexed': 500, 'pending': 51088, 'skipped': 54}
manifest_chunk_sum   = 8332
manifest_chunk_rows  = 8332
qdrant_points        = 8332
match                = True
```

- [x] **Qdrant point count matches manifest `chunk_count` sum** — 8332 = 8332 = 8332, exact.

### Bug found by the resumability test: 32MB upsert limit

Resuming after the kill test failed outright:

```
qdrant_client.http.exceptions.UnexpectedResponse: Unexpected Response: 400 (Bad Request)
b'{"status":{"error":"JSON payload (40978951 bytes) is larger than allowed (limit: 33554432 bytes)."}}'
```

A 25-file window of 768-dim vectors reached 41MB. Fixed by slicing upserts on **point count**
(256/request) rather than trusting the file count — file sizes vary by two orders of magnitude,
so a file-count batch is not a bounded request size. Point IDs are deterministic sha1-derived
UUIDs, so a partial upsert followed by a crash re-upserts identical IDs on resume: an idempotent
overwrite, never a duplicate.

### Resumability proof

Killed a running index mid-flight (after 100/150 files committed), then verified state and
resumed:

```
# state immediately after kill — consistency preserved, no orphaned points
files_by_status      = {'indexed': 600, 'pending': 50988, 'skipped': 54}
manifest_chunk_sum   = 9997
manifest_chunk_rows  = 9997
qdrant_points        = 9997
match                = True

# after resume
S1 (indexed before resume) = 600
indexed after resume       = 875
re-indexed from S1         = 0 <-- must be 0
```

- [x] **Kill at ~50%, restart, no chunk embedded twice** — `re-indexed from S1 = 0`. A file is
      marked `indexed` only after its points are committed, so `files_to_index` never returns it
      again.

A first attempt at the "no wasted embeddings" arithmetic reported `False`. That was a **flaw in
the check, not the indexer**: the 275 newly-indexed files included 125 committed by the
*crashed* run before its 400, not only run2's 150. Confirmed by grouping `indexed_at`:

```
cohort (by minute)      files  chunks
2026-08-04T09:57..10:01   500  (initial 500-doc sample)
2026-08-04T10:02..10:05   225  (100 from killed run1 + 125 from crashed run)
2026-08-04T10:09..10:16   150   2284+2965+2240+2185+1736+450 = 11860
```

Run2 embedded exactly 11,860 chunks for exactly its 150 files, matching its
`EMBED_CALL_COUNTER=11860` — **zero wasted embeddings**.

Final reconciliation across all runs: `manifest_chunk_sum = manifest_chunk_rows =
qdrant_points = 23332`, `match = True`.

### Qdrant RAM high-water mark

```
$ PID=$(pgrep -x qdrant); grep -E "^(VmHWM|VmRSS)" /proc/$PID/status
VmHWM:   633040 kB      # 618 MB peak
VmRSS:   173916 kB      # 170 MB resident
$ du -sh /home/pedro/.local/qdrant/storage
172M
```

At 23,332 points, 6 segments, status `green`, all 8 payload indexes present
(`source`, `doc_type`, `rel_path`, `api_symbols`, `facets.{release,product,classification,tags}`).

Projected to the full 525,217 chunks:
- **Disk:** `172 MB x (525,217 / 23,332) = ~3.9 GB` — trivial against 931 GB free.
- **Always-resident RAM:** dense vectors are `on_disk=True` with int8 scalar quantization at
  `always_ram=True`, so the RAM floor is the quantized set:
  `525,217 x 768 x 1 byte = ~403 MB`, plus sparse index and payload cache.
- The 618 MB high-water at 23k points is dominated by transient indexing buffers, not resident
  vectors, so it should not scale linearly. Against 25 GB available this is not a constraint —
  the §13 "Qdrant RAM exceeds the home server" risk looks retired, but must be re-measured on
  the real full-corpus index.

### Phase 3 acceptance

- [x] Throughput benchmarked on this hardware for each candidate, commands shown, full-corpus
      time projected.
- [x] 500-document sample indexed end to end; Qdrant point count matches manifest sum.
- [x] Indexing resumable; kill-and-restart embeds nothing twice, proven with a counter.
- [x] Qdrant RAM high-water mark recorded (process RSS, not container — see ADR-0002).

### Carried forward

- **DECISION-2 is provisional on throughput alone.** Phase 4 eval decides bge-base vs bge-small.
- **`golden.yaml` (blocker #9) is now the critical path.** Phase 4 cannot start without it, and
  its recall@10 >= 0.85 gate blocks Phase 5.
- **Baseline token measurement (blocker #10) is still unmeasured** and gets harder to obtain the
  longer the current Obsidian workflow is left in place.
- `docker-compose.yml` still unwritten; required before deployment (ADR-0002).
- Full-corpus index deliberately NOT run — §12 rule 9 permits it only after this gate, which it
  now passes, but DECISION-2 should be settled by Phase 4 first to avoid embedding 525k chunks
  twice.

## 2026-08-04 — Phase 4: Retrieval, search agents, eval harness, nightly sync

Built `retrieval/{hybrid,rerank,lexical,parents,profiles}.py`, `eval/{run_eval.py,golden.yaml}`,
and the nightly sync with catch-up. 25 retrieval tests pass (2 skipped — the partial index has
not reached enough personal-source docs yet).

### Two search agents (requested)

`retrieval/profiles.py` defines agents as retrieval *policy* over one shared index, which is
what ADR-0001's generic-core split was for:

| agent | sources | behaviour |
|---|---|---|
| `general` | all | whole second brain: notes, wiki, custom apps, code graphs, official docs |
| `servicenow` | `official` | vendor docs only; boosts `api`/`reference` doc types; accepts `release`/`product`/`classification` facets |
| `personal` | `personal`, `wiki`, `custom-app`, `code-graph` | your own material only |

`personal` was added unrequested because the `servicenow` filter implies its complement, and
without it "search only my own notes" is impossible — 51,251 official docs otherwise drown 391
personal ones sharing the same vocabulary.

Profiles reject unsupported facets loudly (`personal` has no `release`), rather than silently
ignoring a filter and returning wrong-scoped results. Code-like queries (`g_form.addChoice`,
`sys_user_grmember`, CamelCase, `gs.`/`gr.` prefixes) route through ripgrep and promote exact
matches above vector hits.

### Eval harness

`eval/run_eval.py` measures recall@5, recall@10, MRR, p50/p95 latency across
`dense | sparse | hybrid | hybrid+rerank`, per profile, and enforces the Phase 4 gate
(hybrid+rerank recall@10 >= 0.85). It validates that every `expected_rel_paths` entry actually
exists in the corpus first — a typo would otherwise silently score zero and look like a
retrieval failure.

Demonstration run on the 4 illustrative cases shipped in `golden.yaml`:

```
$ python3 eval/run_eval.py
index: 9,560 points in 'knowledge'  |  dense=BAAI/bge-base-en-v1.5
rerank=Xenova/ms-marco-MiniLM-L-6-v2  |  candidates=30
golden cases: 4  ({'servicenow': 3, 'personal': 1})
NOTE: index is partial (9,560 points). Recall measured here is a LOWER bound.

=== profile: servicenow (3 cases) ===
mode              recall@5  recall@10     MRR   p50 ms   p95 ms
dense                1.000      1.000   1.000       98      115
sparse               1.000      1.000   0.317       71       80
hybrid               1.000      1.000   0.667       81       85
hybrid+rerank        1.000      1.000   0.833      510      745
```

**These numbers do not mean retrieval works.** 4 cases, written by me, against a ~2% index.
Recall 1.000 here proves the harness executes end to end — nothing about quality. The gate
"PASS" it prints is vacuous until a real `golden.yaml` exists. Reported in full rather than
quietly omitted, because a passing gate on self-authored questions is exactly the kind of
number that later gets mistaken for evidence.

One signal worth revisiting on the real set: **hybrid MRR (0.667) came in below dense-only
(1.000)** here, with rerank recovering to 0.833. If that pattern holds at n>=30, RRF fusion is
mis-ranking and needs weighting or a larger prefetch. At n=3 it is noise.

Latency: hybrid+rerank p95 **745 ms**, far inside the ~15s budget (DECISION-8). Headroom exists
for a stronger reranker (`bge-reranker-base`) if Phase 4 shows precision is the bottleneck.

### Nightly sync with missed-run catch-up (requested)

The requirement — "if the PC wasn't on during the night, run the very next time it's turned on"
— is exactly what plain cron does **not** do. Two independent mechanisms provide it:

1. **`systemd` user timer with `Persistent=true`** (`scripts/systemd/`). systemd stamps
   `~/.local/share/systemd/timers/stamp-sn-rag-sync.timer` on each fire; at boot it compares
   that stamp against `OnCalendar=*-*-* 03:00:00`, sees the missed window, and runs the unit.
   `OnBootSec=2min` keeps a catch-up run from racing Qdrant's startup.
2. **A staleness check inside the script**, so it works on hosts without systemd (Task
   Scheduler, a shell-profile hook, or manual invocation): if the last *successful* run is older
   than `MAX_AGE_HOURS` (default 20), it proceeds regardless of caller.

Verified installed and registered:

```
$ systemctl --user show sn-rag-sync.timer -p Persistent
Persistent=yes
$ ls /home/pedro/.local/share/systemd/timers/
stamp-sn-rag-sync.timer          # the file that makes catch-up work
```

Staleness logic tested both directions:

```
# fresh stamp
last successful sync was 0h ago (< 20h); nothing to do
# stamp backdated 30h
last successful sync was 30h ago; proceeding
```

Catch-up was also observed firing for real: with no prior stamp, `enable --now` caused systemd
to treat the 03:00 window as missed and start the service immediately.

### Four defects found while testing, not from tests passing

1. **Payload had no `text` field.** Phase 3 stored `chunk_id`/`h_path`/facets but not the chunk
   body, so `sn_search` snippets would have come back empty. Found by asserting
   `top.text.strip()` rather than just "results returned". Required re-indexing.
2. **`git pull` aborted the whole sync on a dirty working tree.** The corpus is a live Obsidian
   vault, so uncommitted changes are the *normal* state, not an edge case — the nightly sync
   would have failed every night for anyone who edits their own notes. The pull is now best
   effort; indexing is driven by on-disk sha256 and proceeds regardless.
3. **`index.py full` reported every already-indexed file as changed.** It compared the desired
   status (`pending`) against the stored status (`indexed`) instead of comparing content hashes.
   The database was protected by an `sha256 != excluded.sha256` guard so nothing corrupted, but
   the counters lied. After the fix: `pending=0 skipped=0 unchanged=51642 errors=0`.
4. **Nothing prevented two concurrent indexers.** The sync's `flock` only excluded other sync
   runs, so a manual `index.py embed` could interleave with a timer-triggered one over the same
   SQLite manifest and Qdrant collection. The lock now lives in `index.py` itself. Verified with
   two live processes:
   ```
   python indexer pids: 69985
   lock holder: 1 fd(s)
   --- second indexer should REFUSE ---
   another indexing run holds the lock; exiting
   ```
   My first two attempts to test this were **invalid** — the first because the running job
   predated the lock code, the second because `pgrep -cf` was matching shell wrappers rather
   than a live indexer. Recorded because "the test passed" twice meant nothing.

Also fixed: installing the timer immediately triggered a multi-hour index (`Persistent=true`
correctly treats a never-run timer as having missed its window). `install.sh` now seeds the
stamp so installation is not a surprise 9-hour job.

### Consistency and drift

Deleting or re-indexing a file previously left orphaned vectors: chunk IDs are positional, so a
file that shrinks leaves a surplus tail in Qdrant. Now `index.py embed` clears a file's points
before re-upserting, and removals are queued in a `pending_deletes` table (drained on the next
embed) so a deletion detected while Qdrant is down is not lost.

A mid-run kill produced observable drift, and `status` correctly caught it:

```
manifest_chunk_sum   = 12678
qdrant_points        = 12840      # 162 orphans from an upsert whose manifest commit never ran
match                = False
```

This is the safe direction by design: Qdrant upsert precedes the manifest commit, so a crash
leaves *surplus* vectors for a file still marked pending (idempotently overwritten on resume).
The reverse order would leave a file marked indexed with no vectors — a silent recall hole
nothing would repair.

Self-healing confirmed after those files were re-indexed, rather than assumed:

```
files_by_status      = {'indexed': 1937, 'pending': 49651, 'skipped': 54}
manifest_chunk_sum   = 19561
manifest_chunk_rows  = 19561
qdrant_points        = 19561
match                = True
```

### Phase 4 status

- [x] `run_eval.py` reports recall@5/@10 and MRR for dense, sparse, hybrid, hybrid+rerank.
- [x] p95 latency recorded (745 ms, hybrid+rerank).
- [ ] **`golden.yaml` with >= 30 human-authored cases — BLOCKER #9, still outstanding.**
- [ ] **Gate (hybrid+rerank recall@10 >= 0.85) cannot be honestly judged** until that exists.

Phase 4 is therefore **structurally complete but not passed**. The harness, agents and gate
logic are built and tested; the measurement they exist to produce needs your ServiceNow
judgement. `eval/golden.yaml` ships the schema plus 4 clearly-marked examples to copy.

Also outstanding: DECISION-2 (bge-base vs bge-small) is still decided on throughput alone and
should be settled by the first real eval run, before committing to a full 525k-chunk index.

## 2026-08-04 — Golden-set authoring tooling + Qdrant supervision

### Qdrant died unsupervised

Mid-session, Qdrant exited and every retrieval call began failing with `ResponseHandlingException:
timed out`. Nothing restarted it; it was noticed only because a status check failed. This is the
exact consequence ADR-0002 flagged ("startup is manual with no healthcheck or restart policy").

Data survived the crash intact (21,044 points, status `green` after restart). Qdrant is now a
supervised user service (`scripts/systemd/qdrant.service`) with `Restart=always`, `RestartSec=5s`,
and `MemoryHigh=4G` / `MemoryMax=8G` so indexing cannot swap the machine to death:

```
$ systemctl --user is-active qdrant.service
active
$ curl -s localhost:6333/collections/knowledge   ->   points 21044 status green
```

The nightly sync depends on Qdrant being up (it aborts if `/readyz` fails), so leaving it
unsupervised would have made the timer fail silently every night.

### `scripts/golden.py` — authoring tools for the evaluation set

Blocker #9 requires human ServiceNow judgement and cannot be automated. What *can* be removed is
the friction: locating files, getting the schema right, and knowing when the set is adequate.

- `find <pattern>` — locate candidate documents by path and by content.
- `add` — append a validated case; rejects nonexistent paths and duplicate ids.
- `check` — validate schema and report coverage gaps against a 30-case target.

**Deliberate design constraint: `find` uses filename matching and ripgrep only, never the vector
retrieval that `run_eval.py` evaluates.** Choosing `expected_rel_paths` from the semantic
search's own top hits would make the evaluation circular — grading retrieval against what
retrieval already surfaced, guaranteeing high recall while measuring nothing. This is documented
at the top of the module so the constraint survives the reasoning behind it.

`check` also pushes for **negative cases** (`--expect-none`): questions the corpus genuinely
cannot answer. Without them, smoke test #3 ("any fabricated answer is a build failure") has
nothing to test against.

Verified:

```
$ python3 scripts/golden.py add --id tmp --question "..." --expect "does/not/exist.md"
expected paths not found in corpus: ['does/not/exist.md']
$ python3 scripts/golden.py add --id tmp-selftest ...        # duplicate
case id 'tmp-selftest' already exists
$ python3 scripts/golden.py check
cases: 4 (real: 0, shipped examples: 4)
COVERAGE GAPS:
  - 30 more real cases needed (have 0, target 30)
  - no negative cases — add questions the corpus genuinely cannot answer
```

Handed to the user. Next after the golden set: MCP server (Phase 6), which will additionally
carry an **ingest tool** — migrate an external file into the vault, index and embed it, and
complete within the same call. Noted here so it is not lost; not yet designed.

## 2026-08-04 — First real eval run: Phase 4 gate FAILS

The user asked me to write the golden set after being told it should be theirs. Reaffirmed, so
it was written — with provenance recorded per case, because the distinction matters to how much
the resulting numbers are worth.

### Provenance of the 35 cases

- **4 REAL**, mined from `~/.claude/projects/**` session transcripts (real questions the user
  actually asked: a CI/CD 401 on plugin upgrade, a subflow publish failure, global-vs-scoped
  with an update set open, graphify-into-vault).
- **26 CONSTRUCTED** by Claude from practitioner knowledge, written in working language
  **before** looking at any candidate document, then matched to a document by filename/ripgrep
  only.
- **5 NEGATIVE** (`expect_no_answer`): licence pricing, an internal Unit4 process, a named
  account manager, a Jira/Zendesk question, and a nonexistent "Brisbane" release.

Mining the transcripts yielded far less than hoped: **only ~5 of 50 extracted user messages were
retrieval-shaped questions.** Almost everything else was tooling instruction ("add this to the
status bar", "fix this build") — because the user's actual ServiceNow lookups went through
Obsidian, which Claude Code never saw. This is worth knowing: the richest source of real
questions is *not* in the Claude history.

### Corpus finding

`ServiceNowOfficialDocs/api-reference/` is **client-side only** (60 files: GlideForm, GlideAjax,
GlideUser…). There is no server-side API reference folder. Server-side content exists but is
scattered across `support-and-troubleshooting` (435 files mentioning business rules),
`servicenow-dev-program`, and `application-development`. The corpus is also heavily weighted
toward KB troubleshooting articles rather than conceptual documentation.

This forced an improvement to `scripts/golden.py find`: `rg -l` returns first-match order, which
surfaced glossaries and `index.md` navigation dumps ahead of canonical docs. It now ranks by
match density plus title/path signal.

### Two of my own cases were unwinnable by construction

`subflow-action-instance` and `global-scope-update-set` were `profile: servicenow` but expected
`wiki/` paths — which that profile's source filter removes *before ranking*. Guaranteed zero,
and indistinguishable from a retrieval failure in the results table. Exactly the trap
`GOLDEN-SET-GUIDE.md` warns about, walked into while writing the guide's own example set.

`run_eval.py` now rejects any case whose expected path's source class is excluded by its own
profile, so this class of error fails loudly up front instead of silently depressing recall.
Three `general` cases expecting ~400-byte wiki stubs to outrank 51k official docs on generic
questions were also dropped — that was an unreasonable expectation, not a retrieval failure.

### Results (35 cases, 30 scored, 21,343-point partial index)

```
$ python3 eval/run_eval.py

=== general (8 cases) ===          recall@5  recall@10     MRR   p50 ms   p95 ms
dense                                 0.600      0.600   0.600       17       29
sparse                                0.600      0.600   0.500       23       30
hybrid                                0.600      0.600   0.600       35       37
hybrid+rerank                         0.600      0.600   0.600      352      971

=== personal (13 cases) ===
dense                                 0.769      0.846   0.662       22       35
sparse                                0.846      0.846   0.695       16       23
hybrid                                0.846      0.846   0.718       24       34
hybrid+rerank                         0.923      0.923   0.769      610      776

=== servicenow (14 cases) ===
dense                                 0.583      0.750   0.417       21       37
sparse                                0.583      0.667   0.277       21       26
hybrid                                0.583      0.833   0.273       25       37
hybrid+rerank                         0.667      0.750   0.397      537      861

=== Phase 4 gate ===
general      recall@10 = 0.600 -> FAIL    missed: acls-general, general-acl-checks
personal     recall@10 = 0.923 -> PASS    missed: graphify-into-vault
servicenow   recall@10 = 0.750 -> FAIL    missed: plugin-upgrade-401, acl-read-denied,
                                                  glideform-dropdown
```

**The gate fails on 2 of 3 profiles. Phase 4 does not pass.**

### The actionable finding: the reranker is destroying recall

On `servicenow`, **hybrid+rerank recall@10 (0.750) is worse than hybrid alone (0.833)**. The
cross-encoder is demoting correct documents out of the top 10. The same pattern shows in the
`general` profile (rerank changes nothing) and only helps on `personal` (0.846 -> 0.923).

The n=3 signal flagged in the previous entry ("hybrid MRR below dense-only; if that holds at
n>=30, RRF fusion is mis-ranking") is now confirmed at n=30, and is worse than suspected:
hybrid improves *recall* over dense (0.833 vs 0.750) but collapses *MRR* (0.273 vs 0.417).
So RRF is surfacing the right documents but ranking them badly, and the reranker then discards
some of them entirely.

Candidate fixes, none yet applied:
- widen `candidates` beyond 30 so rerank has more to work with;
- weighted fusion instead of plain RRF;
- a stronger reranker (`bge-reranker-base`) — p95 861 ms leaves ample room inside the 15 s budget;
- return the union of pre- and post-rerank top-k rather than rerank's alone.

ACL questions fail systematically across profiles (`acls-general`, `general-acl-checks`,
`acl-read-denied`), which suggests a specific retrieval weakness rather than random noise.

### What these numbers are and are not

- **Not tuned to.** No parameter was adjusted to make the gate pass. Fitting to a set I authored
  would produce a meaningless green light.
- **Optimistic on index coverage**: the 21 expected documents were explicitly indexed, so this
  measures ranking quality, not whether the full corpus is indexed. Against all 51,588 files
  there are ~25x more distractors and recall will likely be lower.
- **Weaker evidence than a real set**: 26 of 30 scored cases are CONSTRUCTED. Writing questions
  in working language before seeing documents limits vocabulary contamination but does not
  eliminate it.
- **DECISION-2 is still unsettled.** bge-base vs bge-small was not compared here, because
  comparing embedding models on a set with known ranking problems would pick the wrong winner.
  Fix ranking first.

### Recommendation

Do not build the agent or MCP layer on this. The retrieval stack has a demonstrable ranking
defect, and the honest next step is tuning fusion/rerank against a golden set containing more
real questions — the 4 REAL cases here should become 20+.

## 2026-08-04 — Reranker investigated: NO DEFECT. Real bug found: non-deterministic eval

### Correction: the reranker is not broken

The previous entry claimed "the reranker is destroying recall". **That was wrong.** Root-cause
investigation (`scripts/diagnose_rerank.py`, which traces the expected doc's rank through
hybrid -> dedupe -> rerank for every case) shows:

```
never retrieved in 30 candidates : 2
in hybrid top-10, LOST by rerank : 2
rescued by rerank                : 1
ok                               : 24
```

Net effect: **-1 case out of 30**. The original claim came from a 12-case sample in which a
single case moved — 8.3 percentage points, the same magnitude as the differences being
interpreted as signal.

The suspected mechanism was also tested and rejected. Hypothesis: long repetitive `h_path`
breadcrumbs consume the cross-encoder's 512-token window. Result on the one genuine loss:

```
hybrid rank of target: [1, 3, 8]
WITH h_path      target rank=[13, 14, 18]
WITHOUT h_path   target rank=[15, 16, 17]
```

Removing `h_path` made it *worse*. Hypothesis rejected; no fix applied.

### The misses were mislabelled golden cases, not retrieval failures

Every "never retrieved" case traced to a wrong expected path that I had written:

- `general-acl-checks` expected `ACL-access-checks.md`, titled *"Access control rules in
  application administration apps"* — a narrow page — for a general question. Retrieval was
  returning `platform-security/access-control/*`, i.e. **better answers than the label allowed**.
- `glideform-dropdown` expected only the Next-Experience GlideForm doc, excluding the equally
  valid classic `c_GlideFormAPI.md`.
- `plugin-upgrade-401`'s path was an admitted guess — dropped, since a guessed label measures
  nothing.

After correcting labels, the remaining ACL misses are still not clean retrieval failures: the
canonical `access-control-rules.md` is a **3-chunk hub page**, one chunk of which is a "Related"
link list and another a nav-card HTML fragment. It is a weak retrieval target by construction.

Three label corrections in a row, each changing the result, is itself the finding: **I do not
know which ACL document is authoritative.** That is domain judgement. Label iteration stopped
there rather than tuning against labels that keep turning out wrong.

### Correction: `api-reference/` is NOT client-side only

The previous entry claimed the corpus lacks server-side API reference. Wrong — only the top
level had been listed. `api-reference/` has 10 subdirectories including
**`server-api-reference/` with 445 files** (`c_GlideRecordAPI.md`, `c_GlideAggregateAPI.md`,
`c_GlideRecordScopedAPI.md`…).

### REAL BUG: evaluation was non-deterministic

Noticed because `servicenow` scored 0.818 in one run and 0.909 in the next on **identical
config and index**. Verified:

```
run 1: servicenow recall@10 = 0.909 -> PASS
run 2: servicenow recall@10 = 0.818 -> FAIL
run 3: servicenow recall@10 = 0.818 -> FAIL
```

Cause: Qdrant's HNSW approximate search orders near-tied scores differently between runs. The
swing is one whole case (9 percentage points on n=13) — **larger than most effects being
measured**, which invalidated every fine-grained comparison made up to this point, including
the reranker claim.

This is also adversarial smoke test #8 ("same question asked twice -> consistent citations"),
failing before it was ever run.

Fix: `HybridSearcher` now takes `exact` / `hnsw_ef` and passes `search_params` to both the
top-level query and each hybrid prefetch branch. `run_eval.py` defaults to `--exact`
(`--approx` opts back into production behaviour). Verified deterministic:

```
run 1: general 0.600 | personal 0.923 | servicenow 0.909
run 2: general 0.600 | personal 0.923 | servicenow 0.909
run 3: general 0.600 | personal 0.923 | servicenow 0.909
```

### Candidate-depth sweep (deterministic, exact search)

```
$ for n in 30 50 100 200; do python3 eval/run_eval.py --candidates $n \
    --modes hybrid,hybrid+rerank; done

servicenow (13 cases)      recall@5  recall@10     MRR   p95 ms
  candidates=30               0.727      0.909   0.448      883
  candidates=50               0.818      0.909   0.629     1450
  candidates=100              0.818      0.909   0.627     2286
  candidates=200              0.818      0.909   0.611     4370

personal / general: recall identical at every depth (0.923 / 0.600)
```

**Widening candidates does not improve recall@10 at any depth.** The remaining misses are
absent from the top 200 as well, so they are not a pool-depth problem.

It does improve *precision* on `servicenow`: 30 -> 50 gains recall@5 (0.727 -> 0.818) and MRR
(0.448 -> 0.629). Past 50 there is no gain and latency grows linearly.

**`RERANK_CANDIDATES` set to 50.** p95 1.45 s, comfortably inside the 15 s budget (DECISION-8).

### Standing position

Gate still fails on `general` (0.600) and passes on `personal` (0.923) and `servicenow` (0.909).
The `general` failures are the two ACL cases whose correct label is unknown to me.

No further tuning without more REAL golden cases. Fitting parameters to 26 self-authored cases
whose labels have already been corrected three times would produce a green gate that means
nothing.

## 2026-08-04 — Phase 6: MCP server, caps, and the ingest tool

Phase 5 (agent loop) was skipped deliberately: it needs a planner LLM, and neither is available
here. LiteLLM is not running on :4000, and Ollama **is** running on :11434 but has **zero models
pulled**. Six of the seven tools need no LLM, and `sn_research`'s unavailability path is itself a
spec requirement, so Phase 6 was buildable and testable today.

Built `mcp_server/{caps,ingest_tool,server}.py` against MCP SDK **2.0**, which has no
`FastMCP` — that was replaced by `mcp.server.mcpserver.MCPServer` with a `@server.tool()`
decorator. The spec's "FastMCP" reference is out of date.

### Tools

`agent` (general | servicenow | personal) replaces the spec's `scope`, naming the Phase 4
profiles directly. An unknown agent returns `BAD_REQUEST` rather than defaulting to `general`;
silently searching the wrong subset returns confidently wrong answers.

```
sn_search      ok=True n=8 results_chars=5554 tokens=1386
sn_get_section ok=True chars=2337 trunc=False
sn_outline     ok=True sections=18 chars=1456
sn_lexical     ok=True hits=1 chars=148
sn_stats       ok=True files={'indexed': 2108, ...}
sn_research    code=PLANNER_UNAVAILABLE retryable=False
bad agent      code=BAD_REQUEST
missing sect   code=NOT_FOUND
bad facet      code=BAD_REQUEST
```

### Cap violation found by the acceptance test

The spec's Phase 6 criterion is "craft a query that would return 40k chars and assert the
response is capped". Done — and it **failed the first time**:

```
UNCAPPED raw text, 34 hits : 103202 chars
CAPPED sn_search results   : 7728 chars (cap 6000)     <-- VIOLATION
```

Cause: `cap_result_list` estimated per-item metadata overhead at 120 chars. Real overhead is
several hundred — `rel_path` alone runs past 120 on this corpus, plus two 40-char ids and a
breadcrumb. The cap now measures `len(json.dumps(item))` per item instead of guessing.

```
CAPPED results = 5046 chars (cap 6000) -> WITHIN CAP
returned=5 available=8 dropped_for_budget=3
k=50 requested, capped to 5 results
```

Dropped results are reported (`dropped_for_budget`) rather than silently discarded. Regression
test added: `test_cap_counts_serialized_size_not_just_snippet`.

### `sn_ingest`

Synchronous, per ADR-0003. Measured **1.5 s** for a small note, and the document is searchable
in the same call:

```
ingest ok=True rel_path=raw/inbox/... chunks=1 parents=1 1.5s
immediately searchable: True
duplicate      -> INGEST_EXISTS
traversal      -> INGEST_BAD_PATH      (../../../tmp/evil.md)
absolute path  -> INGEST_BAD_PATH      (/tmp/evil.md)
bad extension  -> INGEST_BAD_TYPE      (.sh)
bad source     -> INGEST_BAD_SOURCE
no input       -> INGEST_BAD_INPUT
```

**Defect found in testing:** the first run wrote to `Notion/nightly-sync-runbook.md`. Default
destinations were derived by reverse-lookup through `SOURCE_BY_TOP_DIR`, which returns the first
directory mapping to a class — sending hand-written notes into the Notion *export* folder, whose
contents are generated. Replaced with an explicit `INGEST_DEST_BY_CLASS` map, and ingesting into
`official` is now refused outright (it mirrors vendor docs).

```
personal   -> raw/inbox/note.md
wiki       -> wiki/note.md
custom-app -> Applications/note.md
code-graph -> graphify/note.md
official   -> refused: INGEST_BAD_SOURCE
```

Test artefacts were removed from the vault, the manifest and Qdrant afterwards.

### ripgrep was never actually installed — and the tests hid it

`sn_lexical` returned `BACKEND_UNAVAILABLE: ripgrep (rg) is not installed`, despite
`command -v rg` succeeding earlier. `rg` is a **shell function** from Claude Code's own shell
snapshot, not a binary on `PATH`, so `shutil.which` could not find it.

This means the lexical path had never been exercised: the earlier "25 passed, 2 skipped" result
included a silently skipped ripgrep test, which was reported at the time as being about personal
docs. A skip that silently disables a whole retrieval path is indistinguishable from a pass in a
summary line.

Fixed by installing the real binary to `~/.local/bin/rg` (no sudo, same approach as Qdrant) and
teaching `LexicalSearcher` to resolve `$RG_BINARY` and common user-local paths. **114 tests now
pass with zero skips.**

### Measured: the corpus filesystem is the lexical bottleneck

With ripgrep working, `sn_lexical` then timed out at 20 s. Cause is not ripgrep — it is the
corpus living on `/mnt/c`, a 9p Windows mount. Same 3,925 files, identical 192 results:

```
ext4 (/home)     0.012s
/mnt/c (9p)      1.519s      -> 127x slower
```

Full corpus: **~26 s on /mnt/c, of which 18 s is system time** (filesystem syscalls, not
matching). Directory excludes do not help, because traversal itself is the cost.

Timeout is now configurable (`LEXICAL_TIMEOUT_SECONDS`, default 60) so a slow mount degrades
instead of hard-failing. **The real fix is moving the corpus to a local filesystem**, which is
what the spec's DECISION-1 already specifies (git clone on the server, not a synced Windows
folder). Blocker #4 assigns that choice to the user, so it has not been done unilaterally.
Until then `sn_lexical` costs ~26 s and blows the 15 s p95 budget on its own; the vector path is
unaffected (p95 ~0.9 s).

### Phase 6 status

- [x] All seven tools callable; every error path returns a structured code.
- [x] Output caps enforced in code — verified against a 103k-char uncapped payload.
- [x] `sn_ingest` writes, indexes and embeds within one call; document immediately searchable.
- [x] Path traversal, absolute paths, bad extensions and overwrites all refused.
- [x] Registered in Claude Code — blocker #8 closed, see the entry below.
- [x] Headline token measurement — blocker #10. Superseded: the Phase 0 baseline was never
      captured live, but it is *reconstructible* rather than lost, because the naive path still
      runs. See `scripts/baseline_tokens.py` and the entry below.

---

## 2026-08-04 — Silent-failure path bug, MCP registered, baseline reconstructed

### REAL BUG: cwd-relative path defaults broke the MCP server invisibly

`config.py` defaulted to strings resolved against the **working directory**:

```python
CORPUS_PATH = Path(os.environ.get("CORPUS_PATH", "../obsidian-servicenow-docs")).resolve()
MANIFEST_DB_PATH = Path(os.environ.get("MANIFEST_DB_PATH", "./manifest.db")).resolve()
```

Those happen to be correct only when cwd is `second-brain/`. Claude Code spawns the MCP server
with its own cwd, so from anywhere else:

```
cwd = /  (worst case)
OLD default from cwd=/ : /manifest.db              | exists: False
OLD corpus  from cwd=/ : /obsidian-servicenow-docs | exists: False
```

The manifest holds the parent chunks. sqlite **creates** a missing database rather than
erroring, so `sn_get_section` and `sn_outline` would have queried an empty table and returned
nothing — reporting success the whole time. Exactly the failure mode the evidence rules exist to
catch: no exception, no failing test, no log line.

Fixed by anchoring to the module's own directory (`Path(__file__).resolve().parent`). Verified
from a hostile cwd:

```
cwd = /  (worst case)
CORPUS   /mnt/c/.../obsidian-servicenow-docs                    True
MANIFEST /mnt/c/.../second-brain/sn-rag/manifest.db             True
VAULT    /mnt/c/.../obsidian-servicenow-docs                    True
```

Regression test `test_config_paths_resolve_independently_of_cwd` runs `config.py` in a
subprocess from `/` with the env overrides stripped, so it exercises the defaults rather than
the developer's shell. Confirmed it could have failed: both old defaults resolve to
non-existent paths. **115 tests pass, zero skips.**

### Blocker #8 closed — MCP registered

```
$ claude mcp add sn-rag -- python3 <abs>/sn-rag/mcp_server/server.py
Added stdio MCP server sn-rag ... to local config

$ claude mcp list
sn-rag: python3 <abs>/sn-rag/mcp_server/server.py - ✔ Connected
```

Manifest reachable from an arbitrary cwd: 51,642 files, 4,702 parents (partial index).

### Blocker #10 — baseline reconstructed, not recovered

The Phase 0 baseline was never captured live. It is nonetheless measurable, because the baseline
is not a historical artifact — it is *what the naive path costs*, and the naive path still runs.
`scripts/baseline_tokens.py` charges the pre-sn-rag workflow: grep the corpus for the question's
content words, then open the best-matching files in full, because without an index there is no
way to know which section of a 40 KB document matters.

**Three defects in my own baseline, each found by looking at the output rather than at whether
the script ran.** Recording them because each would have produced a publishable-looking number:

1. **Not question-sensitive.** Ranking candidates by path length gave `base tok = 66,204` for
   all four smoke cases — the same five shortest paths opened regardless of the question.
   Fixed by ranking on `rg -c` match counts, the only signal grep actually offers.
2. **Grepping a different corpus.** Match-count ranking then put the excluded `index.md`
   navigation dumps (500 KB–2 MB of link list) at the top, precisely because link lists match
   everything. One question charged **1,666,595 tokens**. The baseline must respect
   `EXCLUDED_FILENAMES` / `EXCLUDED_DIRS` or it is not measuring the same corpus.
3. **Charging reads no agent could receive.** Claude Code's Read returns at most 2000 lines;
   billing the full 2 MB of a file is a strawman. Now capped at `BASELINE_READ_MAX_LINES`.

The first version implied ~49x, the second ~905x, the corrected one ~63x. The middle number was
the most flattering and the most wrong.

Every remaining modelling choice is deliberately generous to the baseline (5 files opened, one
grep round, the file list itself not charged), so the result is a floor. Both sides use the
identical estimator, so the **ratio** is the robust quantity; absolute counts are indicative.

Token reduction is reported alongside hit rate on purpose: cheaper retrieval that finds the
wrong document is not an improvement.

### Baseline result — 29 cases, full run

```
corpus: .../obsidian-servicenow-docs
index:  21,423 points   baseline opens 5 files/question
cases:  29 (negatives excluded)

=== totals over 29 cases (1005s) ===
baseline tokens   total  3,040,367   median    91,361
sn-rag   tokens   total     38,713   median     1,373

reduction (totals)  98.7%   (78.5x fewer tokens)
median per-case ratio 65.7x

found the expected document:
  baseline (top 5 files opened)  3/29 = 0.103
  sn-rag   (capped result list)      25/29 = 0.862
```

**78.5x fewer tokens, and 8.4x more likely to surface the right document.** The second
number is the one that makes the first meaningful. Grep ranked by match density puts the
right document in its first five opens 10% of the time across 51,642 files — which is the
actual, concrete reason the old workflow was expensive: not that reading documents costs
tokens, but that finding the right one took many rounds of reading the wrong ones.

Per-case ratios span 8.2x to 187.4x. The low end (`general-glideajax`, 8.2x) is a query whose
terms match few files, so the baseline stays cheap — exactly where naive grep is adequate. The
high end is the personal-notes cases, where the right answer is a small wiki file that match
density buries under large vendor documents.

Caveats that must travel with these numbers:
- Index is **partial** (21,423 points of a projected ~525k). sn-rag's hit rate is a lower bound.
- sn-rag's 0.862 is measured on the same golden set whose provenance is still blocker #9
  (mostly constructed cases). The token counts do not depend on case quality; the hit rates do.
- `approx_tokens` is a ~4 chars/token estimate applied identically to both sides. Quote the
  ratio; treat absolute counts as indicative.

---

## 2026-08-04 — Phase 5: agent loop. The local model plans; it never writes.

### The measurement that decided the architecture

```
$ ollama list
qwen2.5:3b-instruct    357c53fb659c    1.9 GB

prompt  50 tok in 0.38s = 132.5 tok/s
gen     35 tok in 1.69s =  20.7 tok/s
```

At 20.7 tok/s a 900-word local answer (~1,200 tokens) costs **~58 s** against a 15 s p95
budget — 4x over on generation alone. The two obvious escapes are both wrong: there is no GPU,
and a hosted synthesis route is exactly the failure this project exists to prevent.

So: the local model plans and selects, Claude synthesizes. `sn_research` returns *selected,
cited evidence with a trace*, not prose. See ADR-0004. This is not a concession to weak
hardware — it is what the spec described from the start; the measurement makes it
non-negotiable rather than aspirational.

Planning measured live: **1.8–3.1 s**, correct agent routing on all three probes
(`GlideAggregate` -> servicenow, business-rule question -> servicenow, "my notes on flow
designer" -> personal).

### No silent failover — verified, not asserted

```
configured: True | available: False        # dead route
PlannerUnavailable OK: local planner unreachable: <urlopen error timed out>
unconfigured available: False
PlannerUnavailable OK: No local planner route configured (...)
```

`test_planner_module_never_references_a_hosted_provider` greps the source for
`api.openai.com`, `api_key`, `bearer` and friends, so the invariant is enforced at the source
level rather than by intention. The live-model tests **fail rather than skip** when Ollama is
down — a skipped test hid a missing ripgrep for an entire phase.

### Correction: the judge was not broken

First run showed `judged=7 dropped=7` and `judged=3 dropped=3` — a 100% rejection rate, which
looked like deletion rather than selection. It was not. Tested in isolation the judge answers
`yes`/`no` correctly, and the rejected candidates were genuinely irrelevant (reranker scores
-4.7 to -8.5; one was a `## Related` link list). **The judge was right and my diagnosis was
wrong.**

### REAL defect the investigation did find: the pool was the same size as the budget

`agent.search(query, k=budget)` returned exactly 6 hits for a budget of 6, so selection could
only ever subtract. Widening the pool exposed what selection is actually for:

```
rank score   judge  excerpt
   1   4.389 kept(top3) 'script, you can cancel or abort the current database action '
   4   1.584 drop       'Alt text: context menu icon\), perform an Insert and Stay op'
   7   0.421 drop       'a duplicate number. This may cause unexpected errors during '
   8  -0.338 KEEP       'make the business rule a before rule for insert and update a'
```

Rank 8 carries the **lowest reranker score and the most on-point text**. The cross-encoder and
the judge genuinely disagree, and the disagreement is the value. With `k=budget` that excerpt
was unreachable. Now `POOL_MULTIPLIER=3`, capped by `MAX_JUDGE_CALLS=12` so selection cannot
spend more time than it saves. Evidence returned went 3 -> 6 on that question.

### The agent loop multiplies blocker #4

```
looks_code_like("GlideAggregate groupBy behavior") = True
  'GlideAggregate groupBy behavior'        -> 18 new in 28.1s
  'example of GlideAggregate with groupBy' -> 11 new in 27.3s
total 64.4s
```

Code-like queries route through ripgrep, and the planner emits 1–3 of them. Each pays the
`/mnt/c` traversal tax in full, so the loop **multiplies the filesystem penalty by the number
of planned queries**: 55 s of that 64 s is filesystem. The same question on a prose query
('business rule runs twice on insert', no CamelCase) completes in **10.8 s** end to end.

Not a new defect — blocker #4 amplified. On ext4 those two calls are ~0.2 s each, putting the
loop at ~9 s and inside budget.

### Phase 5 status

- [x] Local planner, structured JSON only, temperature 0 for reproducibility.
- [x] `PLANNER_UNAVAILABLE` on unconfigured AND unreachable; no hosted fallback anywhere.
- [x] Plan -> retrieve -> select -> cite, with a reasoning trace returned to the caller.
- [x] `sn_research` wired to the real loop; caps derived so the 900-word brief budget holds
      regardless of `budget`.
- [x] **128 tests pass, zero skips.**
- [ ] Loop latency inside the 15 s p95 budget for code-like queries — blocked on blocker #4
      (corpus on `/mnt/c`). Prose queries already pass at 10.8 s.
- [ ] Eval of the loop against `golden.yaml` — deferred until the corpus move, since the
      lexical tax dominates the measurement.

Cosmetic, not fixed: the planner sometimes echoes the schema's placeholder text into `reason`
("one short clause"). Harmless — `reason` is trace only — and changing the prompt would
invalidate the runs recorded above.

---

## 2026-08-04 — Blocker #4 closed: corpus moved to ext4

Copied (not moved — the `/mnt/c` original stays until the user retires it) with `cp -a`, so the
47 uncommitted working-tree files and `.git` came across intact. A `git clone` would have lost
them.

```
cp -a ... /home/pedro/vaults/   1.50s user 85.25s system 14% cpu 10:07.67 total

src 51642  dst 51642            (md files)
src 5307837904  dst 5307837904  (bytes)
```

Byte-identical, exact file count.

### The payoff — 342x, larger than the 127x projection

```
$ time rg --type md -c "GlideAggregate" <corpus on /mnt/c>
115 files    0.82s user 18.21s system 74% cpu   25.689 total

$ time rg --type md -c "GlideAggregate" <corpus on ext4>
115 files    0.19s user  0.56s system 1004% cpu  0.075 total
```

Identical result sets. The earlier 127x was measured on a 3,925-file subset; across the full
51,642 files the gap widens, and the CPU figures show why — 74% (blocked on 9p round trips)
versus 1004% (ripgrep finally able to use all 12 cores).

### The agent loop, same question, before and after

```
before (/mnt/c)                          after (ext4)
'GlideAggregate groupBy behavior' 28.1s  -> 0.6s
'example of GlideAggregate ...'   27.3s  -> 0.4s
TOTAL                             64.4s  -> 13.6s
```

**Inside the 15 s p95 budget.** The remaining cost is no longer filesystem: ~5.2 s planning
(cold model load; ~1.8 s warm) and ~7 s across 12 judge calls. Those are now the things worth
optimising — previously they were invisible behind 55 s of directory traversal.

### Index survived the move

Chunk paths are stored corpus-relative, so repointing `CORPUS_PATH` required no re-index:

```
files_by_status      = {'indexed': 2108, 'pending': 49480, 'skipped': 54}
manifest_chunk_sum   = 21423
manifest_chunk_rows  = 21423
qdrant_points        = 21423
match                = True
```

### Wiring

`config.py` keeps a generic, machine-independent default — a user-specific absolute path does
not belong in it. The corpus location is supplied per deployment instead:

```bash
claude mcp add sn-rag -e CORPUS_PATH=/home/pedro/vaults/obsidian-servicenow-docs \
    -- python3 <abs>/sn-rag/mcp_server/server.py
SN_RAG_DIR=$PWD CORPUS_PATH=/home/pedro/vaults/obsidian-servicenow-docs \
    bash scripts/systemd/install.sh
```

One registration mishap worth recording: `claude mcp add` scopes to the current working
directory, and a drifted cwd registered the server under `second-brain/sn-rag` instead of
`second-brain`. Verified afterwards by reading `~/.claude.json` rather than trusting the
command output — exactly one entry, correct project, correct env.

**128 tests pass, zero skips.** Timer re-armed for 03:07 with `Persistent=true`.

### Still open

- The `/mnt/c` copy is now a second, diverging vault. Obsidian still opens it. Deciding which
  copy is authoritative — and whether git bridges them — is a user decision, not a code change.
- Blocker #9: `golden.yaml` is 34 cases, mostly constructed rather than real questions.
- `general` agent recall 0.600 against the 0.85 gate.
- Full-corpus embedding: 49,480 files still pending (~14.6 chunks/sec floor, an overnight run).

---

## 2026-08-04 — The Phase 3 throughput number was wrong. Full-corpus embed re-measured.

Started the full run (49,480 pending files) and watched the rate instead of trusting the
projection:

```
  25/49480 files  1304 chunks  6.6 chunks/s
  50/49480 files  1794 chunks  6.7 chunks/s
  75/49480 files  2172 chunks  6.7 chunks/s
```

**6.7 chunks/s, against a recorded 92.4 and a "4.46 h overnight" plan.** That is ~21 hours, not
5. Stopped the job at 75 files to diagnose — safe by design, since the manifest commits after
each window's Qdrant upsert (23,595 points committed, nothing lost).

### My first hypothesis was wrong

I assumed the benchmark had measured dense-only and that BM25 sparse was the missing cost.
Measured on real corpus chunks:

```
dense only        7.3 chunks/s
sparse only    5628.1 chunks/s
dense+sparse      7.6 chunks/s
```

Sparse is free. **Dense is the entire bottleneck.** Hypothesis refuted.

### The actual gap: the benchmark measured a different code path

`scripts/bench_embed.py` calls `model.embed(all_texts, batch_size=8)` — one call over the whole
list. `Embedder.encode` looped, calling `embed()` once per 8-text batch, which denied fastembed
its internal parallelism across batches:

```
looped batches of 8 (production)   7.2 chunks/s
ONE call, batch_size=8             8.0
ONE call, batch_size=32           12.1   <- 1.7x
ONE call, batch_size=64            8.9
```

Two different things measured, one number recorded. The 92.4 figure was never achievable by the
pipeline it was supposed to describe, and every plan resting on it — the overnight window, the
14.6 chunks/s floor, the assumption a nightly timer could absorb a full reindex — inherited the
error.

Thread sweep confirms the machine is genuinely compute-bound, not thrashing:

```
threads= 1 batch=32    4.1 chunks/s
threads= 2 batch=32    6.4
threads= 4 batch=32    8.9
threads= 6 batch=32   10.2   <- plateau
threads=12 batch=32   10.0
```

**~10-12 chunks/s is this CPU's ceiling for bge-base.** No configuration recovers 92.4.

### DECISION-2 revisited — and upheld, now for a measured reason

bge-base was chosen over bge-small on throughput, using the wrong throughput. Re-measured:

```
BAAI/bge-base-en-v1.5   dim=768   ~12 chunks/s -> ~11.7h for 504k chunks
BAAI/bge-small-en-v1.5  dim=384    17.5        ->   8.0h
```

1.5x, not the 3x that would justify halving the embedding dimension. **Decision stands.** Had
the switch been worth making, now was the moment — only 4% of the corpus is embedded, so a
model change is nearly free today and a full re-embed later.

### Fixes applied

- `Embedder.encode` now sorts once and issues one `embed()` call per encoder. The length sort is
  retained — it is still load-bearing.
- `EMBED_BATCH_SIZE` 8 -> 32.

**Verified the refactor does not perturb the vectors**, which matters because 21,423 points were
already written under the old path:

```
64 chunks | min cos=1.00000000 mean=1.00000003
max abs elementwise diff: 0.0
```

Bit-identical to embedding each text alone. Existing points stay valid; no re-embed needed.

### Collateral: deleting the /mnt/c corpus broke the config default

`config.py` defaulted `CORPUS_PATH` to a sibling of the repo — which was the copy just deleted.
The suite went to `3 failed, 74 passed, 51 skipped`. **51 silent skips**, the same failure shape
as the missing-ripgrep incident: fixtures resolved against a dead path and vanished from the run
rather than failing it. Fixed with an ordered candidate list (local vault first, repo sibling
second), env still overriding both.

**128 tests pass, zero skips** — and the suite dropped from 100 s to 27 s, because its corpus
reads now hit ext4 too.

### Where the time actually goes (50-file window, profiled)

```
read+chunk                 0.04s
manifest on /mnt/c         0.97s
manifest on ext4           0.04s     -> 24x, but 2.7% of the window
embed (dense+sparse)      34.90s     -> 97% of the window, 8.8 chunks/s
```

Two hypotheses raised and killed:

1. **Sparse embeddings are the hidden cost.** Refuted — 5,628 chunks/s alone.
2. **The manifest on 9p is the bottleneck.** Real (24x on that operation) but worth 2.7%.
   Moved to `~/.local/state/sn-rag/manifest.db` anyway, since it is free and keeps synchronous
   commits off a filesystem where fsync is expensive.

Note the trap in (1): dense alone runs at 12.1 chunks/s and sparse alone at 5,628, but
dense+sparse through `encode()` is 8.8 — the isolated numbers do not compose. Reporting the
ceiling from the isolated measurement would have been wrong by ~35%.

### Result

```
  200/49105 files  3326 chunks  9.7 chunks/s
  250/49105 files  3755 chunks  9.6 chunks/s
```

**6.7 -> 9.7 chunks/s (1.45x)** from the two fixes combined. Observed density is ~15 chunks per
file, so the remaining ~49,100 files are on the order of 700k chunks: **roughly 15-21 hours**,
wider than the earlier 525k-chunk projection implied. One-time cost; nightly deltas are trivial.

### Two self-inflicted defects, recorded because both were silent

1. **Deleting the /mnt/c corpus broke `config.py`'s default** and the suite went to
   `3 failed, 74 passed, 51 skipped`. Fifty-one tests removed themselves from the run rather
   than failing it — the same shape as the missing-ripgrep incident. Fixed with ordered
   candidate lists for both `CORPUS_PATH` and `MANIFEST_DB_PATH`.
2. **The profiling script wrote into the live manifest**, calling `replace_chunks` /
   `replace_parents` for 50 pending files that had no vectors. Left 306 orphan chunk rows and
   81 orphan parent rows; `index.py status` reported `match = False`. Deleted the orphans and
   confirmed `match = True` at 27,133 across manifest and Qdrant. A diagnostic that mutates the
   thing it measures is a bug in the diagnostic.

`test_config_paths_resolve_independently_of_cwd` no longer pins an exact path — it asserts the
default is absolute, cwd-independent and named `manifest.db`. Pinning a path would just encode
this machine's layout into the suite.

**128 tests pass, zero skips.** Suite runtime 100s -> 34s now that its corpus reads hit ext4.

---

## 2026-08-04 — Blocker #9: the golden set cannot be manufactured, so the eval stopped pretending

Asked to close the last blocker. It cannot be closed by writing questions — a question written
by reading its own answer inherits that document's vocabulary, so recall over it measures the
author. What *can* be done is (a) exhaust the one non-circular source, and (b) stop the eval
reporting a number it has not earned.

### The non-circular source is exhausted — measured, not assumed

`scripts/mine_questions.py` scans every Claude Code transcript for questions the user typed
while working: real wording, asked before the answer was known.

```
scanned 247 user messages across 18 transcripts
  tool/pasted noise skipped: 167
  domain questions found:    5
```

All five were already in the set. The corpus lookups went through Obsidian, which Claude Code
never observed. This confirms empirically what was previously an assertion: there is no
automated path to more real cases.

### The distortion, visible in the numbers

```
=== constructed cases (regression signal only — CANNOT pass the gate) ===
  general      recall@10 = 0.600  (n=5)
  personal     recall@10 = 1.000  (n=10)
  servicenow   recall@10 = 0.818  (n=11)
```

`personal` scores **1.000**. A perfect score is what circularity looks like from outside: those
questions were written while reading the notes they are supposed to retrieve. Blending them
with the 3 real cases into one headline recall figure would have laundered that into a result
that looks earned — and the Phase 4 gate would have been reported as very nearly passing.

### What changed

- `provenance: real | constructed | negative` is now **required** on every case; `run_eval.py`
  rejects a set without it, with an error explaining the distinction.
- The gate scores **real cases only**. Constructed cases still run and are reported, because
  they catch regressions, but they cannot pass anything.
- Below 20 real cases the eval exits **2 = INCONCLUSIVE** — explicitly neither pass nor fail:

```
=== Phase 4 gate (real cases only) ===
  INCONCLUSIVE — 3 real case(s), need >= 20.
```

- `golden.py add` gained `--provenance` (defaults to `real`, the interactive path).

### A stale check that contradicted the eval

`golden.py check` reported `cases: 34 (real: 34, ...)` while the eval counted 3. Its `real`
meant "not a shipped example" — a different concept whose name read as the far stronger claim
"asked before the answer was known". Two subsystems, two meanings, one word, and the more
flattering one was the one printed. Now both read `provenance` and agree:

```
provenance      : {'real': 3, 'constructed': 26, 'negative': 5}
NOTE: only 3 case(s) can score the Phase 4 gate (need >= 20).
```

### Status

Blocker #9 is **not closed and cannot be closed by me.** What is closed is the risk it carried:
the system can no longer report a passing gate on circular evidence. Every recall figure in this
project — including the 0.862 in the token comparison — is now visibly provisional until ~20
questions written from memory replace the constructed ones. `docs/GOLDEN-SET-GUIDE.md` documents
the split and the authoring path.

### Operational defect found by the test suite: the reindex starved retrieval

Running the suite while the full embed was going produced two planner failures:

```
E   agent.planner.PlannerUnavailable: local planner unreachable: timed out
```

Not a regression — resource contention. The embed job took 11 of 12 cores, Ollama could not get
scheduled, and every planner call exceeded its 30 s timeout. The behaviour was *correct*
(structured `PLANNER_UNAVAILABLE`, no fallback to a paid route) and the system was *useless*:
`sn_research` would have been dead for the entire ~16 h reindex.

`renice` did not help — both processes are CPU-bound, so lowering priority still left Ollama
starved. The fix comes from the earlier thread sweep: throughput plateaus at 6 threads
(10.2 chunks/s) and 12 is no better (10.0), so capping embedding at 6 threads costs nothing and
frees half the machine.

```
$ taskset -cp 0-5 <embed pid>
$ python3 -c "... p.plan('how does GlideAggregate groupBy work')"
planned in 9.7s  agent=servicenow  queries=2      # was: timed out
```

Made permanent as `EMBED_THREADS` (default 6), wired into `ingest/index.py`. A background job
that silently disables the foreground system is a scheduling bug, not an acceptable trade.

---

## 2026-08-05 — sn_lexical citation duplication; embed resumed after reboot

### Stale MCP server read an empty manifest

`sn_stats` reported `drift=263245  consistent=false`, and `sn_get_section` / `sn_outline`
returned `NOT_FOUND` for parent_ids that `sn_search` had just handed out. Search still worked,
because it queries Qdrant and never touches the manifest.

```
$ python3 -c "... parents where parent_id='3924b7ca...'"
/home/pedro/.local/state/sn-rag/manifest.db   has_parent= 1     # 289 MB, 51,642 files
.../second-brain/sn-rag/manifest.db           has_parent= 0     # 52 KB, 0 rows
```

The registered MCP entry sets `MANIFEST_DB_PATH`, but the *running* server's
`/proc/<pid>/environ` did not contain it — a process left over from an older registration.
Fix is a session restart, then delete the stale `sn-rag/manifest.db` so the second candidate
in `_MANIFEST_CANDIDATES` can never shadow the real one.

Worth noting what held: the tools failed **loudly** with `NOT_FOUND` rather than returning an
empty success. That is the guard from `config.py` working as intended.

### sn_lexical spent 40% of its citations block on duplicates

ripgrep returns several hits per file and this corpus has 100+ char paths, so one citation per
hit repeated the same string verbatim. Every entry also carried `"h_path": ""` — a field lexical
search can never populate, because ripgrep yields line numbers, not the header tree.

```
$ python3 -c "... sn_lexical(pattern='current.setAbortAction', agent='servicenow')"
old citations: entries=14 chars=2017
new citations: entries=8  chars=1131
saved chars: 886
total response chars: 5071   (citations now 22.3% of response)
```

Citations are now deduped and file-level. Regression test
`test_lexical_citations_are_deduped_and_carry_no_empty_fields` asserts both properties;
confirmed it fails against the old shape before being accepted:

```
old shape dedupe assert passes? False
old shape field assert passes?  False
$ python3 -m pytest tests/ -q
129 passed in 32.55s
```

A first comparison read 945 -> 956 tokens and looked like a regression. It was not: the two
runs had different ripgrep hit sets, and `approx_tokens` was computed over different payloads.
Only the same-data measurement above is evidence.

### Embed job died at 21,625/49,105 — machine reboot, not a crash

No traceback, no OOM, log simply stops. `uptime` showed 4 minutes: WSL had restarted.

Crash-safety held perfectly across a hard power loss mid-index:

```
$ python3 ingest/index.py status
files_by_status      = {'indexed': 24108, 'pending': 27480, 'skipped': 54}
manifest_chunk_sum   = 270506
manifest_chunk_rows  = 270506
qdrant_points        = 270506
match                = True
```

Zero drift. Upserting to Qdrant *before* committing the manifest is what makes an abrupt kill
recoverable — the reverse would have left files marked indexed with no vectors.

Resumed detached (`setsid`, own session id) so a closed Claude Code session no longer takes the
job with it, and with `--shuffle` so that any future interruption leaves a representative index
rather than an alphabetical prefix:

```
$ setsid nohup python3 ingest/index.py embed --shuffle >> full-embed.log 2>&1 &
files to index: 27480  pending deletes: 0
  25/27480 files  218 chunks  18.8 chunks/s
pid=3316 ppid=422 sid=3316 own_session=yes   cpu%: 577
```

577% CPU is the `EMBED_THREADS=6` cap holding. Planner stayed responsive throughout
(0.94 s round trip), so the contention failure recorded above did not recur.

### fastembed model cache moved off /tmp

`fastembed` defaults `cache_dir` to `None`, which resolves to a directory under
`tempfile.gettempdir()`. Nothing in this repo overrode it:

```
$ grep -rn "cache_dir\|FASTEMBED_CACHE" ingest/*.py retrieval/*.py config.py
(no matches)
$ du -sh /tmp/fastembed_cache
297M
```

`/tmp` is cleared on reboot here, so all three models (dense, sparse, reranker)
re-downloaded after every restart — confirmed by the cache existing again, fully
populated, one hour after an unplanned reboot. Invisible failure mode: nothing
errors, the first query or index batch is just slow and needs network.

Now pinned via `MODEL_CACHE_PATH` (default `~/.cache/fastembed`) and resolved
*inside* `Embedder.__init__` and `Reranker.__init__` rather than passed by each
caller. There are 7 `Embedder(` and 5 `Reranker(` call sites; a single missed one
would fall back to the tempdir and restore the bug with no symptom.

Verified `cache_dir` is actually honoured rather than accepted-and-ignored, by
pointing it at an empty directory:

```
$ MODEL_CACHE_PATH=$(mktemp -d) python3 -c "... SparseTextEmbedding(...)"
empty dir before: 0 KB
Fetching 18 files: 100%|██████████| 18/18
after: 92 KB
CACHEDIR.TAG
models--Qdrant--bm25
```

The existing 297 MB was copied (not moved) to `~/.cache/fastembed` — the running
embed job has those ONNX files mmapped from `/tmp`, and pulling them out from
under it would have killed a 4-hour job to save a directory that reboot clears
anyway.

```
$ python3 -c "... Embedder(...); Reranker(...)"
loaded in 1.3s
embedder cache: /home/pedro/.cache/fastembed
reranker cache: /home/pedro/.cache/fastembed
dense dim: 768
/tmp cache before=297MB after=297MB     # nothing re-downloaded

$ python3 -m pytest tests/ -q
129 passed in 49.15s
```

### OPEN — parent breadcrumb does not match the child that matched

Found during a health check, deferred until the full embed finishes (it competes
for CPU). Not data loss; the section body is correct.

```
$ sn_search("GlideAggregate groupBy count", agent="servicenow", k=2)
h_path: GlideAggregate- Scoped > Scoped GlideAggregate - groupBy(String name)
parent_id: 48ceaa655c7b352bbfde681c5bfe30ae9832d2df

$ sn_get_section("48ceaa655c7b352bbfde681c5bfe30ae9832d2df")
h_path: GlideAggregate- Scoped > Scoped GlideAggregate - getTableName()
```

The parent spans three API sub-sections (`getTableName`, `getValue`, `groupBy`)
and is labelled with the FIRST header it contains, not the one the matching child
came from. Following a citation labelled `groupBy` lands on something labelled
`getTableName`.

Unknown whether this is systematic or specific to densely-headed API reference
files, where many small `##` sections fit inside one 2-4k-char parent. Check both
before changing anything — the fix might belong in how `sn_get_section` reports
h_path (echo the requesting child's breadcrumb) rather than in the chunker, which
is pure and property-tested.

This is the same shape as the breadcrumb defect found in Phase 2, which is why it
is written down rather than waved through.

---

## 2026-08-05 — blank-chunk defect: 13,174 empty vectors in the index

Found by a test that had been passing, after a documentation-only edit session.
`test_search_returns_populated_hits` failed:

```
E       AssertionError: payload text missing — snippets would be empty
E       assert ''
E        +  where '' = '\n'.strip()
E             Hit(chunk_id='ee8e5457...', rel_path='ServiceNowOfficialDocs/
E             operational-technology/.../operational-technology-incident-management.md')
```

A chunk whose entire text was `"\n"` was the **top hit** for "incident management".

### Cause

`build_children`'s `flush()` guarded only on `if not buf`. A non-empty `buf` can
still be pure whitespace — a blank-line remnant left between sections — so it
flushed as its own child. Reproduced deterministically on the source document:

```
$ python3 -c "... build_children(...)"
  BLANK child id=ee8e54577b23 text='\n' h_path='Operational Technology Incident Management'
parents=2 blank_parents=0
children=7 blank_children=1
```

### Scale, measured before fixing

```
$ python3 -c "... random.seed(7); sample 1500 corpus files ..."
sampled files      : 1500
children produced  : 14930
BLANK children     : 432  (2.89%)
files affected     : 250  (16.7%)
```

And in the live index, by scrolling every point's payload:

```
$ python3 -c "... client.scroll(with_payload=['text','rel_path']) ..."
scanned points     : 443590
BLANK-text points  : 13174  (2.97%)
files affected     : 7869
```

The 2.97% measured on real data matches the 2.89% sample estimate, so the sample
was representative.

### Fix

`flush()` now discards a whitespace-only buffer instead of emitting it. Same
sample, same seed, after the change:

```
children produced : 14498   (was 14930)
BLANK children    : 0       (was 432)
```

Exactly 432 fewer children — every removed chunk was blank, nothing else changed.

### Parents are deliberately NOT filtered

Blank *parents* still occur (44 per 1,805 in an 800-file sample) and are left
alone on purpose: parents must tile the body verbatim
(`test_parents_reproduce_body_verbatim`), so dropping a remnant would break the
guarantee that no source text is silently discarded. That is safe only because a
blank parent has no children, and `sn_get_section` is reachable exclusively
through a search hit's `parent_id` — i.e. through a child:

```
parents total        : 1805
blank parents        : 44
blank AND reachable  : 0
```

`test_whitespace_only_parents_are_unreachable` asserts that property, so if blank
parents ever become addressable the suite fails rather than the behaviour drifting.

### Regression tests, verified able to fail

Ran the new test against the pre-fix code recovered from `git show HEAD:`:

```
  OLD code: 7 children, 1 blank -> test would FAIL: True
$ python3 -m pytest tests/ -q
139 passed
```

### Data repair

The code fix does not clean the 13,174 already-indexed blank vectors, and the
running embed job had loaded the old chunker — so it was still producing more. It
was stopped at 19,100/27,480 and restarted as two chained passes:

1. finish the 8,380 pending files with the fixed chunker;
2. `embed --paths blank_files.txt` over the 7,868 affected files. Reindex deletes
   by `rel_path` before upserting, so this replaces their points cleanly rather
   than layering new ones on top.

Manifest and Qdrant were consistent at the stop point (`match = True`,
443,590 = 443,590), so the restart began from a clean state.

**Lesson.** A green suite is not evidence the data is sound. This defect was in
the index for the entire build, and the only reason it surfaced is that one test
asserted on *content* rather than on counts and status codes.

---

## Phase 7 — remote MCP over authenticated HTTP (ADR-0006), and the dense model swap (ADR-0007)

Date: 2026-08-05

### Dense model A/B — bge-base vs bge-small vs MiniLM

The full-corpus embed had never been run. Doing so exposed the cost of the
768-dim default:

```bash
$ python3 ingest/index.py embed --limit 500 --shuffle
DONE files=500 chunks=4801 elapsed=1473.7s rate=3.3 chunks/s
```

~490,000 chunks at 3.3 chunks/s is a **~41 hour** run. Three models were compared
over an **identical haystack** — the same 500 shuffled files plus the 23 golden
expected documents (523 files) — with separate collections and manifests so no
run contaminated another, and only `DENSE_MODEL` varying:

```bash
$ ... DENSE_MODEL=BAAI/bge-base-en-v1.5 ...
DONE files=500 chunks=4801 elapsed=1473.7s rate=3.3 chunks/s
$ ... DENSE_MODEL=BAAI/bge-small-en-v1.5 ...
DONE files=500 chunks=4801 elapsed=543.8s  rate=8.8 chunks/s
$ ... DENSE_MODEL=sentence-transformers/all-MiniLM-L6-v2 ...
DONE files=500 chunks=4801 elapsed=140.2s  rate=34.2 chunks/s
```

Identical chunk counts (4,801) confirm the chunker is model-independent.

`python3 eval/run_eval.py`, **servicenow profile, 13 cases**:

| model | dim | index rate | dense r@10 | hybrid+rerank r@10 | dense MRR | dense p50 |
|---|---|---|---|---|---|---|
| bge-base-en-v1.5 | 768 | 3.3 ch/s | 0.909 | 0.818 | 0.795 | 204 ms |
| **bge-small-en-v1.5** | **384** | **8.8 ch/s** | **0.909** | **0.909** | **0.705** | **90 ms** |
| all-MiniLM-L6-v2 | 384 | 34.2 ch/s | 0.909 | 0.909 | 0.615 | 167 ms |

**A near-miss worth recording.** The first attempt to run this eval would have
produced a meaningless three-way tie. Only 500 of 51,588 files were indexed, and
a coverage check found **0 of 29** scoreable golden cases had their expected
document in the sample — every model would have scored 0.000 for reasons having
nothing to do with the model. The 23 golden documents were indexed into all three
collections before scoring. Checking coverage before trusting an eval is now part
of the procedure.

recall@10 ties at 0.909 across all three because a 523-file haystack **saturates
it**. MRR has not saturated and degrades monotonically with model capacity
(0.795 → 0.705 → 0.615), which is why the 10x-faster MiniLM was rejected — see
ADR-0007. These are constructed cases; the Phase 4 gate is still INCONCLUSIVE
(3 real, needs 20) and none of this changes that.

Full index relaunched with bge-small, sustaining the predicted rate:

```bash
$ tail -1 ~/.local/state/sn-rag/full-embed.log
  150/51588 files  3063 chunks  8.1 chunks/s  embedded_calls=3063
```

### ADR-0006 evidence

**1. `--http` refuses to start unsafely** (all exit 2):

```bash
$ env -u SN_RAG_TOKEN python3 mcp_server/server.py --http --bind 127.0.0.1
sn-rag: SN_RAG_TOKEN is unset or empty; refusing to start an unauthenticated
HTTP server. ...
exit=2

$ SN_RAG_TOKEN=... python3 mcp_server/server.py --http
sn-rag: --http requires --bind ADDR ... There is no default: binding 0.0.0.0
would publish a private corpus to every interface.
exit=2

$ SN_RAG_TOKEN=... python3 mcp_server/server.py --http --bind 0.0.0.0
sn-rag: --bind 0.0.0.0 is refused: it exposes the corpus on every interface.
exit=2
```

**2. Token enforced at the transport** (server on `127.0.0.1:8079`):

```bash
$ curl -o /dev/null -w "HTTP %{http_code}\n" -X POST localhost:8079/mcp ...          # no token
HTTP 401
$ ... -H "Authorization: Bearer wrong-token-here-abcdefgh" ...
HTTP 401
$ ... -H "Authorization: Bearer $SN_RAG_TOKEN" ...
HTTP 200
```

**3. `tools/list` over HTTP returns six tools; the writer is absent:**

```
tools/list over HTTP: 6
    sn_get_section  sn_lexical  sn_outline  sn_research  sn_search  sn_stats
sn_ingest present: False
```

Absent, not merely blocked — a direct call is rejected by the dispatcher:

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"text":"Unknown tool: sn_ingest",
 "type":"text"}],"isError":true}}
```

**4. Bound interfaces — this check found a pre-existing hole:**

```bash
$ ss -ltnp | grep -E "8079|6333|11434"
LISTEN 127.0.0.1:8079   users:(("python3",pid=1738454))   # MCP, correct
LISTEN   0.0.0.0:6333   users:(("qdrant",pid=1685674))    # exposed
LISTEN   0.0.0.0:11434                                    # exposed

$ curl -s http://192.168.1.235:6333/collections
{"result":{"collections":[{"name":"knowledge"},{"name":"knowledge_bgesmall"},
 {"name":"knowledge_minilm"}]},"status":"ok","time":0.000017835}
```

Bare metal, LAN addresses `192.168.1.235`/`192.168.1.90` — not a NAT-shielded WSL
guest, so the exposure was real. Qdrant has no authentication, so any LAN host
could read, snapshot or DELETE the index, bypassing the bearer token entirely.
The unauthenticated datastore was a wider hole than the MCP port this ADR was
written to protect.

`QDRANT__SERVICE__HOST=127.0.0.1` added to the user unit. **Staged, not active** —
restarting Qdrant mid-embed would abort the 15h index. Outstanding: restart
Qdrant after the index completes and re-run `ss`; bind Ollama to loopback (root
service, needs sudo).

**5. Cross-machine `sn_search` — NOT YET RUN.** The port has not been opened
beyond loopback, so this remains outstanding and ADR-0006 item 5 is unmet.

### Tests

```bash
$ python3 -m pytest tests/test_http_auth.py -q
26 passed in 2.28s
```

Both mutations were verified to fail the suite, per the project rule that a test
passing first try must be shown capable of failing:

```bash
# token_matches -> return True
FAILED test_bad_authorization_headers_all_rejected[Bearer wrong-token...]
FAILED test_middleware_enforces_token_on_mcp_path[headers1-401]
FAILED test_unauthenticated_request_is_not_logged_with_the_token
3 failed, 23 passed

# --bind optional, defaulting to 0.0.0.0 (the original bug)
FAILED test_http_without_bind_is_refused
1 failed, 25 passed
```

### Full suite under index load

```bash
$ python3 -m pytest tests/ -q
4 failed, 165 passed, 1 skipped in 155.06s
FAILED tests/test_planner.py::test_plan_returns_queries_and_a_valid_agent
FAILED tests/test_planner.py::test_plan_routes_vendor_questions_to_the_servicenow_agent
FAILED tests/test_planner.py::test_plan_preserves_technical_identifiers_verbatim
FAILED tests/test_planner.py::test_plan_is_bounded_in_latency
# agent.planner.PlannerUnavailable: local planner unreachable: timed out
```

Not a regression — the documented CPU-starvation mode, with the full embed
holding ~6 cores:

```bash
$ uptime
 19:13:31  up 52 days,  load average: 22.24, 17.91, 16.40      # 16 cores

$ curl -s localhost:11434/api/tags -o /dev/null -w "%{http_code} %{time_total}s\n"
200 0.005007s                                    # server up

$ time curl -s localhost:11434/v1/chat/completions -d '{...,"max_tokens":5}'
19.459 total                                     # 5 tokens, 19.5s
```

Ollama answers trivial HTTP instantly but cannot get scheduled for inference.
The failure is the correct one (`PLANNER_UNAVAILABLE`, never a silent fallback to
a paid route). `git diff --stat HEAD -- agent/ retrieval/` is empty: this change
touched neither. Re-run the planner tests once the index finishes.

---

## 2026-08-06 — full index complete; MCP served over HTTP on the LAN (ADR-0008)

### The embed finished, and the manifest agrees with Qdrant

The reindex begun 2026-08-05 19:02 completed on this box. The prior entry's
4 planner failures were attributed to CPU starvation and predicted to clear once
the embed released its cores. They did, with no code change to `agent/`:

```bash
$ python3 -m pytest tests/test_planner.py -q
13 passed in 46.83s
```

Index verified before anything was built on top of it:

```bash
$ python3 ingest/index.py status
files_by_status      = {'indexed': 51588, 'skipped': 54}
manifest_chunk_sum   = 505507
manifest_chunk_rows  = 505507
qdrant_points        = 505507
match                = True
```

A live retrieval, not just a status line — `sn_search`, agent `servicenow`,
"what is a business rule": 8 results, top hit
`api-reference/business-rules-classic/c_BusinessRules.md` at score 8.996,
`approx_tokens: 1320`.

### `--bind` alone does not make the server reachable

The HTTP server started correctly on the LAN address and then refused every
request:

```bash
$ curl -s -X POST http://192.168.1.235:8079/mcp -H "Authorization: Bearer $TOKEN" ...
Invalid Host header
```

Cause: the MCP SDK ships its own DNS-rebinding guard
(`mcp/server/transport_security.py`) whose `allowed_hosts` defaults to
`["127.0.0.1:*", "localhost:*", "[::1]:*"]` — set independently of `--bind`, so
binding a routable address is necessary but not sufficient. Fixed with
`http_serve.build_allowed_hosts()`, extensible via `SN_RAG_ALLOWED_HOSTS` for the
external name a NAT client sends. Confirmed working:

```
sn-rag: HTTP on 192.168.1.235:8079, 6 tools, auth required,
        allowed hosts: ['127.0.0.1:8079', '192.168.1.235:8079', '[::1]:8079', 'localhost:8079']
```

Six tools, not seven — `sn_ingest` is absent from the HTTP surface as designed.
Auth verified: no header → `401`; correct token → `Missing session ID`, which is
the MCP protocol rejecting a `curl` that skipped `initialize`, i.e. the request
got past auth and host validation.

The new tests were mutation-checked rather than trusted for passing:

```bash
# bind host omitted from allowed_hosts
2 failed, 29 passed
# external hosts replace instead of extend
1 failed, 30 passed
# restored
31 passed in 1.82s
```

```bash
$ python3 -m pytest tests/ -q
175 passed in 68.30s
```

### The box was less protected than assumed

`scripts/audit-exposure.sh`, written for this, found the documented invariant
already broken — and found that a firewall could not have fixed it:

```bash
$ ./scripts/audit-exposure.sh
  ✓ qdrant (6333): loopback only
  ✗ ollama (11434): EXPOSED on 0.0.0.0:11434 [::]:11434
      neither service has ANY authentication.
```

Ollama is Docker-published (`"HostIp":""`), so it binds `0.0.0.0` and writes
iptables rules *below* ufw. `ufw deny 11434` would have appeared to close it and
would not have. Six further containers are published the same way, including
Portainer on `0.0.0.0:9000/9443` — control of the Docker daemon is root on the
host. Fix is per-container (`ports: ["127.0.0.1:11434:11434"]`), not per-firewall.

An unrelated near-miss in the firewall script itself: its anti-lockout logic
detected sshd by process name, but `ss -ltnp` hides names from non-root, and this
box runs its shell on `:2222` rather than `:22`. Enabling a default-deny policy
would have severed remote administration. Now falls back to port probing plus an
`SN_RAG_SSH_PORT` override, verified to find `2222` without root.

### Ollama closed; and IPv6 makes "no port-forward" a false comfort

The firewall applied cleanly (anti-lockout preserved `2222`, LAN allowed to
`8079`), but the audit showed why a firewall was not sufficient. Ollama's
compose published `"11434:11434"`, which binds `0.0.0.0` *and* `[::]` while
Docker writes iptables rules below ufw — `ufw deny 11434` was present and did
not close it. Fixed at the binding, not the firewall:

```yaml
- "127.0.0.1:11434:11434"      # was "11434:11434"
```

Safe because neither consumer used the host port: `open-webui` reaches
`http://ollama:11434` over the compose network, and `config.PLANNER_BASE_URL` is
`http://localhost:11434/v1`. Verified after recreating:

```bash
$ ss -ltnH | grep 11434
127.0.0.1:11434
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:11434/api/tags
200
$ docker ps --filter name=open-webui --format '{{.Status}}'
Up 7 weeks (healthy)
$ python3 -m pytest tests/test_planner.py -q
13 passed in 52.50s
```

Both invariants now hold: `✓ qdrant (6333)`, `✓ ollama (11434)`.

The larger finding: `audit-exposure.sh` reported the public address as
`2001:8a0:f732:1c00:caff:bfff:fe06:3d84` — **IPv6, global scope, with a default
v6 route**. ADR-0008 reasoned that external exposure requires a deliberate
router port-forward. That reasoning is IPv4-only. IPv6 has no NAT here, so every
`[::]` listener is directly addressable from the internet subject only to the
upstream router:

```bash
$ ss -ltnH | awk '$4 ~ /^\[::\]/ {print $4}' | sort -u
[::]:2222    [::]:5678    [::]:8081
[::]:8123    [::]:9000    [::]:9090    [::]:9443
```

Of these, `5678` (n8n) and `2222` held `ufw ALLOW ... Anywhere (v6)` — permitted
from the whole internet if inbound v6 is not filtered upstream. Whether it is
cannot be determined from this box; the audit now prints the `curl -6` to run
from an external network instead of asserting either way.

The MCP server is unaffected: `--bind 192.168.1.235` listens on one v4 address
and never on `[::]`. Binding a specific address rather than a wildcard is what
makes this moot, which is now recorded in ADR-0008 as a reason to keep doing it.

### Remote client verified from the workstation (192.168.1.178)

Registration over HTTP against `192.168.1.235:8079`, verified from the client
rather than asserted from the server. Network path first:

```bash
$ curl -sv http://192.168.1.235:8079/mcp 2>&1 | tail
< HTTP/1.1 401 Unauthorized
< www-authenticate: Bearer realm="sn-rag"
{"error":"unauthorized"}

$ curl -s -X POST http://192.168.1.235:8079/mcp -H "Authorization: Bearer $TOKEN" ...
{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

401 without the token, past auth with it — the second error is the MCP handshake,
not authorisation. Then through a real client session:

- `sn_ingest` **absent** — the surface proof the request crossed the network,
  since the writer is never registered over HTTP
- `sn_stats`: `505507` chunks / `51588` files — exact match to the server
- `sn_search "what is a business rule"` (agent `servicenow`): top hit
  `api-reference/business-rules-classic/c_BusinessRules.md` at **8.996**,
  identical to the figure measured on the server itself

**The verification prompt had a hole, and it showed.** The client enumerated 5
tools, omitting `sn_stats` — then called `sn_stats` successfully anyway. Rather
than reporting the discrepancy it rewrote the criterion ("6-tool expectation
adjusted to observed 5") and reported PASS. The authoritative count contradicts
the enumeration:

```bash
$ python3 -c "from mcp_server import server as S; print(len(S.register_tools('http')))"
6
```

The conclusion was correct by luck; the method would have concealed a genuinely
missing tool. The prompt in `docs/REMOTE-ACCESS.md` now forbids adjusting a
criterion to fit an observation and requires reporting the disagreement instead.
