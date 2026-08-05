"""Single source of truth: paths, chunk sizes, caps, budgets."""
import os
from pathlib import Path

# Defaults anchor to THIS file's directory, never the working directory. The MCP
# server is spawned by Claude Code with an arbitrary cwd, and cwd-relative
# defaults resolved to a manifest that did not exist — sqlite would then create
# an empty one, so sn_get_section and sn_outline returned nothing while
# reporting success. A path default that depends on cwd is a silent-failure bug.
_ROOT = Path(__file__).resolve().parent

# Candidate corpus locations, first existing wins. The sibling-of-repo layout was
# the original; the corpus now lives on a local filesystem because lexical search
# over a Windows 9p mount measured 342x slower (see docs/BUILD-LOG.md). Listing
# both keeps a fresh checkout working without forcing an env var, while an
# explicit CORPUS_PATH still overrides everything.
_CORPUS_CANDIDATES = (
    Path.home() / "vaults" / "obsidian-servicenow-docs",
    _ROOT.parent.parent / "obsidian-servicenow-docs",
)


def _default_corpus() -> Path:
    for candidate in _CORPUS_CANDIDATES:
        if candidate.is_dir():
            return candidate
    # Falling back to the first candidate rather than raising: config.py is
    # imported by tooling that must not crash just because the corpus is absent.
    # Tests assert the resolved path exists, so a wrong path fails loudly there
    # instead of silently producing empty results.
    return _CORPUS_CANDIDATES[0]


CORPUS_PATH = Path(os.environ.get("CORPUS_PATH") or _default_corpus()).resolve()
# The manifest is a write-heavy SQLite database, so it must not live on a 9p
# Windows mount. Measured over one 50-file indexing window:
#     manifest on /mnt/c   0.97s
#     manifest on ext4     0.04s     -> 24x
# Small next to embedding (97% of the window) but free to fix, and it also keeps
# synchronous commits off a filesystem where fsync is expensive.
_MANIFEST_CANDIDATES = (
    Path.home() / ".local" / "state" / "sn-rag" / "manifest.db",
    _ROOT / "manifest.db",
)


def _default_manifest() -> Path:
    for candidate in _MANIFEST_CANDIDATES:
        if candidate.is_file():
            return candidate
    return _MANIFEST_CANDIDATES[0]


MANIFEST_DB_PATH = Path(os.environ.get("MANIFEST_DB_PATH") or _default_manifest()).resolve()

# Deterministic source classification: top-level dir -> source class.
# Every top-level dir containing .md files in the corpus MUST appear here,
# or normalize.py fails loudly instead of silently skipping files.
SOURCE_BY_TOP_DIR = {
    "ServiceNowOfficialDocs": "official",
    "Notion": "personal",
    "wiki": "wiki",
    "raw": "personal",
    "Applications": "custom-app",
    "ClaudeAgents": "custom-app",
    "ClaudeSkills": "custom-app",
    "graphify": "code-graph",
    "Dashboards": "personal",
    "Clippings": "personal",
}
# Loose files directly at corpus root (no subdirectory) classify as personal.
ROOT_FILES_SOURCE = "personal"

# Directories never walked for content, even though they may contain files
# matching other patterns (plugin data, git internals, editor config).
EXCLUDED_DIRS = {".git", ".obsidian", ".smart-env", ".claude"}

# Per-category navigation/link-dump files: pure link lists, not retrievable
# content (verified 500KB-2MB, e.g. ServiceNowOfficialDocs/*/index.md).
EXCLUDED_FILENAMES = {"index.md"}

# Chunking (Phase 2)
PARENT_CHUNK_MIN_CHARS = 2000
PARENT_CHUNK_MAX_CHARS = 4000
CHILD_CHUNK_CHARS = 500
CHILD_CHUNK_OVERLAP = 100

# MCP tool output caps (§6, enforced in code — not prompt instructions)
CAPS = {
    "sn_search": {"max_results": 8, "max_words_per_result": 150, "max_chars_total": 6000},
    "sn_get_section": {"max_chars": 8000},
    "sn_outline": {"max_chars": 3000},
    "sn_lexical": {"max_hits": 20, "max_chars_total": 4000},
    # 900 words is the budget for the WHOLE brief, not per item. Passing it as a
    # per-item limit would emit 900 x budget words. Per-item is derived so the
    # two can never drift apart.
    "sn_research": {"max_words": 900, "max_results": 6, "max_chars_total": 6000},
    "sn_stats": {"max_chars": 500},
    "sn_ingest": {"max_chars": 1000},
}

