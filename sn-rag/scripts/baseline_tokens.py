"""Blocker #10: the before/after token comparison the build spec's headline needs.

The Phase 0 baseline was never captured live, so this reconstructs it. That is
legitimate only because the baseline is not a historical artifact — it is "what
does the naive path cost", and the naive path is still runnable today.

WHAT THE BASELINE MODELS
------------------------
Before sn-rag, Claude Code had no vector index over the corpus. To answer a
ServiceNow question it could only:

  1. grep the corpus for salient terms from the question, then
  2. open the most promising matching files IN FULL, because it has no way to
     know which section of a 40 KB document is relevant.

Step 2 is where the tokens go, and it is what this measures.

FAIRNESS — read before quoting any number from this
---------------------------------------------------
Every choice below is deliberately generous to the baseline, so the reported
saving is a floor rather than a best case:

  * `--open 5` caps the baseline at five files. An agent actually hunting
    through unfamiliar docs commonly opens more, and re-greps after a miss.
  * The grep is given the question's content words. A real session burns extra
    turns discovering which terms work at all; none of that is counted.
  * Only ONE grep round is charged. Real sessions iterate.
  * The `rg -l` file list itself is not charged to the baseline, though it does
    enter the context.

Both sides are measured with the SAME estimator (`caps.approx_tokens`, ~4
chars/token). It is an estimate, not a tokenizer — but because it is applied
identically to both sides, the RATIO is robust even where the absolute counts
drift. Quote the ratio; treat absolute counts as indicative.

Tokens alone also prove nothing: cheaper-but-wrong is not an improvement. So
this reports, for both paths, whether the golden set's expected document was
actually found.

Usage:
  python3 scripts/baseline_tokens.py --open 5
  python3 scripts/baseline_tokens.py --limit 8       # quick sanity run
"""
import argparse
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, EMBED_BATCH_SIZE, RERANK_MODEL, RERANK_CANDIDATES, CAPS,
                    EXCLUDED_DIRS, EXCLUDED_FILENAMES)
from mcp_server.caps import approx_tokens, cap_result_list
from retrieval.lexical import LexicalSearcher

# Claude Code's Read tool returns at most 2000 lines per call.
BASELINE_READ_MAX_LINES = 2000

# Words that would match half the corpus and tell a grep nothing.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does",
    "did", "how", "what", "why", "when", "where", "which", "who", "can", "i",
    "my", "me", "you", "it", "its", "to", "of", "in", "on", "for", "with",
    "and", "or", "but", "if", "not", "no", "so", "that", "this", "these",
    "from", "by", "at", "as", "we", "our", "have", "has", "want", "need",
    "get", "got", "use", "using", "there", "then", "than", "into", "out",
    "up", "down", "about", "should", "would", "could", "will", "just", "any",
}


def content_terms(question: str, max_terms: int = 6) -> list[str]:
    """Salient terms a person would actually grep for.

    Longest-first: specific identifiers (`GlideAggregate`, `sys_user_grmember`)
    beat generic ones, which is what a human would reach for too.
    """
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", question)
    seen: list[str] = []
    for w in sorted(words, key=len, reverse=True):
        if w.lower() in STOPWORDS:
            continue
        if w not in seen:
            seen.append(w)
    return seen[:max_terms]


def naive_cost(searcher: LexicalSearcher, question: str, open_files: int,
               expected: set) -> dict:
    """Grep, then read the top matching files in full. Returns tokens + whether hit."""
    terms = content_terms(question)
    if not terms:
        return {"tokens": 0, "files_opened": 0, "hit": False, "terms": [], "candidates": 0}

    pattern = "|".join(re.escape(t) for t in terms)
    # `-c` (count per file), NOT `-l`. Ranking by path length instead produced an
    # identical file set for every question — the baseline opened the same five
    # shortest paths regardless of what was asked, which made it look far worse
    # than the naive approach really is. Match density is the only ranking signal
    # grep actually offers, and it is what a person would sort by.
    cmd = [searcher.rg, "-c", "--type", "md", "-i", "--", pattern, str(CORPUS_PATH)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode == 2:
        raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()[:300]}")

    scored: list[tuple[int, Path]] = []
    for line in proc.stdout.splitlines():
        path_str, _, count = line.rpartition(":")
        if not path_str or not count.isdigit():
            continue
        scored.append((int(count), Path(path_str)))
    # Most matches first; shorter path breaks ties.
    scored.sort(key=lambda t: (-t[0], len(str(t[1]))))
    paths = [p for _, p in scored]

    total_chars = 0
    opened_rel = []
    for p in paths:
        if len(opened_rel) >= open_files:
            break
        # Grep the SAME corpus sn-rag indexes. Without this the baseline opened
        # the excluded index.md navigation dumps (500 KB - 2 MB of pure link
        # list), which rank top on match count precisely because they are link
        # lists — inflating the baseline to ~1.6M tokens on a single question
        # and comparing two different corpora.
        if p.name in EXCLUDED_FILENAMES or set(p.parts) & EXCLUDED_DIRS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Claude Code's Read tool returns at most 2000 lines. A baseline that
        # charges the full 2 MB of a file the agent could never have received in
        # one call is not a baseline, it is a strawman.
        lines = text.splitlines()
        if len(lines) > BASELINE_READ_MAX_LINES:
            text = "\n".join(lines[:BASELINE_READ_MAX_LINES])
        total_chars += len(text)
        try:
            opened_rel.append(str(p.resolve().relative_to(CORPUS_PATH.resolve())))
        except ValueError:
            opened_rel.append(str(p))

    return {
        "tokens": approx_tokens("x" * total_chars),
        "files_opened": len(opened_rel),
        "candidates": len(paths),
        "hit": bool(expected & set(opened_rel)),
        "terms": terms,
    }


