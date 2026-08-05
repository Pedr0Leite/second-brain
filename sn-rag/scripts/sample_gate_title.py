"""Sample gate for the embedded-title recipe: is a full re-embed worth 8 hours?

Per CLAUDE.md rule 8, no full-corpus index before a sample gate passes. This
builds the SAME document sample twice — once with the old recipe (h_path + body)
and once with the new one (title + filename words + h_path + body) — into two
throwaway collections, then measures recall over the golden set on each.

What is and is not valid here:
  VALID    the DIFFERENCE between the two arms. Same documents, same chunker,
           same models, same searcher; the embedding recipe is the only variable.
  INVALID  the absolute recall numbers. A 500-document index makes every query
           easier than a 51,588-document one, so these will read high. Do not
           quote them as retrieval quality.

The sample deliberately includes every golden expected_rel_path. Without them
recall is 0 for both arms and the run measures nothing. That is a sampling
choice for a paired comparison, NOT a claim about corpus-wide recall.

Retrieval runs through the shipped `build_agents` searcher pointed at the
throwaway collection — not a reimplementation. Per CLAUDE.md: benchmark the
code path you ship.

Usage:
    python3 scripts/sample_gate_title.py --sample 500
"""
import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qdrant_client import QdrantClient, models

from config import (CORPUS_PATH, QDRANT_URL, DENSE_MODEL, SPARSE_MODEL,
                    RERANK_MODEL, EMBED_BATCH_SIZE, EMBED_THREADS,
                    PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                    CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP,
                    EXCLUDED_DIRS, EXCLUDED_FILENAMES)
from ingest import embed as embed_mod
from ingest.chunker import chunk_document
from ingest.normalize import classify_source, extract_metadata

SEED = 20260805


def corpus_files() -> list[str]:
    """Every indexable markdown file, mirroring the ingest walk's exclusions."""
    out = []
    for path in CORPUS_PATH.rglob("*.md"):
        rel = path.relative_to(CORPUS_PATH)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.name.lower() in EXCLUDED_FILENAMES:
            continue
        out.append(str(rel))
    return out


def build_sample(cases: list[dict], size: int) -> tuple[list[str], int]:
    """Golden expectations first, then a seeded random fill to `size`."""
    every = corpus_files()
    present = set(every)
    wanted, missing = [], []
    for case in cases:
        for rel in case.get("expected_rel_paths") or []:
            if rel in present:
                if rel not in wanted:
                    wanted.append(rel)
            else:
                missing.append(rel)
    if missing:
        print(f"WARNING: {len(missing)} golden expected paths are not in the corpus "
              f"walk and cannot be scored: {missing[:5]}")
    rng = random.Random(SEED)
    filler = [f for f in every if f not in set(wanted)]
    rng.shuffle(filler)
    sample = wanted + filler[:max(0, size - len(wanted))]
    return sample, len(wanted)


def index_sample(client: QdrantClient, collection: str, rel_paths: list[str],
                 embedder, include_title: bool) -> int:
    """Chunk, embed and upsert the sample into a throwaway collection.

    Payload mirrors ingest/index.py exactly — the profile filters key off
    `source`, so a divergence here would silently change which documents each
    agent can even see.
    """
    embed_mod.ensure_collection(client, collection, embedder.dim, recreate=True)
    now = datetime.now(timezone.utc).isoformat()
    total = 0
    window: list[tuple] = []

    def flush(window):
        nonlocal total
        if not window:
            return
        all_texts = [t for _, _, _, ts, _ in window for t in ts]
        dense, sparse = embedder.encode(all_texts)
        points, cursor = [], 0
        for rel_path, source, children, texts, meta in window:
            symbols = {}
            for i, child in enumerate(children):
                d, s = dense[cursor + i], sparse[cursor + i]
                points.append(models.PointStruct(
                    id=embed_mod.chunk_uuid(child.chunk_id),
                    vector={
                        embed_mod.DENSE_VECTOR: d.tolist(),
                        embed_mod.SPARSE_VECTOR: models.SparseVector(
                            indices=s.indices.tolist(), values=s.values.tolist()),
                    },
                    payload={
                        "chunk_id": child.chunk_id, "parent_id": child.parent_id,
                        "text": child.text, "rel_path": rel_path,
                        "doc_title": meta["doc_title"], "h_path": child.h_path,
                        "source": source, "doc_type": meta["doc_type"],
                        "facets": meta["facets"],
                        "api_symbols": symbols.get(child.parent_id, []),
                        "chunk_index": child.child_idx, "updated_at": now,
                    },
                ))
            cursor += len(texts)
        embed_mod.upsert_points(client, collection, points)
        total += len(points)

    for n, rel_path in enumerate(rel_paths, 1):
        path = CORPUS_PATH / rel_path
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            meta = extract_metadata(raw, Path(rel_path))
            source = classify_source(Path(rel_path))
            _parents, children = chunk_document(raw, rel_path, PARENT_CHUNK_MIN_CHARS,
                                                PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS,
                                                CHILD_CHUNK_OVERLAP)
        except Exception as exc:
            print(f"  skip {rel_path}: {exc.__class__.__name__}")
            continue
        if not children:
            continue
        texts = [embed_mod.build_embed_text(c.text, c.h_path, meta["doc_title"],
                                            rel_path, include_title=include_title)
                 for c in children]
        window.append((rel_path, source, children, texts, meta))
        if len(window) >= 25:
            flush(window)
            window = []
            print(f"  {n}/{len(rel_paths)} files  {total:,} chunks", flush=True)
    flush(window)
    return total


