# Reaching sn-rag from another machine

How to let a laptop — on the same Wi-Fi, or anywhere on the internet — query an
index that lives on a server. Written to be followed on any machine, not just
the one it was developed on.

Decisions and rejected alternatives: **ADR-0006** (authenticated HTTP transport)
and **ADR-0008** (LAN and whitelisted-WAN access).

> **Read this first.** The MCP server holds a private documentation corpus. Every
> step below either keeps that closed or opens it. Nothing here is decoration.

---

## Pick a path

| you want | use | opens a router port? |
|---|---|---|
| clients on the same LAN/Wi-Fi | **Path A** | no |
| clients anywhere, minimal exposure | **Path B — zero-trust / mesh VPN** | no |
| clients anywhere, no VPN client installed | **Path C — port-forward + IP whitelist** | yes |

**Prefer B over C.** C is documented because it was asked for, and it works, but
it puts a service on the public internet whose only gates are one bearer token
and a list of IP addresses. B puts nothing on the public internet at all.

---

## Path A — same LAN

**1. Start the server bound to the LAN address.**

The provided unit resolves the address itself, so it survives a DHCP change
across restarts:

```bash
cp scripts/systemd/sn-rag-mcp-http.service ~/.config/systemd/user/
# if the repo is not at ~/sn-rag, substitute the real path:
sed -i "s#%h/sn-rag#$PWD#g" ~/.config/systemd/user/sn-rag-mcp-http.service
systemctl --user daemon-reload
```

**2. Generate the bearer token.** The server refuses to start without one, and
refuses tokens shorter than 16 characters:

```bash
mkdir -p ~/.config/sn-rag
printf 'SN_RAG_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
    > ~/.config/sn-rag/http.env
chmod 600 ~/.config/sn-rag/http.env
```

**3. Survive logout.** User units stop when the last session ends unless
lingering is on — on a headless box that means the server dies and never
returns:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user enable --now sn-rag-mcp-http.service
systemctl --user status sn-rag-mcp-http.service
```

Expect a log line naming the bind address, the tool count, and the allowed
hosts:

```
sn-rag: HTTP on 192.168.1.235:8079, 6 tools, auth required, allowed hosts: [...]
```

Six tools, not seven: `sn_ingest` is never registered over HTTP (ADR-0006).

**4. Open the LAN in the firewall:**

```bash
sudo ./scripts/setup-firewall.sh
```

This detects the LAN CIDR from the default route, preserves SSH first
(anti-lockout), allows the LAN to the MCP port, denies everything else inbound,
and explicitly denies Qdrant and Ollama from off-box.

**5. Register from the client machine:**

```bash
claude mcp add --transport http sn-rag http://<server-lan-ip>:8079/mcp -s user \
  --header "Authorization: Bearer <token>"
