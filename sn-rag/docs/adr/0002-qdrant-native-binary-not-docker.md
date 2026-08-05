# ADR-0002: Run Qdrant as a native binary in development, Docker for deployment

**Date:** 2026-08-04
**Status:** Accepted (development only — deployment target unchanged)
**Phase:** 3

## Context

The build spec (§3, §8) specifies a `docker-compose.yml` running Qdrant, the
embedding server, reranker, Langfuse, agent, and MCP server as containers on the
Nipogi/Ryzen 7 server under Portainer.

Phase 3 needs a working Qdrant *now* to satisfy its acceptance criteria (500-doc
sample index, point-count reconciliation, RAM high-water mark). The current
development machine is a WSL2 environment where `docker`, `docker compose`, and
`nvidia-smi` are all absent — Docker Desktop WSL integration is not enabled for
this distro. Blocker §10.1 assigns Docker installation to the human.

Waiting on Docker would block all of Phase 3 on an unrelated setup task.

## Decision

Run Qdrant from the official statically-linked release binary
(`qdrant-x86_64-unknown-linux-gnu`, v1.18.3, ~85MB extracted) for local
development and phase acceptance testing. Storage lives on the Linux ext4
filesystem (`/home/pedro/.local/qdrant/storage`), not the `/mnt/c` 9p mount,
which is an order of magnitude slower for small random I/O.

This is the **same Qdrant build** the Docker image wraps, at the same version,
with the same configuration surface (`QDRANT__*` environment variables). Vector
storage format, quantization, and the HTTP/gRPC API are identical, so the
collection created here is loadable by a containerised Qdrant without a reindex.

`docker-compose.yml` remains the deployment artifact and must still be authored
before the system runs on the target server.

## Consequences

- Phase 3 proceeds without waiting on Docker installation.
- The Phase 3 "RAM high-water mark of the Qdrant **container**" criterion is
  satisfied as a **process** RSS measurement (`/proc/<pid>/status` `VmHWM`)
  instead. This is arguably a truer number — it excludes container runtime
  overhead — but it is not the containerised figure the spec asked for, and must
  be re-measured once the compose stack exists.
- Development and deployment environments differ. Anything that depends on
  container networking, volume mounts, or restart policies is untested until
  the compose file is written and run.
- Startup is manual (`nohup ./qdrant &`) with no healthcheck or restart policy,
  which the compose deployment must supply.

## Rejected alternatives

- **`qdrant-client` local/in-memory mode** (`QdrantClient(path=...)`). Rejected:
  it is a pure-Python reimplementation, not Qdrant. It does not support scalar
  quantization or on-disk vectors, so it could not produce a meaningful RAM
  measurement, and its performance characteristics at ~500k points would not
  predict the real system's at all. Using it would have made the Phase 3
  numbers misleading — the exact failure mode §12 rule 3 prohibits.
- **Install Docker / enable WSL integration first.** Rejected for now: it is a
  human-gated task (§10.1) and blocking an entire phase on it is avoidable.
  Still required before deployment.
- **Defer Phase 3 until the Nipogi server is reachable.** Rejected: the user
  explicitly scoped the build as "any local PC or local server, nothing
  specific", so development must not depend on one particular host.

## Update — 2026-08-04: the deployment path exists

`docker-compose.yml` at the repo root now provides the containerised path this ADR deferred.
It pins `qdrant/qdrant:v1.18.3` (not `latest` — an unannounced engine upgrade under a live
collection is exactly the silent breakage this project keeps finding), binds both ports to
`127.0.0.1` (Qdrant ships no authentication, and this corpus originates from a corporate
tenant), and uses named volumes so the derived index can never be staged for commit beside
the corpus.

This does not reverse the decision. Development still runs the native binary under
`qdrant.service`, because this WSL distro still has no Docker:

```
$ docker compose config -q
(eval):1: command not found: docker
```

The compose file was therefore validated as YAML and by asserting its invariants (pinned tag,
loopback-only ports, named volumes) rather than by `docker compose config`. **It has never been
run.** Treat it as reviewed-but-unexercised until someone starts it on a host with Docker.

The two paths must never run simultaneously: they contend for port 6333 and, more damagingly,
for the same storage directory.
