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
# /home/pedro/vaults/obsidian-servicenow-docs
# /home/pedro/.local/state/sn-rag/manifest.db
```

Current layout — **both on ext4, deliberately**:

| what | where | why |
|---|---|---|
| corpus | `~/vaults/obsidian-servicenow-docs` | lexical search 342x faster than `/mnt/c` |
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

Full per-tool reference with caps and signatures lives in `README.md`.

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