claude mcp list        # expect: sn-rag ... ✔ Connected
```

**6. Verify it actually retrieves**, not just connects. In a new session ask
something the corpus answers, and confirm citations come back.

---

## Path B — zero-trust / mesh VPN (recommended for remote)

No router port is opened. The client joins a private network and reaches the
server as if it were local.

**Twingate**, **Tailscale**, and **WireGuard** all work. Whichever you use:

1. Install its client on the server and on the client machine, and authenticate
   both (this step is interactive and cannot be scripted for you).
2. Find the address the server has *on that private network* — for Tailscale,
   `tailscale ip -4` gives a `100.x.y.z`.
3. Bind the MCP server to that address instead of the LAN one:
   ```bash
   systemctl --user edit sn-rag-mcp-http.service
   # add:  [Service]
   #       Environment=SN_RAG_BIND=100.x.y.z
   systemctl --user restart sn-rag-mcp-http.service
   ```
4. Register on the client with that address. No firewall change, no router
   change.

The bearer token still applies. The VPN authenticates the *device*; the token
authenticates the *request*. Neither is a substitute for the other.

---

## Path C — port-forward with an IP whitelist

Only after Path A works on the LAN. If it does not work locally it will not work
remotely, and you will be debugging two problems at once.

### C1. Give the server a fixed LAN address

A port-forward points at an IP. If DHCP reassigns it, the forward silently aims
at the wrong machine. In the router's DHCP settings, reserve the server's
current address against its MAC:

```bash
ip -4 addr show | grep 'inet '        # the address
ip link show                          # the MAC (link/ether)
```

### C2. Forward a port on the router

Router UIs differ; the page is called **Port Forwarding**, **Virtual Server**,
or **NAT** — usually under Advanced, Firewall, or WAN.

1. Open the router's admin page (commonly the default gateway address:
   `ip -4 route | awk '/default/{print $3; exit}'`).
2. Create a rule:

   | field | value |
   |---|---|
   | external / WAN port | `8079` (or a different one — see below) |
   | internal / LAN IP | the server's reserved address |
   | internal / LAN port | `8079` |
   | protocol | TCP |

3. Save and apply. Some routers require a reboot.

**Consider a non-obvious external port.** Mapping external `41879` → internal
`8079` will not stop a determined scanner, but it does drop the volume of
automated background probing. Update `SN_RAG_ALLOWED_HOSTS` accordingly (C4).

### C3. Whitelist the specific client, then verify the default is closed

Get the client's public address **from the client machine**:

```bash
curl -s https://ifconfig.me
```

Then, on the server:

```bash
sudo ./scripts/setup-firewall.sh --allow-external <that-ip>
sudo ./scripts/setup-firewall.sh --status
```

Whitelisting is one address at a time by design. The script refuses a `/0`
CIDR outright — that is not a whitelist, it is an open port.

**Most home connections have a dynamic public IP.** When the client's ISP
reassigns it, access stops working with a timeout rather than an error. Re-run
`--allow-external` with the new address and `--revoke` the old one.

### C4. Teach the server its external name

A client arriving through NAT sends the *external* address in its `Host` header,
which the MCP SDK's DNS-rebinding protection rejects unless it is listed:

```bash
systemctl --user edit sn-rag-mcp-http.service
# add:  [Service]
#       Environment=SN_RAG_ALLOWED_HOSTS=your.ddns.example:41879,203.0.113.9:41879
systemctl --user restart sn-rag-mcp-http.service
```

Without this the request fails with `Invalid Host header` (HTTP 421) even though
the token and the firewall are both correct.

### C5. A dynamic public IP on the *server* breaks everything

If the server's own public address changes, remote clients point at nothing. Use
a dynamic-DNS hostname and register that name in `SN_RAG_ALLOWED_HOSTS`.

---

## Before you leave a port open

**Plaintext HTTP is the current state, and it is a real exposure.**

The bearer token and every retrieved document travel unencrypted. On the LAN
that matches every other unencrypted service on your network. **Across the
internet it means any intermediary can read the token and the corpus content**,
and a captured token works until it is rotated.

Path B avoids this entirely — the VPN encrypts the tunnel. If you use Path C,
put TLS in front before treating it as permanent: a reverse proxy (Caddy is two
lines of config) with a dynamic-DNS hostname gets a free Let's Encrypt
certificate. This is deliberately deferred, not solved — ADR-0008 records it.

**Rotate the token** if it may have been observed:

```bash
printf 'SN_RAG_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
    > ~/.config/sn-rag/http.env
