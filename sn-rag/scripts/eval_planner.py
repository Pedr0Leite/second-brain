"""A/B one planner model against another over the golden set.

`eval/run_eval.py` never touches the planner — it measures hybrid+rerank recall
directly. Swapping PLANNER_MODEL therefore leaves every number in it unchanged,
which would read as "the bigger model changes nothing" when in fact the bigger
model was never exercised. This script exists to close that gap.

It drives `agent.research.research()` — the code path `sn_research` actually
ships — rather than reimplementing plan-then-retrieve. Per CLAUDE.md: benchmark
the code path you ship, not a lookalike.

What it measures, per model:
    routing     plan.agent vs the golden case's `profile`  (planner's own call)
    recall      any evidence rel_path in expected_rel_paths (end-to-end)
    latency     wall clock for the whole research() call
    judge       how many candidates were judged and dropped
    failures    PlannerUnavailable / unparseable JSON

Paired A/B on identical cases, so the golden set's known circularity
(constructed questions inherit document vocabulary) biases both arms equally.
Provenance is still reported separately — a constructed-only win is not
evidence.

Usage:
    python3 scripts/eval_planner.py --models qwen2.5:3b-instruct qwen2.5:7b-instruct
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from config import (CORPUS_PATH, QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL,
                    SPARSE_MODEL, RERANK_MODEL, EMBED_BATCH_SIZE, PLANNER_BASE_URL,
                    PLANNER_TIMEOUT_SECONDS, PLANNER_MAX_TOKENS)
from agent.planner import Planner, PlannerUnavailable
from agent.research import research
from ingest.embed import Embedder
from retrieval.profiles import build_agents

GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "golden.yaml"


def load_cases(path: Path) -> list[dict]:
    """Reuse run_eval's loader so schema validation stays in one place."""
    from eval.run_eval import load_golden
    return [c for c in load_golden(path) if not c.get("expect_no_answer")]


def run_model(model: str, cases: list[dict], agents: dict, budget: int,
              use_judge: bool = True, label: str | None = None) -> dict:
    """Drive research() over every case with one planner model.

    `use_judge=False` skips the selection stage entirely, so the arm measures
    plan-then-retrieve with no filtering. Comparing the two arms for the SAME
    model isolates judge() — the A/B measured it dropping 266 of 312 candidates
    (85%), which is the leading explanation for research() scoring below plain
    search on the same cases.
    """
    planner = Planner(base_url=PLANNER_BASE_URL, model=model,
                      timeout=PLANNER_TIMEOUT_SECONDS, max_tokens=PLANNER_MAX_TOKENS)
    if not planner.available():
        raise SystemExit(
            f"planner model {model!r} not reachable at {PLANNER_BASE_URL}. "
            f"Pull it first: ollama pull {model}")

    rows = []
    for case in cases:
        expected = set(case.get("expected_rel_paths") or [])
        want_agent = case.get("profile", "general")
        t0 = time.perf_counter()
        try:
            brief = research(case["question"], agents, planner=planner, budget=budget,
                             use_judge=use_judge)
        except PlannerUnavailable as exc:
            rows.append({"id": case["id"], "provenance": case.get("provenance", "?"),
                         "failed": str(exc)[:120], "elapsed": time.perf_counter() - t0,
                         "hit": False, "routed": False, "judged": 0, "dropped": 0,
                         "n_queries": 0})
            continue
        got = {e.rel_path for e in brief.evidence}
        rows.append({
            "id": case["id"],
            "provenance": case.get("provenance", "?"),
            "failed": None,
            "elapsed": brief.elapsed_s,
            # No expected paths means the case cannot score recall; routing still can.
            "hit": bool(expected & got) if expected else None,
            "routed": brief.agent == want_agent,
            "judged": brief.judged,
            "dropped": brief.dropped_by_judge,
            "n_queries": len(brief.queries),
        })
    return {"model": label or model, "rows": rows}


