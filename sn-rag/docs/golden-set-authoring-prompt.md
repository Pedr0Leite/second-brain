# Prompt: author the golden set in a separate session

Paste everything below the line into a fresh Claude Code session, started in the
`sn-rag` directory.

**Why a separate session:** this is interview + archaeology work, unrelated to the
build. It benefits from a clean context, and keeps the build session free.

**Read this first — the failure mode this prompt is designed around:**
An LLM asked to "write evaluation questions" will read documents and invent
questions about them. That is the one thing that must not happen: questions
reverse-engineered from a document inherit its vocabulary, so retrieval matches on
shared words, recall comes out near-perfect, and the evaluation measures nothing.
The prompt below therefore forbids inventing questions and pushes toward mining
your real history and interviewing you instead.

---

You are helping me build a **golden evaluation set** for a local RAG system over a
~51,000-file ServiceNow documentation corpus plus my personal notes (Notion
migration, wiki layer, custom app notes).

A golden set is a list of questions paired with the documents that *should* answer
them. It is used to measure retrieval recall. Read
`docs/GOLDEN-SET-GUIDE.md` in this repo before doing anything.

## Absolute constraints

1. **Never invent questions by reading documents.** A question written while
   looking at its answer inherits that document's vocabulary and makes the
   evaluation worthless. This is the single most important rule.
2. **Never choose expected file paths using semantic/vector search.** Use only
   `python3 scripts/golden.py find <pattern>` (filename + ripgrep) or plain
   `rg`/`ls`. Choosing paths from vector-search output makes the evaluation
   circular.
3. **Do not write cases on my behalf from your own ServiceNow knowledge.** Generic
   questions you already know the answer to are not what I actually asked at work.
4. Every case must be added via `python3 scripts/golden.py add` so paths are
   validated. Do not hand-edit `eval/golden.yaml`.

## Your task, in order

### Step 1 — Mine my real history first (highest value)

Search for questions I have actually asked. Look in:

- `~/.claude/` history / transcripts, and any `*.jsonl` session logs
- the corpus vault: `raw/sessions/`, `raw/inbox/`, `chats/`, `logs/`
- any TODO/question/"how do I" phrasing in my personal notes (`Notion/`, `wiki/`)

Extract things phrased as genuine questions or problems. Show me a numbered list of
candidates with where you found each one. Do **not** add anything yet.

### Step 2 — Interview me for the rest

For whatever the history does not supply, ask me — a few questions at a time, not
all at once:

- What did you last have to look up at work?
- What do you look up repeatedly?
- What do colleagues ask you?
- What did the old `INDEX.md` + grep workflow handle badly?
- Where do your own notes contradict or extend the official docs?
- What have you looked for that genuinely is not in the corpus? (negatives)

Record my phrasing **verbatim**. Do not tidy it up, do not make it more
documentation-like. Awkward, vague and ambiguous phrasings are the valuable ones.

### Step 3 — Find the expected documents

For each question, help me locate the file(s) that should answer it, using
`scripts/golden.py find` or `rg`. Show candidates; **I** confirm which are correct.
If I am unsure, ask rather than guess.

If nothing answers it, that is a negative case — record it with `--expect-none`.

### Step 4 — Record

```bash
python3 scripts/golden.py add \
  --id <short-slug> \
  --question "<my exact phrasing>" \
  --profile <general|servicenow|personal> \
  --expect "<rel/path.md>" \
  --notes "<where this question came from>"
```

Profiles: `servicenow` = official vendor docs only; `personal` = my own
notes/wiki/apps only; `general` = everything.

### Step 5 — Track coverage

Run `python3 scripts/golden.py check` regularly and report gaps. Targets:

- 30+ real cases (shipped `example-*` cases replaced or removed)
- at least 3 per profile
- 3–5 negative cases
- spread across: exact API symbols, "how do I" tasks, conceptual questions with no
  single canonical page, release-specific questions, personal-notes-only questions

## Output

Work incrementally. After each batch of ~5 cases, show me what was added and the
current `check` output. Stop and ask when you are unsure — a wrong expected path is
worse than a missing case, because it looks like a retrieval failure at eval time.

Do not run `eval/run_eval.py`; that is for the build session.
