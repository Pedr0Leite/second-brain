"""Retrieval evaluation: recall@k, MRR, latency across retrieval modes.

Answers one question only: does retrieval actually find the right document?
Everything downstream (agent loop, MCP surface, token savings) is built on the
assumption that it does, so this runs before any of it.

Usage:
  python3 eval/run_eval.py --golden eval/golden.yaml
  python3 eval/run_eval.py --modes dense,sparse,hybrid,hybrid+rerank
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, EMBED_BATCH_SIZE, RERANK_MODEL)
from ingest.embed import Embedder
from retrieval.profiles import build_agents, PROFILES

# Phase 4 gate from the build spec: hybrid+rerank recall@10 must clear this.
RECALL_GATE = 0.85
GATE_MODE = "hybrid+rerank"
GATE_K = 10

# Provenance is the difference between a measurement and a flattering number.
#
# A question written by reading the document it is supposed to find inherits that
# document's vocabulary, so retrieval finds it easily. Recall over such cases
# measures the author's memory, not the system. Cases mined from real session
# transcripts — typed before the answer was known — are the only ones that
# measure retrieval.
#
# The gate therefore scores REAL cases only. Constructed cases are still run and
# reported, because they catch regressions, but they cannot pass the gate.
VALID_PROVENANCE = ("real", "constructed", "negative")
MIN_REAL_CASES = 20


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a YAML list of cases")
    required = {"id", "question", "expected_rel_paths", "provenance"}
    for i, case in enumerate(data):
        missing = required - set(case)
        if missing:
            raise ValueError(f"case #{i} ({case.get('id', '?')}) missing keys: {sorted(missing)}")
        if case["provenance"] not in VALID_PROVENANCE:
            raise ValueError(
                f"case {case['id']}: provenance must be one of {VALID_PROVENANCE}, "
                f"got {case['provenance']!r}. 'real' means the question was asked before "
                f"the answer was known (mined from a transcript or written from memory); "
                f"'constructed' means it was written while looking at documents.")
        if not case["expected_rel_paths"] and not case.get("expect_no_answer"):
            raise ValueError(
                f"case {case['id']} has no expected_rel_paths "
                f"(set expect_no_answer: true if the corpus genuinely cannot answer it)")
    return data


def verify_expectations_exist(cases: list[dict], corpus: Path) -> list[str]:
    """Catch golden cases that cannot possibly score.

    Two ways a case is unwinnable regardless of retrieval quality:
      1. the expected file does not exist;
      2. the expected file's source class is excluded by the case's own profile
         (e.g. profile 'servicenow' expecting a wiki/ path — the source filter
         removes it before ranking).

    Both look identical to a retrieval failure in the results table, so they are
    rejected up front rather than silently depressing recall.
    """
    from ingest.normalize import classify_source
    from retrieval.profiles import PROFILES

    problems = []
    for case in cases:
        profile_name = case.get("profile", "general")
        profile = PROFILES.get(profile_name)
        for rel in case.get("expected_rel_paths") or []:
            if not (corpus / rel).exists():
                problems.append(f"{case['id']}: expected path not in corpus: {rel}")
                continue
            if profile is None or profile.sources is None:
                continue
            try:
                source = classify_source(Path(rel))
            except ValueError as exc:
                problems.append(f"{case['id']}: {exc}")
                continue
            if source not in profile.sources:
                problems.append(
                    f"{case['id']}: profile '{profile_name}' excludes source '{source}', "
                    f"so {rel} can never be returned — fix the profile or the expected path")
    return problems


def evaluate(agent, cases: list[dict], mode: str, k_values=(5, 10), candidates=30):
    """Return per-mode metrics. `mode` may carry a '+rerank' suffix.

    Negative cases (`expect_no_answer`) are excluded from recall and MRR: they
    have no correct document by construction, so counting them as misses would
    understate retrieval quality. They exist to test that the *agent* declines
    to answer (Phase 5), not that retrieval finds something.
    """
    cases = [c for c in cases if not c.get("expect_no_answer")]
    if not cases:
        return None
    base_mode, rerank = (mode.split("+")[0], mode.endswith("+rerank"))
    max_k = max(k_values)
    ranks: list[float] = []
    hits_at: dict[int, list[bool]] = {k: [] for k in k_values}
    latencies: list[float] = []
    misses: list[str] = []

    for case in cases:
        t0 = time.perf_counter()
        result = agent.search(case["question"], k=max_k, candidates=candidates,
                              mode=base_mode, rerank=rerank)
        latencies.append(time.perf_counter() - t0)

        expected = set(case["expected_rel_paths"])
        retrieved = [h.rel_path for h in result.hits]
        rank = next((i + 1 for i, rel in enumerate(retrieved) if rel in expected), None)
        ranks.append(1.0 / rank if rank else 0.0)
        for k in k_values:
            hits_at[k].append(bool(rank and rank <= k))
        if not rank:
            misses.append(case["id"])

    n = len(cases)
    return {
        "mode": mode,
        "n": n,
        "recall": {k: sum(hits_at[k]) / n for k in k_values},
        "mrr": sum(ranks) / n,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": (sorted(latencies)[int(n * 0.95) - 1] if n >= 20 else max(latencies)) * 1000,
        "misses": misses,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, default=Path(__file__).parent / "golden.yaml")
    ap.add_argument("--modes", type=str, default="dense,sparse,hybrid,hybrid+rerank")
    ap.add_argument("--candidates", type=int, default=30)
    ap.add_argument("--exact", action="store_true", default=True,
                    help="deterministic exact search (default on: HNSW noise exceeds "
                         "the effect sizes being measured)")
    ap.add_argument("--approx", dest="exact", action="store_false",
                    help="use approximate HNSW search, as production does")
    ap.add_argument("--profile", type=str, default=None,
                    help="force one profile; default uses each case's own 'profile' field")
    args = ap.parse_args()

    if not args.golden.exists():
        print(f"ERROR: golden set not found at {args.golden}", file=sys.stderr)
        print("This is blocker #9 — it requires human ServiceNow judgement and cannot be "
              "auto-generated. See eval/golden.yaml for the required schema.", file=sys.stderr)
        sys.exit(2)

    cases = load_golden(args.golden)
    problems = verify_expectations_exist(cases, CORPUS_PATH)
    if problems:
        print("ERROR: golden set references files that are not in the corpus:", file=sys.stderr)
        for p in problems[:20]:
            print(f"  {p}", file=sys.stderr)
        sys.exit(2)

    from qdrant_client import QdrantClient
    from retrieval.rerank import Reranker

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    indexed = client.count(QDRANT_COLLECTION, exact=True).count
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    reranker = Reranker(RERANK_MODEL)
    agents = build_agents(client, QDRANT_COLLECTION, embedder, reranker, CORPUS_PATH,
                          exact=args.exact)

    by_profile: dict[str, list[dict]] = {}
    for case in cases:
        name = args.profile or case.get("profile", "general")
        if name not in PROFILES:
            raise ValueError(f"case {case['id']}: unknown profile {name!r}")
        by_profile.setdefault(name, []).append(case)

    print(f"index: {indexed:,} points in '{QDRANT_COLLECTION}'  |  dense={DENSE_MODEL}")
    print(f"rerank={RERANK_MODEL}  |  candidates={args.candidates}  |  search={'exact' if args.exact else 'approx-hnsw'}")
    negatives = sum(1 for c in cases if c.get("expect_no_answer"))
    print(f"golden cases: {len(cases)}  ({ {p: len(c) for p, c in by_profile.items()} })")
    print(f"  scored for recall: {len(cases) - negatives}   negatives (excluded, for Phase 5): {negatives}")
    if indexed < 500_000:
        print(f"NOTE: index is partial ({indexed:,} points). Recall measured here is a LOWER "
              f"bound — expected documents may simply not be indexed yet.")
    print()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    gate_results = {}

    for profile_name, profile_cases in sorted(by_profile.items()):
        agent = agents[profile_name]
        print(f"=== profile: {profile_name} ({len(profile_cases)} cases) ===")
        header = f"{'mode':16s} {'recall@5':>9s} {'recall@10':>10s} {'MRR':>7s} {'p50 ms':>8s} {'p95 ms':>8s}"
        print(header)
        print("-" * len(header))
        for mode in modes:
            m = evaluate(agent, profile_cases, mode, candidates=args.candidates)
            if m is None:
                print(f"{mode:16s} (only negative cases; nothing to score)")
                continue
            print(f"{mode:16s} {m['recall'][5]:9.3f} {m['recall'][10]:10.3f} "
                  f"{m['mrr']:7.3f} {m['p50_ms']:8.0f} {m['p95_ms']:8.0f}")
            if mode == GATE_MODE:
                gate_results[profile_name] = m
        print()

    # --- provenance split: the gate scores REAL cases only --------------------
    real_cases = [c for c in cases if c.get("provenance") == "real"
                  and not c.get("expect_no_answer")]
    constructed = [c for c in cases if c.get("provenance") == "constructed"
                   and not c.get("expect_no_answer")]

    print("=== provenance ===")
    print(f"  real (asked before the answer was known): {len(real_cases)}")
    print(f"  constructed (written while reading docs): {len(constructed)}")
    print()

    if constructed:
        print("=== constructed cases (regression signal only — CANNOT pass the gate) ===")
        for profile_name in sorted({c.get("profile", "general") for c in constructed}):
            subset = [c for c in constructed if c.get("profile", "general") == profile_name]
            m = evaluate(agents[profile_name], subset, GATE_MODE, candidates=args.candidates)
            if m:
                print(f"  {profile_name:12s} recall@{GATE_K} = {m['recall'][GATE_K]:.3f}  (n={m['n']})")
        print()

    print("=== Phase 4 gate (real cases only) ===")
    if len(real_cases) < MIN_REAL_CASES:
        print(f"  INCONCLUSIVE — {len(real_cases)} real case(s), need >= {MIN_REAL_CASES}.")
        print()
        print("  The gate is NOT passed and NOT failed: there is not enough non-circular")
        print("  evidence to decide. Questions written while reading the target document")
        print("  inherit its vocabulary, so recall over them measures the author, not")
        print("  retrieval. Reporting a blended number here would launder that into a")
        print("  result that looks earned.")
        print()
        print("  Add real cases:  python3 scripts/golden.py add   (see docs/GOLDEN-SET-GUIDE.md)")
        print("  Mine transcripts: python3 scripts/mine_questions.py")
        sys.exit(2)

    overall_pass = True
    by_profile_real: dict[str, list] = {}
    for case in real_cases:
        by_profile_real.setdefault(case.get("profile", "general"), []).append(case)
    for profile_name, subset in sorted(by_profile_real.items()):
        m = evaluate(agents[profile_name], subset, GATE_MODE, candidates=args.candidates)
        if not m:
            continue
        value = m["recall"][GATE_K]
        ok = value >= RECALL_GATE
        overall_pass &= ok
        print(f"{profile_name:12s} {GATE_MODE} recall@{GATE_K} = {value:.3f} "
              f"(gate {RECALL_GATE}, n={m['n']}) -> {'PASS' if ok else 'FAIL'}")
        if m["misses"]:
            print(f"             missed: {', '.join(m['misses'][:12])}")
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
