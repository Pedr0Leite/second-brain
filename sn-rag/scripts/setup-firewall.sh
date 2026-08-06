#!/usr/bin/env bash
# Host firewall for the MCP HTTP surface (ADR-0008).
#
# The bind address is not the security boundary — this is. `--bind` decides
# which interface answers; ufw decides who is allowed to reach it. External
# access is deny-by-default: WAN clients are permitted one IP at a time, never
# as a range and never as "allow anything from the internet".
#
# Needs root. Run it yourself; it is deliberately not invoked by any unit.
#
#   sudo ./scripts/setup-firewall.sh                       # LAN only
#   sudo ./scripts/setup-firewall.sh --allow-external IP   # add one WAN client
#   sudo ./scripts/setup-firewall.sh --revoke IP           # remove one
#   sudo ./scripts/setup-firewall.sh --status              # show current rules
set -euo pipefail

PORT="${SN_RAG_PORT:-8079}"
LAN_CIDR="${SN_RAG_LAN_CIDR:-}"

die() { echo "sn-rag firewall: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"
command -v ufw >/dev/null || die "ufw not installed: apt install ufw"

# Derive the LAN from the default route rather than hardcoding a /24. A wrong
# guess here either locks out the LAN or opens more than intended.
if [[ -z "$LAN_CIDR" ]]; then
    lan_ip="$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
    [[ -n "$lan_ip" ]] || die "could not detect LAN IP; set SN_RAG_LAN_CIDR explicitly"
    LAN_CIDR="$(ip -4 route | awk -v ip="$lan_ip" '$0 ~ "src "ip && /\// {print $1; exit}')"
    [[ -n "$LAN_CIDR" ]] || die "could not detect LAN CIDR; set SN_RAG_LAN_CIDR explicitly"
fi

case "${1:-}" in
    --status)
        ufw status numbered
        exit 0
        ;;
    --allow-external)
        ip="${2:?usage: --allow-external <ip-or-cidr>}"
        # A bare /0 would silently turn the whitelist into an open port, which is
        # the exact outcome this script exists to prevent.
        [[ "$ip" == *"/0" ]] && die "refusing $ip: that is the whole internet, not a whitelist"
        ufw allow from "$ip" to any port "$PORT" proto tcp \
            comment "sn-rag MCP: external whitelist"
        echo "allowed $ip -> :$PORT"
        ufw status numbered | grep "$PORT" || true
        exit 0
        ;;
    --revoke)
        ip="${2:?usage: --revoke <ip-or-cidr>}"
        ufw delete allow from "$ip" to any port "$PORT" proto tcp
        echo "revoked $ip"
        exit 0
        ;;
    "") ;;
    *) die "unknown argument: $1" ;;
esac

echo "LAN detected as $LAN_CIDR, MCP port $PORT"

# Anti-lockout. `ufw default deny incoming` drops remote administration on a box
# whose only access path is SSH, so every candidate SSH port is allowed BEFORE
# the policy is enabled.
#
# Do not trust process-name detection alone: `ss -ltnp` shows names only to root,
# and sshd is routinely moved off 22 (this box listens on 2222). A missed port
# here is a locked-out machine, so the rule is to over-allow SSH rather than
# guess precisely — an open SSH port is a known, authenticated service; a
# firewall that severs administration is an outage.
ssh_ports="$(ss -ltnpH 2>/dev/null | grep -i sshd \
    | awk '{print $4}' | sed 's/.*://' | sort -u || true)"

# Explicit override always wins, for a box reached on a port nothing detects.
[[ -n "${SN_RAG_SSH_PORT:-}" ]] && ssh_ports="$ssh_ports ${SN_RAG_SSH_PORT}"

# Fall back to any port that looks like an administrative shell, detected or not.
for candidate in 22 2222; do
    if ss -ltnH "sport = :$candidate" 2>/dev/null | grep -q LISTEN; then
        grep -qw "$candidate" <<<"$ssh_ports" || ssh_ports="$ssh_ports $candidate"
    fi
done

ssh_ports="$(tr ' ' '\n' <<<"$ssh_ports" | grep -v '^$' | sort -u || true)"

if [[ -n "$ssh_ports" ]]; then
    for p in $ssh_ports; do
        echo "preserving remote access on port $p (anti-lockout)"
        ufw allow "$p"/tcp comment "remote shell (anti-lockout)"
    done
else
    echo
    echo "WARNING: no SSH-like listener found on 22 or 2222."
    echo "If you administer this box remotely on some other port, abort now"
    echo "(Ctrl-C) and re-run with: sudo SN_RAG_SSH_PORT=<port> $0"
    echo "Continuing in 10s — enabling a default-deny firewall without this is"
    echo "how a remote box becomes unreachable."
    sleep 10
fi

ufw default deny incoming
ufw default allow outgoing

# The LAN is trusted for this service: any device already on the network can
# reach it without a VPN. The bearer token still applies to every request.
ufw allow from "$LAN_CIDR" to any port "$PORT" proto tcp \
    comment "sn-rag MCP: LAN"

# Qdrant and Ollama are loopback-bound in their units and must stay that way;
# these denies are defence in depth for the case where a future edit rebinds
# one of them to a routable address without anyone noticing.
ufw deny "${QDRANT_PORT:-6333}"/tcp comment "qdrant: never off-box (no auth)"
ufw deny "${OLLAMA_PORT:-11434}"/tcp comment "ollama: never off-box (no auth)"

ufw --force enable

echo
ufw status numbered
cat <<EOF

LAN clients can now reach the MCP server. External clients cannot yet — that is
deliberate; nothing from outside is permitted until you name an address:

    sudo $0 --allow-external <their-public-ip>

Find a client's public IP from that client: curl -s https://ifconfig.me

WARNING: Docker publishes container ports by writing iptables rules directly and
bypasses ufw entirely. The sn-rag MCP server is not containerised, so these rules
govern it — but do not assume ufw protects anything you later run in Docker.
EOF
