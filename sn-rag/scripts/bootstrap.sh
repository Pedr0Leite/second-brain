#!/usr/bin/env bash
# One-command install for sn-rag. Prompts only for what cannot be derived.
#
#   ./scripts/bootstrap.sh                 interactive
#   ./scripts/bootstrap.sh --dry-run       show every step, change nothing
#   CORPUS_PATH=/path ./scripts/bootstrap.sh --yes    fully non-interactive
#
# Installs: Python deps, ripgrep, Qdrant (+ user systemd unit), optionally
# Ollama and the planner model, the MCP registration and the /second-brain
# command.
#
# Does NOT build the index. That is ~8 hours for a 51k-file corpus and belongs
# to a decision you make with your eyes open, not to an installer. The last
# step prints the command.
#
# Every step is idempotent and re-runnable after a git pull. The final section
# VERIFIES each component independently rather than trusting that the install
# steps worked — a bundled installer's real risk is looking successful while
# leaving something half-configured.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/sn-rag"
QDRANT_DIR="$HOME/.local/qdrant"
QDRANT_VERSION="${QDRANT_VERSION:-v1.15.1}"
PLANNER_MODEL="${PLANNER_MODEL:-qwen2.5:3b-instruct}"

DRY=0; YES=0; WANT_OLLAMA=""; WANT_TIMER=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --yes|-y)  YES=1 ;;
    --no-ollama) WANT_OLLAMA=n ;;
    --no-timer)  WANT_TIMER=n ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_off=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  %sok%s   %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '  %swarn%s %s\n' "$c_warn" "$c_off" "$*"; }
err()  { printf '  %sFAIL%s %s\n' "$c_err" "$c_off" "$*"; }
run()  { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

ask() {  # ask <prompt> <default>
  local reply
  if [ "$YES" = 1 ] || [ "$DRY" = 1 ]; then printf '%s' "$2"; return; fi
  read -r -p "$1 [$2]: " reply </dev/tty
  printf '%s' "${reply:-$2}"
}
ask_yn() {
  local reply
  if [ "$YES" = 1 ] || [ "$DRY" = 1 ]; then printf '%s' "$2"; return; fi
  read -r -p "$1 [y/n, default $2]: " reply </dev/tty
  case "${reply:-$2}" in [Yy]*) printf 'y' ;; *) printf 'n' ;; esac
}

say "sn-rag bootstrap"
say "  repo: $REPO"
[ "$DRY" = 1 ] && say "  DRY RUN — nothing will change"

