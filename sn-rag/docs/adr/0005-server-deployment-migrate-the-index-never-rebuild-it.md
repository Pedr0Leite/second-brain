# ADR-0005: Deploying to a home server — migrate the index, never rebuild it

**Date:** 2026-08-05
**Status:** Superseded by events 2026-08-06 — see "What actually happened"
below. The migrate-never-rebuild *principle* still holds; the specific plan of
copying a Qdrant snapshot from the workstation did not happen because the
target box did its own embed instead.
**Phase:** 7 (operations)

## What actually happened (2026-08-06)

Hardware got selected and deployed before this ADR's plan was executed: the
target machine is `milkserver` — AMD Ryzen 7 7730U, 16 threads, 14 GB RAM, no
GPU (`nvidia-smi` absent) — reachable on the LAN at `192.168.1.235`. This is a
different box than either the original workstation (Ryzen 5 9600X, 25 GB) or
the N100-class NiPoGi this ADR's throughput estimates were built around.

**The embed ran in place on this box, not migrated in.** `python3
ingest/index.py embed --recreate` ran directly on `milkserver`, taking roughly
16 hours (started 2026-08-05 19:02, still running the next morning). This
directly contradicts the "bulk embedding stays on the workstation" decision
below — there was no separate workstation available to embed on ahead of time,
so the choice was between waiting one long embed on the deploy box or not
deploying. Verified on completion:

```
$ python3 ingest/index.py status
files_by_status      = {'indexed': 51588, 'skipped': 54}
manifest_chunk_sum   = 505507
manifest_chunk_rows  = 505507
qdrant_points        = 505507
match                = True
```

This does not invalidate the ADR's core argument — a *second* box added later
should still receive a copied snapshot rather than re-embedding, since the
16-hour cost is exactly what the "migrate, never rebuild" principle exists to
avoid paying twice. It only means the first deployment target paid that cost
once, out of necessity rather than by the ADR's original design. See
`docs/NIPOGI-BOOTSTRAP-PROMPT.md` and ADR-0008 for what is built on top of this
box now that it holds the live index.

## Context

The system currently runs entirely on one workstation: an AMD Ryzen 5 9600X
(6 cores / 12 threads, Zen 5), 25 GB RAM, CPU-only (`nvidia-smi` absent).
Everything binds `127.0.0.1`, and the MCP server is spawned as a **stdio**
subprocess by Claude Code — which only works when client and server share a
machine.

Moving it to an always-on low-power box (a NiPoGi mini PC or similar) would make
the second brain available without the workstation running. That change touches
three things the current design takes for granted: available CPU, the network
boundary, and the transport.

### Measured footprint

```
$ du -sh /home/pedro/vaults/obsidian-servicenow-docs
5.2G
$ du -sh ~/.local/qdrant/storage
1.7G                       # at 306k points; ~3.4 GB projected at full ~600k
$ du -sh ~/.local/state/sn-rag/manifest.db
315M                       # ~600 MB when the corpus is fully indexed
$ curl -s localhost:11434/api/tags     # qwen2.5:3b-instruct
1.93 GB
$ du -sh /tmp/fastembed_cache
297M                       # dense + sparse + reranker
```

Live resident memory with the stack running:

```
$ ps -eo rss,args --sort=-rss | grep -E "qdrant|ollama|index.py"
3118 MB  python3 ingest/index.py embed    # indexing only, not steady state
1672 MB  qdrant
  37 MB  ollama                            # idle; ~2.5 GB with the model resident
```

### The throughput problem

Embedding is the only genuinely expensive operation, and it is CPU-bound.
Measured on the Ryzen with `EMBED_THREADS=6`:

```
$ tail -1 ~/.local/state/sn-rag/full-embed.log
  3875/27480 files  36168 chunks  18.4 chunks/s
```

Typical NiPoGi hardware is an Intel N100/N97: 4 Alder Lake-N E-cores, no SMT.
That is roughly a quarter of this machine's per-core throughput at two-thirds the
core count. **Estimated** 3-5 chunks/s — an estimate from core counts and
microarchitecture, *not* a measurement, and this project's own history says
estimates like it have been wrong by an order of magnitude before (see the
92.4 vs 6.7 chunks/s entry in `BUILD-LOG.md`).

At that rate a full corpus embed is **35-50 hours**. Even if the estimate is
generous by 2x it remains an overnight-plus job on hardware that also has to
serve queries.

## Decision

**Bulk embedding stays on the workstation. The server receives the finished
index by file copy. It never rebuilds one.**

Embeddings are deterministic for a fixed model. This was verified when
`Embedder.encode()` was refactored to length-sorted batching:

```
max abs elementwise diff: 0.0
```

Same model, same text, same vectors — so a copied Qdrant collection is not an
approximation of a rebuilt one, it is identical to it. Copying takes minutes;
rebuilding takes days and produces nothing new.

Incremental nightly updates (a few hundred changed files) are cheap and stay on
the server. Only the full-corpus case is prohibited.

Three further decisions follow from the move:

