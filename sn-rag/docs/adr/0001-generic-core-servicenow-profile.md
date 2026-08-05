# ADR-0001: Generic knowledge core, ServiceNow as first corpus profile

**Date:** 2026-08-04
**Status:** Accepted
**Phase:** Decided before Phase 2 (chunker), because Phase 2 fixes the metadata schema.

## Context

The build spec is written around the ServiceNow documentation corpus, and the
current corpus is 99.2% official ServiceNow docs (51,251 of 51,642 files, per
Phase 0 recon). But the vault also holds a Notion migration, a wiki layer,
custom-app notes, and Graphify code graphs — and the stated mission covers
"both the official docs corpus and the personal second brain."

Asked directly whether this is a ServiceNow tool or a second brain, the answer
is: a second brain whose first and currently largest corpus is ServiceNow docs.

Nothing in the architecture is domain-specific. The chunker, SQLite manifest,
hybrid dense/sparse retrieval, cross-encoder rerank, LangGraph agent loop, and
capped MCP tool surface are all corpus-agnostic. What *is* ServiceNow-specific:

1. `SOURCE_BY_TOP_DIR` — already config, already generic.
2. The `product_family` and `release` payload fields (§5.1) — ServiceNow
   vocabulary baked into the schema as typed fields.
3. `api_symbols` — framed as ServiceNow API extraction, but mechanically it is
   "code identifiers found in fenced blocks", which is language- and
   domain-agnostic.
4. The `sn_*` MCP tool names — cosmetic.

Item 2 is the one that is cheap to get right now and expensive to retrofit
after ~400k chunks are embedded and indexed with a fixed payload shape.

## Decision

Build a generic core with ServiceNow as the first **corpus profile**.

- The chunk payload carries a generic `facets: dict[str, str]` for arbitrary
  corpus-specific metadata, populated from source frontmatter.
- ServiceNow's `release` and `product`/`classification` frontmatter fields map
  into `facets` under a ServiceNow profile, rather than being typed top-level
  schema fields.
- `doc_type` and `source` stay typed and top-level — every corpus has a notion
  of "what kind of document is this" and "where did it come from".
- `api_symbols` is retained by name but defined generically: identifiers
  extracted from fenced code blocks (and, for reference-style docs, headings).
  It is not ServiceNow-aware.
- Qdrant payload indexes are created per-profile over the `facets` keys that a
  corpus actually uses, instead of over hard-coded `product_family`/`release`.

Deferred, not decided here: renaming the `sn_*` MCP tools. They are trivially
renameable before Phase 6 registration and nothing depends on the names yet.

## Consequences

- Adding a second corpus (personal notes, a different product's docs, a code
  repo) requires a profile entry in config, not a schema migration or a
  reindex.
- Slightly weaker typing: `facets` values are strings, so numeric or date
  range filters on corpus-specific fields need explicit handling.
- Filter queries become `facets.release = "australia"` rather than
  `release = "australia"`. Payload indexes must be declared per used facet key
  or filters on ~400k points will be slow (§5.1 already flags this).

## Rejected alternatives

- **Typed `product_family` / `release` columns as specified in §5.1.** Rejected:
  bakes one vendor's vocabulary into the storage schema of a system explicitly
  intended to also hold Notion notes, wiki pages, and code graphs, none of
  which have a "release".
- **Separate collections or separate deployments per corpus.** Rejected: already
  rejected as DECISION-3 (one collection, `source` filter). Splitting by corpus
  would also prevent cross-corpus retrieval, which is a core second-brain use
  case — asking one question and getting both the official answer and your own
  notes on it.
- **Defer genericization until a second corpus actually exists.** Rejected: the
  second corpus already exists (325 personal + 39 wiki + 25 custom-app + 2
  code-graph files), and the retrofit cost after a full-corpus embed is a
  complete reindex — the exact failure mode §13 flags as the top risk.
