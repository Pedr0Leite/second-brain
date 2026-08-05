"""Search agents: distinct retrieval strategies over one shared index.

Two agents, per ADR-0001's generic-core/profile split:

  general     — the whole second brain: personal notes, wiki, custom apps, code
                graphs AND official docs. Source-agnostic, no vendor assumptions.
  servicenow  — official vendor documentation only, with domain-aware behaviour:
                exact API-symbol matching, release/product facet filters, and a
                bias toward reference material.

They share the index, embedder, reranker and parent store; they differ in
filtering, routing and ranking policy. Adding a third corpus means adding a
profile here, not touching retrieval internals.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from retrieval.hybrid import Hit, HybridSearcher, build_filter
from retrieval.lexical import LexicalSearcher, looks_code_like, extract_symbols
from retrieval.parents import ParentStore

# Source classes as assigned by ingest/normalize.py.
OFFICIAL_SOURCES = ("official",)
PERSONAL_SOURCES = ("personal", "wiki", "custom-app", "code-graph")
ALL_SOURCES = OFFICIAL_SOURCES + PERSONAL_SOURCES


@dataclass(frozen=True)
class SearchProfile:
    name: str
    description: str
    sources: Optional[tuple] = None          # None = no source filter
    doc_type_boost: tuple = ()               # doc_types nudged up after rerank
    prefer_lexical_for_code: bool = False
    supports_facets: tuple = ()              # facet keys this profile accepts


GENERAL = SearchProfile(
    name="general",
    description="Whole second brain: personal notes, wiki, custom apps, code graphs, and official docs.",
    sources=None,
    prefer_lexical_for_code=True,
    supports_facets=("tags",),
)

SERVICENOW = SearchProfile(
    name="servicenow",
    description="Official ServiceNow documentation only, with API-symbol and release awareness.",
    sources=OFFICIAL_SOURCES,
    doc_type_boost=("api", "reference"),
    prefer_lexical_for_code=True,
    supports_facets=("release", "product", "classification", "tags"),
)

PERSONAL = SearchProfile(
    name="personal",
    description="Your own material only: Notion migration, wiki layer, app notes, code graphs.",
    sources=PERSONAL_SOURCES,
    prefer_lexical_for_code=True,
    supports_facets=("tags",),
)

PROFILES = {p.name: p for p in (GENERAL, SERVICENOW, PERSONAL)}


@dataclass
class SearchResult:
    hits: list
    mode: str
    used_lexical: bool = False
    lexical_paths: tuple = ()
    candidates_considered: int = 0


class SearchAgent:
    """One retrieval strategy. Owns filtering, routing and ranking policy."""

    def __init__(self, profile: SearchProfile, searcher: HybridSearcher,
                 reranker=None, lexical: Optional[LexicalSearcher] = None,
                 parents: Optional[ParentStore] = None):
        self.profile = profile
        self.searcher = searcher
        self.reranker = reranker
        self.lexical = lexical
        self.parents = parents or ParentStore()

    def _filter(self, facets: Optional[dict], doc_types: Optional[Sequence[str]],
                api_symbols: Optional[Sequence[str]] = None):
        allowed = {k: v for k, v in (facets or {}).items() if k in self.profile.supports_facets}
        rejected = set(facets or {}) - set(allowed)
        if rejected:
            raise ValueError(
                f"profile '{self.profile.name}' does not support facets {sorted(rejected)}; "
                f"supported: {list(self.profile.supports_facets)}")
        return build_filter(sources=self.profile.sources, doc_types=doc_types,
                            facets=allowed, api_symbols=api_symbols)

    def search(self, query: str, k: int = 8, candidates: int = 30,
               facets: Optional[dict] = None, doc_types: Optional[Sequence[str]] = None,
               mode: str = "hybrid", rerank: bool = True,
               dedupe_parents: bool = True) -> SearchResult:
        query_filter = self._filter(facets, doc_types)
        hits = self.searcher.search(query, limit=candidates, query_filter=query_filter, mode=mode)
        considered = len(hits)

        if dedupe_parents:
            hits = self.parents.dedupe_by_parent(hits)

        if rerank and self.reranker is not None and hits:
            hits = self.reranker.rerank(query, hits, top_k=k)
        else:
            hits = hits[:k]

        if self.profile.doc_type_boost:
            hits = self._apply_doc_type_boost(hits)

        used_lexical, lexical_paths = False, ()
        if self.profile.prefer_lexical_for_code and self.lexical and looks_code_like(query):
            used_lexical, lexical_paths = True, self._lexical_paths(query)
            hits = self._promote_lexical_matches(hits, lexical_paths)

        return SearchResult(hits=hits, mode=mode, used_lexical=used_lexical,
                            lexical_paths=lexical_paths, candidates_considered=considered)

    def _apply_doc_type_boost(self, hits: list) -> list:
        """Stable partition: boosted doc_types first, original order preserved."""
        boosted = [h for h in hits if h.doc_type in self.profile.doc_type_boost]
        rest = [h for h in hits if h.doc_type not in self.profile.doc_type_boost]
        return boosted + rest

    def _lexical_paths(self, query: str) -> tuple:
        symbols = extract_symbols(query)
        if not symbols or self.lexical is None or not self.lexical.available:
            return ()
        paths: list[str] = []
        for symbol in symbols[:3]:
            try:
                for hit in self.lexical.search(symbol, max_hits=10, fixed_string=True):
                    if hit.rel_path not in paths:
                        paths.append(hit.rel_path)
            except (RuntimeError, OSError):
                # Lexical is an enhancement; vector results still stand. The
                # caller sees used_lexical=False rather than a fabricated result.
                return ()
        return tuple(paths)

    def _promote_lexical_matches(self, hits: list, lexical_paths: tuple) -> list:
        """Stable partition: chunks from files containing the exact symbol first."""
        if not lexical_paths:
            return hits
        lex = set(lexical_paths)
        exact = [h for h in hits if h.rel_path in lex]
        rest = [h for h in hits if h.rel_path not in lex]
        return exact + rest


def build_agents(client, collection: str, embedder, reranker=None,
                 corpus_path: Optional[Path] = None, exact: bool = False,
                 hnsw_ef: Optional[int] = None) -> dict:
    """Construct every profile's agent against shared infrastructure.

    `exact=True` makes retrieval deterministic; evaluation must use it, since
    HNSW's approximate ordering otherwise varies a whole case between runs.
    """
    searcher = HybridSearcher(client, collection, embedder, exact=exact, hnsw_ef=hnsw_ef)
    lexical = LexicalSearcher(corpus_path) if corpus_path else None
    parents = ParentStore()
    return {name: SearchAgent(profile, searcher, reranker, lexical, parents)
            for name, profile in PROFILES.items()}
