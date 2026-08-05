"""Phase 4 retrieval tests. Structural, not content-dependent: they run against
a partially-built index, so they assert plumbing and policy rather than recall.
Recall is the eval harness's job (eval/run_eval.py against golden.yaml)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, EMBED_BATCH_SIZE, RERANK_MODEL)
from retrieval.lexical import LexicalSearcher, looks_code_like, extract_symbols
from retrieval.profiles import (PROFILES, GENERAL, SERVICENOW, PERSONAL,
                                OFFICIAL_SOURCES, build_agents)


@pytest.fixture(scope="module")
def client():
    from qdrant_client import QdrantClient
    c = QdrantClient(url=QDRANT_URL, timeout=60)
    if not c.collection_exists(QDRANT_COLLECTION):
        pytest.skip("collection not built yet")
    if c.count(QDRANT_COLLECTION, exact=True).count == 0:
        pytest.skip("collection is empty")
    return c


@pytest.fixture(scope="module")
def agents(client):
    from ingest.embed import Embedder
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    return build_agents(client, QDRANT_COLLECTION, embedder, None, CORPUS_PATH)


@pytest.fixture(scope="module")
def reranking_agents(client):
    from ingest.embed import Embedder
    from retrieval.rerank import Reranker
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    return build_agents(client, QDRANT_COLLECTION, embedder, Reranker(RERANK_MODEL), CORPUS_PATH)


# --- code-like query routing (pure, no index needed) ------------------------

@pytest.mark.parametrize("query", [
    "GlideRecord.addQuery",
    "gs.getUserID()",
    "sys_user_grmember table",
    "how do I use g_form.setValue",
    "GlideAjax",
])
def test_code_like_queries_detected(query):
    assert looks_code_like(query)


@pytest.mark.parametrize("query", [
    "how do I approve a change request",
    "what is the difference between a catalog item and a record producer",
    "explain incident escalation",
])
def test_prose_queries_not_code_like(query):
    assert not looks_code_like(query)


def test_extract_symbols():
    assert "gr.addQuery" in extract_symbols("call gr.addQuery on the record")
    assert "sys_user" in extract_symbols("query the sys_user table")


# --- lexical search ---------------------------------------------------------

def test_lexical_finds_known_symbol():
    lex = LexicalSearcher(CORPUS_PATH)
    if not lex.available:
        pytest.skip("ripgrep not installed")
    hits = lex.search("g_form.addChoice", max_hits=5, fixed_string=True)
    assert hits, "expected at least one hit for a symbol known to exist in the corpus"
    assert all(h.rel_path.endswith(".md") for h in hits)
    assert all(h.line_no > 0 for h in hits)


def test_lexical_no_match_returns_empty_not_error():
    lex = LexicalSearcher(CORPUS_PATH)
    if not lex.available:
        pytest.skip("ripgrep not installed")
    assert lex.search("zzz_definitely_not_in_corpus_zzz", fixed_string=True) == []


def test_lexical_missing_binary_raises():
    lex = LexicalSearcher(CORPUS_PATH, rg_binary="rg-does-not-exist")
    assert not lex.available
    with pytest.raises(RuntimeError):
        lex.search("anything")


# --- profiles ---------------------------------------------------------------

def test_three_profiles_registered():
    assert set(PROFILES) == {"general", "servicenow", "personal"}


def test_servicenow_profile_is_official_only():
    assert SERVICENOW.sources == OFFICIAL_SOURCES
    assert "official" not in (PERSONAL.sources or ())
    assert GENERAL.sources is None  # no source filter at all


def test_search_returns_populated_hits(agents):
    result = agents["general"].search("incident management", k=5, rerank=False)
    assert result.hits, "no hits from a general query"
    top = result.hits[0]
    assert top.rel_path and top.rel_path.endswith(".md")
    assert top.text.strip(), "payload text missing — snippets would be empty"
    assert top.source in {"official", "personal", "wiki", "custom-app", "code-graph"}


def test_servicenow_agent_returns_only_official(agents):
    result = agents["servicenow"].search("incident management", k=8, rerank=False)
    if not result.hits:
        pytest.skip("no official docs indexed yet")
    assert {h.source for h in result.hits} == {"official"}


def test_personal_agent_excludes_official(agents):
    result = agents["personal"].search("servicenow notes", k=8, rerank=False)
    if not result.hits:
        pytest.skip("no personal docs indexed yet")
    assert "official" not in {h.source for h in result.hits}


def test_profile_rejects_unsupported_facet(agents):
    with pytest.raises(ValueError, match="does not support facets"):
        agents["personal"].search("anything", facets={"release": "australia"})


def test_servicenow_accepts_release_facet(agents):
    result = agents["servicenow"].search("release notes", k=5,
                                         facets={"release": "australia"}, rerank=False)
    for hit in result.hits:
        assert hit.facets.get("release") == "australia"


def test_k_limit_respected(agents):
    result = agents["general"].search("configuration", k=3, rerank=False)
    assert len(result.hits) <= 3


def test_parent_dedupe_keeps_one_chunk_per_parent(agents):
    result = agents["general"].search("incident", k=8, rerank=False, dedupe_parents=True)
    parent_ids = [h.parent_id for h in result.hits]
    assert len(parent_ids) == len(set(parent_ids))


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid"])
def test_all_retrieval_modes_work(agents, mode):
    result = agents["general"].search("change request approval", k=5, mode=mode, rerank=False)
    assert result.mode == mode
    assert isinstance(result.hits, list)


def test_unknown_mode_raises(agents):
    with pytest.raises(ValueError, match="unknown search mode"):
        agents["general"].search("x", mode="telepathy")


def test_rerank_reorders_and_caps(reranking_agents):
    agent = reranking_agents["general"]
    plain = agent.search("how do I create an incident", k=8, candidates=30, rerank=False)
    ranked = agent.search("how do I create an incident", k=8, candidates=30, rerank=True)
    assert len(ranked.hits) <= 8
    if len(plain.hits) >= 5:
        assert ranked.candidates_considered >= len(ranked.hits)


def test_code_query_triggers_lexical_path(reranking_agents):
    result = reranking_agents["servicenow"].search("g_form.addChoice", k=8)
    assert result.used_lexical, "code-like query should route through lexical search"
