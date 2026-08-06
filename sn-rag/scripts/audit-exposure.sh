#!/usr/bin/env bash
# What on this box is reachable from off-box, and what the firewall does NOT
# cover. Read-only; safe to run any time, no root required (though root shows
# process names and ufw state).
#
# Exists because "the firewall is on" is not the same claim as "nothing is
# exposed". Docker publishes ports by writing iptables rules directly, below
# ufw, so a container on 0.0.0.0 stays reachable no matter what ufw says.
set -uo pipefail

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }

bold "1. Listening on a routable address (reachable from the LAN)"
ss -ltnH 2>/dev/null | awk '{print $4}' \
    | grep -vE '^(127\.|\[::1\])' | sort -u \
    | sed 's/^/  /' || echo "  none"

bold "2. Loopback-only (safe: this box only)"
ss -ltnH 2>/dev/null | awk '{print $4}' \
    | grep -E '^(127\.|\[::1\])' | sort -u \
    | sed 's/^/  /' || echo "  none"

bold "3. sn-rag invariants (CLAUDE.md: both MUST be 127.0.0.1)"
for svc in "qdrant:6333" "ollama:11434"; do
    name="${svc%%:*}"; port="${svc##*:}"
    addrs="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep ":${port}$" | sort -u)"
    if [[ -z "$addrs" ]]; then
        echo "  $name ($port): not listening"
    elif grep -qvE '^(127\.|\[::1\])' <<<"$addrs"; then
        echo "  ✗ $name ($port): EXPOSED on $(tr '\n' ' ' <<<"$addrs")"
        echo "      neither service has ANY authentication."
    else
        echo "  ✓ $name ($port): loopback only"
    fi
done

bold "4. Docker-published ports — these BYPASS ufw"
if command -v docker >/dev/null && docker ps -q >/dev/null 2>&1; then
    exposed=0
    while IFS=$'\t' read -r cname cports; do
        [[ -z "$cports" ]] && continue
        if grep -q '0\.0\.0\.0:' <<<"$cports"; then
            echo "  ✗ $cname"
            tr ',' '\n' <<<"$cports" | grep '0\.0\.0\.0:' | sed 's/^ */      /'
            exposed=$((exposed+1))
        fi
    done < <(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null)
    if [[ $exposed -eq 0 ]]; then
        echo "  ✓ no container published on 0.0.0.0"
    else
        echo
        echo "  ufw rules do NOT apply to the above. To restrict one, change its"
        echo "  port binding to loopback in its compose file and recreate it:"
        echo "      ports: [\"127.0.0.1:8080:8080\"]   # not \"8080:8080\""
    fi
else
    echo "  docker not present or not accessible"
fi

bold "5. Firewall state"
if command -v ufw >/dev/null; then
    ufw status verbose 2>/dev/null | sed 's/^/  /' || echo "  (run with sudo to read ufw state)"
else
    echo "  ufw not installed"
fi

bold "6. IPv6 — the exposure that needs no port-forward"
# IPv4 reaches this box only through NAT, so nothing is externally reachable
# until the router forwards a port. IPv6 usually has no NAT: a global address is
# routable from the internet directly, and every listener on [::] is a candidate
# target the moment the upstream firewall permits it. "I never forwarded a port"
# is not protection here.
v6_global="$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2}' | cut -d/ -f1)"
if [[ -z "$v6_global" ]]; then
    echo "  ✓ no global IPv6 address; only a router port-forward can expose this box"
else
    echo "  This box has GLOBALLY ROUTABLE IPv6:"
    sed 's/^/      /' <<<"$v6_global"
    v6_listeners="$(ss -ltnH 2>/dev/null | awk '$4 ~ /^\[::\]/ {print $4}' | sort -u)"
    if [[ -n "$v6_listeners" ]]; then
        echo
        echo "  Listening on ALL IPv6 interfaces (includes the global address):"
        sed 's/^/      /' <<<"$v6_listeners"
        echo
        echo "  Each is reachable from the internet UNLESS your router firewalls"
        echo "  inbound IPv6. Most consumer routers do by default — but that is a"
        echo "  vendor default, not a guarantee, and it is one setting away from"
        echo "  changing. Verify from outside the network (mobile data, v6 enabled):"
        echo "      curl -6 -sS -m 5 http://[$(head -1 <<<"$v6_global")]:PORT/"
        echo "  A refusal or timeout is good. A response means it is open."
    fi
fi

bold "7. Reachable over IPv4?"
echo "  Only if the router forwards a port to this box. Check its"
echo "  port-forwarding page — this script cannot see the router."
echo "  Public IPv4: $(curl -4 -s --max-time 5 https://ifconfig.me 2>/dev/null || echo '(none / lookup failed)')"
