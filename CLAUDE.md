# sn-rag — working instructions

Local agentic RAG over ~51,600 markdown files, exposed to Claude Code over MCP.
Read `docs/ARCHITECTURE.md` for how it works, `docs/BUILD-LOG.md` for what has been
measured and why.

## The one thing that matters most

**Retrieval must never call an expensive model.** The entire project exists to move
retrieval onto free local inference and hand Claude only a compressed, cited brief.
A silent fallback to an expensive route destroys the point while appearing to work.
Failure is explicit: `PLANNER_UNAVAILABLE`.

## Environment

`CORPUS_PATH` and `MANIFEST_DB_PATH` resolve through **ordered candidate lists**
anchored to absolute locations, never the working directory:

```bash
python3 -c "import config; print(config.CORPUS_PATH, config.MANIFEST_DB_PATH)"
# /home/mrmilk/gitHubRepos/obsidian-servicenow-docs
# /home/mrmilk/.local/state/sn-rag/manifest.db
```

Current layout — **both on ext4, deliberately**:

| what | where | why |
|---|---|---|
| corpus | `~/gitHubRepos/obsidian-servicenow-docs` | lexical search 342x faster than `/mnt/c` |
| manifest | `~/.local/state/sn-rag/manifest.db` | SQLite fsync 24x faster off 9p |
| models | `~/.cache/fastembed` (`MODEL_CACHE_PATH`) | fastembed defaults to a tempdir, wiped on reboot |
| code | `.../second-brain/sn-rag` | on `/mnt/c`; fine, it is read once |

Override with `export CORPUS_PATH=...` only to point at a different corpus.

Never reintroduce a cwd-relative default. The MCP server is spawned by Claude
Code with an arbitrary cwd; `./manifest.db` resolved to a path that did not
exist, sqlite created an empty database, and `sn_get_section` / `sn_outline`
returned nothing while reporting success. Guarded by
`test_config_paths_resolve_independently_of_cwd`.

- Qdrant: user systemd unit `qdrant.service` (`Restart=always`), HTTP :6333.
  Check with `systemctl --user is-active qdrant.service` before blaming retrieval.
- Python deps are in **user site** (`pip install --user --break-system-packages`);
  `python3 -m venv` fails here (`ensurepip` unavailable).
- No Docker in this WSL distro. Qdrant runs from the official static binary — see
  `docs/adr/0002`.
- No GPU passthrough (`nvidia-smi` absent). Treat as CPU-only.

## The corpus is a live, partly machine-written vault

