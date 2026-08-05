#!/usr/bin/env bash
# Install the nightly sync as a user systemd timer with missed-run catch-up.
#
# User units (not system units) are used deliberately: they need no root, and
# they run as the account that owns the corpus checkout and the model cache.
#
# NOTE: user timers only run while the user has a session. If the sync must run
# on a headless server with nobody logged in, enable lingering:
#     sudo loginctl enable-linger "$USER"
# Without lingering, the catch-up still works — it fires when you next log in.

set -euo pipefail

SN_RAG_DIR="${SN_RAG_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CORPUS_PATH="${CORPUS_PATH:?set CORPUS_PATH to the corpus checkout}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"

UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="$HOME/.config/sn-rag"
mkdir -p "$UNIT_DIR" "$ENV_DIR"

# Resolve the manifest through config rather than hardcoding it. This line used
# to read "$SN_RAG_DIR/manifest.db", which on this layout is a repo-local path on
# /mnt/c that sqlite happily CREATES EMPTY — the nightly sync would then index
# into a database nothing else reads, reporting success the whole time. That
# failure has happened once already; see CLAUDE.md "Environment".
MANIFEST_DB_PATH="${MANIFEST_DB_PATH:-$(cd "$SN_RAG_DIR" && python3 -c 'import config; print(config.MANIFEST_DB_PATH)')}"

cat > "$ENV_DIR/sync.env" <<EOF
CORPUS_PATH=$CORPUS_PATH
MANIFEST_DB_PATH=$MANIFEST_DB_PATH
QDRANT_URL=$QDRANT_URL
SN_RAG_DIR=$SN_RAG_DIR
PYTHONPATH=$SN_RAG_DIR
EOF

# %h in the units resolves to $HOME; the units expect the repo at ~/sn-rag.
# If it lives elsewhere, substitute the real path while installing.
sed "s#%h/sn-rag#$SN_RAG_DIR#g" "$SN_RAG_DIR/scripts/systemd/sn-rag-sync.service" \
    > "$UNIT_DIR/sn-rag-sync.service"
cp "$SN_RAG_DIR/scripts/systemd/sn-rag-sync.timer" "$UNIT_DIR/sn-rag-sync.timer"
chmod +x "$SN_RAG_DIR/scripts/nightly_sync.sh"

# Seed the last-run stamp so installing the timer does not immediately trigger
# a full index. Persistent=true treats a never-run timer as having missed its
# window and fires it at once — correct for catch-up, surprising on install,
# especially when the first index is a multi-hour job.
STATE_DIR="${STATE_DIR:-$HOME/.local/state/sn-rag}"
mkdir -p "$STATE_DIR"
if [[ ! -f "$STATE_DIR/last-successful-sync" ]]; then
    date +%s > "$STATE_DIR/last-successful-sync"
    echo "seeded sync stamp; first automatic run will be at the next scheduled window"
    echo "to index now instead, run: $SN_RAG_DIR/scripts/nightly_sync.sh --force"
fi

systemctl --user daemon-reload
systemctl --user enable --now sn-rag-sync.timer

echo
echo "installed. verify with:"
echo "  systemctl --user list-timers sn-rag-sync.timer"
echo "  systemctl --user status sn-rag-sync.service"
echo "  journalctl --user -u sn-rag-sync.service -n 50"
echo
echo "run once now:  systemctl --user start sn-rag-sync.service"
echo "force a full:  $SN_RAG_DIR/scripts/nightly_sync.sh --force"
