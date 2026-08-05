"""Embedding + Qdrant upsert. Batched, resumable, progress-reporting.

Length-sorted batching is not an optimization detail — it is load-bearing.
Transformer batches pad to the longest member, and this corpus mixes 200-char
prose with 30,000-char code blocks. Measured 5.4x throughput difference
(16.9 -> 92.4 chunks/sec on bge-small). See docs/BUILD-LOG.md Phase 3.
"""
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from qdrant_client import QdrantClient, models

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"

# Generic `facets.*` keys per ADR-0001 — corpus vocabulary, not schema fields.
PAYLOAD_INDEX_FIELDS = (
    "source", "doc_type", "rel_path", "api_symbols",
    "facets.release", "facets.product", "facets.classification", "facets.tags",
)


@dataclass(frozen=True)
class EmbedStats:
    embedded: int = 0
    batches: int = 0


def chunk_uuid(chunk_id_sha1: str) -> str:
    """Qdrant point IDs must be UUID or uint; chunk_id is a sha1 hex digest."""
    return str(uuid.UUID(chunk_id_sha1[:32]))


def length_sorted_batches(texts: Sequence[str], batch_size: int) -> Iterator[tuple[list[int], list[str]]]:
    """Yield (original_indices, texts) batches grouped by similar length."""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    for start in range(0, len(order), batch_size):
        idxs = order[start:start + batch_size]
        yield idxs, [texts[i] for i in idxs]


class Embedder:
    """Dense + sparse encoders with length-sorted batching."""

    def __init__(self, dense_model: str, sparse_model: str, batch_size: int, threads: int | None = None,
                 cache_dir=None):
        from fastembed import TextEmbedding, SparseTextEmbedding
        # Resolved here rather than threaded through every call site. There are
        # seven, and one missed site would silently fall back to fastembed's
        # tempdir default and re-download ~300 MB after each reboot — the exact
        # bug this parameter exists to prevent.
        if cache_dir is None:
            from config import MODEL_CACHE_PATH
            cache_dir = MODEL_CACHE_PATH
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir
        self.dense_model_name = dense_model
        self.sparse_model_name = sparse_model
        self.batch_size = batch_size
        self._dense = TextEmbedding(model_name=dense_model, threads=threads, cache_dir=str(cache_dir))
        self._sparse = SparseTextEmbedding(model_name=sparse_model, threads=threads, cache_dir=str(cache_dir))
        self.stats = EmbedStats()

    @property
    def dim(self) -> int:
        for m in self._dense.list_supported_models():
            if m["model"] == self.dense_model_name:
                return int(m["dim"])
        raise ValueError(f"unknown dense model {self.dense_model_name}")

    def encode(self, texts: Sequence[str]) -> tuple[list, list]:
        """Return (dense_vectors, sparse_vectors) aligned to input order.

        Hands fastembed the WHOLE length-sorted list in one call rather than
        looping batch-by-batch. Both forms produce identical vectors, but the
        loop denied fastembed its internal parallelism across batches and
        measured slower on real corpus chunks:

            looped batches of 8 (was)   7.2 chunks/s
            one call, batch_size=8      8.0
            one call, batch_size=32    12.1   <- 1.7x

        The length sort is still load-bearing and is applied before the call:
        transformer batches pad to their longest member, and this corpus mixes
        200-char prose with 30,000-char code blocks.
        """
        if not texts:
            return [], []
        # One global sort, then one call each — order restored afterwards.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        ordered = [texts[i] for i in order]

        d = list(self._dense.embed(ordered, batch_size=self.batch_size))
        s = list(self._sparse.embed(ordered, batch_size=self.batch_size))

        dense: list = [None] * len(texts)
        sparse: list = [None] * len(texts)
        for slot, i in enumerate(order):
            dense[i] = d[slot]
            sparse[i] = s[slot]

        n_batches = (len(texts) + self.batch_size - 1) // self.batch_size
        self.stats = EmbedStats(self.stats.embedded + len(texts), self.stats.batches + n_batches)
        return dense, sparse


def ensure_collection(client: QdrantClient, name: str, dim: int, recreate: bool = False):
    """Create the collection with int8 quantization and on-disk vectors."""
    exists = client.collection_exists(name)
    if exists and recreate:
        client.delete_collection(name)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=name,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True),
            },
            sparse_vectors_config={
                SPARSE_VECTOR: models.SparseVectorParams(index=models.SparseIndexParams(on_disk=True)),
            },
            quantization_config=models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8, always_ram=True),
            ),
        )
    # Payload indexes: unindexed filters over ~500k points are slow (spec §5.1).
    # Only create the missing ones — a blanket try/except would hide real errors.
    existing = set(client.get_collection(name).payload_schema or {})
    for field in PAYLOAD_INDEX_FIELDS:
        if field not in existing:
            client.create_payload_index(collection_name=name, field_name=field,
                                        field_schema=models.PayloadSchemaType.KEYWORD)


def upsert_points(client: QdrantClient, collection: str, points: list[models.PointStruct],
                  max_points_per_request: int = 256):
    """Upsert in point-bounded slices.

    Qdrant rejects request bodies over 32MB. A window of 25 dense-768 files can
    exceed 40MB, so slice on point count rather than trusting the file count.
    Point IDs are deterministic (sha1-derived UUIDs), so a partial upsert
    followed by a crash re-upserts the same IDs on resume — an idempotent
    overwrite, never a duplicate.
    """
    for start in range(0, len(points), max_points_per_request):
        client.upsert(collection_name=collection,
                      points=points[start:start + max_points_per_request], wait=True)


def delete_by_rel_path(client: QdrantClient, collection: str, rel_path: str):
    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="rel_path", match=models.MatchValue(value=rel_path))])
        ),
        wait=True,
    )