It is not a static dataset. The vault carries its own `CLAUDE.md` ("AI Agent
Guide") describing layers that other tools write to continuously. Read it before
assuming anything about corpus stability.

Measured composition and churn:

```bash
$ find . -name '*.md' | wc -l                     # per top-level dir
ServiceNowOfficialDocs 51251   Notion 287   wiki 39
raw/inbox 23           Applications 14      raw/sessions 10   graphify 2

$ find . -name '*.md' -mtime -7 | cut -d/ -f2 | sort | uniq -c | sort -rn
     24 raw      9 Applications      4 wiki      3 ServiceNowOfficialDocs
```

**99.3% of the corpus is vendor documentation that barely moves; essentially all
churn is in the remaining 0.7%.** That is why content-hash incremental sync is the
right design — a nightly run touches ~40 files, not 51,588. It also means the
fast-changing material (`wiki/`, `Applications/`, `raw/`) is a rounding error by
file count while carrying most of the answers to "has this been solved before".

Which layers write themselves, and what it costs us:

| layer | written by | consequence for sn-rag |
|---|---|---|
| `wiki/` | an LLM (claude-memory-compiler), continuously | changes with no human action; the nightly sync is load-bearing, not a nicety |
| `raw/sessions/` | session hooks, automatically, daily | new file per day, **moved** to `raw/sessions/archive/` after ~30 days |
| `graphify/` | generator, regenerated wholesale | mass rewrites look like mass edits to the manifest |
| `ServiceNowOfficialDocs/`, `Notion/` | human, curated | effectively immutable |

`prune.py` **moves** archived session logs rather than deleting them. To the
manifest that is a delete at the old path plus an insert at the new one — correct
behaviour, but it shows up as churn. Do not mistake it for a bug.

Every top-level directory containing `.md` must appear in `SOURCE_BY_TOP_DIR` or
ingest fails loudly. Currently exact: 10 directories, 10 classifications, no gaps.
The vault gains layers over time, so a `ValueError` from `classify_source` after a
`git pull` means the vault grew, not that ingest broke.

`.smart-env/` is in `EXCLUDED_DIRS` deliberately — it is the Smart Connections
plugin's own embedding index. Indexing it would mean embedding an index.

### There are two semantic search systems over this one vault

The vault's guide points its agent pipeline (`ba-agent`, `architect`, `developer`)
at a `semantic_search` MCP tool backed by smart-connections, with `obsidian-cli`
as fallback. sn-rag is a second, independent stack over the same files.

- **smart-connections** reads an index the Obsidian plugin builds, so it requires
  the vault to have been **opened in Obsidian**.
- **sn-rag never needs Obsidian**, enforces caps in code, and has measured token
  and recall numbers behind it.

Neither supersedes the other on paper. Be explicit about which one produced an
answer, never mix them in a single response, and never cite one having queried the
other.

### `index.md` exclusion is the whole point, not a detail

The vault guide tells readers to "read `INDEX.md` first — do not scan directories
blindly". Those navigation files are exactly what sn-rag replaces:

```bash
$ find . -name 'index.md' | wc -l        # 54
$ ...                                    # 19.5 MB total
```

19.5 MB of link dumps, excluded on purpose. Re-including them would restore the
token-burning workflow this project exists to remove — the 78.5x measurement was
taken against precisely that baseline.

## Evidence rules — non-negotiable

These come from the build spec and are the reason this project is trustworthy.

1. **Every number must come with the command that produced it.** A throughput,
   recall, latency or token figure without its command is not evidence. Paste raw
   output into `docs/BUILD-LOG.md`.
2. **No mocked or random embeddings** outside a clearly named test double.
3. **No `TODO`, `pass`, `NotImplementedError`, or `...`** in committed non-test code.
4. **No `except: pass`**, and no handler that returns an empty success. If a
   backend fails, return a structured error.
5. **No retrieval-quality claim without an eval run** against `eval/golden.yaml`.
6. **Output caps in code, never in prompts.** A prompt instruction is a suggestion.
7. **No architectural change without an ADR** in `docs/adr/` recording rejected
   alternatives.
8. **No full-corpus index before the 500-doc sample gate passes.**

Tests passing is not sufficient evidence. Several real defects here were found by
inspecting actual output *after* a green suite — an empty payload field, a
breadcrumb off by one section, a lock that never engaged. When a test passes on the
first try, check that it could have failed.

## Phase discipline

All seven phases are built. A phase is complete only when every criterion has
pasted evidence in `BUILD-LOG.md`. The Phase 4 recall gate (hybrid+rerank
recall@10 >= 0.85) is currently **INCONCLUSIVE**, not passed — see Golden set
below. Phase 5 shipped on top of it deliberately, with that stated, rather than
by quietly declaring the gate passed on circular evidence.

## The MCP surface

Seven tools, all defined in `mcp_server/server.py`, all capped in
`mcp_server/caps.py`: `sn_search`, `sn_get_section`, `sn_outline`, `sn_lexical`,
`sn_research`, `sn_stats`, `sn_ingest`. Three agents scope every search:
`general`, `servicenow`, `personal` — an unknown name is `BAD_REQUEST`, never a
silent fallback.

`sn_ingest` is the only writer and the only real security boundary: it rejects
absolute paths, `..`, symlink escapes, null bytes, non-markdown, and writes into
the `official` vendor corpus. Never loosen those without an ADR.

**The surface is transport-dependent (ADR-0006).** Over stdio: seven tools. Over
HTTP: **six** — `sn_ingest` is not registered at all, so it is absent from
`tools/list` and a direct call returns `Unknown tool`. It is not
permission-checked, because an unadvertised tool is verifiable by reading the
tool list while a permission check is only as good as its implementation.
`register_tools()` in `server.py` is the single place this is decided, and
`main()` refuses to serve if a write tool ever appears on the HTTP surface.

HTTP mode refuses to start without `--bind` (no default; `0.0.0.0` rejected) or
without a `SN_RAG_TOKEN` of at least 16 characters.

**Qdrant and Ollama must be bound to `127.0.0.1`.** Qdrant has no authentication
and defaults to `0.0.0.0`; on 2026-08-05 the whole index was listable and
deletable from any host on the LAN. An authenticated MCP server in front of a
datastore that answers the network directly protects nothing:

```bash
ss -ltnp | grep -E '6333|11434'      # both must show 127.0.0.1
```

Full per-tool reference with caps and signatures lives in `README.md`.

**Both `README.md` and this file sit at the repo root; the code is in `sn-rag/`.**
Every command in either document is relative to `sn-rag/` — `cd sn-rag` first, or
`import config` fails and `pytest tests/` collects nothing.

## Things that will bite you

- **Length-sorted batching is load-bearing.** Batches pad to their longest member
  and this corpus mixes 200-char prose with 30,000-char code blocks. Do not
  "simplify" it away.
- **Benchmark the code path you ship, not a lookalike.** `bench_embed.py` called
  `embed()` once over a whole list and recorded 92.4 chunks/s; production looped
  per 8-text batch and sustained 6.7. That figure sat in the build log for three
  phases and funded an overnight plan that was never achievable. Real ceiling on
  this CPU is ~10-12 chunks/s.
- **A background job must not starve the foreground.** The full reindex took 11 of
  12 cores, Ollama could not get scheduled, and every `sn_research` call returned
  `PLANNER_UNAVAILABLE` for hours. Correct behaviour, useless system. `EMBED_THREADS`
  caps it at 6 — throughput plateaus there anyway, so it costs nothing.
- **Library defaults are not neutral.** `fastembed` caches ONNX models under
  `tempfile.gettempdir()` unless told otherwise, so ~300 MB re-downloaded after
  every reboot and nothing looked broken — just slow, occasionally. `cache_dir` is
  resolved inside `Embedder.__init__` / `Reranker.__init__` rather than threaded
  through the twelve call sites: one missed site would silently revert to the
  tempdir and reintroduce the bug invisibly.
- **Isolated measurements do not compose.** Dense alone 12.1 chunks/s, sparse alone
  5,628 — but dense+sparse through `encode()` is 8.8. Quoting the isolated pair
  would have overstated the ceiling by ~35%.
- **`--shuffle` when partially indexing.** An alphabetical prefix is almost
  entirely one product area; evaluating against it is misleading.
- **Content hash decides staleness, not indexing status.** Comparing desired
  status against stored status marks every indexed file as changed.
- **Upsert to Qdrant before committing the manifest.** The reverse leaves files
  marked indexed with no vectors — a silent recall hole. Surplus vectors are
  self-healing; missing ones are not.
- **`git pull` in the sync is best effort.** The corpus is a live Obsidian vault;
  a dirty tree is normal and must not abort the nightly run.
- **`index.md` files are excluded** — 500 KB–2 MB navigation dumps, not content.
- **Code fences and tables are atomic.** Never split them, even oversized. A
  bisected `GlideRecord` example is wrong, not merely large.
- **A non-empty buffer can still be blank.** `build_children`'s flush guarded on
  `if not buf`, so a whitespace-only remnant between sections became a chunk whose
  text was `"\n"` — 13,174 real vectors in the live index (2.97%), one of which
  ranked *first* for "incident management". Blank **parents** are left alone on
  purpose: they must tile the body verbatim, and they are unreachable because
  `sn_get_section` is only addressable through a child's `parent_id`.
- **A green suite is not evidence the data is sound.** That defect survived the
  entire build. It surfaced only because one test asserted on *content*
  (`hit.text.strip()`) instead of counts and status codes. Prefer at least one
  assertion per subsystem that would notice empty-but-well-formed output.

## Golden set

`eval/golden.yaml` is **human-authored and cannot be generated**. Questions written
by reading documents inherit their vocabulary, so recall comes out near-perfect and
measures nothing. Likewise, never choose `expected_rel_paths` from vector-search
output — that is circular. `scripts/golden.py find` uses filename + ripgrep only,
deliberately.

Every case carries a required `provenance: real | constructed | negative`.
**The Phase 4 gate scores `real` cases only**; below 20 of them `run_eval.py` exits
`2 = INCONCLUSIVE` rather than reporting a blended number. Current state: 3 real,
26 constructed. The constructed ones score **1.000** on `personal` — that perfect
score is the circularity, visible.

Do not "fix" this by lowering `MIN_REAL_CASES` or by writing more questions from
documents. The mining path (`scripts/mine_questions.py`) is exhausted: 247 user
messages across 18 transcripts yielded 5 domain questions, all already present.

See `docs/GOLDEN-SET-GUIDE.md`.

## Conventions

- Files under 500 lines.
- `ingest/chunker.py` is **pure** — no I/O, no network. Keep it that way; its
  property tests depend on it.
- `config.py` is the single source of truth for models, sizes, caps and budgets.
  No magic numbers elsewhere.
- Corpus vocabulary (`release`, `product`) goes in the generic `facets` dict, never
  as typed schema fields — see `docs/adr/0001`.
- Run `python3 -m pytest tests/ -q` after changes to ingest or retrieval.
