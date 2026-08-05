"""CLI: full | verify. Incremental/embed subcommands land in Phase 3."""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CORPUS_PATH, MANIFEST_DB_PATH
from ingest import manifest
from ingest.normalize import iter_corpus_files, classify_source, skip_reason, sha256_of


def cmd_full(args):
    with manifest.connect(MANIFEST_DB_PATH) as conn:
        seen = set()
        n_pending, n_skipped, n_unchanged, n_errors = 0, 0, 0, 0
        for path in iter_corpus_files(CORPUS_PATH):
            rel_path = str(path.relative_to(CORPUS_PATH))
            seen.add(rel_path)
            try:
                source = classify_source(path.relative_to(CORPUS_PATH))
            except ValueError as e:
                manifest.record_error(conn, rel_path, "classify", str(e), datetime.now(timezone.utc).isoformat())
                n_errors += 1
                continue

            reason = skip_reason(path.relative_to(CORPUS_PATH))
            status = "skipped" if reason else "pending"

            stat = path.stat()
            sha = sha256_of(path)
            existing = manifest.get_file(conn, rel_path)
            # Content is what decides staleness, not the indexing state. A file
            # already embedded sits at status='indexed' while this pass computes
            # the desired status as 'pending'; comparing those directly would
            # report every indexed file as changed on every run.
            if existing and existing["sha256"] == sha:
                skip_matches = (status == "skipped") == (existing["status"] == "skipped")
                if skip_matches:
                    n_unchanged += 1
                    continue

            manifest.upsert_file(conn, rel_path, sha, stat.st_size, stat.st_mtime, source, status)
            if status == "skipped":
                n_skipped += 1
            else:
                n_pending += 1

        # Drop manifest rows for files no longer on disk, and queue their
        # vectors for removal. Queued rather than deleted inline so a corpus
        # change is not lost when Qdrant happens to be down.
        all_rows = conn.execute("SELECT rel_path FROM files").fetchall()
        n_deleted = 0
        now = datetime.now(timezone.utc).isoformat()
        for (rel_path,) in all_rows:
            if rel_path not in seen:
                if manifest.was_indexed(conn, rel_path):
                    manifest.queue_delete(conn, rel_path, now)
                manifest.drop_parents(conn, rel_path)
                manifest.delete_file(conn, rel_path)
                n_deleted += 1

        print(f"pending={n_pending} skipped={n_skipped} unchanged={n_unchanged} errors={n_errors} deleted={n_deleted}")


def cmd_verify(args):
    fs_count = sum(1 for _ in iter_corpus_files(CORPUS_PATH))
    with manifest.connect(MANIFEST_DB_PATH) as conn:
        manifest_rows = manifest.count_files(conn)
        by_status = manifest.count_by_status(conn)
        by_source = manifest.count_by_source(conn)

    print(f"filesystem_md_count={fs_count}")
    print(f"manifest_rows={manifest_rows}")
    print(f"match={fs_count == manifest_rows}")
    print(f"by_status={by_status}")
    print(f"by_source={by_source}")
    sys.exit(0 if fs_count == manifest_rows else 1)


def _acquire_index_lock():
    """Exclusive lock shared with scripts/nightly_sync.sh.

    Two concurrent embedders would interleave writes to the same SQLite manifest
    and Qdrant collection. The lock lives in the indexer itself rather than only
    in the sync wrapper, so a manual run and a timer-triggered run cannot
    overlap either.
    """
    import fcntl
    state_dir = Path(os.environ.get("STATE_DIR", Path.home() / ".local/state/sn-rag"))
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "sync.lock", "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit("another indexing run holds the lock; exiting")
    return handle


