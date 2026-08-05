---
description: Search the second brain and answer only from cited results
argument-hint: <question>
allowed-tools: mcp__sn-rag__sn_search, mcp__sn-rag__sn_lexical, mcp__sn-rag__sn_get_section, mcp__sn-rag__sn_outline
---

Answer this question from the second brain, and **only** from what retrieval returns:

$ARGUMENTS

## How to retrieve

1. Call `sn_search` with the question. Pick the agent deliberately:
   - `servicenow` — vendor platform documentation
   - `personal` — the user's own notes, wiki and applications
   - `general` — both, or when unsure
2. If the question contains an exact identifier — an API name, table name, error
   string, property — also call `sn_lexical` with that literal string. Dense
   search paraphrases; ripgrep does not.
3. Expand a promising hit with `sn_get_section` using its `parent_id`. Use
   `sn_outline` when you need a document's shape before deciding what to read.

**Prefer `sn_search` over `sn_research`.** Measured 2026-08-05: `sn_research`
retrieves *worse* than plain search (recall 0.345 vs ~0.53) while costing 3-11
seconds. Use it only if plain search plus one lexical pass genuinely fails.

## How to answer

- **Every claim cites a `rel_path`.** If it did not come back from a tool, it
  does not go in the answer.
- **No filling gaps from general knowledge.** You know things about ServiceNow
  that are not in this corpus. Those do not belong here — the point is what *the
  vault* says. If your own knowledge contradicts a retrieved document, say so
  explicitly and cite the document.
- **Say when it is not there.** "The corpus does not answer this" is a correct
  and useful answer. An invented one is worse than nothing, because this exists
  to be trusted without re-checking.
- **Quote exactly** for code, API names and error strings. Never paraphrase an
  identifier.
- **Be brief.** Lead with the answer, then the citations. No preamble.

## Do not mix retrieval systems

This vault has a second, independent semantic search stack (smart-connections,
via the Obsidian plugin). Never blend results from both in one answer, and never
cite one as if the other had produced it. If you used sn-rag, say so.

## If retrieval fails

Report the structured error verbatim — `BACKEND_UNAVAILABLE`,
`PLANNER_UNAVAILABLE`, `BAD_REQUEST`. Do not answer from memory instead. A
plausible answer that bypassed a broken backend is the failure this system is
built to prevent.

Common causes worth naming: Qdrant not running
(`systemctl --user is-active qdrant.service`), or a Claude Code session older
than the last code change still holding stale modules — restart the session.
