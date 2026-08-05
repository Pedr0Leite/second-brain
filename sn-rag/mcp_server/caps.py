"""Output caps and token accounting — enforced in code, never by prompt.

This module is the project's cost boundary. Every tool response passes through
it. A prompt instruction asking a model to "keep results short" is a suggestion;
truncation here is a guarantee.

Spec §12 rule 7: caps implemented as prompt instructions are a build failure.
"""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CAPS

TRUNCATION_MARKER = "\n\n[TRUNCATED]"


def approx_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token for English prose + code).

    Deliberately an estimate, not a tokenizer call: this runs on every response
    and its purpose is budget visibility, not billing accuracy.
    """
    return max(1, len(text) // 4)


def truncate_chars(text: str, max_chars: int, marker: str = TRUNCATION_MARKER) -> tuple[str, bool]:
    """Hard character cap. Returns (text, was_truncated).

    Cuts at a whitespace boundary when one is available near the limit, so the
    output does not end mid-token.
    """
    if len(text) <= max_chars:
        return text, False
    budget = max_chars - len(marker)
    if budget <= 0:
        return marker.strip(), True
    cut = text[:budget]
    space = cut.rfind("\n")
    if space < budget * 0.8:
        space = cut.rfind(" ")
    if space > budget * 0.5:
        cut = cut[:space]
    return cut + marker, True


def truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    words = text.split()
    if len(words) <= max_words:
        return text, False
    return " ".join(words[:max_words]) + TRUNCATION_MARKER, True


def snippet(text: str, max_words: int) -> str:
    """A single result's excerpt. Collapses whitespace to avoid burning the
    character budget on the blank lines and indentation markdown is full of."""
    collapsed = " ".join(text.split())
    out, _ = truncate_words(collapsed, max_words)
    return out


def cap_result_list(items: list[dict], text_key: str, max_items: int,
                    max_words_each: int, max_chars_total: int) -> tuple[list[dict], dict]:
    """Apply per-item and whole-response caps to a ranked result list.

    Items are dropped from the tail rather than truncated to nothing, so every
    returned result stays usable and citable.
    """
    import json

    kept: list[dict] = []
    running = 0
    dropped_for_budget = 0
    for item in items[:max_items]:
        item = dict(item)
        item[text_key] = snippet(item.get(text_key, ""), max_words_each)
        # Measure the ACTUAL serialized size. An estimated per-item overhead
        # under-counts badly here: rel_path, chunk_id and parent_id together run
        # to several hundred characters, and a 120-char guess let a 6,000-char
        # cap emit 7,728. The cap is the contract — it must be measured, not
        # approximated.
        cost = len(json.dumps(item))
        if kept and running + cost > max_chars_total:
            dropped_for_budget = len(items[:max_items]) - len(kept)
            break
        kept.append(item)
        running += cost
        if running >= max_chars_total:
            dropped_for_budget = len(items[:max_items]) - len(kept)
            break
    meta = {
        "returned": len(kept),
        "available": len(items),
        "dropped_for_budget": dropped_for_budget,
        "approx_tokens": approx_tokens(str(kept)),
    }
    return kept, meta


def error(code: str, message: str, retryable: bool = False, **extra) -> dict:
    """Structured error. Never a prose apology, never an empty success.

    Spec §6: on any backend failure return {error_code, message, retryable}.
    """
    payload = {"error_code": code, "message": message, "retryable": retryable, "ok": False}
    payload.update(extra)
    return payload


def ok(payload: dict) -> dict:
    """Success envelope with a token estimate attached."""
    payload = dict(payload)
    payload["ok"] = True
    payload.setdefault("approx_tokens", approx_tokens(str(payload)))
    return payload


def cap_for(tool: str) -> dict:
    if tool not in CAPS:
        raise KeyError(f"no cap configured for tool {tool!r} — every tool must declare one")
    return CAPS[tool]
