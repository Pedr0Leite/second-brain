"""Cross-encoder reranking: top-N candidates -> top-K precise.

Precision is where the token savings come from. A score_threshold on raw vector
similarity is not enough on a 51k-document technical corpus — near-duplicate
boilerplate scores highly against almost anything.
"""
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from retrieval.hybrid import Hit


class Reranker:
    def __init__(self, model_name: str, threads: int | None = None, cache_dir=None):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        # Default resolved here, not at the call sites — see Embedder.__init__.
        if cache_dir is None:
            from config import MODEL_CACHE_PATH
            cache_dir = MODEL_CACHE_PATH
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir
        self.model_name = model_name
        self._model = TextCrossEncoder(model_name=model_name, threads=threads,
                                       cache_dir=str(cache_dir))

    def rerank(self, query: str, hits: Sequence[Hit], top_k: int) -> list[Hit]:
        """Re-score hits against the query and return the top_k, best first."""
        if not hits:
            return []
        # The breadcrumb is part of what makes a 500-char fragment interpretable,
        # so the reranker must see the same text the embedder indexed.
        documents = [f"{h.h_path}\n\n{h.text}" if h.h_path else h.text for h in hits]
        scores = list(self._model.rerank(query, documents))
        ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
        return [
            Hit(chunk_id=h.chunk_id, parent_id=h.parent_id, rel_path=h.rel_path,
                doc_title=h.doc_title, h_path=h.h_path, source=h.source,
                doc_type=h.doc_type, text=h.text, score=float(score),
                facets=h.facets, api_symbols=h.api_symbols)
            for h, score in ranked[:top_k]
        ]
