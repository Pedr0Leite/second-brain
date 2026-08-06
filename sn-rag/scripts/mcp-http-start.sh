#!/usr/bin/env bash
# Resolves the box's LAN IP at start time and execs the HTTP MCP server bound
# to it (ADR-0008). Dynamic resolution, not a hardcoded address, so the same
# unit works on any machine and self-heals across a DHCP lease change on
# restart — it will NOT rebind while already running if the IP changes
# underneath it; Restart=always only helps on a crash or manual restart.
set -euo pipefail

SN_RAG_DIR="${SN_RAG_DIR:?set SN_RAG_DIR to the sn-rag repo path}"
cd "$SN_RAG_DIR"

if [[ -z "${SN_RAG_BIND:-}" ]]; then
    SN_RAG_BIND="$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
fi

if [[ -z "$SN_RAG_BIND" ]]; then
    echo "sn-rag: could not auto-detect a LAN IP to bind; set SN_RAG_BIND explicitly" >&2
    exit 1
fi

exec python3 mcp_server/server.py --http --bind "$SN_RAG_BIND" --port "${SN_RAG_PORT:-8079}"
