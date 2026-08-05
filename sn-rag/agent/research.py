"""Phase 5 agent loop: plan locally, retrieve, select, cite. No local prose.

Per ADR-0004 this returns *selected, cited evidence with a reasoning trace* —
not a written answer. Claude synthesizes; the local model only decides what to
look for and what is worth keeping. That boundary is what keeps the loop inside
its latency budget on CPU (~2s planning vs ~58s if it wrote the answer itself).

Cost shape per call, measured on this box:
    plan            ~2.0s   (one ~40-token JSON emission)
    retrieve        ~0.9s   per query, 1-3 queries, hybrid + rerank
    select          ~0.3s   per judged candidate, only for borderline ranks
"""
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.planner import Planner, PlannerUnavailable

# Candidates ranked above this are kept without spending a judge call on them:
# the reranker is already a cross-encoder, and re-judging its top results with a
# 3B model costs latency to make the ranking worse, not better.
JUDGE_FROM_RANK = 3

# How much wider than the budget to retrieve, so selection has something to
# select from. Each extra candidate costs one judge call (~0.3s), so this trades
# latency for the chance to rescue a good excerpt the reranker ranked low.
POOL_MULTIPLIER = 3

# Ceiling on judge calls per research() call. Without it, three queries x a wide
# pool could spend 30+ seconds judging — the loop would blow its own budget
# doing the thing that is supposed to keep it cheap.
MAX_JUDGE_CALLS = 12


@dataclass
class Evidence:
    rel_path: str
    h_path: str
    parent_id: str
    text: str
    score: float
    from_query: str


@dataclass
class Brief:
    question: str
    agent: str
    queries: list[str]
    evidence: list[Evidence]
    trace: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    judged: int = 0
    dropped_by_judge: int = 0


def research(question: str, agents: dict, planner: Optional[Planner] = None,
             budget: int = 6, agent_override: Optional[str] = None,
             candidates: int = 50, use_judge: bool = True) -> Brief:
    """Plan -> retrieve -> select. Raises PlannerUnavailable rather than degrading.

    `budget` caps the evidence items returned, not the model's effort: an agent
    loop that silently keeps working past its budget is how a "fast local" system
    becomes a slow one.
    """
    t0 = time.perf_counter()
    planner = planner or Planner()
    trace: list[str] = []

    plan = planner.plan(question)          # raises PlannerUnavailable
    chosen = agent_override or plan.agent
    if chosen not in agents:
        chosen = "general"
    trace.append(f"planned in {plan.elapsed_s:.1f}s -> agent={chosen}, "
                 f"{len(plan.queries)} quer{'y' if len(plan.queries) == 1 else 'ies'}"
                 + (f" ({plan.reason})" if plan.reason else ""))

    agent = agents[chosen]
    seen_parents: set[str] = set()
    pool: list[Evidence] = []

    # Retrieve a POOL larger than the budget. With k=budget the judge had almost
    # nothing to choose from — one query returned exactly 6 hits for a budget of
    # 6, so "selection" could only ever subtract. Measured on
    # 'business rule runs twice on insert', the excerpt the judge rated most
    # relevant sat at rank 8 with the reranker's *lowest* score (-0.338): the
    # cross-encoder and the judge genuinely disagree, and the disagreement is
    # where the value is. A pool that stops at the budget throws it away.
    pool_size = max(budget * POOL_MULTIPLIER, budget + 4)

    for query in plan.queries:
        t_q = time.perf_counter()
        result = agent.search(query, k=pool_size, candidates=candidates,
                              mode="hybrid", rerank=True)
        new = 0
        for hit in result.hits:
            pid = getattr(hit, "parent_id", "") or getattr(hit, "chunk_id", "")
            # Multiple planned queries routinely converge on the same parent.
            # Returning it twice wastes the caller's budget on duplicate text.
            if pid and pid in seen_parents:
                continue
            if pid:
                seen_parents.add(pid)
            pool.append(Evidence(
                rel_path=hit.rel_path,
                h_path=getattr(hit, "h_path", "") or "",
                parent_id=pid,
                text=getattr(hit, "text", "") or "",
                score=float(getattr(hit, "score", 0.0)),
                from_query=query,
            ))
            new += 1
        trace.append(f"'{query[:60]}' -> {new} new in {time.perf_counter() - t_q:.1f}s")

    pool.sort(key=lambda e: -e.score)

    judged = dropped = 0
    if use_judge and len(pool) > JUDGE_FROM_RANK:
        kept = pool[:JUDGE_FROM_RANK]
        for item in pool[JUDGE_FROM_RANK:]:
            if len(kept) >= budget or judged >= MAX_JUDGE_CALLS:
                break
            try:
                judged += 1
                if planner.judge(question, item.text):
                    kept.append(item)
                else:
                    dropped += 1
            except PlannerUnavailable:
                # The plan already succeeded, so the planner was alive a moment
                # ago. Losing it mid-selection must not discard evidence already
                # retrieved — keep the item and stop judging.
                trace.append("planner lost during selection; keeping remaining evidence unjudged")
                kept.extend(pool[len(kept):budget])
                break
        pool = kept

    evidence = pool[:budget]
    if dropped:
        trace.append(f"judged {judged} borderline candidates, dropped {dropped}")

    return Brief(question=question, agent=chosen, queries=plan.queries,
                 evidence=evidence, trace=trace,
                 elapsed_s=time.perf_counter() - t0,
                 judged=judged, dropped_by_judge=dropped)
