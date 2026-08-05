# ADR-0006: Remote MCP — serve reads over authenticated HTTP, keep the writer local

**Date:** 2026-08-05
**Status:** Accepted — implemented 2026-08-05 in `mcp_server/http_serve.py`.
Auth, bind validation and the six-tool surface are built and evidenced below.
The port is **not yet opened beyond loopback**: that remains gated on retrieval
quality, per "Sequencing" below.
**Phase:** 7 (operations)
**Follows:** ADR-0005 (how the index gets to the server), ADR-0003 (the tool surface)

## Context

ADR-0005 decided how the *index* reaches a home server: build it on the
workstation, copy it, never rebuild remotely. It deliberately said nothing about
how clients would then *reach* it, because at that point the answer was "they
don't — you SSH in."

The actual goal is different. The desired shape is:

> sn-rag runs on the NiPoGi. Any laptop types `/second-brain <question>` and gets
> a cited answer, the way `obsidian-cli` answers locally today. Each machine
> configures it once in `~/.claude/`.

The MCP server is currently spawned as a **stdio** subprocess by Claude Code:
`python3 mcp_server/server.py`, one process per session, talking over
stdin/stdout. That works only when client and server share a machine and a
filesystem. It cannot span two computers.

Three facts constrain the decision:

1. **An HTTP transport already exists and is unsafe.** `server.py:281`:

   ```python
   if "--http" in sys.argv:
       server.settings.host = "0.0.0.0"
       server.run(transport="streamable-http")
   ```

   It binds every interface with **no authentication of any kind**. It was
   written to smoke-test the transport, and it has never been exposed. Shipping
   it as-is would publish the whole tool surface to the network.

2. **One of the seven tools is a writer.** ADR-0003 established `sn_ingest` as
   the only writer and the only real security boundary: it rejects absolute
   paths, `..`, symlink escapes, null bytes, non-markdown, and refuses the
   `official` vendor source class. Those checks defend against *malformed input*.
   None of them defend against *an unauthorised caller*, because until now there
   was no such thing — the caller was always the local user.

3. **The corpus is not public.** It originates from a corporate OneDrive tenant
   and the repositories are private. Read access is not harmless.

There is also a quality fact that bears on timing, not on design:
`servicenow` recall@10 is **0.455**, and `sn_research` currently retrieves worse
than plain `sn_search` (0.345 vs ~0.53). Widening distribution multiplies the
reach of a system that does not yet answer well.

## Decision

**Serve the six read tools over authenticated HTTP on a private network
interface. Do not expose `sn_ingest` over HTTP at all.**

Concretely:

1. **Transport.** `streamable-http`, the MCP SDK 2.0 transport already imported.
   Clients register a URL instead of a command:

   ```bash
   claude mcp add --transport http sn-rag https://nipogi:8079/mcp -s user \
     --header "Authorization: Bearer $SN_RAG_TOKEN"
   ```

2. **Bind address is explicit and never `0.0.0.0`.** A new `--bind` argument,
   required whenever `--http` is given. The current implicit `0.0.0.0` is
   removed, not defaulted differently — a security-relevant default should have
   to be typed out.

3. **Bearer token, checked before dispatch.** Read from `SN_RAG_TOKEN` in the
   server's environment; a missing or empty token makes `--http` refuse to
   start rather than run open. Comparison uses `hmac.compare_digest`. Failures
   return 401 and are logged with the peer address, never with the presented
   token.

4. **`sn_ingest` is not registered when the transport is HTTP.** Not
   "permission-checked", not "admin-only" — absent from the tool list. Writes
   continue to happen over stdio on the machine that owns the vault, or by SSH.
   A remote caller cannot invoke a tool the server never advertised.

5. **Qdrant and Ollama stay on `127.0.0.1` on the server.** Only the MCP process
   is reachable. This is unchanged from ADR-0002 and is what keeps the
   unauthenticated datastore unauthenticated-but-unreachable.

6. **Network scope is a private overlay (Tailscale) or LAN, not the internet.**
   TLS terminates at the server for anything beyond a trusted LAN segment.

7. **`/second-brain <text>` is a client-side slash command** — a markdown file in
   `~/.claude/commands/` per machine. It is not server functionality and needs no
   protocol support.

**Sequencing:** this is gated behind retrieval quality. The auth work can be
built now; the port should not open until `servicenow` recall is understood.
Distribution is not a fix for relevance.

## Consequences

**Good**

- Client machines need nothing: no Python, no fastembed, no ~300 MB of ONNX
  models, no Qdrant, no copy of the index. One config line.
- One index, one truth. Today, two machines would mean two indexes drifting
  apart, and the vault is a live, partly machine-written corpus.
- The server is always on, so the nightly sync actually runs. On a laptop it
  only runs when the laptop is awake — `Persistent=true` catches up, but late.
- Embedding and reranking happen on the server's CPU, so a thin client gets the
  same latency as a workstation.

**Bad / accepted**

- **A token is a shared secret and this design has exactly one.** Rotation means
  editing every client. Accepted for a single-user, few-machine deployment;
  revisit if it ever becomes multi-user.
