```
      _---~~(~~-_.
    _{        )   )
  ,   ) -~~- ( ,-' )_
 (  `-,_..`., )-- '_,)
( ` _)  (  -~( -_ `,  }
(_-  _  ~_-~~~~`,  ,' )
  `~ -^(    __;-,((()))
        ~~~~ {_ -_(())
               `\  }
                 { }

        S E C O N D   B R A I N
```

# sn-rag

Local agentic RAG over ~51,600 markdown files — the ServiceNow documentation
corpus plus a personal second brain — exposed to Claude Code over MCP.

It replaces an `INDEX.md` + grep + read-whole-file workflow that burned tokens
finding one paragraph, and it takes the 51k-file corpus out of Obsidian, which
was crashing under it.

Measured against that old workflow on 29 questions: **78.5× fewer tokens**
(3,040,367 → 38,713) and **8.4× more likely to surface the right document**
(0.862 vs 0.103 hit rate). The hit rate is provisional — see [Status](#status).

Generic by design: ServiceNow is the first corpus profile, not a hard-coded
assumption (ADR-0001).

**The one rule: retrieval never calls a paid model.** Embedding, hybrid search,
reranking, query planning and evidence selection all run on free local models on
this CPU. Claude only ever sees a small, capped, cited brief.

Two consequences follow, and they explain most of the design:

- **Claude synthesizes; the local model only plans and selects.** A 3B model at
  20.7 tok/s would need ~58 s to write a 900-word answer against a 15 s budget.
  It emits short structured JSON instead (ADR-0004).
- **Failure is explicit, never a fallback.** No local planner means
  `PLANNER_UNAVAILABLE`. There is no hosted route to fail over to, by design — a
  silent fallback would destroy the point while making every metric look good.

---

## Contents

- [Architecture](#architecture) · [Do I need Obsidian?](#do-i-need-obsidian)
- [Requirements](#requirements) · [Install](#install) · [Build the index](#build-the-index)
- [Daily use](#daily-use) · [MCP tool reference](#mcp-tool-reference)
- [Command reference](#command-reference)
- [Keeping it current](#keeping-it-current) · [Install on a local server](#install-on-a-local-server)
- [Configuration](#configuration) · [Troubleshooting](#troubleshooting)
- [Testing and evaluation](#testing-and-evaluation) · [Status](#status) · [Layout](#layout)

---

## Architecture

```mermaid
flowchart TB
    subgraph vault["Obsidian vault — plain markdown on disk"]
        V["~/vaults/obsidian-servicenow-docs<br/>51,642 files · 5.2 GB<br/>git repo, synced nightly"]
    end

    subgraph ingest["Ingest — runs offline, never during a query"]
        W["walk + classify<br/>source from top-level dir<br/>skips .git / .obsidian / index.md"]
        C["chunk hierarchically<br/>parent 2-4k chars = context<br/>child 500 chars = search unit<br/>code fences + tables ATOMIC"]
        E["embed<br/>bge-base-en-v1.5 768d dense<br/>+ BM25 sparse<br/>length-sorted batches of 32"]
        W --> C --> E
    end

    subgraph store["Storage"]
        Q[("Qdrant :6333<br/>collection 'knowledge'<br/>int8 quantized, on-disk")]
        M[("SQLite manifest<br/>sha256 per file<br/>parents · chunks · status")]
    end

    subgraph retrieval["Retrieval — per query"]
        H["hybrid search<br/>dense + BM25, RRF fused"]
        RR["cross-encoder rerank<br/>50 candidates to top 8"]
        LX["ripgrep<br/>exact API symbols"]
        PR["agent profiles<br/>general · servicenow · personal"]
    end

    subgraph agent["Agent loop — Phase 5"]
        PL["local planner<br/>qwen2.5:3b via Ollama<br/>JSON only, never prose"]
        JU["relevance judge<br/>yes/no, 4 tokens max"]
        PL --> JU
    end

    subgraph mcp["MCP server — stdio, spawned per session"]
        T["sn_search · sn_get_section · sn_outline<br/>sn_lexical · sn_research · sn_stats · sn_ingest"]
        CAP["caps enforced IN CODE<br/>never as prompt instructions"]
        T --> CAP
    end

    CL(["Claude Code<br/>synthesis + citations"])

    V --> W
    E -->|"1. upsert vectors"| Q
    E -->|"2. THEN commit manifest<br/>order is the crash-safety"| M
    Q --> H --> RR --> T
    V -.-> LX --> T
    M --> T
    PR --> H
    RR --> JU
    JU --> T
    CAP --> CL
    CL -.->|"sn_ingest: the only writer"| V

    TIMER{{"systemd timer 03:00<br/>Persistent=true catches up<br/>runs missed during downtime"}}
    TIMER -.-> W
```

**Why steps 1 and 2 are numbered.** Vectors are upserted to Qdrant *before* the
manifest is committed. Reversed, a crash leaves files marked indexed with no
vectors — a silent recall hole. Surplus vectors self-heal on the next run;
missing ones never do. This was tested by an unplanned reboot mid-index: zero
drift, `match = True`.

### Retrieval flow for one question

```
question
   ├─ planner decomposes → 2-3 sub-queries + agent profile     (~2 s, local)
   ├─ per sub-query: dense + BM25 → RRF fuse → 50 candidates   (~0.9 s)
   ├─ cross-encoder reranks 50 → top 8                          (~0.5 s)
   ├─ dedupe by parent_id, pool = 3× budget
   ├─ judge each candidate from rank 3 down: relevant? yes/no  (≤4 tok each)
   └─ cap in code → ≤900 words, ≤6 items, ≤6000 chars → Claude
```

Why parent/child chunking: small chunks search precisely, large chunks read
usefully. You search 500-char children and read 2-4k-char parents.

---

## Do I need Obsidian?

**No.** Nothing here talks to Obsidian. The corpus is plain markdown files in a
directory; Obsidian is just one editor that happens to read them.

| you want to | Obsidian required? |
|---|---|
| search / read the corpus from Claude | no |
| index new or changed files | no |
| have Claude write a note into the vault | no |
| edit notes yourself with backlinks and graph view | yes — that's what it's for |

Obsidian and sn-rag touch the same folder independently. Edit in Obsidian, in
`vim`, or via `sn_ingest` — the nightly sync notices whatever changed by content
hash and reindexes only that.

---

## Requirements

### Hardware

| | minimum | this build |
|---|---|---|
| CPU | 4 cores | AMD Ryzen 5 9600X, 6c/12t |
| RAM | 8 GB (tight) · **16 GB recommended** | 25 GB |
| disk | 25 GB free | — |
| GPU | none — **everything is CPU-only** | none (`nvidia-smi` absent) |

Disk: corpus 5.2 GB · Qdrant ~3.4 GB at full index · manifest ~600 MB · models
~300 MB · Ollama planner 1.9 GB.

**Filesystem matters more than it looks.** The corpus must live on a native Linux
filesystem. Measured on the same files: lexical search took **25.689 s** on a
Windows 9p mount (`/mnt/c`) versus **0.075 s** on ext4 — **342×**. SQLite commits
on the manifest were 24× slower. Both now live under `$HOME`.

### Software

| component | version here | notes |
|---|---|---|
| Python | 3.14.4 | 3.11+ expected to work; not tested below 3.14 |
| Qdrant | 1.18.3 | official static binary, not Docker (ADR-0002) |
| Ollama | 0.32.5 | only for `sn_research`; everything else works without it |
| ripgrep | 14.1.1 | required by `sn_lexical`; its absence once caused 51 silent test skips |
| git | any | the corpus is a git repo, synced nightly |

Python packages live in `requirements.txt`: **fastembed 0.8.0**, **qdrant-client
1.18.0**, **mcp 2.0.0**, **PyYAML 6.0.3**, plus pytest for the suite.

The planner talks to Ollama over stdlib `urllib`. The `openai` SDK is
deliberately **not** a dependency — an SDK whose default base URL is a paid
endpoint is exactly the accident this project exists to prevent.

### Models — all free, all local, ~300 MB total

| role | model | why |
|---|---|---|
| dense | `BAAI/bge-base-en-v1.5` (768d) | bge-small was only 1.5× faster, not the 3× that would justify halving dimensions |
| sparse | `Qdrant/bm25` | exact term matching for API symbols |
| rerank | `Xenova/ms-marco-MiniLM-L-6-v2` | 50 candidates → top 8 |
| planner | `qwen2.5:3b-instruct` | JSON only; ~20.7 tok/s on this CPU |

---

## Install

### 1. Python packages

```bash
cd sn-rag
pip install --user --break-system-packages -r requirements.txt
```

Prefer a virtualenv where your machine supports it (`python3 -m venv` fails on
this box — `ensurepip` is unavailable, which is why the `--user` form is
documented first).

### 2. ripgrep

```bash
sudo apt install ripgrep        # or: brew install ripgrep
rg --version                    # must print — sn_lexical is dead without it
```

### 3. Qdrant

```bash
mkdir -p ~/.local/qdrant && cd ~/.local/qdrant
curl -L -o qdrant.tar.gz \
  https://github.com/qdrant/qdrant/releases/download/v1.18.3/qdrant-x86_64-unknown-linux-musl.tar.gz
tar xzf qdrant.tar.gz && chmod +x qdrant
./qdrant --version              # qdrant 1.18.3
```

Run it under systemd so it survives reboots:

```bash
cp scripts/systemd/qdrant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now qdrant.service
systemctl --user is-active qdrant.service      # active
```

> **Qdrant ships with no authentication.** It binds `127.0.0.1` here, and that is
> the only reason it is safe. Do not move it to a LAN address without setting
> `QDRANT__SERVICE__API_KEY` — see [Install on a local server](#install-on-a-local-server).

### 4. The corpus

```bash
git clone <your-vault-repo> ~/vaults/obsidian-servicenow-docs
```

Every top-level directory containing `.md` files must be classified in
`SOURCE_BY_TOP_DIR` in `config.py`, or ingest **fails loudly** rather than
silently skipping files. Add yours there.

### 5. Ollama — optional, only for `sn_research`

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b-instruct
curl -s localhost:11434/api/tags     # should list the model
```

Skip this and everything except `sn_research` works normally.

### 6. Register the MCP server with Claude Code

Run from the **project directory** you want it available in — scope follows cwd:

```bash
cd /path/to/second-brain
claude mcp add sn-rag \
  --env CORPUS_PATH=$HOME/vaults/obsidian-servicenow-docs \
  --env MANIFEST_DB_PATH=$HOME/.local/state/sn-rag/manifest.db \
  -- python3 /full/path/to/sn-rag/mcp_server/server.py
```

Add `--scope user` to make it available in every project.

### 7. Verify

```bash
python3 -c "import config; print(config.CORPUS_PATH, config.MANIFEST_DB_PATH)"
python3 -m pytest tests/ -q          # 129 passed
```

---

## Build the index

**Always sample first.** Full indexing takes hours; a broken chunker discovered
at hour six is expensive.

```bash
python3 ingest/index.py full --limit 500 --shuffle    # sample gate
python3 ingest/index.py status                        # must report match = True
```

`--shuffle` is not cosmetic: an alphabetical prefix of this corpus is almost
entirely one product area, so evaluating against it is misleading.

Then the full run, detached so a closed terminal cannot kill it:

```bash
mkdir -p ~/.local/state/sn-rag
setsid nohup python3 ingest/index.py embed --shuffle \
  >> ~/.local/state/sn-rag/full-embed.log 2>&1 < /dev/null &
tail -f ~/.local/state/sn-rag/full-embed.log
```

**Expect ~18 chunks/s on this CPU**, ~600k chunks total. It is resumable —
content hash decides staleness, so a killed run picks up where it stopped.

| subcommand | does |
|---|---|
| `full` | walk, chunk, embed, upsert — the whole pipeline |
| `embed` | embed whatever the manifest marks pending |
| `verify` | compare manifest against Qdrant, report drift |
| `status` | counts by status, chunk sums, `match` |

Flags: `--limit N` · `--shuffle` · `--recreate` (drops the collection) ·
`--paths file.txt` (reindex specific rel_paths, ignoring status).

---

## Daily use

### The launcher

```bash
ln -s "$PWD/scripts/second-brain" ~/.local/bin/second-brain
second-brain
```

Splash, then preflight and a session picker:

```
  ● qdrant    351,164 vectors
  ● planner   ollama up — sn_research available
  ● indexing  running — 8900/27480 files 18.0 chunks/s

  1  05 Aug 11:07  fix the fastembed cache path
  2  03 Aug 18:27  current obsidian vault path: ...

  number to resume · u to paste a session id · Enter for a fresh session
```

The preflight exists because service state is invisible until it bites: Qdrant
down means *every* search fails, and a stale MCP server once made
`sn_get_section` return `NOT_FOUND` while `sn_search` kept working. Arguments
pass straight through, so `second-brain --resume <id>` behaves like `claude`.

### Asking things

Just talk. Claude picks the tool.

| you say | what runs |
|---|---|
| "how do business rules work?" | `sn_search` (agent `servicenow`) |
| "what did I write about ACL debugging?" | `sn_search` (agent `personal`) |
| "show me every use of `setAbortAction`" | `sn_lexical` — exact, not semantic |
| "expand that third result" | `sn_get_section` on its `parent_id` |
| "research X and cite sources" | `sn_research` — full local agent loop |
| "save this summary to my vault" | `sn_ingest` |

**Pick the agent deliberately** — it is a hard source filter, not a hint:

- `general` — everything: official docs, personal notes, wiki, apps, code graphs
- `servicenow` — official vendor documentation only
- `personal` — your notes, wiki and apps only

An unknown agent name is `BAD_REQUEST`, never a silent fallback to `general`.

---

## MCP tool reference

Seven tools. **Every cap is enforced in code** (`mcp_server/caps.py`), never as a
prompt instruction — a prompt instruction is a suggestion.

### `sn_search`
```python
sn_search(query: str, agent: str = "general", k: int = 8,
          doc_type: str | None = None, product: str | None = None,
          release: str | None = None) -> dict
```
Hybrid dense + BM25, RRF-fused, cross-encoder reranked. The default entry point.
Returns ranked snippets with `chunk_id`, `parent_id`, `rel_path`, `h_path`
breadcrumb, `source`, `score`.
**Caps:** 8 results · 150 words each · 6,000 chars total.

### `sn_get_section`
```python
sn_get_section(parent_id: str) -> dict
```
One parent section verbatim, by `parent_id` from a search result. This is how you
expand a 500-char snippet into its full 2-4k-char context.
**Cap:** 8,000 chars.

### `sn_outline`
```python
sn_outline(rel_path: str) -> dict
```
Header tree for one document — structure only, no body text. Cheap way to see
what a large file contains before pulling any of it.
**Cap:** 3,000 chars.

### `sn_lexical`
```python
sn_lexical(pattern: str, agent: str = "general", fixed_string: bool = True) -> dict
```
Exact match via ripgrep. Use for API symbols, method names and error strings —
anything where semantic similarity is the wrong tool. Returns `rel_path` and line
number per hit; citations are file-level and deduped.
**Caps:** 20 hits · 4,000 chars total.

### `sn_research`
```python
sn_research(question: str, agent: str | None = None, budget: int = 6,
            candidates: int = 50, use_judge: bool = True) -> dict
```
The full local agent loop: the planner decomposes the question, retrieval runs
per sub-query, a local judge filters candidates, and the result is **selected
cited evidence with a reasoning trace — not a written answer**. Synthesis is
Claude's job (ADR-0004). Requires Ollama; returns `PLANNER_UNAVAILABLE` if it is
down.
**Caps:** 900 words for the whole brief · 6 items · 6,000 chars.

### `sn_stats`
```python
sn_stats() -> dict
```
Index health: file counts by status, counts by source, manifest chunk sum, Qdrant
point count, and **drift** between them. `consistent: false` means the manifest
and vector store disagree — investigate before trusting results.
**Cap:** 500 chars.

### `sn_ingest`
```python
sn_ingest(source_path: str | None = None, content: str | None = None,
          dest_rel_path: str | None = None, title: str | None = None) -> dict
```
**The only writer, and the only real security boundary.** Migrates a file (or
literal content) into the vault, then chunks, embeds and upserts it so it is
immediately searchable — no waiting for the nightly run.

Rejects: absolute paths · `..` traversal · symlink escapes · null bytes ·
non-markdown suffixes · anything targeting the `official` vendor corpus. Writes
are confined to `VAULT_PATH`. It never deletes.
**Cap:** 1,000 chars. Limits: 2 MB, 400 chunks per file.

---

## Command reference

Everything you can run, in one place. All paths are relative to `sn-rag/`.

### Launcher

| command | does |
|---|---|
| `second-brain` | splash, health checks, session picker |
| `second-brain --status` | health checks only, then exit — no session |
| `second-brain --sessions` | list recent sessions and exit |
| `second-brain --help` | usage |
| `second-brain --resume <id>` | resume directly; any `claude` flag passes through |

### Indexing

| command | does |
|---|---|
| `python3 ingest/index.py full` | **walk** the corpus: hash every file, mark changed ones pending, queue deletions. Does *not* embed |
| `python3 ingest/index.py embed` | embed + upsert everything marked pending |
| `python3 ingest/index.py verify` | compare manifest against Qdrant, report drift |
| `python3 ingest/index.py status` | counts by status, chunk sums, `match` |

Flags for `full` / `embed`: `--limit N` · `--shuffle` · `--recreate` (drops the
collection) · `--paths file.txt` (reindex listed rel_paths, ignoring status).

**`full` and `embed` are separate on purpose.** `embed` only processes what the
manifest already knows about; it never walks the corpus. After editing the vault,
run `full` first or the new files are invisible:

```bash
python3 ingest/index.py full && python3 ingest/index.py embed
```

First index of a new corpus — sample before committing hours:

```bash
python3 ingest/index.py full --limit 500 --shuffle    # gate
python3 ingest/index.py status                        # match = True
setsid nohup python3 ingest/index.py embed --shuffle \
  >> ~/.local/state/sn-rag/full-embed.log 2>&1 < /dev/null &
```

### Evaluation and measurement

| command | does |
|---|---|
| `python3 -m pytest tests/ -q` | the suite (129 tests) |
| `python3 eval/run_eval.py` | recall@k, MRR, latency across all four modes |
| `python3 eval/run_eval.py --modes hybrid+rerank` | one mode only |
| `python3 eval/run_eval.py --approx` | approximate HNSW, as production runs |
| `python3 scripts/golden.py check` | golden-set coverage and provenance |
| `python3 scripts/golden.py find <pattern>` | locate candidate docs — **lexical only, never vector search** |
| `python3 scripts/golden.py add --id … --question …` | append a golden case |
| `python3 scripts/baseline_tokens.py` | token savings vs reading whole files |
| `python3 scripts/mine_questions.py` | mine real questions from session transcripts |
| `python3 scripts/bench_embed.py` | embedding throughput sweep |
| `python3 scripts/diagnose_rerank.py` | inspect reranker scores for one query |

`run_eval.py` exit codes: **0** pass · **1** fail · **2** INCONCLUSIVE (fewer than
20 `provenance: real` cases, or a golden set referencing missing files).

### Services

| command | does |
|---|---|
| `systemctl --user is-active qdrant.service` | is the vector store up |
| `systemctl --user restart qdrant.service` | restart it |
| `systemctl --user list-timers sn-rag-sync.timer` | when the nightly sync next fires |
| `systemctl --user start sn-rag-sync.service` | run the sync now |
| `journalctl --user -u sn-rag-sync.service -n 50` | sync logs |
| `curl -s localhost:6333/collections/knowledge` | raw collection stats |
| `curl -s localhost:11434/api/tags` | is the planner model loaded |
| `sudo loginctl enable-linger "$USER"` | **headless servers only** — without it user timers never fire |

### Inspecting state

```bash
python3 -c "import config; print(config.CORPUS_PATH, config.MANIFEST_DB_PATH)"
tail -f ~/.local/state/sn-rag/full-embed.log
pgrep -af 'index.py embed'                       # is an index job running
```

---

## Keeping it current

A systemd **user timer** runs nightly at 03:00:

```bash
bash scripts/systemd/install.sh          # or, by hand:
cp scripts/systemd/sn-rag-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sn-rag-sync.timer
systemctl --user list-timers sn-rag-sync.timer
```

**User units, not system units** — deliberately. They need no root and run as the
account that owns the corpus checkout and the model cache.

The unit runs `scripts/nightly_sync.sh`: `git pull` the vault (best effort — the
vault is live and a dirty tree must not abort the run), then an incremental
reindex of whatever changed by content hash.

**`Persistent=true` is the point.** If the machine is off at 03:00, systemd
records the missed window and fires on the next boot. Plain cron silently skips
until tomorrow. `OnBootSec=2min` keeps a catch-up run from racing Qdrant's
startup; `RandomizedDelaySec=15min` keeps it off the desktop's login path.

Run it by hand any time:

```bash
systemctl --user start sn-rag-sync.service
journalctl --user -u sn-rag-sync.service -n 50
```

---

## Install on a local server

Full reasoning, alternatives and rejections: **`docs/adr/0005`**. Summary:

### The rule: migrate the index, never rebuild it

Embeddings are deterministic for a fixed model — verified during a refactor at
`max abs elementwise diff: 0.0`. A copied collection is *identical* to a rebuilt
one, not an approximation. On N100-class hardware a full rebuild is an estimated
**35-50 hours**; the copy takes minutes.

Bulk embedding stays on the workstation. Incremental nightly updates are cheap
and stay on the server.

### Sequence

1. **Finish the index on the workstation.** Migrating a partial one wastes the trip.
2. **Stop Qdrant, or use its snapshot API.** `rsync` of a live storage directory
   gives a torn copy — segment files and their metadata are written independently.
3. **Copy** — over Tailscale/WireGuard, not a Windows share:
   ```bash
   rsync -a ~/vaults/obsidian-servicenow-docs/  server:~/vaults/obsidian-servicenow-docs/
   rsync -a ~/.local/qdrant/storage/            server:~/.local/qdrant/storage/
   rsync -a ~/.local/state/sn-rag/manifest.db   server:~/.local/state/sn-rag/
   rsync -a ~/.cache/fastembed/                 server:~/.cache/fastembed/
   ```
   The model cache matters: without it the server needs ~300 MB of Hugging Face
   downloads before its first query, and fails outright if firewalled from the
   public internet — a reasonable posture for a box holding a corporate corpus.
4. **Verify before trusting it:**
   ```bash
   python3 ingest/index.py status     # match = True, counts identical to source
   ```
   A mismatch is a silent recall hole: searches succeed and quietly miss documents.
5. **systemd units** for Qdrant, Ollama, the MCP server and the sync timer.
   On a headless server, **enable lingering** or the user timers never fire with
   nobody logged in:
   ```bash
   sudo loginctl enable-linger "$USER"
   ```
   Without it the nightly sync only catches up when you next log in — which on a
   box you never log into means never.
6. **Re-run the eval on the server.** Same golden set, same index — the numbers
   should match. If they don't, something didn't migrate cleanly.

### Network boundary — read before exposing anything

**Qdrant and Ollama both ship with no authentication.** On a LAN address, anyone
on your network can read the entire corpus over HTTP with no credential and no
audit trail. This corpus originates from a corporate tenant.

- Bind services to `127.0.0.1` **plus a mesh-VPN interface only** — never `0.0.0.0`
- Set `QDRANT__SERVICE__API_KEY` anyway. Redundant with the VPN by design: a VPN
  is a configuration, and configurations get changed
- The MCP server must move from **stdio to streamable HTTP** to cross a machine
  boundary, and **requires a bearer token** — `sn_ingest` is a writer

### What degrades on weak hardware

| symptom | first lever |
|---|---|
| search latency high | `RERANK_CANDIDATES` 50 → 30. recall@10 is *flat* across depths; only recall@5 and MRR drop, p95 improves 1450 ms → 883 ms |
| `sn_research` times out | `qwen2.5:1.5b-instruct`, or lower `MAX_JUDGE_CALLS`, or run without a planner |
| indexing starves everything | `EMBED_THREADS` — throughput plateaus at 6 anyway, so capping costs nothing |

---

## Configuration

`config.py` is the single source of truth. Everything below is an environment
variable override.

### Paths
| variable | default | notes |
|---|---|---|
| `CORPUS_PATH` | `~/vaults/obsidian-servicenow-docs` | ordered candidate list, never cwd-relative |
| `MANIFEST_DB_PATH` | `~/.local/state/sn-rag/manifest.db` | on ext4 — 24× faster fsync |
| `MODEL_CACHE_PATH` | `~/.cache/fastembed` | fastembed's own default is a tempdir, wiped on reboot |
| `VAULT_PATH` | = `CORPUS_PATH` | write root for `sn_ingest` |

### Vector store and embedding
| variable | default |
|---|---|
| `QDRANT_URL` | `http://localhost:6333` |
| `QDRANT_COLLECTION` | `knowledge` |
| `DENSE_MODEL` | `BAAI/bge-base-en-v1.5` |
| `SPARSE_MODEL` | `Qdrant/bm25` |
| `EMBED_BATCH_SIZE` | `32` — measured 8→8.0, 32→12.1, 64→8.9 chunks/s |
| `EMBED_THREADS` | `6` — plateaus here; uncapped it starved the planner for hours |
| `UPSERT_FILE_BATCH` | `25` |

### Retrieval
| variable | default |
|---|---|
| `RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` |
| `RERANK_CANDIDATES` | `50` — from a measured 30/50/100/200 sweep |
| `RERANK_TOP_K` | `8` |
| `SEARCH_HNSW_EF` | `128` |
| `EVAL_EXACT_SEARCH` | `1` — eval must be deterministic; HNSW noise exceeds the effects measured |

### Planner
| variable | default |
|---|---|
| `PLANNER_BASE_URL` | `http://localhost:11434/v1` — **never point this at a paid endpoint** |
| `PLANNER_MODEL` | `qwen2.5:3b-instruct` |
| `PLANNER_MAX_TOKENS` | `256` |
| `PLANNER_TIMEOUT_SECONDS` | `30` |

### Ingest
| variable | default |
|---|---|
| `INGEST_DEFAULT_DIR` | `raw/inbox` |
| `INGEST_MAX_BYTES` | `2097152` (2 MB) |
| `INGEST_MAX_CHUNKS` | `400` |

### Not overridable — code constants, deliberately
Chunk sizes (`PARENT_CHUNK_MIN/MAX_CHARS` 2000/4000, `CHILD_CHUNK_CHARS` 500,
`CHILD_CHUNK_OVERLAP` 100) · all output `CAPS` · `MAX_TOOL_CALLS` 12 ·
`MAX_ITERATIONS` 6 · `SOURCE_BY_TOP_DIR` · `EXCLUDED_DIRS` · `EXCLUDED_FILENAMES`.

---

## Troubleshooting

**Every search fails / `BACKEND_UNAVAILABLE`**
```bash
systemctl --user is-active qdrant.service
curl -s localhost:6333/collections/knowledge | head -c 200
```

**`sn_research` returns `PLANNER_UNAVAILABLE`**
Ollama is down, or starved of CPU. Both are real causes — a full reindex once
took 11 of 12 cores and the planner timed out for hours. That is what
`EMBED_THREADS=6` prevents.
```bash
curl -s localhost:11434/api/tags
```

**`sn_search` works but `sn_get_section` returns `NOT_FOUND`**
The MCP server is reading the wrong manifest — search hits Qdrant, section
lookups hit SQLite. Usually a leftover server process from an older registration.
Restart the Claude Code session, and delete any stray `sn-rag/manifest.db` (the
second path candidate) so it cannot shadow the real one.

**`sn_stats` shows `consistent: false`**
Drift between manifest and Qdrant. `python3 ingest/index.py verify` to see it,
`status` for counts. Surplus vectors self-heal; missing ones do not.

**Indexing is slow, or makes everything else unusable**
~18 chunks/s is expected. If the machine is unusable, lower `EMBED_THREADS`.

**Tests skip silently**
Usually a missing corpus or missing ripgrep. 51 tests once skipped quietly
because `rg` was absent. Read the skip reasons, not just the pass count.

**Models re-download after every reboot**
`MODEL_CACHE_PATH` unset and fastembed defaulted to a tempdir. Fixed by default
now; verify with `python3 -c "import config; print(config.MODEL_CACHE_PATH)"`.

---

## Testing and evaluation

```bash
python3 -m pytest tests/ -q                    # 129 passed
python3 eval/run_eval.py --golden eval/golden.yaml
python3 scripts/baseline_tokens.py             # token savings vs reading files
python3 scripts/golden.py check                # golden-set coverage
```

**Tests passing is not sufficient evidence.** Several real defects here were
found by inspecting output *after* a green suite — an empty payload field, a
breadcrumb off by one section, a lock that never engaged. When a test passes on
the first try, check that it could have failed.

### The golden set is human-authored and cannot be generated

Questions written by reading a document inherit its vocabulary, so recall comes
out near-perfect and measures nothing. Every case carries
`provenance: real | constructed | negative`:

- **real** — asked before the answer was known. The only kind that scores the gate.
- **constructed** — written while reading docs. Regression signal only.
- **negative** — the corpus genuinely cannot answer it; tests fabrication.

Below 20 real cases, `run_eval.py` exits **2 = INCONCLUSIVE** rather than
reporting a blended number. Current: **3 real, 26 constructed**. The constructed
`personal` cases score **1.000** — that perfect score *is* the circularity, left
visible on purpose.

Do not "fix" this by lowering `MIN_REAL_CASES`. The mining path is exhausted: 247
user messages across 18 transcripts yielded 5 domain questions, all already
present. See `docs/GOLDEN-SET-GUIDE.md`.

---

## Status

| area | state | evidence |
|---|---|---|
| ingest + chunking | done | property tests, 129 passing |
| embedding | ~18 chunks/s | `full-embed.log` |
| hybrid + rerank | done | measured sweep in `BUILD-LOG.md` |
| MCP surface | 7 tools, capped in code | `tests/test_mcp.py` |
| agent loop | done | ADR-0004 |
| nightly sync | done | systemd timer, `Persistent=true` |
| token savings | **78.5×** (3,040,367 → 38,713 over 29 cases) | `scripts/baseline_tokens.py` |
| **Phase 4 recall gate** | **INCONCLUSIVE — not passed** | 3 real cases, needs 20 |

The token figures are solid. **The 0.862 hit rate is provisional** — it rests on
constructed cases, and constructed cases measure the author's vocabulary rather
than retrieval. Reporting it as passed would make this README read better and the
project less trustworthy.

---

## Layout

```
sn-rag/
├── config.py              single source of truth: paths, models, caps, budgets
├── requirements.txt
├── ingest/
│   ├── index.py           CLI: full · embed · verify · status
│   ├── chunker.py         PURE — no I/O, no network. Property-tested.
│   ├── embed.py           length-sorted batching + Qdrant upsert
│   ├── manifest.py        SQLite: sha256 staleness, parents, chunks
│   └── normalize.py       source classification, frontmatter, facets
├── retrieval/
│   ├── hybrid.py          dense + sparse, RRF fusion
│   ├── rerank.py          cross-encoder
│   ├── lexical.py         ripgrep
│   └── profiles.py        general · servicenow · personal
├── agent/
│   ├── planner.py         local JSON-only planner + judge
│   └── research.py        the Phase 5 loop
├── mcp_server/
│   ├── server.py          7 tools, stdio transport
│   ├── caps.py            output caps, enforced in code
│   └── ingest_tool.py     path containment — the security boundary
├── eval/
│   ├── run_eval.py        recall@k, MRR, latency, provenance gate
│   └── golden.yaml        human-authored, cannot be generated
├── scripts/
│   ├── second-brain       the launcher
│   ├── nightly_sync.sh    git pull + incremental reindex
│   ├── golden.py          authoring tools (lexical only, never vector search)
│   ├── baseline_tokens.py token-savings measurement
│   └── systemd/           qdrant.service, sn-rag-sync.{service,timer}
├── tests/                 129 tests
└── docs/
    ├── ARCHITECTURE.md
    ├── BUILD-LOG.md       every number, with the command that produced it
    ├── GOLDEN-SET-GUIDE.md
    └── adr/               0001-0005
```

---

## Ground rules

These are why the numbers here can be trusted.

1. **Every number comes with the command that produced it.** A figure without its
   command is not evidence — it goes in `BUILD-LOG.md` with raw output.
2. **No mocked or random embeddings** outside a clearly named test double.
3. **No `TODO`, `pass`, `NotImplementedError`** in committed non-test code.
4. **No `except: pass`**, and no handler returning an empty success. Backends fail
   with structured errors.
5. **No retrieval-quality claim without an eval run.**
6. **Output caps in code, never in prompts.**
7. **No architectural change without an ADR** recording rejected alternatives.
8. **Benchmark the code path you ship.** A benchmark that called `embed()` once
   over a whole list reported 92.4 chunks/s while production sustained 6.7. That
   number sat in the build log for three phases and funded a plan that was never
   achievable.
