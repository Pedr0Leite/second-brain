#!/usr/bin/env bash
# Nightly corpus sync: git pull -> incremental reindex.
#
# CATCH-UP SEMANTICS: this must still run if the machine was off at the
# scheduled time. Two independent mechanisms provide that:
#
#   1. systemd timer with Persistent=true (see scripts/systemd/) — systemd
#      records the last trigger and fires the unit shortly after the next boot
#      if the window was missed. This is the primary mechanism.
#   2. The staleness check below — if the last successful run is older than
#      MAX_AGE_HOURS, the run proceeds regardless of who invoked it. This makes
#      the script safe to also wire into a shell profile or Task Scheduler on
#      hosts without systemd.
#
# Idempotent: running it twice in a row does nothing the second time unless
# --force is passed or the corpus actually changed.

set -euo pipefail

SN_RAG_DIR="${SN_RAG_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CORPUS_PATH="${CORPUS_PATH:?CORPUS_PATH must be set}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/sn-rag}"
STAMP="$STATE_DIR/last-successful-sync"
LOCK="$STATE_DIR/sync.lock"
LOG="$STATE_DIR/sync.log"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-20}"
FORCE=0

[[ "${1:-}" == "--force" ]] && FORCE=1

mkdir -p "$STATE_DIR"
exec > >(tee -a "$LOG") 2>&1
echo "=== sn-rag sync $(date -Is) ==="

# Single instance only. A slow reindex must not overlap the next trigger.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another sync is already running; exiting"
    exit 0
fi

if [[ $FORCE -eq 0 && -f "$STAMP" ]]; then
    last=$(cat "$STAMP")
    age_h=$(( ( $(date +%s) - last ) / 3600 ))
    if (( age_h < MAX_AGE_HOURS )); then
        echo "last successful sync was ${age_h}h ago (< ${MAX_AGE_HOURS}h); nothing to do"
        exit 0
    fi
    echo "last successful sync was ${age_h}h ago; proceeding"
else
    echo "no previous successful sync recorded (or --force); proceeding"
fi

# Qdrant must be up before we touch the index.
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
if ! curl -sf --max-time 10 "$QDRANT_URL/readyz" >/dev/null 2>&1; then
    echo "ERROR: Qdrant not ready at $QDRANT_URL" >&2
    exit 1
fi

cd "$CORPUS_PATH"
before_rev=$(git rev-parse HEAD 2>/dev/null || echo "no-git")
echo "corpus at $before_rev"

# The pull is BEST EFFORT and must never abort the sync.
#
# When the corpus is a live Obsidian vault the user edits, a dirty working tree
# is the normal state, and `git pull` refuses to run. Indexing is driven by
# sha256 of the files actually on disk, so local edits index correctly with or
# without a successful pull. Failing here would mean the index never updates
# for anyone who edits their own notes — the common case, not the edge case.
if [[ "$before_rev" != "no-git" ]] && git remote get-url origin >/dev/null 2>&1; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "WARN: corpus has uncommitted changes; skipping pull, indexing working tree as-is"
    elif git pull --ff-only 2>&1; then
        echo "pull ok"
    else
        echo "WARN: git pull failed; indexing existing working tree anyway"
    fi
else
    echo "no git remote; indexing local corpus as-is"
fi
after_rev=$(git rev-parse HEAD 2>/dev/null || echo "no-git")

if [[ "$before_rev" == "$after_rev" ]]; then
    echo "corpus revision unchanged ($after_rev)"
else
    echo "corpus $before_rev -> $after_rev"
fi

cd "$SN_RAG_DIR"
# 'full' rehashes and marks changed files pending; unchanged files stay indexed.
python3 ingest/index.py full
python3 ingest/index.py embed
python3 ingest/index.py status

date +%s > "$STAMP"
echo "=== sync complete $(date -Is) ==="