- **Ingest becomes less convenient.** Adding a document from a laptop now means
  SSH or a sync into the vault, not an MCP call. Accepted deliberately: the
  writer is the security boundary, and convenience there is what would sell it.
- **A network hop is a new failure mode.** Qdrant being down used to be the only
  backend failure; now the link and the token are too. Errors must stay
  structured (`BACKEND_UNAVAILABLE`), never a silent empty result.
- **The server holds corporate-derived content on an always-on box.** Disk
  encryption and physical security become relevant in a way they were not for a
  laptop that sleeps.

## Alternatives rejected

**Expose the existing `--http` as-is.** Zero work, and it is the reason this ADR
exists. `0.0.0.0` with no auth publishes read access to a private corpus and
write access to the vault, to anyone on the network. Rejected outright.

**SSH tunnel per client, keep stdio.** `ssh -L 8079:localhost:8079` and point the
client at localhost. Genuinely secure, reuses existing key management, and needs
no new server code. Rejected because the tunnel must be up before Claude Code
spawns, and a dropped tunnel surfaces as a confusing tool failure rather than an
auth error. Worth reconsidering if token management proves worse in practice —
it is the strongest rejected option.

**mTLS instead of a bearer token.** Stronger: per-client certificates, real
revocation, no shared secret. Rejected as disproportionate for three machines and
one user, and because client certificate configuration in Claude Code's MCP
client is unproven here. A token that is actually deployed beats a certificate
scheme that is half-configured.

**Keep `sn_ingest` over HTTP behind a second token.** Tempting — remote capture
into the vault is genuinely useful. Rejected because it makes the writer's
security depend on credential hygiene rather than on topology. The current
guarantee is "the writer is unreachable from the network," which is checkable by
looking at the tool list. "The writer is reachable but needs a different token"
is only as good as the weakest client that stores it.

**Replicate the index to each machine instead of serving it.** No network
surface at all, best possible latency, works offline. Rejected because the vault
changes continuously (`wiki/` is LLM-written, `raw/sessions/` is written daily by
hooks) and N copies means N divergent indexes. It also multiplies an 8-hour
embed by N, and ADR-0005 already rejected rebuilding for the same reason.

**A REST API in front of retrieval, with MCP only locally.** More portable —
curl, scripts, other editors. Rejected as a second interface to maintain with its
own caps, error shapes and tests, when the MCP surface already enforces every cap
in code (`mcp_server/caps.py`). Revisit only if a non-MCP client is actually
needed.

## Evidence required before this moves past Proposed

Per the project's evidence rules, none of the following may be asserted without
pasted command output in `BUILD-LOG.md`:

1. ✅ `--http` without `SN_RAG_TOKEN` set **refuses to start** (paste the exit).
2. ✅ A request with no token, and one with a wrong token, both return 401.
3. ✅ `tools/list` over HTTP returns **six** tools, and `sn_ingest` is absent.
4. ⚠️ `ss -ltnp` on the server shows the MCP port bound to the private interface
   only, and 6333/11434 bound to `127.0.0.1`. — **MCP yes; see below.**
5. ⬜ End-to-end `sn_search` from a second machine, with latency compared against
   the same query run locally on the server. — **not yet run.**

Full output in `BUILD-LOG.md`, Phase 7.

## Item 4 found a pre-existing hole, and it was the more serious one

Running the check the ADR itself demanded surfaced that the datastores had been
exposed all along:

```bash
$ ss -ltnp | grep -E "8079|6333|11434"
LISTEN 127.0.0.1:8079   users:(("python3",...))    # MCP, correct
LISTEN   0.0.0.0:6333   users:(("qdrant",...))     # <-- every interface
LISTEN   0.0.0.0:11434                             # <-- every interface

$ curl -s http://192.168.1.235:6333/collections     # from the box's own LAN IP
{"result":{"collections":[{"name":"knowledge"},...]},"status":"ok"}
```

This box is bare metal with LAN addresses `192.168.1.235` / `192.168.1.90`, not a
NAT-shielded WSL guest, so the exposure was real rather than theoretical. Qdrant
has **no authentication**, so any host on the subnet could read, snapshot or
`DELETE` the entire index — bypassing the bearer token entirely.

That inverts part of this ADR's reasoning. The stated risk was "opening an MCP
port widens access"; in fact access was already wider than the MCP port would
have made it, and the datastore had no auth at all where the MCP surface at
least has one. **Putting an authenticated proxy in front of an unauthenticated
datastore that also answers the network directly buys nothing.**

Fix applied: `QDRANT__SERVICE__HOST=127.0.0.1` in the user unit
(`~/.config/systemd/user/qdrant.service`). Staged, not yet active — a restart
mid-index would abort the 15h full embed. **Two actions remain outstanding:**

- restart `qdrant.service` once the full index completes, then re-run `ss -ltnp`
- bind Ollama to loopback (`OLLAMA_HOST=127.0.0.1` in
  `/etc/systemd/system/ollama.service`); it runs as root, so this needs sudo