# Vault ingest (sn_ingest). Writes are confined to this root; see ADR-0003.
VAULT_PATH = Path(os.environ.get("VAULT_PATH", str(CORPUS_PATH))).resolve()
INGEST_DEFAULT_DIR = os.environ.get("INGEST_DEFAULT_DIR", "raw/inbox")
INGEST_MAX_BYTES = int(os.environ.get("INGEST_MAX_BYTES", str(2 * 1024 * 1024)))
INGEST_MAX_CHUNKS = int(os.environ.get("INGEST_MAX_CHUNKS", "400"))
INGEST_ALLOWED_SUFFIXES = (".md", ".markdown", ".txt")

# Agent planner route (Phase 5). Local only — a hosted route here would defeat
# the entire project, so this must never be pointed at a paid endpoint.
#
# Defaults are set rather than left empty because the MCP server is spawned by
# Claude Code with no environment: an env-only planner config would mean
# sn_research returned PLANNER_UNAVAILABLE forever in real use while working
# fine in a developer shell. Configuration presence is NOT availability —
# reachability is checked at call time, and a dead Ollama still yields a
# structured PLANNER_UNAVAILABLE.
PLANNER_BASE_URL = os.environ.get("PLANNER_BASE_URL", "http://localhost:11434/v1")
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "qwen2.5:3b-instruct")

# The planner emits short structured JSON only — never prose. Measured at 20.7
# tok/s on this CPU, a 900-word local answer would take ~58s against a 15s p95
# budget. Synthesis is Claude's job. See docs/adr/0004.
PLANNER_MAX_TOKENS = int(os.environ.get("PLANNER_MAX_TOKENS", "256"))
PLANNER_TIMEOUT_SECONDS = int(os.environ.get("PLANNER_TIMEOUT_SECONDS", "30"))

# Where fastembed stores downloaded ONNX models (~300 MB: dense + sparse +
# reranker). fastembed's own default is a directory under tempfile.gettempdir(),
# i.e. /tmp/fastembed_cache — which this system clears on reboot, so every
# restart re-downloaded all three models before the first query or index batch
# could run. Pinned under ~/.cache so the download happens once.
MODEL_CACHE_PATH = Path(
    os.environ.get("MODEL_CACHE_PATH") or Path.home() / ".cache" / "fastembed"
).resolve()

# Embedding / vector store (Phase 3)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "knowledge")
DENSE_MODEL = os.environ.get("DENSE_MODEL", "BAAI/bge-base-en-v1.5")
SPARSE_MODEL = os.environ.get("SPARSE_MODEL", "Qdrant/bm25")
# Re-measured 2026-08-04 on real corpus chunks, once Embedder.encode stopped
# looping batch-by-batch and handed fastembed the whole length-sorted list:
#
#     batch  8   8.0 chunks/s
#     batch 32  12.1 chunks/s   <- chosen
#     batch 64   8.9 chunks/s
#
# 8 was carried over from a benchmark that measured a different code path (one
# embed() call over a whole list) and reported 92.4 chunks/s — a figure the
# production pipeline never came close to. Padding waste still argues against
# very large batches, which is why 64 is worse than 32; the length sort is what
# keeps 32 viable at all.
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))

# Cap embedding threads so a long reindex does not starve interactive retrieval.
# Measured: throughput plateaus at 6 threads (10.2 chunks/s) and 12 is no better
# (10.0), so this costs nothing. Left unpinned, the reindex took every core and
# the local planner timed out — sn_research returned PLANNER_UNAVAILABLE for the
# entire run. Correct behaviour, useless system.
EMBED_THREADS = int(os.environ.get("EMBED_THREADS", "6"))
# Files per Qdrant upsert round-trip.
UPSERT_FILE_BATCH = int(os.environ.get("UPSERT_FILE_BATCH", "25"))

# Retrieval (Phase 4)
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
# 50, chosen from a measured sweep (30/50/100/200) — see docs/BUILD-LOG.md.
# recall@10 is FLAT across all depths; 30 -> 50 buys recall@5 (0.727 -> 0.818)
# and MRR (0.448 -> 0.629) on the servicenow profile. Beyond 50 buys nothing
# and costs latency: p95 883ms @30, 1450ms @50, 2286ms @100, 4370ms @200.
RERANK_CANDIDATES = int(os.environ.get("RERANK_CANDIDATES", "50"))
RERANK_TOP_K = int(os.environ.get("RERANK_TOP_K", "8"))

# Evaluation must use exact search. With approximate HNSW, near-tied scores are
# ordered differently between identical runs and recall swings a whole case
# (servicenow measured at both 0.818 and 0.909 on the same config), which is
# larger than most effects worth measuring.
EVAL_EXACT_SEARCH = os.environ.get("EVAL_EXACT_SEARCH", "1") == "1"
# Production uses approximate search; raise ef to reduce ordering instability.
SEARCH_HNSW_EF = int(os.environ.get("SEARCH_HNSW_EF", "128"))

# Agent budgets (Phase 5)
MAX_TOOL_CALLS = 12
MAX_ITERATIONS = 6
