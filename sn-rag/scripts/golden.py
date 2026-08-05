"""Authoring tools for eval/golden.yaml — the human-authored evaluation set.

METHODOLOGY WARNING
-------------------
`find` deliberately uses filename and full-text (ripgrep) search only. It never
touches the vector retrieval that run_eval.py evaluates.

If you choose expected_rel_paths by running the semantic search and picking from
its top hits, the evaluation becomes circular: you would be grading retrieval
against the documents retrieval already surfaced, and recall would come out high
by construction while telling you nothing. Expected paths must come from what
*should* answer the question.

Best practice: write the question first, from a task you actually had. Then find
the document you believe answers it. If you cannot find one, that is a valuable
case too — record it with `--expect-none` (see `add --help`).

Subcommands
-----------
  find <pattern>   locate candidate files by path or content (lexical only)
  add              append a case to golden.yaml
  check            validate schema + paths, report coverage gaps
"""
import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CORPUS_PATH

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden.yaml"
VALID_PROFILES = ("general", "servicenow", "personal")
# Kept in step with eval/run_eval.py:MIN_REAL_CASES — only 'real' cases score the gate.
MIN_REAL_FOR_GATE = 20
TARGET_CASES = 30


def cmd_find(args):
    """Locate candidate documents by path or content. Lexical only, by design."""
    corpus = CORPUS_PATH
    matches: list[tuple[str, str]] = []

    # 1. filename / path match
    for path in corpus.rglob("*.md"):
        rel = str(path.relative_to(corpus))
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if args.pattern.lower() in rel.lower():
            matches.append((rel, "path"))
        if len(matches) >= args.limit:
            break

    # 2. content match via ripgrep, RANKED.
    #    `rg -l` yields first-match order, which surfaces glossaries and index
    #    pages ahead of the canonical document. Rank by match density plus
    #    title/path signal so the real answer is not buried.
    if len(matches) < args.limit:
        seen = {rel for rel, _ in matches}
        cmd = ["rg", "--count-matches", "--type", "md", "-i",
               "--", args.pattern, str(corpus)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode == 2:
            print(f"ripgrep error: {proc.stderr.strip()[:300]}", file=sys.stderr)

        scored: list[tuple[float, str]] = []
        needle = args.pattern.lower()
        for line in proc.stdout.splitlines():
            path_str, _, count_str = line.rpartition(":")
            try:
                rel = str(Path(path_str).resolve().relative_to(corpus.resolve()))
                count = int(count_str)
            except (ValueError, OSError):
                continue
            if rel in seen or any(p.startswith(".") for p in Path(rel).parts):
                continue
            if Path(rel).name == "index.md" or rel.endswith("INDEX.md"):
                continue  # navigation dumps, excluded from the index too
            title = (_title_of(corpus / rel) or "").lower()
            score = min(count, 20)
            if needle in title:
                score += 40
            if needle in Path(rel).stem.lower():
                score += 25
            if "glossary" in rel.lower():
                score -= 15
            scored.append((score, rel))

        for score, rel in sorted(scored, reverse=True)[:args.limit - len(matches)]:
            matches.append((rel, f"content:{int(score)}"))

    if not matches:
        print(f"no files matched {args.pattern!r}")
        print("If nothing in the corpus answers your question, that is worth recording:")
        print("  python3 scripts/golden.py add --id <slug> --question '...' --expect-none")
        return

    for rel, how in matches:
        title = _title_of(corpus / rel)
        print(f"[{how:7s}] {rel}")
        if title:
            print(f"            {title}")


def _title_of(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                if line.startswith("title:"):
                    return line.split(":", 1)[1].strip().strip('"')
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        return ""
    return ""


def _load(path: Path) -> list:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def cmd_add(args):
    cases = _load(GOLDEN_PATH)
    if any(c.get("id") == args.id for c in cases):
        raise SystemExit(f"case id {args.id!r} already exists")
    if args.profile not in VALID_PROFILES:
        raise SystemExit(f"profile must be one of {VALID_PROFILES}")

    if args.expect_none:
        expected = []
    else:
        if not args.expect:
            raise SystemExit("pass --expect PATH (repeatable), or --expect-none")
        expected = list(args.expect)
        missing = [p for p in expected if not (CORPUS_PATH / p).exists()]
        if missing:
            raise SystemExit(f"expected paths not found in corpus: {missing}")

    # Provenance decides whether this case can count toward the Phase 4 gate.
    # It defaults to 'real' because that is what the interactive flow is for:
    # someone typing a question they actually asked. Anything written while
    # reading the target document must be marked 'constructed' — it measures the
    # author's vocabulary, not retrieval.
    provenance = "negative" if args.expect_none else args.provenance
    case = {"id": args.id, "question": args.question, "profile": args.profile,
            "provenance": provenance, "expected_rel_paths": expected}
    if args.notes:
        case["notes"] = args.notes
    if args.expect_none:
        case["expect_no_answer"] = True

    with open(GOLDEN_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n" + yaml.safe_dump([case], sort_keys=False, allow_unicode=True,
                                       default_flow_style=False))
    print(f"added {args.id} ({len(cases) + 1} cases total)")


def cmd_check(args):
    cases = _load(GOLDEN_PATH)
    if not cases:
        raise SystemExit(f"no cases in {GOLDEN_PATH}")

    errors: list[str] = []
    seen_ids: set = set()
    for i, case in enumerate(cases):
        cid = case.get("id", f"#{i}")
        for key in ("id", "question", "expected_rel_paths"):
            if key not in case:
                errors.append(f"{cid}: missing '{key}'")
        if cid in seen_ids:
            errors.append(f"{cid}: duplicate id")
        seen_ids.add(cid)
        profile = case.get("profile", "general")
        if profile not in VALID_PROFILES:
            errors.append(f"{cid}: unknown profile {profile!r}")
        paths = case.get("expected_rel_paths") or []
        if not paths and not case.get("expect_no_answer"):
            errors.append(f"{cid}: no expected_rel_paths (use expect_no_answer: true if deliberate)")
        for rel in paths:
            if not (CORPUS_PATH / rel).exists():
                errors.append(f"{cid}: path not in corpus: {rel}")

    examples = [c["id"] for c in cases if str(c.get("id", "")).startswith("example-")]
    by_profile = Counter(c.get("profile", "general") for c in cases)
    negatives = sum(1 for c in cases if c.get("expect_no_answer"))

    # 'real' previously meant "not a shipped example", which read as the much
    # stronger claim "asked before the answer was known" and reported 34/34 while
    # the eval counted 3. Provenance is the authority now.
    prov = Counter(c.get("provenance", "MISSING") for c in cases)
    scoreable = prov.get("real", 0)

    print(f"cases           : {len(cases)}  (shipped examples: {len(examples)})")
    print(f"provenance      : {dict(prov)}")
    print(f"by profile      : {dict(by_profile)}")
    print(f"negative cases  : {negatives}   (questions the corpus should NOT answer)")
    print()
    if prov.get("MISSING"):
        errors.append(f"{prov['MISSING']} case(s) have no 'provenance' field "
                      f"(real | constructed | negative)")
    if scoreable < MIN_REAL_FOR_GATE:
        print(f"NOTE: only {scoreable} case(s) can score the Phase 4 gate "
              f"(need >= {MIN_REAL_FOR_GATE}).")
        print("      Constructed cases still run as a regression signal, but a question")
        print("      written while reading its own answer measures vocabulary, not recall.")
        print()

    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  {e}")
        print()

    gaps = []
    if scoreable < TARGET_CASES:
        gaps.append(f"{TARGET_CASES - scoreable} more REAL cases needed for the gate "
                    f"(have {scoreable} real of {len(cases)} total, target {TARGET_CASES}); "
                    f"constructed cases do not count")
    if examples:
        gaps.append(f"shipped examples still present ({', '.join(examples)}) — replace with real cases")
    for profile in VALID_PROFILES:
        if by_profile.get(profile, 0) < 3:
            gaps.append(f"profile '{profile}' has only {by_profile.get(profile, 0)} cases (aim for >=3)")
    if negatives == 0:
        gaps.append("no negative cases — add questions the corpus genuinely cannot answer, "
                    "so fabrication is detectable (smoke test #3)")

    if gaps:
        print("COVERAGE GAPS:")
        for g in gaps:
            print(f"  - {g}")
    else:
        print("coverage looks adequate; ready to run eval/run_eval.py")

    sys.exit(1 if errors else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", help="locate candidate files (lexical only, never vector search)")
    p_find.add_argument("pattern")
    p_find.add_argument("--limit", type=int, default=15)
    p_find.set_defaults(func=cmd_find)

    p_add = sub.add_parser("add", help="append a case to golden.yaml")
    p_add.add_argument("--id", required=True)
    p_add.add_argument("--question", required=True)
    p_add.add_argument("--profile", default="general")
    p_add.add_argument("--expect", action="append", default=[],
                       help="expected rel_path; repeat for alternatives")
    p_add.add_argument("--expect-none", action="store_true",
                       help="the corpus should NOT be able to answer this (tests fabrication)")
    p_add.add_argument("--notes", default=None)
    p_add.add_argument("--provenance", choices=("real", "constructed"), default="real",
                       help="'real' = you asked this before knowing the answer (counts "
                            "toward the gate); 'constructed' = written while reading the "
                            "target document (regression signal only)")
    p_add.set_defaults(func=cmd_add)

    p_check = sub.add_parser("check", help="validate schema and report coverage gaps")
    p_check.set_defaults(func=cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
