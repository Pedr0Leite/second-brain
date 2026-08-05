"""Local planner client — short structured output only, never prose.

Per ADR-0004 the local model plans and selects; Claude synthesizes. This module
therefore exposes no "answer" or "summarize" call. It exists to turn a question
into a retrieval plan and to judge candidate evidence, both in tens of tokens.

The one rule that matters: **there is no fallback to a hosted model.** If the
local route is unreachable, callers get PLANNER_UNAVAILABLE. A silent failover
would make every metric improve while destroying the reason the project exists.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (PLANNER_BASE_URL, PLANNER_MODEL, PLANNER_MAX_TOKENS,
                    PLANNER_TIMEOUT_SECONDS)

VALID_AGENTS = ("general", "servicenow", "personal")


class PlannerUnavailable(Exception):
    """The local planner could not be reached or returned nothing usable.

    Deliberately a distinct exception rather than a None return: callers must
    surface PLANNER_UNAVAILABLE rather than quietly degrading to a worse plan.
    """


@dataclass
class Plan:
    queries: list[str]
    agent: str = "general"
    reason: str = ""
    raw: str = ""
    elapsed_s: float = 0.0
    facets: dict = field(default_factory=dict)


def _extract_json(text: str):
    """Pull the first JSON object/array out of a model response.

    Small instruct models wrap JSON in prose or code fences even when told not
    to. Failing the whole call over a ```json fence would be brittle, so the
    fence is stripped and the first balanced structure is parsed. If nothing
    parses, that is a real failure and raises.
    """
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if not match:
        raise PlannerUnavailable(
            f"planner returned no parseable JSON: {text[:200]!r}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise PlannerUnavailable(
            f"planner returned malformed JSON ({exc}): {text[:200]!r}") from exc


class Planner:
    """OpenAI-compatible chat client against a local route (Ollama, llama.cpp)."""

    def __init__(self, base_url: str = PLANNER_BASE_URL, model: str = PLANNER_MODEL,
                 timeout: int = PLANNER_TIMEOUT_SECONDS,
                 max_tokens: int = PLANNER_MAX_TOKENS):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = timeout
        self.max_tokens = max_tokens

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def available(self) -> bool:
        """Reachability, checked live. Configuration presence is not availability.

        Cheap enough to call per request; the failure mode this guards against
        (Ollama installed but not running, or the model never pulled) is common
        and otherwise surfaces as a confusing timeout mid-plan.
        """
        if not self.configured:
            return False
        try:
            req = urllib.request.Request(f"{self.base_url}/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return False
        names = {m.get("id", "") for m in body.get("data", [])}
        # Ollama reports "qwen2.5:3b-instruct"; accept a prefix match so a tag
        # difference does not read as "model missing".
        return any(n == self.model or n.startswith(self.model.split(":")[0]) for n in names)

    def _chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> tuple[str, float]:
        if not self.configured:
            raise PlannerUnavailable(
                "No local planner route configured (PLANNER_BASE_URL / PLANNER_MODEL).")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": 0,  # planning must be reproducible; eval depends on it
            "stream": False,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PlannerUnavailable(f"local planner unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PlannerUnavailable(f"planner returned non-JSON envelope: {exc}") from exc
        elapsed = time.perf_counter() - t0
        try:
            return body["choices"][0]["message"]["content"], elapsed
        except (KeyError, IndexError) as exc:
            raise PlannerUnavailable(f"unexpected planner response shape: {body}") from exc

    PLAN_SYSTEM = (
        "You plan document retrieval over a ServiceNow knowledge base. "
        "Output ONLY a JSON object, no prose, no code fence. Schema:\n"
        '{"queries": ["...", "..."], "agent": "general|servicenow|personal", '
        '"reason": "one short clause"}\n'
        "Rules: 1-3 queries. Keep the user\'s exact technical identifiers "
        "(table names, API names, error strings) verbatim — do not paraphrase them. "
        "Use agent 'servicenow' for vendor/platform documentation questions, "
        "'personal' for the user's own notes, wiki and applications, "
        "'general' when unsure."
    )

    def plan(self, question: str) -> Plan:
        """Question -> retrieval plan. ~40 tokens, ~2s on CPU."""
        raw, elapsed = self._chat(self.PLAN_SYSTEM, question.strip())
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            raise PlannerUnavailable(f"planner returned {type(obj).__name__}, expected object")

        queries = [str(q).strip() for q in (obj.get("queries") or []) if str(q).strip()]
        if not queries:
            # Falling back to the raw question is correct here: retrieval with the
            # user's own words is a sound plan, and it is not a silent model
            # substitution — no other model is consulted.
            queries = [question.strip()]

        agent = str(obj.get("agent", "general")).strip().lower()
        if agent not in VALID_AGENTS:
            agent = "general"

        return Plan(queries=queries[:3], agent=agent,
                    reason=str(obj.get("reason", ""))[:200],
                    raw=raw, elapsed_s=elapsed)

    JUDGE_SYSTEM = (
        "You judge whether a document excerpt helps answer a question. "
        'Reply with ONLY one word: "yes" or "no".'
    )

    def judge(self, question: str, excerpt: str, max_excerpt_chars: int = 1200) -> bool:
        """Is this excerpt relevant? ~5 tokens, sub-second.

        Anything that is not an unambiguous "no" counts as relevant: a planner
        hiccup should not silently delete evidence the user might need.
        """
        user = f"Question: {question}\n\nExcerpt:\n{excerpt[:max_excerpt_chars]}"
        raw, _ = self._chat(self.JUDGE_SYSTEM, user, max_tokens=4)
        return not raw.strip().lower().startswith("no")
