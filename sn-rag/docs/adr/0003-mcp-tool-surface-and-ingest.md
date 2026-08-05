# ADR-0003: MCP tool surface, agent exposure, and the ingest tool

**Date:** 2026-08-04
**Status:** Accepted — implemented in Phase 6 (115 tests, all seven tools live-verified)
**Phase:** 6

## Context

The MCP server is the interface between Claude Code and everything built in Phases
1–5. It is also where the project's central claim is either delivered or lost: if
tools return large payloads, the token bill re-inflates and the system is a slower,
more complicated version of the workflow it replaced.

Two additions to the spec's original six tools are needed:

1. **The two search agents built in Phase 4** (`general`, `servicenow`, plus
   `personal`) must be reachable. The spec assumed a single `scope` string.
2. **An ingest tool**, requested directly: migrate a file into the vault, index and
   embed it, and complete within the same call.

## Decision

### Tool surface

Every cap is enforced **in code**, in `mcp_server/caps.py`, never by prompt
instruction. Every response carries `approx_tokens` and `citations`.

| tool | input | returns | hard cap |
|---|---|---|---|
| `sn_search` | `query`, `agent`, `filters{}`, `k<=8` | ranked `{chunk_id, parent_id, rel_path, h_path, score, snippet}` | 8 results, 150 words each, **6,000 chars** |
| `sn_get_section` | `parent_id` | one parent chunk verbatim | **8,000 chars**, explicit `[TRUNCATED]` |
| `sn_outline` | `rel_path` | header tree, no body | **3,000 chars** |
| `sn_lexical` | `pattern`, `agent` | ripgrep hits `rel_path:line` ±2 lines | 20 hits, **4,000 chars** |
| `sn_research` | `question`, `agent`, `budget` | compressed brief + `Gaps` + `Sources` | **900 words** |
| `sn_stats` | — | index health, drift, last sync | **500 chars** |
| `sn_ingest` | see below | ingest receipt | **1,000 chars** |

`scope` becomes **`agent`** (`general` | `servicenow` | `personal`), naming the
Phase 4 profiles directly rather than re-deriving scoping inside the MCP layer.
Default `general`. An unknown agent is a structured error listing valid values, not
a silent fallback to `general` — silently searching the wrong corpus subset returns
confidently wrong results.

Unchanged from the spec: no tool returns a whole file; `sn_research` never returns
raw chunks, only the compressed brief; failures return
`{error_code, message, retryable}`, never prose apologies or empty successes.

### `sn_ingest`

```jsonc
{
  "source_path":  "/abs/path/to/file.md",   // or "content" + "filename"
  "content":      "...",                     // alternative to source_path
  "filename":     "note.md",                 // required with content
  "dest":         "raw/inbox/note.md",       // vault-relative; default by source class
  "source_class": "personal",                // official|personal|wiki|custom-app|code-graph
  "facets":       {"tags": ["servicenow"]},  // optional frontmatter facets
  "overwrite":    false
}
```

Synchronous pipeline, completing before the call returns:

1. **Validate** — resolve `dest` against the vault root and reject any path that
   escapes it, is absolute, or contains `..`. Reject non-markdown extensions and
   content over a size ceiling.
2. **Collision check** — refuse an existing `dest` unless `overwrite: true`.
3. **Write** into the vault; add minimal frontmatter if absent.
4. **Manifest** — hash, classify, upsert as `pending`.
5. **Chunk + embed + upsert** to Qdrant, reusing `index.py`'s single-file path.
6. **Mark indexed**, return a receipt.

Receipt: `{rel_path, source, chunk_count, parent_count, indexed_at, approx_tokens}`.

**Synchronous is the right call here** despite being the slower option: a
fire-and-forget ingest that returns before indexing means the very next `sn_search`
misses the document that was just added, which reads as a bug and is untestable
from the caller's side. Measured single-file cost is ~13s for a large API-reference
document (139 chunks) and well under 2s for a typical note — acceptable inside one
tool call. Files projected to exceed a chunk ceiling return
`INGEST_TOO_LARGE` with a pointer to the batch CLI rather than blocking the call.

`sn_ingest` takes the same exclusive index lock as `index.py`. If the nightly sync
is mid-run it returns `INDEX_BUSY, retryable: true` rather than corrupting the
manifest.

### Writing to the vault is a side effect

`sn_ingest` is the only tool that mutates state. It therefore:

- never writes outside the configured vault root;
- never overwrites without explicit `overwrite: true`;
- never deletes (removal stays a Phase 7 CLI concern);
- logs every write with its resulting `rel_path` and chunk count.

## Consequences

- Claude Code can file a note and immediately retrieve it, closing the capture loop
  that Obsidian previously owned.
- The MCP layer gains a write path, so path traversal and overwrite become real
  security concerns rather than theoretical ones. Hence the validation in step 1.
- `sn_ingest` competes with the nightly sync for the index lock; both must handle
  contention explicitly.
- Exposing three agents means the caller can pick the wrong one. Mitigated by
  `sn_stats` reporting per-source counts and by tool descriptions stating clearly
  what each agent covers.

## Rejected alternatives

- **Asynchronous ingest returning a job id.** Rejected: a document that is not yet
  searchable when the call returns is indistinguishable from a failed ingest, and
  it forces a polling protocol into every caller for a sub-second-to-15-second
  operation.
- **Keeping the spec's single `scope` string.** Rejected: it would re-implement
  source filtering inside the MCP layer, duplicating the profile logic already
  built and tested in Phase 4, and would leave the two requested agents unreachable
  by name.
- **Letting `sn_ingest` write anywhere on disk.** Rejected outright — an
  LLM-invokable arbitrary-file-write tool is a serious hazard. Confined to the
  vault root with traversal rejection.
- **Deferring ingest to Phase 7 with the rest of the sync work.** Rejected: it was
  explicitly requested, and it is what makes the vault the capture surface rather
  than a read-only mirror.
- **Auto-committing ingested files to git.** Rejected for v1: it couples a
  retrieval tool to the user's version-control state and would produce commits
  nobody reviewed. The nightly sync already tolerates a dirty tree.
