"""Hybrid dense + sparse retrieval over Qdrant, with payload filtering.

Sparse (BM25) is not optional here: dense embeddings smear exact identifiers
like `sys_user_grmember` or `GlideRecord.addQuery`, which is precisely what
technical lookups hinge on.
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from qdrant_client import QdrantClient, models

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest.embed import DENSE_VECTOR, SPARSE_VECTOR


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    parent_id: str
    rel_path: str
    doc_title: str
    h_path: str
    source: str
    doc_type: str
    text: str
    score: float
    facets: dict
    api_symbols: tuple

    @classmethod
    def from_point(cls, point) -> "Hit":
        p = point.payload or {}
        return cls(
            chunk_id=p.get("chunk_id", ""),
            parent_id=p.get("parent_id", ""),
            rel_path=p.get("rel_path", ""),
            doc_title=p.get("doc_title", ""),
            h_path=p.get("h_path", ""),
            source=p.get("source", ""),
            doc_type=p.get("doc_type", ""),
            text=p.get("text", ""),
            score=float(getattr(point, "score", 0.0) or 0.0),
            facets=p.get("facets", {}) or {},
            api_symbols=tuple(p.get("api_symbols", []) or []),
        )


def build_filter(sources: Optional[Sequence[str]] = None,
                 doc_types: Optional[Sequence[str]] = None,
                 facets: Optional[dict] = None,
                 api_symbols: Optional[Sequence[str]] = None) -> Optional[models.Filter]:
    """Metadata pre-filter. All conditions are ANDed; list values are ORed."""
    must: list = []
    if sources:
        must.append(models.FieldCondition(key="source", match=models.MatchAny(any=list(sources))))
    if doc_types:
        must.append(models.FieldCondition(key="doc_type", match=models.MatchAny(any=list(doc_types))))
    if api_symbols:
        must.append(models.FieldCondition(key="api_symbols", match=models.MatchAny(any=list(api_symbols))))
    for key, value in (facets or {}).items():
        field = f"facets.{key}"
        if isinstance(value, (list, tuple, set)):
            must.append(models.FieldCondition(key=field, match=models.MatchAny(any=list(value))))
        else:
            must.append(models.FieldCondition(key=field, match=models.MatchValue(value=value)))
    return models.Filter(must=must) if must else None


class HybridSearcher:
    """Hybrid retrieval.

    `exact` and `hnsw_ef` control the accuracy/speed trade-off of Qdrant's
    approximate index. This is not a tuning nicety: with default HNSW settings,
    near-tied scores are broken differently between identical runs, so recall on
    a fixed query set varies by a whole case run to run. Evaluation must use
    exact search or a high `hnsw_ef`, otherwise measured differences smaller
    than the noise floor get mistaken for real effects.
    """

    def __init__(self, client: QdrantClient, collection: str, embedder,
                 exact: bool = False, hnsw_ef: Optional[int] = None):
        self.client = client
        self.collection = collection
        self.embedder = embedder
        self.search_params = models.SearchParams(exact=exact, hnsw_ef=hnsw_ef) \
            if (exact or hnsw_ef) else None

    def _encode(self, query: str):
        dense, sparse = self.embedder.encode([query])
        return dense[0], sparse[0]

    def search(self, query: str, limit: int = 30, query_filter=None,
               mode: str = "hybrid", prefetch_multiplier: int = 4) -> list[Hit]:
        """mode: 'hybrid' (RRF of dense+sparse) | 'dense' | 'sparse'.

        The three modes exist so the eval harness can measure what hybrid
        actually buys over dense alone, rather than assuming it.
        """
        dense_vec, sparse_vec = self._encode(query)
        sparse_q = models.SparseVector(indices=sparse_vec.indices.tolist(),
                                       values=sparse_vec.values.tolist())

        sp = self.search_params
        if mode == "dense":
            result = self.client.query_points(
                collection_name=self.collection, query=dense_vec.tolist(),
                using=DENSE_VECTOR, limit=limit, query_filter=query_filter,
                search_params=sp, with_payload=True)
        elif mode == "sparse":
            result = self.client.query_points(
                collection_name=self.collection, query=sparse_q,
                using=SPARSE_VECTOR, limit=limit, query_filter=query_filter,
                search_params=sp, with_payload=True)
        elif mode == "hybrid":
            prefetch_n = limit * prefetch_multiplier
            result = self.client.query_points(
                collection_name=self.collection,
                prefetch=[
                    models.Prefetch(query=dense_vec.tolist(), using=DENSE_VECTOR,
                                    limit=prefetch_n, filter=query_filter, params=sp),
                    models.Prefetch(query=sparse_q, using=SPARSE_VECTOR,
                                    limit=prefetch_n, filter=query_filter, params=sp),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit, with_payload=True)
        else:
            raise ValueError(f"unknown search mode {mode!r}")

        return [Hit.from_point(p) for p in result.points]