def cmd_embed(args):
    """Embed and upsert pending files. Resumable: a file is marked 'indexed'
    only after its points are committed, so an interrupted run re-embeds
    nothing already committed.

    Ordering is deliberate: Qdrant upsert happens BEFORE the manifest commit.
    A crash between them leaves surplus vectors for a file still marked pending,
    which the next run overwrites idempotently (point IDs are deterministic).
    The reverse order would leave a file marked indexed with no vectors — a
    silent recall hole that nothing would ever repair.
    """
    lock = _acquire_index_lock()
    from qdrant_client import QdrantClient, models
    from config import (QDRANT_URL, QDRANT_COLLECTION, DENSE_MODEL, SPARSE_MODEL,
                        EMBED_BATCH_SIZE, EMBED_THREADS, UPSERT_FILE_BATCH, PARENT_CHUNK_MIN_CHARS,
                        PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP,
                        EMBED_DOC_TITLE)
    from ingest.chunker import chunk_document
    from ingest.normalize import extract_metadata
    from ingest import embed as embed_mod

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    embedder = embed_mod.Embedder(DENSE_MODEL, SPARSE_MODEL, EMBED_BATCH_SIZE,
                                  threads=EMBED_THREADS)
    embed_mod.ensure_collection(client, QDRANT_COLLECTION, embedder.dim, recreate=args.recreate)

    with manifest.connect(MANIFEST_DB_PATH) as conn:
        if args.recreate:
            # Dropping the collection invalidates every 'indexed' mark; leaving
            # them would make files_to_index skip files that no longer exist in
            # Qdrant, silently producing a half-empty index.
            conn.execute("UPDATE files SET status='pending', chunk_count=NULL, indexed_at=NULL "
                         "WHERE status='indexed'")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM parents")
        if args.paths:
            wanted = [p.strip() for p in Path(args.paths).read_text().splitlines() if p.strip()]
            rows = conn.execute(
                f"SELECT rel_path, source FROM files WHERE rel_path IN "
                f"({','.join('?' for _ in wanted)})", wanted).fetchall()
            found = {r[0] for r in rows}
            for missing in set(wanted) - found:
                raise SystemExit(f"path not in manifest: {missing}")
            todo = rows
        else:
            todo = manifest.files_to_index(conn, limit=args.limit, shuffle=args.shuffle)
        deletes = manifest.list_pending_deletes(conn)
    print(f"model={DENSE_MODEL} dim={embedder.dim} sparse={SPARSE_MODEL} batch={EMBED_BATCH_SIZE}")
    print(f"files to index: {len(todo)}  pending deletes: {len(deletes)}")

    # Drain deletions first so a file that was removed and re-added is not
    # deleted after its fresh vectors were written.
    for rel_path in deletes:
        embed_mod.delete_by_rel_path(client, QDRANT_COLLECTION, rel_path)
        with manifest.connect(MANIFEST_DB_PATH) as conn:
            manifest.clear_pending_delete(conn, rel_path)
    if deletes:
        print(f"purged vectors for {len(deletes)} removed files")

    t0 = time.perf_counter()
    files_done = chunks_done = 0
    for start in range(0, len(todo), UPSERT_FILE_BATCH):
        window = todo[start:start + UPSERT_FILE_BATCH]
        points, per_file = [], []
        for rel_path, source in window:
            path = CORPUS_PATH / rel_path
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                meta = extract_metadata(raw, Path(rel_path))
                parents, children = chunk_document(raw, rel_path, PARENT_CHUNK_MIN_CHARS,
                                                   PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS,
                                                   CHILD_CHUNK_OVERLAP)
            except Exception as exc:
                with manifest.connect(MANIFEST_DB_PATH) as conn:
                    manifest.record_error(conn, rel_path, "embed", repr(exc),
                                          datetime.now(timezone.utc).isoformat())
                continue
            if not children:
                per_file.append((rel_path, source, [], [], [], meta))
                continue
            symbols_by_parent = {p.parent_id: list(p.api_symbols) for p in parents}
            texts = [embed_mod.build_embed_text(c.text, c.h_path, meta["doc_title"],
                                                rel_path, include_title=EMBED_DOC_TITLE)
                     for c in children]
            per_file.append((rel_path, source, children, parents, texts, meta))

        all_texts = [t for _, _, _, _, ts, _ in per_file for t in ts]
        if all_texts:
            dense, sparse = embedder.encode(all_texts)
        cursor = 0
        now = datetime.now(timezone.utc).isoformat()
        for rel_path, source, children, parents, texts, meta in per_file:
            symbols_by_parent = {p.parent_id: list(p.api_symbols) for p in parents}
            for i, child in enumerate(children):
                d, s = dense[cursor + i], sparse[cursor + i]
                points.append(models.PointStruct(
                    id=embed_mod.chunk_uuid(child.chunk_id),
                    vector={
                        embed_mod.DENSE_VECTOR: d.tolist(),
                        embed_mod.SPARSE_VECTOR: models.SparseVector(
                            indices=s.indices.tolist(), values=s.values.tolist()),
                    },
                    payload={
                        "chunk_id": child.chunk_id,
                        "parent_id": child.parent_id,
                        # Child text lives in the payload so sn_search can return
                        # a snippet without a second round-trip to the parent store.
                        "text": child.text,
                        "rel_path": rel_path,
                        "doc_title": meta["doc_title"],
                        "h_path": child.h_path,
                        "source": source,
                        "doc_type": meta["doc_type"],
                        "facets": meta["facets"],
                        "api_symbols": symbols_by_parent.get(child.parent_id, []),
                        "chunk_index": child.child_idx,
                        "updated_at": now,
                    },
                ))
            cursor += len(texts)

        # A re-indexed file may now produce fewer chunks than before. Chunk IDs
        # are positional (sha1 of rel_path + indices), so the surplus tail would
        # survive as orphaned vectors. Clear the file's points before upserting.
        with manifest.connect(MANIFEST_DB_PATH) as conn:
            stale = [rp for rp, *_ in per_file if manifest.was_indexed(conn, rp)]
        for rel_path in stale:
            embed_mod.delete_by_rel_path(client, QDRANT_COLLECTION, rel_path)

        embed_mod.upsert_points(client, QDRANT_COLLECTION, points)
        with manifest.connect(MANIFEST_DB_PATH) as conn:
            for rel_path, source, children, parents, texts, meta in per_file:
                manifest.replace_chunks(conn, rel_path, [(c.chunk_id, c.parent_id) for c in children])
                manifest.replace_parents(conn, rel_path,
                                         [(p.parent_id, p.parent_idx, p.h_path, p.text) for p in parents])
                manifest.mark_indexed(conn, rel_path, len(children), now)
                files_done += 1
                chunks_done += len(children)

        elapsed = time.perf_counter() - t0
        print(f"  {files_done}/{len(todo)} files  {chunks_done} chunks  "
              f"{chunks_done / elapsed:.1f} chunks/s  embedded_calls={embedder.stats.embedded}", flush=True)

    elapsed = time.perf_counter() - t0
    print(f"DONE files={files_done} chunks={chunks_done} elapsed={elapsed:.1f}s "
          f"rate={chunks_done / elapsed:.1f} chunks/s")
    print(f"EMBED_CALL_COUNTER={embedder.stats.embedded}")


