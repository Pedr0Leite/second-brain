"""sn_ingest: migrate a file into the vault, index and embed it, synchronously.

This is the only tool that mutates state, which makes path containment a real
security boundary rather than a theoretical one: an LLM-invokable
arbitrary-file-write is a serious hazard. Every destination is resolved against
the vault root and rejected if it escapes.

Synchronous by design (ADR-0003): a document that is not searchable when the
call returns is indistinguishable from a failed ingest.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (VAULT_PATH, INGEST_DEFAULT_DIR, INGEST_MAX_BYTES, INGEST_MAX_CHUNKS,
                    INGEST_ALLOWED_SUFFIXES, MANIFEST_DB_PATH, QDRANT_COLLECTION,
                    PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                    CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP, SOURCE_BY_TOP_DIR)
from ingest import manifest
from ingest.chunker import chunk_document
from ingest.normalize import extract_metadata, sha256_of


class IngestError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def resolve_dest(dest: str) -> Path:
    """Resolve `dest` inside the vault, rejecting anything that escapes it.

    Rejects absolute paths, `..` traversal, symlink escapes and null bytes.
    Checked after resolution, so a symlink pointing outside is caught too.
    """
    if not dest or not dest.strip():
        raise IngestError("INGEST_BAD_PATH", "dest must not be empty")
    if "\x00" in dest:
        raise IngestError("INGEST_BAD_PATH", "dest contains a null byte")
    candidate = Path(dest)
    if candidate.is_absolute():
        raise IngestError("INGEST_BAD_PATH", f"dest must be vault-relative, got absolute: {dest}")
    if ".." in candidate.parts:
        raise IngestError("INGEST_BAD_PATH", f"dest must not traverse upward: {dest}")

    root = VAULT_PATH.resolve()
    full = (root / candidate).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise IngestError("INGEST_BAD_PATH", f"dest escapes the vault root: {dest}")

    if full.suffix.lower() not in INGEST_ALLOWED_SUFFIXES:
        raise IngestError(
            "INGEST_BAD_TYPE",
            f"only {', '.join(INGEST_ALLOWED_SUFFIXES)} may be ingested, got '{full.suffix}'")
    return full


# Where each source class lands when the caller does not name a destination.
# Deliberately NOT derived by reverse-lookup through SOURCE_BY_TOP_DIR: that
# picked the first directory mapping to the class, which sent personal notes
# into `Notion/` — the Notion *export* folder, whose contents are generated and
# should not receive hand-written material.
INGEST_DEST_BY_CLASS = {
    "personal": INGEST_DEFAULT_DIR,
    "wiki": "wiki",
    "custom-app": "Applications",
    "code-graph": "graphify",
}


def default_dest(filename: str, source_class: str) -> str:
    stem = Path(filename).name
    if not stem.endswith(tuple(INGEST_ALLOWED_SUFFIXES)):
        stem += ".md"
    if source_class == "official":
        # The official corpus is a vendor mirror kept in sync from upstream;
        # hand-ingested files must not be written into it.
        raise IngestError("INGEST_BAD_SOURCE",
                          "cannot ingest into the 'official' corpus — it mirrors vendor docs. "
                          "Use source_class personal/wiki/custom-app, or pass an explicit dest.")
    return f"{INGEST_DEST_BY_CLASS.get(source_class, INGEST_DEFAULT_DIR)}/{stem}"


def build_frontmatter(existing: str, facets: Optional[dict], title: str) -> str:
    """Add a minimal frontmatter block when the document has none."""
    import yaml
    if existing.lstrip().startswith("---"):
        return ""
    meta = {"title": title, "source": "ingested",
            "ingested_at": datetime.now(timezone.utc).isoformat()}
    for key, value in (facets or {}).items():
        meta[key] = value
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n"


def ingest(client, embedder, *, source_path: Optional[str] = None,
           content: Optional[str] = None, filename: Optional[str] = None,
           dest: Optional[str] = None, source_class: str = "personal",
           facets: Optional[dict] = None, overwrite: bool = False) -> dict:
    """Write into the vault, then chunk, embed and upsert. Returns a receipt."""
    from ingest import embed as embed_mod
    from qdrant_client import models

    # --- 1. resolve input -------------------------------------------------
    if source_path and content is not None:
        raise IngestError("INGEST_BAD_INPUT", "pass source_path or content, not both")
    if source_path:
        src = Path(source_path)
        if not src.is_file():
            raise IngestError("INGEST_NOT_FOUND", f"source_path not found: {source_path}")
        if src.stat().st_size > INGEST_MAX_BYTES:
            raise IngestError("INGEST_TOO_LARGE",
                              f"file is {src.stat().st_size} bytes, limit {INGEST_MAX_BYTES}")
        text = src.read_text(encoding="utf-8", errors="replace")
        filename = filename or src.name
    elif content is not None:
        if not filename:
            raise IngestError("INGEST_BAD_INPUT", "filename is required when passing content")
        if len(content.encode()) > INGEST_MAX_BYTES:
            raise IngestError("INGEST_TOO_LARGE",
                              f"content is {len(content.encode())} bytes, limit {INGEST_MAX_BYTES}")
        text = content
    else:
        raise IngestError("INGEST_BAD_INPUT", "one of source_path or content is required")

    if source_class not in set(SOURCE_BY_TOP_DIR.values()):
        raise IngestError("INGEST_BAD_SOURCE",
                          f"unknown source_class {source_class!r}; "
                          f"valid: {sorted(set(SOURCE_BY_TOP_DIR.values()))}")

    # --- 2. destination + collision --------------------------------------
    full = resolve_dest(dest or default_dest(filename, source_class))
    rel_path = str(full.relative_to(VAULT_PATH.resolve()))
    if full.exists() and not overwrite:
        raise IngestError("INGEST_EXISTS",
                          f"{rel_path} already exists; pass overwrite=true to replace it")

    # --- 3. chunk before writing so an oversized doc never lands in the vault
    title = Path(filename).stem
    prefix = build_frontmatter(text, facets, title)
    final_text = prefix + text
    parents, children = chunk_document(final_text, rel_path, PARENT_CHUNK_MIN_CHARS,
                                       PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS,
                                       CHILD_CHUNK_OVERLAP)
    if len(children) > INGEST_MAX_CHUNKS:
        raise IngestError(
            "INGEST_TOO_LARGE",
            f"document produces {len(children)} chunks (limit {INGEST_MAX_CHUNKS}); "
            f"copy it into the vault and run: python3 ingest/index.py full && "
            f"python3 ingest/index.py embed")
    if not children:
        raise IngestError("INGEST_EMPTY", "document produced no indexable content")

    # --- 4. write ---------------------------------------------------------
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(final_text, encoding="utf-8")

    # --- 5. embed + upsert (before manifest commit, per the crash-safety rule)
    meta = extract_metadata(final_text, Path(rel_path))
    symbols_by_parent = {p.parent_id: list(p.api_symbols) for p in parents}
    texts = [f"{c.h_path}\n\n{c.text}" if c.h_path else c.text for c in children]
    dense, sparse = embedder.encode(texts)
    now = datetime.now(timezone.utc).isoformat()

    points = [
        models.PointStruct(
            id=embed_mod.chunk_uuid(c.chunk_id),
            vector={
                embed_mod.DENSE_VECTOR: dense[i].tolist(),
                embed_mod.SPARSE_VECTOR: models.SparseVector(
                    indices=sparse[i].indices.tolist(), values=sparse[i].values.tolist()),
            },
            payload={
                "chunk_id": c.chunk_id, "parent_id": c.parent_id, "text": c.text,
                "rel_path": rel_path, "doc_title": meta["doc_title"], "h_path": c.h_path,
                "source": source_class, "doc_type": meta["doc_type"],
                "facets": meta["facets"], "api_symbols": symbols_by_parent.get(c.parent_id, []),
                "chunk_index": c.child_idx, "updated_at": now,
            },
        )
        for i, c in enumerate(children)
    ]
    embed_mod.delete_by_rel_path(client, QDRANT_COLLECTION, rel_path)
    embed_mod.upsert_points(client, QDRANT_COLLECTION, points)

    # --- 6. manifest ------------------------------------------------------
    stat = full.stat()
    with manifest.connect(MANIFEST_DB_PATH) as conn:
        manifest.upsert_file(conn, rel_path, sha256_of(full), stat.st_size, stat.st_mtime,
                             source_class, "pending")
        manifest.replace_chunks(conn, rel_path, [(c.chunk_id, c.parent_id) for c in children])
        manifest.replace_parents(conn, rel_path,
                                 [(p.parent_id, p.parent_idx, p.h_path, p.text) for p in parents])
        manifest.mark_indexed(conn, rel_path, len(children), now)

    return {
        "rel_path": rel_path,
        "source": source_class,
        "doc_title": meta["doc_title"],
        "chunk_count": len(children),
        "parent_count": len(parents),
        "bytes": stat.st_size,
        "indexed_at": now,
        "searchable": True,
    }