def rag_cost(agent, question: str, expected: set) -> dict:
    """Exactly what sn_search returns to Claude, caps applied."""
    cap = CAPS["sn_search"]
    result = agent.search(question, k=cap["max_results"],
                          candidates=RERANK_CANDIDATES, mode="hybrid", rerank=True)
    items = [{
        "rel_path": h.rel_path,
        "h_path": getattr(h, "h_path", ""),
        "parent_id": getattr(h, "parent_id", ""),
        "score": round(float(getattr(h, "score", 0.0)), 4),
        "snippet": getattr(h, "text", ""),
    } for h in result.hits]
    kept, meta = cap_result_list(items, "snippet", cap["max_results"],
                                 cap["max_words_per_result"], cap["max_chars_total"])
    return {
        "tokens": meta["approx_tokens"],
        "results": len(kept),
        "hit": bool(expected & {k["rel_path"] for k in kept}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path,
                    default=Path(__file__).resolve().parent.parent / "eval" / "golden.yaml")
    ap.add_argument("--open", type=int, default=5,
                    help="files the baseline reads in full per question (default 5)")
    ap.add_argument("--limit", type=int, default=None, help="only run the first N cases")
    args = ap.parse_args()

    cases = [c for c in yaml.safe_load(args.golden.read_text(encoding="utf-8"))
             if not c.get("expect_no_answer")]
    if args.limit:
        cases = cases[:args.limit]

    searcher = LexicalSearcher(CORPUS_PATH)
    if not searcher.available:
        print("ERROR: ripgrep not found — the baseline cannot be measured without it.",
              file=sys.stderr)
        sys.exit(2)

    from qdrant_client import QdrantClient
    from ingest.embed import Embedder
    from retrieval.rerank import Reranker
    from retrieval.profiles import build_agents

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    indexed = client.count(QDRANT_COLLECTION, exact=True).count
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    reranker = Reranker(RERANK_MODEL)
    agents = build_agents(client, QDRANT_COLLECTION, embedder, reranker, CORPUS_PATH, exact=True)

    print(f"corpus: {CORPUS_PATH}")
    print(f"index:  {indexed:,} points   baseline opens {args.open} files/question")
    print(f"cases:  {len(cases)} (negatives excluded)\n")

    header = (f"{'case':28s} {'base tok':>9s} {'rag tok':>8s} {'ratio':>7s} "
              f"{'base hit':>9s} {'rag hit':>8s}")
    print(header)
    print("-" * len(header))

    base_tokens, rag_tokens, base_hits, rag_hits, ratios = [], [], [], [], []
    t0 = time.perf_counter()

    for case in cases:
        expected = set(case.get("expected_rel_paths") or [])
        agent = agents[case.get("profile", "general")]
        b = naive_cost(searcher, case["question"], args.open, expected)
        r = rag_cost(agent, case["question"], expected)

        ratio = (b["tokens"] / r["tokens"]) if r["tokens"] else float("nan")
        base_tokens.append(b["tokens"])
        rag_tokens.append(r["tokens"])
        base_hits.append(b["hit"])
        rag_hits.append(r["hit"])
        ratios.append(ratio)

        print(f"{case['id'][:28]:28s} {b['tokens']:9,d} {r['tokens']:8,d} {ratio:6.1f}x "
              f"{'YES' if b['hit'] else 'no':>9s} {'YES' if r['hit'] else 'no':>8s}")

    n = len(cases)
    elapsed = time.perf_counter() - t0
    print("-" * len(header))
    print(f"\n=== totals over {n} cases ({elapsed:.0f}s) ===")
    print(f"baseline tokens   total {sum(base_tokens):>10,d}   median {statistics.median(base_tokens):>9,.0f}")
    print(f"sn-rag   tokens   total {sum(rag_tokens):>10,d}   median {statistics.median(rag_tokens):>9,.0f}")
    print(f"\nreduction (totals)  {100 * (1 - sum(rag_tokens) / sum(base_tokens)):.1f}%"
          f"   ({sum(base_tokens) / sum(rag_tokens):.1f}x fewer tokens)")
    print(f"median per-case ratio {statistics.median(ratios):.1f}x")
    print(f"\nfound the expected document:")
    print(f"  baseline (top {args.open} files opened)  {sum(base_hits)}/{n} = {sum(base_hits)/n:.3f}")
    print(f"  sn-rag   (capped result list)       {sum(rag_hits)}/{n} = {sum(rag_hits)/n:.3f}")
    print("\nA token reduction is only meaningful alongside the hit rates above:")
    print("cheaper retrieval that finds the wrong document is not an improvement.")


if __name__ == "__main__":
    main()