def cmd_status(args):
    """Index health: manifest vs Qdrant point count (drift detection)."""
    from qdrant_client import QdrantClient
    from config import QDRANT_URL, QDRANT_COLLECTION

    with manifest.connect(MANIFEST_DB_PATH) as conn:
        by_status = manifest.count_by_status(conn)
        manifest_chunks = manifest.total_chunk_count(conn)
        chunk_rows = manifest.count_chunk_rows(conn)

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    if client.collection_exists(QDRANT_COLLECTION):
        qdrant_points = client.count(QDRANT_COLLECTION, exact=True).count
    else:
        qdrant_points = 0

    print(f"files_by_status      = {by_status}")
    print(f"manifest_chunk_sum   = {manifest_chunks}")
    print(f"manifest_chunk_rows  = {chunk_rows}")
    print(f"qdrant_points        = {qdrant_points}")
    print(f"match                = {manifest_chunks == qdrant_points == chunk_rows}")
    sys.exit(0 if manifest_chunks == qdrant_points == chunk_rows else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("full")
    sub.add_parser("verify")
    p_embed = sub.add_parser("embed")
    p_embed.add_argument("--limit", type=int, default=None)
    p_embed.add_argument("--recreate", action="store_true")
    p_embed.add_argument("--shuffle", action="store_true",
                         help="deterministic spread across the corpus instead of alphabetical")
    p_embed.add_argument("--paths", type=str, default=None,
                         help="file of rel_paths to (re)index, one per line; ignores status")
    sub.add_parser("status")
    args = parser.parse_args()
    {"full": cmd_full, "verify": cmd_verify, "embed": cmd_embed, "status": cmd_status}[args.cmd](args)
