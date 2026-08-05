# NiPoGi bootstrap prompt

Paste everything between the rules into Claude Code, running **on the NiPoGi**,
from the cloned repo root.

---

You are bringing up `sn-rag` on a home server. The repo is cloned; nothing else
is set up.

**Read first, before running anything:** `CLAUDE.md` (repo root),
`sn-rag/docs/adr/0005-server-deployment-migrate-the-index-never-rebuild-it.md`,
and `sn-rag/docs/adr/0006-remote-mcp-serve-reads-over-http-keep-the-writer-local.md`.
Both ADRs are **Proposed**, not implemented. They constrain what you may do.

All commands run from `sn-rag/`. The repo root holds `README.md` and `CLAUDE.md`;
the code is one level down. `cd sn-rag` first or `import config` fails and
`pytest tests/` collects nothing.

## Hard constraints — violating any of these is a failure, not a shortcut

1. **Do NOT build or rebuild the index.** A full embed is ~8 hours at 17.6
   chunks/s for 505,507 chunks, and this box is slower than the workstation.
   Per ADR-0005 the index is **copied**, never rebuilt. If Qdrant storage and
   `manifest.db` have not been transferred yet, stop and say so — do not start
   an embed to "get something working".
2. **Do NOT open a network port.** ADR-0006 requires bearer-token auth and an
   explicit non-`0.0.0.0` bind, neither of which exists yet. `server.py:281`
   currently binds `0.0.0.0` with no authentication; treat `--http` as unusable
   until the auth work is done and reviewed.
3. **Qdrant and Ollama bind `127.0.0.1` only.** Both ship with no
   authentication. Verify with `ss -ltnp`, do not assume.
4. **No number without the command that produced it.** Paste raw output into
   `docs/BUILD-LOG.md`. A throughput or latency figure without its command is
   not evidence.
5. **No silent fallbacks.** If a backend is missing, report a structured error.
   Never make retrieval call a hosted model — that defeats the project.
6. **Report honestly.** If a step fails or is skipped, say which and why. Do not
   report success for work that did not run.

## Tasks, in order

**0. Check what arrived before doing anything else.** The index transfer is
driven from the workstation, not from here. Report which of these exist and
their sizes, then stop if the first two are missing:

```bash
ls -la ~/qdrant-snapshots/ 2>/dev/null          # expect knowledge-*.snapshot, ~2-3 GB
ls -la ~/.local/state/sn-rag/manifest.db        # expect ~527 MB
ls -d ~/.cache/fastembed 2>/dev/null            # optional, ~297 MB, re-downloads if absent
```

A missing snapshot is not a reason to embed. It is a reason to stop and say the
transfer has not happened. Restore it **after** Qdrant is running (task 3):

```bash
curl -X POST 'http://127.0.0.1:6333/collections/knowledge/snapshots/upload' \
     -H 'content-type:multipart/form-data' \
     -F "snapshot=@$HOME/qdrant-snapshots/<file>.snapshot"
```

Only `knowledge` matters. Any `sample_*` collection on the source is throwaway
evaluation scaffolding — do not transfer or recreate it.

**1. Record the hardware.** CPU model, core/thread count, total RAM, free disk,
whether `nvidia-smi` exists, and the filesystem type of the paths below. Compare
against the workstation baseline in ADR-0005 (Ryzen 5 9600X, 6c/12t, 25 GB, no
GPU). State plainly whether this box is slower and by roughly how much.

**2. Install dependencies.** `requirements.txt` has pinned versions. This
project uses user-site installs (`pip install --user --break-system-packages`)
because `python3 -m venv` fails in the source environment — check whether venv
works *here* and prefer it if it does. Install ripgrep. Do not install `torch`,
`docker`, or `openai`; `requirements.txt` documents why each is deliberately
absent.

**3. Qdrant from the official static binary**, as a `systemd --user` unit with
`Restart=always`, bound to `127.0.0.1:6333`. See ADR-0002 for why not Docker.
Run `loginctl enable-linger $USER` so it survives logout on a headless box.

**4. Confirm the paths resolve correctly.**

```bash
python3 -c "import config; print(config.CORPUS_PATH, config.MANIFEST_DB_PATH, config.MODEL_CACHE_PATH)"
```

These resolve through ordered candidate lists anchored to absolute locations,
never the working directory. If they point somewhere that does not exist, fix
the environment — never reintroduce a cwd-relative default. A previous bug had
sqlite silently create an empty database and `sn_get_section` returned nothing
while reporting success.

**5. Verify the transferred index** — do not proceed if this fails:

```bash
python3 ingest/index.py status
```

Expect `match = True` and roughly `{'indexed': 51588, 'skipped': 54}` with
505,507 chunks. A mismatch means the transfer is incomplete; report it rather
than repairing by re-embedding.

**6. Run the suite.** `python3 -m pytest tests/ -q` — expect 144 passing. If
anything fails, that is the finding; report it before continuing.

**7. Register the MCP server locally (stdio, no network yet)** and verify with a
real query through `sn_search`, not just a tool listing.

**8. Measure this box against the workstation.** Embedding throughput on a small
sample, and `sn_search` p50/p95. Do not run a full eval — the golden set gate is
INCONCLUSIVE (3 real cases, needs 20) and re-running it here proves nothing new.

**9. Make the workstation able to reach it — over SSH, not over a port.** Do not
open `--http`. Instead confirm this box can serve the MCP server as an SSH stdio
child, which is what the workstation will register:

```bash
ssh <this-box> 'cd ~/sn-rag && python3 mcp_server/server.py' < /dev/null
```

It should start and exit on EOF without a traceback. Report the exact repo path
and the username, since the workstation needs them verbatim. If `python3` is not
found under a non-interactive SSH session, that is the finding — non-login shells
do not read the same profile, and the fix is an absolute interpreter path in the
command, not editing `~/.zshrc`.

This is a refinement of the SSH option ADR-0006 rejected. The rejection was aimed
at `ssh -L` port forwards, which must be up before Claude Code spawns; here `ssh`
*is* the child process, so it starts on demand and a dropped link exits cleanly
as an MCP disconnect. If it works, say so — it is grounds to amend ADR-0006
rather than build the token auth.

## Then stop and report

Do **not** implement the ADR-0006 auth work in the same pass. Report:

- hardware, and how much slower than the workstation
- what installed cleanly and what did not
- index status output, verbatim
- test results
- measured latency vs the workstation figures
- a concrete list of what remains before a port could safely open

Known state you should not rediscover: `servicenow` recall@10 is 0.455 and
`sn_research` currently retrieves *worse* than plain `sn_search` (0.345 vs
~0.53). Retrieval quality is an open problem. Deployment does not fix it, and
you are not being asked to fix it here.
