"""SQLite manifest: rel_path -> sha256 -> chunk_ids. Enables true incremental indexing."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  rel_path      TEXT PRIMARY KEY,
  sha256        TEXT NOT NULL,
  bytes         INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  source        TEXT NOT NULL,
  indexed_at    TEXT,
  chunk_count   INTEGER,
  status        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id      TEXT PRIMARY KEY,
  rel_path      TEXT NOT NULL REFERENCES files(rel_path) ON DELETE CASCADE,
  parent_id     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
  rel_path TEXT, phase TEXT, message TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS parents (
  parent_id     TEXT PRIMARY KEY,
  rel_path      TEXT NOT NULL,
  parent_idx    INTEGER NOT NULL,
  h_path        TEXT NOT NULL,
  text          TEXT NOT NULL
);
-- Files removed from disk (or superseded) whose vectors are still in Qdrant.
-- Drained by `index.py embed`, so a purge survives Qdrant being unreachable
-- at the moment the deletion is detected.
CREATE TABLE IF NOT EXISTS pending_deletes (
  rel_path      TEXT PRIMARY KEY,
  detected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_rel_path ON chunks(rel_path);
CREATE INDEX IF NOT EXISTS idx_parents_rel_path ON parents(rel_path);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
"""


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_file(conn, rel_path: str, sha256: str, size_bytes: int, mtime: float, source: str, status: str):
    conn.execute(
        """
        INSERT INTO files (rel_path, sha256, bytes, mtime, source, indexed_at, chunk_count, status)
        VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(rel_path) DO UPDATE SET
            sha256=excluded.sha256, bytes=excluded.bytes, mtime=excluded.mtime,
            source=excluded.source, status=excluded.status
        WHERE files.sha256 != excluded.sha256
        """,
        (rel_path, sha256, size_bytes, mtime, source, status),
    )


def get_file(conn, rel_path: str):
    row = conn.execute("SELECT rel_path, sha256, bytes, mtime, source, status FROM files WHERE rel_path=?", (rel_path,)).fetchone()
    if row is None:
        return None
    keys = ("rel_path", "sha256", "bytes", "mtime", "source", "status")
    return dict(zip(keys, row))


def count_files(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]


def count_by_status(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
    return dict(rows)


def count_by_source(conn) -> dict:
    rows = conn.execute("SELECT source, COUNT(*) FROM files GROUP BY source").fetchall()
    return dict(rows)


def delete_file(conn, rel_path: str):
    conn.execute("DELETE FROM files WHERE rel_path=?", (rel_path,))


def record_error(conn, rel_path: str, phase: str, message: str, ts: str):
    conn.execute("INSERT INTO errors (rel_path, phase, message, ts) VALUES (?, ?, ?, ?)", (rel_path, phase, message, ts))


# --- indexing state ---------------------------------------------------------

def files_to_index(conn, limit: int | None = None, statuses=("pending",), shuffle: bool = False):
    """Files still needing embedding. Drives resumability: an interrupted run
    leaves already-indexed files at status='indexed', so they are not returned.

    `shuffle` draws a deterministic pseudo-random spread instead of walking
    alphabetically — an alphabetical prefix of this corpus is almost entirely
    one product area, which would make any partial-index evaluation misleading.
    """
    placeholders = ",".join("?" for _ in statuses)
    order = "ORDER BY substr(sha256, 1, 8)" if shuffle else "ORDER BY rel_path"
    sql = f"SELECT rel_path, source FROM files WHERE status IN ({placeholders}) {order}"
    params = list(statuses)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def replace_chunks(conn, rel_path: str, chunk_rows: list[tuple[str, str]]):
    """Swap in this file's chunk rows. (chunk_id, parent_id) pairs."""
    conn.execute("DELETE FROM chunks WHERE rel_path=?", (rel_path,))
    conn.executemany(
        "INSERT OR REPLACE INTO chunks (chunk_id, rel_path, parent_id) VALUES (?, ?, ?)",
        [(chunk_id, rel_path, parent_id) for chunk_id, parent_id in chunk_rows],
    )


def replace_parents(conn, rel_path: str, parent_rows: list[tuple[str, int, str, str]]):
    """Parent store: (parent_id, parent_idx, h_path, text)."""
    conn.execute("DELETE FROM parents WHERE rel_path=?", (rel_path,))
    conn.executemany(
        "INSERT OR REPLACE INTO parents (parent_id, rel_path, parent_idx, h_path, text) VALUES (?, ?, ?, ?, ?)",
        [(pid, rel_path, idx, h_path, text) for pid, idx, h_path, text in parent_rows],
    )


def get_parent(conn, parent_id: str):
    row = conn.execute(
        "SELECT parent_id, rel_path, parent_idx, h_path, text FROM parents WHERE parent_id=?",
        (parent_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(("parent_id", "rel_path", "parent_idx", "h_path", "text"), row))


def mark_indexed(conn, rel_path: str, chunk_count: int, indexed_at: str):
    conn.execute("UPDATE files SET status='indexed', chunk_count=?, indexed_at=? WHERE rel_path=?",
                 (chunk_count, indexed_at, rel_path))


def total_chunk_count(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(chunk_count), 0) FROM files WHERE status='indexed'").fetchone()[0]


def count_chunk_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def queue_delete(conn, rel_path: str, detected_at: str):
    conn.execute("INSERT OR REPLACE INTO pending_deletes (rel_path, detected_at) VALUES (?, ?)",
                 (rel_path, detected_at))


def list_pending_deletes(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT rel_path FROM pending_deletes").fetchall()]


def clear_pending_delete(conn, rel_path: str):
    conn.execute("DELETE FROM pending_deletes WHERE rel_path=?", (rel_path,))


def drop_parents(conn, rel_path: str):
    conn.execute("DELETE FROM parents WHERE rel_path=?", (rel_path,))


def was_indexed(conn, rel_path: str) -> bool:
    row = conn.execute("SELECT 1 FROM chunks WHERE rel_path=? LIMIT 1", (rel_path,)).fetchone()
    return row is not None


def chunk_ids_for(conn, rel_path: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT chunk_id FROM chunks WHERE rel_path=?", (rel_path,)).fetchall()]