def summarize(rows: list[dict], provenance: str | None = None) -> dict:
    sel = [r for r in rows if provenance is None or r["provenance"] == provenance]
    if not sel:
        return {}
    scored = [r for r in sel if r["hit"] is not None and not r["failed"]]
    lat = [r["elapsed"] for r in sel]
    return {
        "n": len(sel),
        "scored": len(scored),
        "recall": (sum(1 for r in scored if r["hit"]) / len(scored)) if scored else float("nan"),
        "routing": sum(1 for r in sel if r["routed"]) / len(sel),
        "failures": sum(1 for r in sel if r["failed"]),
        "p50": statistics.median(lat),
        "p95": sorted(lat)[max(0, int(len(lat) * 0.95) - 1)],
        "judged": sum(r["judged"] for r in sel),
        "dropped": sum(r["dropped"] for r in sel),
        "queries": statistics.mean([r["n_queries"] for r in sel]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--budget", type=int, default=6)
    ap.add_argument("--judge-arms", action="store_true",
                    help="run each model twice, with and without judge(), to isolate "
                         "the selection stage")
    args = ap.parse_args()

    cases = load_cases(args.golden)
    print(f"golden: {args.golden}   scorable cases: {len(cases)} "
          f"(negatives excluded)\n")

    from qdrant_client import QdrantClient
    from retrieval.rerank import Reranker

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    indexed = client.count(QDRANT_COLLECTION, exact=True).count
    embedder = Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE)
    reranker = Reranker(RERANK_MODEL)
    # exact=True for the same reason run_eval defaults to it: HNSW's approximate
    # ordering varies whole cases between runs, and that noise is larger than the
    # planner difference being measured. Both arms must see identical retrieval.
    agents = build_agents(client, QDRANT_COLLECTION, embedder, reranker,
                          CORPUS_PATH, exact=True)
    print(f"index: {indexed:,} points in '{QDRANT_COLLECTION}'  |  search=exact\n")

    arms = [(True, "")] if not args.judge_arms else [(True, " +judge"), (False, " -judge")]
    results = []
    for model in args.models:
        for use_judge, suffix in arms:
            label = f"{model}{suffix}"
            print(f"--- {label} ---", flush=True)
            t0 = time.perf_counter()
            results.append(run_model(model, cases, agents, args.budget,
                                     use_judge=use_judge, label=label))
            print(f"    done in {time.perf_counter() - t0:.0f}s", flush=True)

    provs = ["real", "constructed"]
    for scope in [None] + provs:
        label = scope or "ALL"
        print(f"\n=== {label} ===")
        header = (f"{'model':28s} {'n':>3s} {'recall':>7s} {'routing':>8s} "
                  f"{'fail':>5s} {'p50 s':>7s} {'p95 s':>7s} {'q/case':>7s} {'judged':>7s} {'dropped':>8s}")
        print(header)
        print("-" * len(header))
        for res in results:
            s = summarize(res["rows"], scope)
            if not s:
                continue
            print(f"{res['model']:24s} {s['n']:3d} {s['recall']:7.3f} {s['routing']:8.3f} "
                  f"{s['failures']:5d} {s['p50']:7.1f} {s['p95']:7.1f} {s['queries']:7.2f} "
                  f"{s['judged']:7d} {s['dropped']:8d}")

    # Per-case disagreements: where the models actually differ is the signal.
    if len(results) == 2:
        a, b = results
        by_id_a = {r["id"]: r for r in a["rows"]}
        diffs = [(r["id"], by_id_a[r["id"]], r) for r in b["rows"]
                 if r["id"] in by_id_a and
                 (by_id_a[r["id"]]["hit"] != r["hit"] or by_id_a[r["id"]]["routed"] != r["routed"])]
        print(f"\n=== disagreements ({len(diffs)}) ===")
        if not diffs:
            print("none — the models made identical decisions on every case.")
        for cid, ra, rb in diffs:
            print(f"  {cid:28s} {a['model']}: hit={ra['hit']} route={ra['routed']}   "
                  f"{b['model']}: hit={rb['hit']} route={rb['routed']}")

    print("\nNote: the golden set is 3 real / 26 constructed / 5 negative (negatives "
          "excluded here). Constructed questions inherit document vocabulary, so a "
          "win visible only in the constructed rows is not evidence. Compare the "
          "'real' block first — and with n=3 it can only ever be a smoke signal, "
          "not a result.")


if __name__ == "__main__":
    main()
