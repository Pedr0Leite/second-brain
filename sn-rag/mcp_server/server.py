"""MCP server: the interface between Claude Code and the retrieval stack.

This layer is where the project's cost claim is delivered or lost. Every tool
output is capped in `caps.py` — in code, not by asking a model nicely. No tool
returns a whole file; reading one is an explicit escalation Claude performs
itself with the `rel_path` a search returned.

Run:  python3 mcp_server/server.py            # stdio
      python3 mcp_server/server.py --http     # streamable HTTP on :8079
"""
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, VAULT_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, EMBED_BATCH_SIZE, RERANK_MODEL, RERANK_CANDIDATES,
                    MANIFEST_DB_PATH, SEARCH_HNSW_EF, PLANNER_BASE_URL, PLANNER_MODEL)
from mcp_server import caps
from mcp_server.ingest_tool import IngestError, ingest as do_ingest

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="sn-rag",
    instructions=(
        "Local retrieval over a ServiceNow documentation corpus and a personal second brain.\n"
        "Pick the agent deliberately:\n"
        "  general    - everything (official docs + personal notes, wiki, apps, code graphs)\n"
        "  servicenow - official vendor documentation only\n"
        "  personal   - your own notes/wiki/apps only\n"
        "Start with sn_search. Use sn_get_section to expand one result, sn_outline to see a\n"
        "document's structure, and sn_lexical for exact API symbols. No tool returns a whole\n"
        "file: read it yourself with the rel_path if you genuinely need all of it."
    ),
)

_state: dict = {}


def _lazy():
    """Build models and clients on first use, not at import.

    Loading two embedding models plus a cross-encoder takes seconds; doing it at
    import would make the server appear to hang during MCP handshake.
    """
    if _state:
        return _state
    from qdrant_client import QdrantClient
    from ingest.embed import Embedder
    from retrieval.rerank import Reranker
    from retrieval.profiles import build_agents

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    _state.update(
        client=client,
        embedder=embedder,
        agents=build_agents(client, QDRANT_COLLECTION, embedder, Reranker(RERANK_MODEL),
                            CORPUS_PATH, hnsw_ef=SEARCH_HNSW_EF),
    )
    return _state


def _agent(name: str):
    from retrieval.profiles import PROFILES
    if name not in PROFILES:
        # Never silently fall back to 'general': searching the wrong subset of
        # the corpus returns confidently wrong results.
        raise ValueError(f"unknown agent {name!r}; valid: {sorted(PROFILES)}")
    return _lazy()["agents"][name]


def _citation(hit) -> dict:
    return {"rel_path": hit.rel_path, "h_path": hit.h_path}


@server.tool(
    description="Search the knowledge base. Returns ranked, capped snippets with citations. "
                "agent: general | servicenow | personal.")
def sn_search(query: str, agent: str = "general", k: int = 8,
              release: Optional[str] = None, product: Optional[str] = None,
              doc_type: Optional[str] = None) -> dict:
    cap = caps.cap_for("sn_search")
    try:
        ag = _agent(agent)
        facets = {kk: vv for kk, vv in (("release", release), ("product", product)) if vv}
        result = ag.search(query, k=min(k, cap["max_results"]), candidates=RERANK_CANDIDATES,
                           facets=facets or None,
                           doc_types=[doc_type] if doc_type else None)
    except ValueError as exc:
        return caps.error("BAD_REQUEST", str(exc))
    except Exception as exc:
        return caps.error("BACKEND_UNAVAILABLE", f"{type(exc).__name__}: {exc}", retryable=True)

    items = [{"chunk_id": h.chunk_id, "parent_id": h.parent_id, "rel_path": h.rel_path,
              "h_path": h.h_path, "source": h.source, "score": round(h.score, 4),
              "snippet": h.text} for h in result.hits]
    kept, meta = caps.cap_result_list(items, "snippet", cap["max_results"],
                                      cap["max_words_per_result"], cap["max_chars_total"])
    return caps.ok({"agent": agent, "results": kept,
                    "citations": [_citation(h) for h in result.hits[:len(kept)]],
                    "used_lexical": result.used_lexical, **meta})


@server.tool(description="Return one parent section verbatim, by parent_id from sn_search.")
def sn_get_section(parent_id: str) -> dict:
    cap = caps.cap_for("sn_get_section")
    from retrieval.parents import ParentStore
    try:
        parent = ParentStore(MANIFEST_DB_PATH).get(parent_id)
    except Exception as exc:
        return caps.error("BACKEND_UNAVAILABLE", f"{type(exc).__name__}: {exc}", retryable=True)
    if parent is None:
        return caps.error("NOT_FOUND", f"no section with parent_id {parent_id}")
    text, truncated = caps.truncate_chars(parent["text"], cap["max_chars"])
    return caps.ok({"rel_path": parent["rel_path"], "h_path": parent["h_path"],
                    "text": text, "truncated": truncated,
                    "citations": [{"rel_path": parent["rel_path"], "h_path": parent["h_path"]}]})


