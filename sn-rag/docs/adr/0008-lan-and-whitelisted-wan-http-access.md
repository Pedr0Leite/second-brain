# ADR-0008: LAN access, and whitelisted-WAN access, for the HTTP MCP transport

**Date:** 2026-08-06
**Status:** Accepted — infra prepared 2026-08-06; router port-forward and IP
whitelist are pending user-side steps (no external IP chosen yet).
**Phase:** 7 (operations)

## Context

ADR-0006 built authenticated HTTP transport and, in its running text, argued for
binding it to a mesh-VPN interface (Tailscale/WireGuard) and never a LAN
address. In practice the README's own installation instructions (`README.md`,
"Serving other machines over HTTP") already documented `--bind` accepting
"Tailscale or LAN" — the code never actually restricted it to VPN-only, only to
"not `0.0.0.0`, and required explicit". ADR-0006's prose was stricter than what
was shipped.

This box (`milkserver`) is now the live deployment target: Qdrant holds the full
505,507-chunk index, verified `match = True` against the manifest
(see ADR-0005's update). Two more access patterns are now wanted:

1. **Same-LAN/Wi-Fi clients** reaching this box directly, no VPN required.
2. **Clients outside the LAN** reaching it too, restricted to specific
   whitelisted external IP addresses via a router port-forward plus a host
   firewall rule — not a mesh VPN.

## Decision

**Bind the MCP HTTP server to the box's LAN IP address** (e.g. `192.168.1.235`),
not to a VPN-only interface. This was already permitted by the code
(`http_serve.py` only rejects a missing `--bind` and rejects `0.0.0.0`
specifically); this ADR makes it the recorded, intentional policy rather than
an unexercised option in a help string.

**External access is layered on top with a host firewall, not by loosening the
bind.** The bind address does not change for WAN access — the router forwards
`WAN_PORT -> LAN_IP:8079`, and `ufw` on this box is the actual gate:

- LAN CIDR (`192.168.1.0/24`) is allowed to the MCP port unconditionally —
  trusted network, matches every other service already reachable there.
- WAN traffic is **denied by default**. Specific external IPs are allowed only
  as explicit `ufw allow from <ip> to any port 8079` rules, added one at a time
  as the user provides them. There is no "allow all external" state at any
  point in this design.
- Qdrant (`6333`) and Ollama (`11434`) are **not** touched by this ADR and stay
  bound to `127.0.0.1` only, per ADR-0006's original finding — they have no
  authentication of their own, and the MCP HTTP layer's bearer token is the
  only thing standing between the network and the corpus.

**TLS is explicitly deferred, not silently skipped.** The token and all
request/response bodies travel as plaintext HTTP for both LAN and any
whitelisted-WAN traffic. This is an accepted, named risk for now:

- On the LAN this is the same exposure every other unencrypted LAN service
  already has.
- Over WAN, plaintext means the bearer token and corpus content are readable by
  any network hop between the external client and this box's ISP — a real,
  larger exposure, accepted here explicitly rather than assumed away.
- The mitigation path, when wanted, is a reverse proxy (Caddy) terminating TLS
  in front of the MCP server, using a domain name or free dynamic DNS hostname
  for a Let's Encrypt certificate. Not built now.

## IPv6 undercuts the whole "no port-forward, no exposure" assumption

Discovered while applying this ADR, and material enough to change what "LAN
only" means. This box holds **globally routable IPv6 addresses**
(`2001:8a0:f732:1c00::/64`) with a default IPv6 route to the internet.

IPv4 reaches it only through NAT, so the reasoning above — "external access
requires a deliberate router port-forward" — holds for v4. **It does not hold
for v6.** There is no NAT: a global address is directly routable, and every
listener on `[::]` is a candidate target the instant the upstream firewall
allows it. Nothing has to be forwarded for that to be true.

At the time of writing, seven ports listened on `[::]`, of which `5678` (n8n)
and `2222` (remote shell) carried explicit `ufw ALLOW ... Anywhere (v6)` rules —
i.e. permitted from the entire internet, subject only to whatever the ISP router
does with inbound v6 by default.

Two consequences for this ADR:

- **`--bind <lan-ipv4>` is genuinely LAN-scoped.** The MCP server binds one v4
  address and does not listen on `[::]`, so it is not exposed this way. That is
  a property worth keeping: binding a specific address rather than a wildcard is
  what makes the v6 question moot for this service.
- **The `ufw` rules this project ships are v4-and-v6**, but "ufw denies it" is a
  weaker claim over v6 than over v4. Docker does not create ip6tables DNAT rules
  while `EnableIPv6` is false, so container ports on `[::]` are served by
  `docker-proxy` through the INPUT chain where ufw does apply — but that is an
  artefact of a Docker default, not a guarantee, and it silently inverts if
  Docker's IPv6 support is ever enabled. Binding a container to
  `127.0.0.1:PORT:PORT` does not depend on that behaviour, so it is preferred.

Verification is external by necessity — a box cannot prove its own reachability
from outside. `scripts/audit-exposure.sh` prints the exact `curl -6` to run from
a network that is not this one.

## Consequences

- **IP-whitelisting is a manual, incremental firewall edit**, not a config file
  the app reads. Adding a new remote location means running one more `ufw
  allow from` command. This is deliberate: an app-level whitelist is one more
  thing that can silently drift from the actual firewall state, whereas `ufw
  status numbered` is always the ground truth.
- **A dynamic residential IP breaks external access silently.** Most ISPs do
  not give static IPs; if the box's LAN IP or the client's external IP changes
  (DHCP lease renewal, ISP reassignment), the forward or the whitelist rule
  goes stale. This is not handled here — a static DHCP reservation for
  `192.168.1.235` on the router is recommended but out of scope for this ADR.
- **The router port-forward step cannot be automated from this box.** It is a
  manual step on hardware this project has no access to; `docs/REMOTE-ACCESS.md`
  documents it generically since router UIs vary by vendor.
- **Every privileged step (`ufw`, package installs) requires interactive sudo**
  on this box; none of it was runnable non-interactively during this session.
  Commands are documented for the user to run, not assumed to have run.

## Rejected alternatives

**VPN-only, exactly as ADR-0006's prose originally argued.** Rejected per
explicit user request: LAN clients should not need a VPN client installed and
authenticated just to reach a box on their own network, and some external
clients are wanted without standing up a full mesh VPN for each of them.
Tailscale is still being installed on this box for the cases where a VPN *is*
the right tool (roaming clients, no port-forward needed) — this ADR adds LAN
and whitelisted-WAN as additional paths, it does not remove the VPN one.

**Open WAN access with no whitelist ("it's got a bearer token, that's enough").**
Rejected: a token-only HTTP endpoint on the open internet is a brute-force and
credential-stuffing target, and `hmac.compare_digest` in `http_serve.py`
prevents timing attacks but not a leaked or guessed token. IP-restricting the
network path in front of the token is a second, independent gate.

**TLS required before any WAN exposure.** Considered and rejected for now, not
because it is unimportant, but because the user explicitly chose to accept
plaintext short-term and tighten later — recorded here so the gap is visible
and not rediscovered as a surprise.

**App-level IP whitelist (a config list `http_serve.py` checks per-request).**
Rejected in favor of the OS firewall: `ufw` is already the enforcement point
for every other service on this box, is independently auditable with `ufw
status`, and fails closed if the app crashes — an app-level check only runs if
the app is up.
