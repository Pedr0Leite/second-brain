#!/usr/bin/env bash
# Install sn-rag into Claude Code: register the MCP server and the
# /second-brain slash command. Idempotent — safe to re-run after a git pull.
#
# Does NOT install Python packages, Qdrant, ripgrep or Ollama, and does NOT
# build the index. See README.md; those steps have their own failure modes and
# hiding them inside a one-shot installer is how people end up with a silently
# half-working system.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$REPO_ROOT/mcp_server/server.py"
COMMAND_SRC="$REPO_ROOT/scripts/commands/second-brain.md"
COMMAND_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands"
COMMAND_DST="$COMMAND_DIR/second-brain.md"

SCOPE="user"
DRY=0
for arg in "$@"; do
  case "$arg" in
    --scope=*) SCOPE="${arg#*=}" ;;
    --dry-run) DRY=1 ;;
    -h|--help)
      cat <<USAGE
usage: scripts/install.sh [--scope=user|project|local] [--dry-run]

  --scope   where to register the MCP server. Default 'user' = every project on
            this machine. 'project' writes .mcp.json, which gets committed and
            carries machine-specific absolute paths — avoid unless you mean it.
  --dry-run print what would happen, change nothing.
USAGE
      exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

run() { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

echo "sn-rag install"
echo "  repo   : $REPO_ROOT"
echo "  scope  : $SCOPE"
[ "$DRY" = 1 ] && echo "  DRY RUN — nothing will change"
echo

# --- preflight -------------------------------------------------------------
# Fail loudly on a missing prerequisite rather than registering a server that
# cannot start. A tool that registers but always errors is worse than one that
# was never installed: it looks configured.
fail=0
[ -f "$SERVER" ]      || { echo "MISSING: $SERVER"; fail=1; }
[ -f "$COMMAND_SRC" ] || { echo "MISSING: $COMMAND_SRC"; fail=1; }
command -v claude >/dev/null 2>&1 || { echo "MISSING: 'claude' CLI not on PATH"; fail=1; }
command -v python3 >/dev/null 2>&1 || { echo "MISSING: python3"; fail=1; }
[ "$fail" = 0 ] || { echo; echo "install aborted"; exit 1; }

CORPUS="$(cd "$REPO_ROOT" && python3 -c 'import config; print(config.CORPUS_PATH)' 2>/dev/null || true)"
MANIFEST="$(cd "$REPO_ROOT" && python3 -c 'import config; print(config.MANIFEST_DB_PATH)' 2>/dev/null || true)"
if [ -z "$CORPUS" ] || [ -z "$MANIFEST" ]; then
  echo "could not import config from $REPO_ROOT — install Python deps first (see README)"
  exit 1
fi
echo "  corpus : $CORPUS"
echo "  manifest: $MANIFEST"
[ -d "$CORPUS" ] || echo "  WARNING: corpus path does not exist yet"
[ -f "$MANIFEST" ] || echo "  WARNING: manifest does not exist yet — the index has not been built"
echo

# --- 1. slash command ------------------------------------------------------
echo "[1/2] /second-brain command -> $COMMAND_DST"
run mkdir -p "$COMMAND_DIR"
if [ -f "$COMMAND_DST" ] && ! cmp -s "$COMMAND_SRC" "$COMMAND_DST"; then
  # Never silently overwrite an edited command: the user may have tuned it.
  BACKUP="$COMMAND_DST.bak.$(date +%Y%m%d%H%M%S)"
  echo "  existing file differs — backing up to $(basename "$BACKUP")"
  run cp "$COMMAND_DST" "$BACKUP"
fi
run cp "$COMMAND_SRC" "$COMMAND_DST"
echo "  ok"

# --- 2. MCP server ---------------------------------------------------------
echo "[2/2] MCP server 'sn-rag' (scope: $SCOPE)"
# `claude mcp add` errors if the name already exists, so remove first. The
# remove is allowed to fail — on a fresh machine there is nothing to remove.
if [ "$DRY" = 0 ]; then
  claude mcp remove sn-rag --scope "$SCOPE" >/dev/null 2>&1 || true
fi
run claude mcp add sn-rag --scope "$SCOPE" \
  --env "CORPUS_PATH=$CORPUS" \
  --env "MANIFEST_DB_PATH=$MANIFEST" \
  -- python3 "$SERVER"
echo "  ok"

echo
echo "done. verify with:"
echo "  claude mcp list"
echo "  systemctl --user is-active qdrant.service"
echo
echo "then, in a NEW Claude Code session:  /second-brain what is a business rule"
echo "(a session started before this runs will not see either change)"