# --------------------------------------------------------------- 1. the vault
step "1. Corpus location"
# The only thing genuinely unknowable. Everything else derives from it or from
# the repo location.
DEFAULT_CORPUS="${CORPUS_PATH:-$HOME/vaults/obsidian-servicenow-docs}"
CORPUS="$(ask "Path to your Obsidian vault / markdown corpus" "$DEFAULT_CORPUS")"
CORPUS="${CORPUS/#\~/$HOME}"
if [ -d "$CORPUS" ]; then
  n_md=$(find "$CORPUS" -name '*.md' -not -path '*/.git/*' 2>/dev/null | wc -l)
  ok "$CORPUS ($n_md markdown files)"
  case "$CORPUS" in
    /mnt/[a-z]/*) warn "corpus is on a Windows drive: lexical search measured 342x slower than ext4" ;;
  esac
else
  err "$CORPUS does not exist"
  [ "$DRY" = 0 ] && { say; say "create it or re-run with the right path"; exit 1; }
fi
export CORPUS_PATH="$CORPUS"

# ------------------------------------------------------------ 2. python deps
step "2. Python packages"
if ! have python3; then err "python3 not found — install it first"; exit 1; fi
say "  $(python3 --version)"
if [ -f "$REPO/requirements.txt" ]; then
  # venv is preferred, but ensurepip is unavailable in some WSL images; fall
  # back to user-site rather than failing. --break-system-packages is required
  # on PEP 668 distros and is a no-op elsewhere.
  if [ "$DRY" = 1 ]; then
    say "  would: pip install -r requirements.txt (venv, else --user)"
  elif python3 -m venv --help >/dev/null 2>&1 && [ -n "${SN_RAG_VENV:-}" ]; then
    python3 -m venv "$SN_RAG_VENV" && "$SN_RAG_VENV/bin/pip" install -q -r "$REPO/requirements.txt" \
      && ok "installed into $SN_RAG_VENV" || err "venv install failed"
  else
    python3 -m pip install -q --user --break-system-packages -r "$REPO/requirements.txt" 2>/dev/null \
      || python3 -m pip install -q --user -r "$REPO/requirements.txt"
    [ $? -eq 0 ] && ok "installed to user site" || err "pip install failed"
  fi
else
  err "requirements.txt missing"
fi

# ---------------------------------------------------------------- 3. ripgrep
step "3. ripgrep (sn_lexical)"
if have rg; then
  ok "$(rg --version | head -1)"
elif [ "$DRY" = 1 ]; then
  say "  would: install ripgrep"
else
  if   have apt-get; then sudo apt-get install -y ripgrep && ok "installed"
  elif have dnf;     then sudo dnf install -y ripgrep && ok "installed"
  elif have pacman;  then sudo pacman -S --noconfirm ripgrep && ok "installed"
  elif have brew;    then brew install ripgrep && ok "installed"
  else err "no known package manager — install ripgrep manually"; fi
fi

# ----------------------------------------------------------------- 4. Qdrant
step "4. Qdrant $QDRANT_VERSION"
# Static binary, not Docker — see docs/adr/0002. No Docker in the target WSL.
if [ -x "$QDRANT_DIR/qdrant" ]; then
  ok "already at $QDRANT_DIR/qdrant"
elif [ "$DRY" = 1 ]; then
  say "  would: download qdrant $QDRANT_VERSION into $QDRANT_DIR"
else
  arch="$(uname -m)"; case "$arch" in
    x86_64|amd64) tgt=x86_64-unknown-linux-musl ;;
    aarch64|arm64) tgt=aarch64-unknown-linux-musl ;;
    *) err "unsupported architecture $arch"; tgt="" ;;
  esac
  if [ -n "$tgt" ]; then
    mkdir -p "$QDRANT_DIR/storage" "$QDRANT_DIR/snapshots"
    url="https://github.com/qdrant/qdrant/releases/download/$QDRANT_VERSION/qdrant-$tgt.tar.gz"
    say "  downloading $url"
    if curl -fsSL "$url" | tar -xz -C "$QDRANT_DIR"; then
      chmod +x "$QDRANT_DIR/qdrant"; ok "installed"
    else
      err "download failed — check QDRANT_VERSION and network"
    fi
  fi
fi

if [ -f "$REPO/scripts/systemd/qdrant.service" ]; then
  run mkdir -p "$HOME/.config/systemd/user"
  run cp "$REPO/scripts/systemd/qdrant.service" "$HOME/.config/systemd/user/qdrant.service"
  if [ "$DRY" = 0 ] && have systemctl; then
    systemctl --user daemon-reload
    systemctl --user enable --now qdrant.service >/dev/null 2>&1 \
      && ok "qdrant.service enabled" || warn "could not enable qdrant.service (no systemd user session?)"
  else
    say "  would: enable qdrant.service"
  fi
fi

# ----------------------------------------------------------- 5. state + cache
step "5. Directories"
run mkdir -p "$STATE_DIR" "$HOME/.cache/fastembed"
ok "$STATE_DIR"
ok "$HOME/.cache/fastembed  (models cached here, not \$TMPDIR)"

# --------------------------------------------------------- 6. Ollama, planner
step "6. Ollama — optional, only for sn_research"
if [ -z "$WANT_OLLAMA" ]; then
  say "  sn_research plans queries with a local model. Measured: it currently"
  say "  retrieves WORSE than plain sn_search (0.345 vs ~0.53). Optional."
  WANT_OLLAMA="$(ask_yn "  install Ollama and pull $PLANNER_MODEL?" n)"
fi
if [ "$WANT_OLLAMA" = y ]; then
  if have ollama; then ok "ollama present"
  elif [ "$DRY" = 1 ]; then say "  would: install ollama"
  else curl -fsSL https://ollama.com/install.sh | sh && ok "installed" || err "install failed"
  fi
  if have ollama && [ "$DRY" = 0 ]; then
    ollama pull "$PLANNER_MODEL" && ok "$PLANNER_MODEL pulled" || warn "pull failed"
  fi
else
  say "  skipped — sn_search, sn_lexical, sn_get_section, sn_outline all work without it"
fi

# ------------------------------------------------- 7. MCP + /second-brain
step "7. Claude Code registration"
if [ -x "$REPO/scripts/install.sh" ]; then
  if [ "$DRY" = 1 ]; then "$REPO/scripts/install.sh" --dry-run | sed 's/^/  /'
  else "$REPO/scripts/install.sh" | sed 's/^/  /'; fi
else
  err "scripts/install.sh missing"
fi

# ------------------------------------------------------------ 8. nightly sync
step "8. Nightly sync timer"
if [ -z "$WANT_TIMER" ]; then
  WANT_TIMER="$(ask_yn "  install the nightly incremental sync timer?" y)"
fi
if [ "$WANT_TIMER" = y ] && [ -x "$REPO/scripts/systemd/install.sh" ]; then
  if [ "$DRY" = 1 ]; then say "  would: install sn-rag-sync.timer"
  else CORPUS_PATH="$CORPUS" SN_RAG_DIR="$REPO" "$REPO/scripts/systemd/install.sh" | sed 's/^/  /'; fi
else
  say "  skipped"
fi

# ------------------------------------------------------------- 9. verify
step "9. Verify — checking each component independently"
[ "$DRY" = 1 ] && { say "  (skipped in dry run)"; exit 0; }

fails=0
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else err "$1"; fails=$((fails+1)); fi; }

check "python deps import"      "cd '$REPO' && python3 -c 'import fastembed, qdrant_client, mcp, yaml'"
check "config imports"          "cd '$REPO' && python3 -c 'import config'"
check "ripgrep on PATH"         "command -v rg"
check "qdrant binary"           "test -x '$QDRANT_DIR/qdrant'"
check "qdrant responding :6333" "curl -fsS http://localhost:6333/readyz"
check "claude CLI"              "command -v claude"
check "sn-rag registered"       "claude mcp list 2>/dev/null | grep -q sn-rag"
check "/second-brain installed" "test -f '$HOME/.claude/commands/second-brain.md'"
check "test suite"              "cd '$REPO' && python3 -m pytest tests/ -q"

say
paths="$(cd "$REPO" && python3 -c 'import config; print(config.CORPUS_PATH); print(config.MANIFEST_DB_PATH)' 2>/dev/null)"
say "  resolved corpus  : $(printf '%s' "$paths" | sed -n 1p)"
say "  resolved manifest: $(printf '%s' "$paths" | sed -n 2p)"

if [ "$fails" -gt 0 ]; then
  say
  err "$fails check(s) failed — fix these before building the index"
  exit 1
fi

say
say "${c_ok}All checks passed.${c_off}"
say

# Whether the index exists is a fact to look up, not to assume. Telling someone
# with a 505k-chunk index to go build one reads as "the installer has no idea
# what state this machine is in" — which would be true.
COLL="$(cd "$REPO" && python3 -c 'import config; print(config.QDRANT_COLLECTION)' 2>/dev/null || echo knowledge)"
POINTS="$(curl -fsS "http://localhost:6333/collections/$COLL" 2>/dev/null \
          | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["points_count"])' 2>/dev/null || echo "")"

if [ -n "$POINTS" ] && [ "$POINTS" -gt 0 ] 2>/dev/null; then
  say "Index present: ${POINTS} chunks in '$COLL'."
  say
  say "  Confirm manifest and Qdrant agree:"
  say "    cd $REPO && python3 ingest/index.py status      # expect match = True"
  say
  say "  Pick up new or changed files (incremental, minutes):"
  say "    python3 ingest/index.py full && python3 ingest/index.py embed"
else
  say "The index is NOT built yet — nothing will return results until it is."
  say
  say "  Sample first (minutes), confirm it looks sane:"
  say "    cd $REPO"
  say "    python3 ingest/index.py full --limit 500 --shuffle"
  say "    python3 ingest/index.py embed --limit 500 --shuffle"
  say "    python3 ingest/index.py status          # expect match = True"
  say
  say "  Then the full corpus (hours; ~8h for 51k files at 17.6 chunks/s):"
  say "    python3 ingest/index.py full"
  say "    setsid nohup python3 ingest/index.py embed --shuffle \\"
  say "      >> $STATE_DIR/full-embed.log 2>&1 < /dev/null &"
fi

say
say "Then start a NEW Claude Code session and try:"
say "    /second-brain what is a business rule"