def score(agents: dict, cases: list[dict], k_values=(5, 10), candidates=30) -> dict:
    hits = {k: [] for k in k_values}
    ranks = []
    for case in cases:
        expected = set(case.get("expected_rel_paths") or [])
        if not expected:
            continue
        agent = agents[case.get("profile", "general")]
        result = agent.search(case["question"], k=max(k_values), candidates=candidates,
                              mode="hybrid", rerank=True)
        paths = [h.rel_path for h in result.hits]
        rank = next((i + 1 for i, p in enumerate(paths) if p in expected), None)
        ranks.append(rank)
        for k in k_values:
            hits[k].append(bool(rank and rank <= k))
    n = len(ranks)
    return {
        "n": n,
        "recall": {k: sum(hits[k]) / n for k in k_values},
        "mrr": sum(1 / r for r in ranks if r) / n if n else 0.0,
        "never": sum(1 for r in ranks if r is None),
        "ranks": ranks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--candidates", type=int, default=30)
    args = ap.parse_args()

    from eval.run_eval import load_golden
    from retrieval.profiles import build_agents
    from retrieval.rerank import Reranker

    golden = Path(__file__).resolve().parent.parent / "eval" / "golden.yaml"
    cases = [c for c in load_golden(golden) if not c.get("expect_no_answer")]
    sample, n_expected = build_sample(cases, args.sample)
    print(f"sample: {len(sample)} documents "
          f"({n_expected} golden expectations + {len(sample) - n_expected} random, seed {SEED})")
    print(f"scoring {len(cases)} golden cases\n")

    client = QdrantClient(url=QDRANT_URL, timeout=300)
    embedder = embed_mod.Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE,
                                  threads=EMBED_THREADS)
    reranker = Reranker(RERANK_MODEL)

    results = {}
    for label, include_title, collection in (
            ("without title (old)", False, "sample_notitle"),
            ("with title (new)", True, "sample_title")):
        print(f"--- indexing {collection}: {label} ---", flush=True)
        t0 = time.perf_counter()
        n_chunks = index_sample(client, collection, sample, embedder, include_title)
        print(f"    {n_chunks:,} chunks in {time.perf_counter() - t0:.0f}s", flush=True)
        agents = build_agents(client, collection, embedder, reranker, CORPUS_PATH, exact=True)
        results[label] = score(agents, cases, candidates=args.candidates)

    print(f"\n{'arm':22s} {'n':>3s} {'recall@5':>9s} {'recall@10':>10s} {'MRR':>7s} {'never found':>12s}")
    print("-" * 68)
    for label, m in results.items():
        print(f"{label:22s} {m['n']:3d} {m['recall'][5]:9.3f} {m['recall'][10]:10.3f} "
              f"{m['mrr']:7.3f} {m['never']:12d}")

    old = results["without title (old)"]
    new = results["with title (new)"]
    print(f"\ndelta recall@10 : {new['recall'][10] - old['recall'][10]:+.3f}")
    print(f"delta MRR       : {new['mrr'] - old['mrr']:+.3f}")
    print(f"never-found     : {old['never']} -> {new['never']}")

    print("\nper-case rank (None = not in top 10):")
    print(f"  {'case':28s} {'old':>6s} {'new':>6s}")
    moved = 0
    for case, r_old, r_new in zip([c for c in cases if c.get("expected_rel_paths")],
                                  old["ranks"], new["ranks"]):
        if r_old != r_new:
            moved += 1
            print(f"  {case['id'][:28]:28s} {str(r_old):>6s} {str(r_new):>6s}")
    if not moved:
        print("  no case changed rank — the title signal did nothing on this sample.")

    print("\nAbsolute recall here is inflated: a 500-document index is far easier to "
          "search than 51,588. Only the DELTA between the two arms is evidence, and "
          "it is what decides whether the ~8h full re-embed is worth running.")


if __name__ == "__main__":
    main()
