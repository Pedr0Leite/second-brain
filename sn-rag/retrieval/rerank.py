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
        # getattr, not a direct import: a long-lived MCP server that imported
        # `config` before MODEL_CACHE_PATH existed would otherwise ImportError
        # and take down every tool. See ingest/embed.py:_default_cache_dir.
        if cache_dir is None:
            from ingest.embed import _default_cache_dir
            cache_dir = _default_cache_dir()
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
        # The reranker must see the same text shape the embedder indexed —
        # including the title, now that it is part of the recipe. Scoring a
        # different representation than was indexed makes the cross-encoder
        # disagree with retrieval for reasons unrelated to relevance.
        from ingest.embed import build_embed_text
        from config import EMBED_DOC_TITLE
        documents = [build_embed_text(h.text, h.h_path, getattr(h, "doc_title", "") or "",
                                      h.rel_path, include_title=EMBED_DOC_TITLE)
                     for h in hits]
        scores = list(self._model.rerank(query, documents))
        ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
        return [
            Hit(chunk_id=h.chunk_id, parent_id=h.parent_id, rel_path=h.rel_path,
                doc_title=h.doc_title, h_path=h.h_path, source=h.source,
                doc_type=h.doc_type, text=h.text, score=float(score),
                facets=h.facets, api_symbols=h.api_symbols)
            for h, score in ranked[:top_k]
        ]
