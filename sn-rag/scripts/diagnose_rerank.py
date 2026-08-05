"""Diagnostic: trace where the expected document is lost in the retrieval pipeline.

Stages instrumented, in order:
  1. hybrid retrieval  -> rank of expected doc among `candidates`
  2. parent dedupe     -> rank after collapsing same-parent chunks
  3. cross-encoder     -> rank after reranking

Evidence only. Proposes nothing.
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, EMBED_BATCH_SIZE, RERANK_MODEL)


def rank_of(hits, expected: set):
    for i, h in enumerate(hits):
        if h.rel_path in expected:
            return i + 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=int, default=30)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--only-missed", action="store_true")
    ap.add_argument("--profile", default=None)
    args = ap.parse_args()

    from qdrant_client import QdrantClient
    from ingest.embed import Embedder
    from retrieval.rerank import Reranker
    from retrieval.profiles import build_agents

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    reranker = Reranker(RERANK_MODEL)
    agents = build_agents(client, QDRANT_COLLECTION, embedder, reranker, CORPUS_PATH)

    cases = yaml.safe_load((Path(__file__).parent.parent / "eval" / "golden.yaml").read_text())
    cases = [c for c in cases if not c.get("expect_no_answer")]
    if args.profile:
        cases = [c for c in cases if c.get("profile", "general") == args.profile]

    print(f"{'case':28s} {'hybrid':>7s} {'dedupe':>7s} {'rerank':>7s}  verdict")
    print("-" * 78)

    summary = {"lost_by_rerank": [], "never_retrieved": [], "ok": [], "rescued": []}

    for case in cases:
        agent = agents[case.get("profile", "general")]
        expected = set(case["expected_rel_paths"])
        query = case["question"]

        raw = agent.searcher.search(query, limit=args.candidates,
                                    query_filter=agent._filter(None, None), mode="hybrid")
        r_hybrid = rank_of(raw, expected)

        deduped = agent.parents.dedupe_by_parent(raw)
        r_dedupe = rank_of(deduped, expected)

        reranked = reranker.rerank(query, deduped, top_k=args.k)
        r_rerank = rank_of(reranked, expected)

        in_hybrid_k = r_dedupe is not None and r_dedupe <= args.k
        in_rerank_k = r_rerank is not None and r_rerank <= args.k

        if r_hybrid is None:
            verdict, bucket = "NEVER RETRIEVED", "never_retrieved"
        elif in_hybrid_k and not in_rerank_k:
            verdict, bucket = "LOST BY RERANK", "lost_by_rerank"
        elif not in_hybrid_k and in_rerank_k:
            verdict, bucket = "rescued by rerank", "rescued"
        else:
            verdict, bucket = "ok", "ok"
        summary[bucket].append(case["id"])

        if args.only_missed and bucket == "ok":
            continue
        fmt = lambda r: str(r) if r else "-"
        print(f"{case['id'][:28]:28s} {fmt(r_hybrid):>7s} {fmt(r_dedupe):>7s} "
              f"{fmt(r_rerank):>7s}  {verdict}")

    print()
    print(f"never retrieved in {args.candidates} candidates : {len(summary['never_retrieved'])}"
          f"  {summary['never_retrieved']}")
    print(f"in hybrid top-{args.k}, LOST by rerank        : {len(summary['lost_by_rerank'])}"
          f"  {summary['lost_by_rerank']}")
    print(f"rescued by rerank                       : {len(summary['rescued'])}"
          f"  {summary['rescued']}")
    print(f"ok                                      : {len(summary['ok'])}")


if __name__ == "__main__":
    main()
