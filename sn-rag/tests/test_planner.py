"""Phase 5: the planner must plan locally, or fail loudly. Never both.

The single most important property tested here is that there is NO path from an
unreachable local planner to a working answer. A silent failover to a hosted
model would make every metric in this project improve while destroying the
reason it exists.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.planner import Planner, PlannerUnavailable, Plan, _extract_json, VALID_AGENTS


# --- JSON extraction (pure) -------------------------------------------------

def test_extract_plain_json():
    assert _extract_json('{"queries": ["a"]}') == {"queries": ["a"]}


def test_extract_strips_code_fence():
    """Small instruct models emit fences even when told not to."""
    assert _extract_json('```json\n{"queries": ["a"]}\n```') == {"queries": ["a"]}


def test_extract_finds_json_embedded_in_prose():
    out = _extract_json('Sure! Here is the plan:\n{"queries": ["a"], "agent": "general"}\nHope that helps.')
    assert out["agent"] == "general"


def test_extract_raises_on_unparseable():
    with pytest.raises(PlannerUnavailable):
        _extract_json("I cannot help with that.")


def test_extract_raises_on_malformed_json():
    with pytest.raises(PlannerUnavailable):
        _extract_json('{"queries": ["a",}')


# --- no silent failover -----------------------------------------------------

def test_unconfigured_planner_raises_never_returns_a_plan():
    p = Planner(base_url="", model="")
    assert p.configured is False
    assert p.available() is False
    with pytest.raises(PlannerUnavailable):
        p.plan("anything")


def test_unreachable_planner_raises_never_returns_a_plan():
    """Configuration presence is not availability. A dead local route must fail,
    not degrade to some other model."""
    p = Planner(base_url="http://127.0.0.1:59999/v1", model="whatever", timeout=2)
    assert p.configured is True      # configured...
    assert p.available() is False    # ...but not available
    with pytest.raises(PlannerUnavailable):
        p.plan("anything")


def test_planner_module_never_references_a_hosted_provider():
    """Guards the project's central invariant at the source level."""
    source = (Path(__file__).resolve().parent.parent / "agent" / "planner.py").read_text()
    lowered = source.lower()
    for forbidden in ("api.openai.com", "api.anthropic.com", "openrouter",
                      "api_key", "bearer ", "generativelanguage"):
        assert forbidden not in lowered, f"planner references a hosted route: {forbidden!r}"


# --- plan shaping (uses the live local model when present) ------------------

def _live() -> Planner:
    p = Planner()
    if not p.available():
        pytest.fail(
            "local planner not available — Phase 5 cannot be validated without it. "
            "Start Ollama and run: ollama pull qwen2.5:3b-instruct\n"
            "This FAILS rather than skips on purpose: a skipped test hid a missing "
            "ripgrep for an entire phase (see docs/BUILD-LOG.md).")
    return p


def test_plan_returns_queries_and_a_valid_agent():
    plan = _live().plan("how does GlideAggregate groupBy work")
    assert isinstance(plan, Plan)
    assert 1 <= len(plan.queries) <= 3
    assert all(q.strip() for q in plan.queries)
    assert plan.agent in VALID_AGENTS


def test_plan_routes_vendor_questions_to_the_servicenow_agent():
    plan = _live().plan("what does the GlideRecord addEncodedQuery API do")
    assert plan.agent == "servicenow", f"routed to {plan.agent}: {plan.raw[:200]}"


def test_plan_preserves_technical_identifiers_verbatim():
    """Paraphrasing 'sys_user_grmember' into 'group member table' destroys the
    exact-match signal that lexical retrieval depends on."""
    plan = _live().plan("which fields does sys_user_grmember have")
    assert any("sys_user_grmember" in q for q in plan.queries), plan.queries


def test_plan_is_bounded_in_latency():
    """ADR-0004 budgets ~2s for planning. Generous ceiling; catches a model swap
    that quietly makes planning cost more than retrieval."""
    plan = _live().plan("business rule runs twice on insert")
    assert plan.elapsed_s < 15, f"planning took {plan.elapsed_s:.1f}s"


def test_judge_returns_a_bool_and_keeps_relevant_evidence():
    p = _live()
    relevant = p.judge("how does GlideAggregate groupBy work",
                       "GlideAggregate.groupBy(String name) groups the aggregation "
                       "results by the specified field, like SQL GROUP BY.")
    assert relevant is True