@server.tool(description="Header tree for one document. Structure only, no body text.")
def sn_outline(rel_path: str) -> dict:
    cap = caps.cap_for("sn_outline")
    from retrieval.parents import ParentStore
    try:
        rows = ParentStore(MANIFEST_DB_PATH).outline(rel_path)
    except Exception as exc:
        return caps.error("BACKEND_UNAVAILABLE", f"{type(exc).__name__}: {exc}", retryable=True)
    if not rows:
        return caps.error("NOT_FOUND", f"{rel_path} is not indexed (or has no sections)")
    body = "\n".join(f"{r['parent_idx']:>3}  {r['h_path']}" for r in rows)
    text, truncated = caps.truncate_chars(body, cap["max_chars"])
    return caps.ok({"rel_path": rel_path, "sections": len(rows),
                    "outline": text, "truncated": truncated,
                    "parent_ids": [r["parent_id"] for r in rows][:40]})


@server.tool(description="Exact-match search (ripgrep) for API symbols and identifiers.")
def sn_lexical(pattern: str, agent: str = "general", fixed_string: bool = True) -> dict:
    cap = caps.cap_for("sn_lexical")
    from retrieval.lexical import LexicalSearcher
    from retrieval.profiles import PROFILES, OFFICIAL_SOURCES
    if agent not in PROFILES:
        return caps.error("BAD_REQUEST", f"unknown agent {agent!r}; valid: {sorted(PROFILES)}")
    if len(pattern) > 500:
        return caps.error("BAD_REQUEST", f"pattern too long ({len(pattern)} chars, max 500)")
    lex = LexicalSearcher(CORPUS_PATH)
    if not lex.available:
        return caps.error("BACKEND_UNAVAILABLE", "ripgrep (rg) is not installed", retryable=False)
    subdirs = None
    sources = PROFILES[agent].sources
    if sources == OFFICIAL_SOURCES:
        subdirs = ["ServiceNowOfficialDocs"]
    try:
        hits = lex.search(pattern, max_hits=cap["max_hits"], subdirs=subdirs,
                          fixed_string=fixed_string)
    except RuntimeError as exc:
        return caps.error("LEXICAL_FAILED", str(exc))
    items = [{"rel_path": h.rel_path, "line": h.line_no, "text": h.line} for h in hits]
    kept, meta = caps.cap_result_list(items, "text", cap["max_hits"], 40, cap["max_chars_total"])
    # Citations are file-level and DEDUPED. ripgrep routinely returns several hits
    # per file, and this corpus has 100+ char paths, so one citation per hit
    # repeated the same string verbatim — 15 hits over 9 files spent ~40% of the
    # response on duplicates. There is also no h_path to report: ripgrep yields
    # line numbers, not the header tree, and emitting "h_path": "" per hit paid
    # tokens for a field that never carried information. Use sn_search when the
    # section path matters.
    seen: set[str] = set()
    citations = [{"rel_path": i["rel_path"]} for i in kept
                 if not (i["rel_path"] in seen or seen.add(i["rel_path"]))]
    return caps.ok({"pattern": pattern, "agent": agent, "hits": kept,
                    "citations": citations, **meta})


@server.tool(description="Compressed research brief from the local agent loop. "
                         "Requires the planner route.")