**1. The network boundary is explicit and closed by default.** Qdrant ships with
no authentication and Ollama has none either. Today that is safe only because
both bind `127.0.0.1`. On a server they must bind loopback plus a mesh-VPN
interface (Tailscale/WireGuard) — never a LAN address — *and* set
`QDRANT__SERVICE__API_KEY`. The API key is redundant with the VPN by design: a
mesh VPN is a configuration, and configurations get changed.

This is not generic caution. The corpus originates from a corporate OneDrive
tenant and lives in a private repository. An unauthenticated Qdrant on a LAN
address serves the entire corpus over HTTP to anyone on the network, with no
credential and no audit trail.

**2. Transport becomes streamable HTTP, with a bearer token.** stdio cannot cross
a machine boundary. The MCP SDK 2.0 already supports streamable HTTP, so this is
a transport swap in `mcp_server/server.py` rather than a redesign. The token is
mandatory, not optional: `sn_ingest` is a writer, and an unauthenticated MCP
endpoint exposes it.

**3. Quality knobs are turned down before the architecture is changed.** If
retrieval latency is unacceptable on weak hardware, the first lever is
`RERANK_CANDIDATES` 50 -> 30. The measured sweep in `BUILD-LOG.md` shows
recall@10 is **flat** across 30/50/100/200; only recall@5 and MRR degrade, while
p95 improves 1450 ms -> 883 ms. That is a known, bounded trade with evidence
behind it.

## Consequences

- **The workstation stays load-bearing** for corpus-wide operations: a model
  change, a chunking change, or anything else requiring a full re-embed means
  running it there and re-copying. The server is not self-sufficient, and the
  plan should not pretend otherwise.
- **Migration must be verified, not assumed.** After copying, the server must
  report `match = True` from `ingest/index.py status` with counts identical to
  the source. A manifest/Qdrant mismatch is a silent recall hole — searches
  succeed and quietly miss documents. This is the same failure class as the
  cwd-relative manifest bug: everything reports success while returning less.
- **Copy a stopped Qdrant or use its snapshot API.** `rsync` of a live storage
  directory yields a torn snapshot: segment files and their metadata are written
  independently, so the copy can be internally inconsistent while looking
  complete.
- **`sn_research` may not survive N100-class hardware.** The planner runs at
  20.7 tok/s here; a quarter of that against a 15 s budget with up to 12 judge
  calls does not fit. Mitigations in order: `qwen2.5:1.5b-instruct`, lower
  `MAX_JUDGE_CALLS`, or run without a planner and use `sn_search` directly.
  Failure remains `PLANNER_UNAVAILABLE` — **no hosted fallback is added, on any
  hardware, for any reason** (ADR-0004).
- **Sizing floor:** 16 GB RAM (8 GB swaps once Ollama loads alongside Qdrant and
  the reranker), 128 GB SSD, native Linux filesystem. Not a network mount and not
  exFAT — the 342x lexical-search measurement was the cost of a non-native
  filesystem.
- **Deferred until hardware exists:** whether `sn_research` stays enabled, and
  which planner model. Both are decidable in an hour of benchmarking on the
  actual box and are not worth guessing now.

## Rejected alternatives

**Re-embed on the server.** The obvious approach and the expensive one: 35-50
hours of estimated compute to reproduce, bit for bit, an artifact that copies in
minutes. It would also occupy the machine's entire CPU during the window, which
this project already established makes the foreground unusable — the reindex that
starved Ollama and returned `PLANNER_UNAVAILABLE` for hours is recorded in
`BUILD-LOG.md`.

**Expose Qdrant on the LAN with no auth, "it's only my home network".** A flat
home network includes IoT devices, guest phones, and anything with a foothold.
The asset is a corporate documentation corpus. The cost of the alternative is one
environment variable.

**Run the corpus from a network share instead of copying 5.2 GB.** Directly
contradicts the measured 342x lexical-search penalty from a non-native
filesystem, which is exactly why the corpus was moved off `/mnt/c` in the first
place. Disk is cheaper than re-learning that.

**Keep stdio and SSH in to run Claude Code on the server.** Workable, and it
avoids the transport change — but it puts the client on the weak machine too, and
every session pays the model-loading cost on hardware chosen for idle power draw.
The transport swap is a few hours; this tax is permanent.

**Ship Qdrant in Docker for portability.** Rejected for the same reason as
ADR-0002: no Docker in the WSL distro, and the compose file written there remains
reviewed-but-unexercised. A second unexercised deployment path is a liability, not
portability.

## Unrelated finding — since fixed

The fastembed model cache resolved to `/tmp/fastembed_cache` (297 MB), and `/tmp`
is cleared on reboot, so this workstation re-downloaded all three models after
every restart.

Fixed 2026-08-05: `MODEL_CACHE_PATH` in `config.py`, defaulting to
`~/.cache/fastembed`, resolved inside the `Embedder` and `Reranker` constructors.
See `BUILD-LOG.md`.

This matters more for the server than it did here: a mini PC that reboots on
power loss would otherwise need ~300 MB of Hugging Face downloads before serving
its first query, and would fail outright if the box were firewalled off from the
public internet — a reasonable thing to want for a machine holding a corporate
corpus. Seed `~/.cache/fastembed` as part of the migration copy.
