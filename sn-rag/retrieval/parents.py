"""Parent expansion: small-to-search, large-to-read.

Children are sized for embedding precision; parents are sized for a model to
actually read. Retrieval matches a child, then this returns the surrounding
section so the answer has enough context to be correct.
"""
import sys
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MANIFEST_DB_PATH
from ingest import manifest


class ParentStore:
    def __init__(self, db_path: Path = MANIFEST_DB_PATH):
        self.db_path = db_path

    def get(self, parent_id: str) -> Optional[dict]:
        with manifest.connect(self.db_path) as conn:
            return manifest.get_parent(conn, parent_id)

    def get_many(self, parent_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(parent_ids))
        if not ids:
            return {}
        with manifest.connect(self.db_path) as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT parent_id, rel_path, parent_idx, h_path, text FROM parents "
                f"WHERE parent_id IN ({placeholders})", ids).fetchall()
        keys = ("parent_id", "rel_path", "parent_idx", "h_path", "text")
        return {row[0]: dict(zip(keys, row)) for row in rows}

    def outline(self, rel_path: str) -> list[dict]:
        """Header tree for a document: breadcrumb per parent, no body text."""
        with manifest.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT parent_id, parent_idx, h_path FROM parents WHERE rel_path=? ORDER BY parent_idx",
                (rel_path,)).fetchall()
        return [{"parent_id": r[0], "parent_idx": r[1], "h_path": r[2]} for r in rows]

    def dedupe_by_parent(self, hits) -> list:
        """Collapse hits sharing a parent, keeping the best-scoring child.

        Without this, one long section can occupy every result slot and crowd
        out other documents entirely.
        """
        best: dict = {}
        for hit in hits:
            current = best.get(hit.parent_id)
            if current is None or hit.score > current.score:
                best[hit.parent_id] = hit
        return sorted(best.values(), key=lambda h: h.score, reverse=True)