def sn_research(question: str, agent: str = "general", budget: int = 6) -> dict:
    # Spec §3: if the planner is unavailable this returns a structured error and
    # MUST NOT fall back to an expensive route. A silent failover would destroy
    # the cost boundary the whole project exists to create.
    if not PLANNER_BASE_URL or not PLANNER_MODEL:
        return caps.error(
            "PLANNER_UNAVAILABLE",
            "No local planner route is configured (PLANNER_BASE_URL / PLANNER_MODEL unset). "
            "sn_research needs the Phase 5 agent loop. Use sn_search + sn_get_section instead; "
            "there is deliberately no fallback to a paid model.",
            retryable=False)

    from agent.planner import PlannerUnavailable
    from agent.research import research

    cap = caps.cap_for("sn_research")
    state = _lazy()
    if agent not in state.agents:
        return caps.error("BAD_REQUEST",
                          f"unknown agent {agent!r}; valid: {sorted(state.agents)}")
    try:
        brief = research(question, state.agents, budget=budget,
                         agent_override=agent if agent != "general" else None,
                         candidates=RERANK_CANDIDATES)
    except PlannerUnavailable as exc:
        # Retryable: the local route may simply be down, and the correct
        # response is to start it — never to reach for a hosted model.
        return caps.error("PLANNER_UNAVAILABLE", str(exc), retryable=True)

    # Evidence, not prose. The word cap bounds the excerpts Claude receives;
    # synthesis happens in Claude, per ADR-0004.
    items = [{"rel_path": e.rel_path, "h_path": e.h_path, "parent_id": e.parent_id,
              "from_query": e.from_query, "score": round(e.score, 4),
              "evidence": e.text} for e in brief.evidence]
    # max_words is the whole-brief budget; divide it across the items actually
    # returned so the total holds regardless of `budget`.
    per_item_words = max(40, cap["max_words"] // max(1, min(budget, cap["max_results"])))
    kept, meta = caps.cap_result_list(items, "evidence", min(budget, cap["max_results"]),
                                      per_item_words, cap["max_chars_total"])
    return caps.ok({
        "question": question, "agent": brief.agent, "queries": brief.queries,
        "evidence": kept,
        "citations": [{"rel_path": i["rel_path"], "h_path": i["h_path"]} for i in kept],
        "trace": brief.trace,
        "elapsed_s": round(brief.elapsed_s, 2),
        "note": "Selected evidence, not an answer — synthesis is the caller's job (ADR-0004).",
        **meta})


@server.tool(description="Index health: document and chunk counts, sources, drift.")
def sn_stats() -> dict:
    cap = caps.cap_for("sn_stats")
    from ingest import manifest
    try:
        with manifest.connect(MANIFEST_DB_PATH) as conn:
            by_status = manifest.count_by_status(conn)
            by_source = manifest.count_by_source(conn)
            chunk_sum = manifest.total_chunk_count(conn)
        points = _lazy()["client"].count(QDRANT_COLLECTION, exact=True).count
    except Exception as exc:
        return caps.error("BACKEND_UNAVAILABLE", f"{type(exc).__name__}: {exc}", retryable=True)
    drift = points - chunk_sum
    summary = (f"files={by_status} sources={by_source} chunks={chunk_sum} "
               f"points={points} drift={drift}")
    text, _ = caps.truncate_chars(summary, cap["max_chars"])
    return caps.ok({"summary": text, "indexed_chunks": chunk_sum,
                    "qdrant_points": points, "drift": drift, "consistent": drift == 0})


# NOT decorated: registered conditionally at the bottom of this file, and only
# for stdio. ADR-0006 keeps the single writing tool off the network surface
# entirely rather than guarding it with a permission check — an unadvertised
# tool cannot be called, which is verifiable by reading `tools/list`.
def sn_ingest(source_path: Optional[str] = None, content: Optional[str] = None,
              filename: Optional[str] = None, dest: Optional[str] = None,
              source_class: str = "personal", overwrite: bool = False) -> dict:
    cap = caps.cap_for("sn_ingest")
    try:
        state = _lazy()
    except Exception as exc:
        return caps.error("BACKEND_UNAVAILABLE", f"{type(exc).__name__}: {exc}", retryable=True)

    # Share the indexer's lock: a nightly sync mid-run must not interleave.
    try:
        from ingest.index import _acquire_index_lock
        lock = _acquire_index_lock()
    except SystemExit:
        return caps.error("INDEX_BUSY", "an indexing run is in progress; retry shortly",
                          retryable=True)
    try:
        receipt = do_ingest(state["client"], state["embedder"], source_path=source_path,
                            content=content, filename=filename, dest=dest,
                            source_class=source_class, overwrite=overwrite)
    except IngestError as exc:
        return caps.error(exc.code, exc.message, retryable=exc.retryable)
    except Exception as exc:
        return caps.error("INGEST_FAILED", f"{type(exc).__name__}: {exc}", retryable=False)
    finally:
        lock.close()

    text, _ = caps.truncate_chars(str(receipt), cap["max_chars"])
    return caps.ok({**receipt, "summary": text})


WRITE_TOOLS = ("sn_ingest",)


def register_tools(transport: str) -> list[str]:
    """Register the tool surface for a transport and return the tool names.

    Read tools are registered by decorator at import. The writer is added here,
    for stdio only. Returning the names lets a test assert on the surface
    without starting a server.
    """
    if transport == "stdio":
        server.tool(
            description="Migrate a file into the vault, index and embed it. Completes "
                        "before returning, so the document is immediately searchable."
        )(sn_ingest)
    return sorted(t.name for t in server._tool_manager.list_tools())


def main(argv: list[str]) -> int:
    from mcp_server import http_serve

    try:
        cfg = http_serve.parse_serve_args(argv)
    except http_serve.ConfigError as exc:
        print(f"sn-rag: {exc}", file=sys.stderr)
        return 2

    if cfg.transport == "stdio":
        register_tools("stdio")
        server.run()
        return 0

    # HTTP: token is resolved BEFORE the socket is opened, so a missing token can
    # never result in a running-but-open server.
    try:
        token = http_serve.require_token()
    except http_serve.ConfigError as exc:
        print(f"sn-rag: {exc}", file=sys.stderr)
        return 2

    names = register_tools("http")
    if any(w in names for w in WRITE_TOOLS):
        # Defence in depth: if a future edit decorates sn_ingest again, refuse to
        # serve rather than quietly publishing a writer to the network.
        print(f"sn-rag: refusing to serve write tools over HTTP: {names}", file=sys.stderr)
        return 2

    import logging
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = http_serve.build_allowed_hosts(cfg.host, cfg.port)
    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts)
    )
    app.add_middleware(http_serve.build_auth_middleware(token))

    print(f"sn-rag: HTTP on {cfg.host}:{cfg.port}, {len(names)} tools, auth required, "
          f"allowed hosts: {allowed_hosts}", file=sys.stderr)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