chmod 600 ~/.config/sn-rag/http.env
systemctl --user restart sn-rag-mcp-http.service
```

Every client must be re-registered with the new value.

---

## IPv6: exposure without a port-forward

Everything above assumes external access requires forwarding a port. **That is
only true for IPv4.**

If your ISP provides IPv6, this box likely has a *globally routable* address and
there is no NAT in front of it. Any service listening on `[::]` — the IPv6
wildcard — is directly addressable from the internet the moment the upstream
router permits inbound v6. You never forwarded anything; it does not matter.

```bash
ip -6 addr show scope global          # a 2000::/3 address means globally routable
ss -ltnH | awk '$4 ~ /^\[::\]/'       # everything a v6 client could reach
```

Most consumer routers firewall inbound IPv6 by default. That is a vendor
default, not a guarantee, and it is one settings change away from being untrue.
**Test it rather than trusting it**, from a network that is not yours — a phone
on mobile data works:

```bash
curl -6 -sS -m 5 "http://[<the-global-v6-address>]:<port>/"
```

A timeout or refusal is what you want. A response means that port is open to the
internet.

The mitigations, in order of preference:

1. **Bind to a specific address, not a wildcard.** `--bind 192.168.1.235` listens
   on one IPv4 address and never on `[::]`. This is what sn-rag does, and it is
   why the MCP server is not exposed this way.
2. **Bind containers to loopback**: `ports: ["127.0.0.1:8080:8080"]`. Unlike ufw
   rules, this does not depend on Docker's iptables behaviour staying as it is.
3. Enable inbound IPv6 filtering on the router.

## Audit what is actually exposed

Assertions about a firewall are worth less than its output:

```bash
./scripts/audit-exposure.sh
```

It lists what is reachable off-box, checks the two sn-rag invariants (Qdrant and
Ollama must be loopback-only — neither has any authentication), and flags
something a firewall alone will not save you from:

> **Docker bypasses ufw.** Containers published with `-p 8080:8080` bind
> `0.0.0.0` and write their own iptables rules *below* ufw. `ufw deny 8080` does
> not close them. The fix is per-container, in its compose file:
> `ports: ["127.0.0.1:8080:8080"]`, then recreate it.

That caveat applies to anything else already running on the box — a container
management UI reachable on the LAN is a path to the Docker daemon, which is
root.

---

## Verifying a client, in one prompt

Paste this into a **new** Claude Code session on the client machine. It is
written so that a wrong answer is visible rather than plausible:

```text
Verify my sn-rag MCP connection is reaching the remote server, not a local copy.

1. List the sn-rag tools available to you and count them.
2. Call sn_stats and report the indexed chunk and file counts.
3. Call sn_search with the query "what is a business rule" and agent
   "servicenow". Report the top result's rel_path and score.

Then tell me PASS or FAIL against these, and do not fill any gap from your own
knowledge — if a tool errors, quote the structured error verbatim:

  - exactly 6 tools, and sn_ingest is NOT among them
  - sn_stats reports 505507 chunks across 51588 indexed files
  - the search returns real results with rel_path citations

If sn_ingest IS present, this is a local stdio server and the answer is FAIL
regardless of anything else.

Do NOT adjust any criterion to match what you observe. If your count disagrees
with the expected one, that is the finding — report the disagreement and list
the tool names you actually see. A criterion you rewrote to fit the result has
verified nothing.
```

**Why these three.** The tool count is the one thing a local process cannot
imitate — `sn_ingest` is never registered over HTTP, so its absence proves the
request crossed the network. The chunk count proves it reached the *right*
index rather than an empty local one. The search proves retrieval actually
works, since a server can hold an index and still fail to query it.

Substitute your own chunk/file counts from `python3 ingest/index.py status` on
the server.

## Troubleshooting

| symptom | cause |
|---|---|
| `Invalid Host header` (421) | the `Host` the client sent is not in `SN_RAG_ALLOWED_HOSTS` — see C4 |
| `401 unauthorized` | token missing or wrong; check the `Authorization: Bearer` header |
| `Missing session ID` from `curl` | expected — a raw `curl` is not an MCP client; it skipped `initialize` |
| refuses to start, exit 2 | no `--bind`, a `0.0.0.0` bind, or a token under 16 chars. All deliberate |
| connects on LAN, times out remotely | port-forward missing, or the client's public IP is not whitelisted, or it changed |
| works, then stops after a reboot | lingering not enabled: `sudo loginctl enable-linger $USER` |
| `sn_ingest` missing over HTTP | correct. It is never registered on the HTTP surface (ADR-0006) |
