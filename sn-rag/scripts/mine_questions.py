"""Mine REAL user questions from Claude Code transcripts for the golden set.

Why this and not "write 20 questions":
    A question written by reading a document inherits that document's vocabulary,
    so retrieval finds it easily and recall measures nothing. The golden set is
    only worth having if the questions predate the answer.

    Claude Code transcripts contain exactly that: things the user typed while
    working, in their own words, before they knew where the answer was. That
    makes them the one non-circular source available without asking the user to
    sit down and write.

What this does NOT do:
    It does not pick expected_rel_paths. That is deliberate — choosing expected
    documents from vector-search output is circular in the other direction, and
    would make the eval grade retrieval against its own opinion. Path selection
    stays with `scripts/golden.py find`, which uses filename + ripgrep only.

Usage:
    python3 scripts/mine_questions.py --min-words 4 --out /tmp/mined.yaml
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# A message is a candidate question if it looks like someone asking about
# knowledge rather than commanding the agent. Commands ("run the tests", "fix
# this", "continue") are the overwhelming majority of transcript traffic and
# tell retrieval nothing.
COMMAND_PREFIXES = (
    "run ", "fix ", "continue", "go ahead", "proceed", "do it", "make ", "add ",
    "remove ", "delete ", "create ", "write ", "update ", "commit", "push",
    "yes", "no", "ok", "okay", "thanks", "please continue", "next", "stop",
    "read ", "open ", "show me the file", "let's ", "lets ", "can you also",
    "now ", "then ", "also ", "и ", "test ", "check the ", "install ", "build ",
    "start ", "restart", "kill ", "git ", "npm ", "undo", "revert", "retry",
)

# Vocabulary that marks a message as being about the ServiceNow / second-brain
# knowledge domain rather than about this repo's own plumbing.
DOMAIN_TERMS = re.compile(
    r"\b("
    r"servicenow|glide\w*|gs\.\w+|g_form|g_user|sys_\w+|"
    r"business rule|client script|script include|acl|access control|"
    r"flow designer|subflow|workflow|catalog item|record producer|"
    r"update set|scoped app|scope|now assist|virtual agent|va |"
    r"service portal|ui builder|uib|now experience|"
    r"cmdb|ci class|discovery|mid server|"
    r"itsm|csm|itom|hrsd|itbm|sam|ham|"
    r"incident|change request|problem|knowledge base|"
    r"glideajax|glide record|gliderecord|glideaggregate|glidedatetime|"
    r"atf|automated test|instance scan|"
    r"table api|rest api|scripted rest|import set|transform map|"
    r"notification|event registry|scheduled job|"
    r"impersonat|role|group|delegat"
    r")\b",
    re.IGNORECASE,
)

# Interrogative shape. A question does not need a '?' — "how do I x" counts.
QUESTION_SHAPE = re.compile(
    r"^\s*(how|what|why|when|where|which|who|can |could |does |do |is |are |"
    r"should |would |any way|anyone know|is there|whats|what's|"
    r"i (need|want|am trying|can't|cannot|dont|don't))",
    re.IGNORECASE,
)

# Noise that means the message is about THIS build, not the knowledge domain.
META_TERMS = re.compile(
    r"\b(sn-rag|qdrant|embed(ding)?s?|chunk(er|ing)?|golden set|blocker|"
    r"phase \d|mcp server|ripgrep|ext4|/mnt/c|manifest|rerank\w*|"
    r"claude code|caveman|commit|pytest|build log)\b",
    re.IGNORECASE,
)


def iter_user_messages(root: Path):
    """Yield (project, text) for every human-authored message in the transcripts."""
    for path in sorted(root.rglob("*.jsonl")):
        project = path.parent.name
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "user":
                        continue
                    msg = event.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text")
                    else:
                        continue
                    text = text.strip()
                    if text:
                        yield project, text
        except OSError:
            continue


def is_tool_noise(text: str) -> bool:
    """Transcripts embed tool results, hook output and pasted files as user turns."""
    if text.startswith(("<", "[Request interrupted", "Caveat:", "#", "```")):
        return True
    if "tool_use_id" in text or "system-reminder" in text:
        return True
    if text.count("\n") > 6:          # pasted blocks, not typed questions
        return True
    if len(text) > 600:
        return True
    return False


def looks_like_question(text: str, min_words: int) -> bool:
    words = text.split()
    if not (min_words <= len(words) <= 60):
        return False
    lowered = text.lower()
    if any(lowered.startswith(p) for p in COMMAND_PREFIXES):
        return False
    if META_TERMS.search(text):
        return False
    if not DOMAIN_TERMS.search(text):
        return False
    return bool(QUESTION_SHAPE.match(text) or text.rstrip().endswith("?"))


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=TRANSCRIPT_ROOT)
    ap.add_argument("--min-words", type=int, default=4)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    total = 0
    noise = 0
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    for project, text in iter_user_messages(args.root):
        total += 1
        text = normalise(text)
        if is_tool_noise(text):
            noise += 1
            continue
        if not looks_like_question(text, args.min_words):
            continue
        key = re.sub(r"[^a-z0-9 ]", "", text.lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        candidates.append((project, text))

    print(f"scanned {total:,} user messages across {len(list(args.root.rglob('*.jsonl')))} transcripts")
    print(f"  tool/pasted noise skipped: {noise:,}")
    print(f"  domain questions found:    {len(candidates)}\n")

    by_project = Counter(p for p, _ in candidates)
    for project, n in by_project.most_common():
        print(f"  {n:3d}  {project}")
    print()

    for i, (project, text) in enumerate(candidates, 1):
        print(f"{i:3d}. {text}")

    if args.out:
        lines = ["# Mined from real Claude Code transcripts — questions the user actually",
                 "# typed while working. expected_rel_paths deliberately left EMPTY: fill",
                 "# them with `scripts/golden.py find`, which uses filename + ripgrep only.",
                 "# Choosing them from vector-search output would make the eval circular.", ""]
        for i, (_, text) in enumerate(candidates, 1):
            slug = re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-")
            lines += [f"- id: mined-{i:03d}-{slug}",
                      f"  question: {json.dumps(text)}",
                      "  profile: general",
                      "  expected_rel_paths: []",
                      '  notes: "REAL: mined from session transcript."', ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nwrote {len(candidates)} candidates to {args.out}")


if __name__ == "__main__":
    main()
