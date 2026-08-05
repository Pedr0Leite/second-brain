"""Pure functions: walk corpus, classify source, hash files. No manifest I/O here."""
import hashlib
from pathlib import Path
from typing import Iterator, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CORPUS_PATH, SOURCE_BY_TOP_DIR, ROOT_FILES_SOURCE, EXCLUDED_DIRS, EXCLUDED_FILENAMES


def iter_corpus_files(corpus_root: Path = CORPUS_PATH) -> Iterator[Path]:
    for path in corpus_root.rglob("*.md"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(corpus_root).parts[:-1]):
            continue
        yield path


def classify_source(rel_path: Path) -> str:
    """Deterministic top-level-dir -> source class. Raises if undetermined."""
    parts = rel_path.parts
    if len(parts) == 1:
        return ROOT_FILES_SOURCE
    top_dir = parts[0]
    if top_dir not in SOURCE_BY_TOP_DIR:
        raise ValueError(f"Unclassified top-level dir '{top_dir}' for {rel_path} — add it to SOURCE_BY_TOP_DIR")
    return SOURCE_BY_TOP_DIR[top_dir]


def skip_reason(rel_path: Path) -> Optional[str]:
    if rel_path.name in EXCLUDED_FILENAMES:
        return "navigation-dump (index.md, not retrievable content)"
    return None


# Frontmatter keys promoted into the generic `facets` dict (ADR-0001).
# Corpus-specific vocabulary lives here, not in the payload schema.
FACET_KEYS = ("release", "product", "classification", "area", "locale", "source")
LIST_FACET_KEYS = ("tags",)


def extract_metadata(text: str, rel_path: Path) -> dict:
    """Doc title, doc_type and generic facets from YAML frontmatter.

    Never raises on malformed frontmatter — a bad YAML header degrades to
    filename-derived defaults and is reported via the returned 'warnings'.
    """
    import yaml
    from ingest.chunker import parse_frontmatter, unescape_markdown, HEADING_RE

    raw_fm, body = parse_frontmatter(text)
    warnings: list[str] = []
    meta: dict = {}
    if raw_fm.strip():
        try:
            loaded = yaml.safe_load(raw_fm)
            if isinstance(loaded, dict):
                meta = loaded
            else:
                warnings.append("frontmatter is not a mapping")
        except Exception as exc:
            warnings.append(f"frontmatter parse failed: {exc.__class__.__name__}")

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        title = None
        for line in body.splitlines():
            m = HEADING_RE.match(line)
            if m and len(m.group(1)) == 1:
                title = m.group(2).strip()
                break
    title = unescape_markdown(title.strip()) if isinstance(title, str) else rel_path.stem

    doc_type = meta.get("topic_type") or meta.get("classification") or "note"
    if not isinstance(doc_type, str):
        doc_type = "note"

    facets: dict = {}
    for key in FACET_KEYS:
        value = meta.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            facets[key] = str(value).strip()
    for key in LIST_FACET_KEYS:
        value = meta.get(key)
        if isinstance(value, list):
            items = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
            if items:
                facets[key] = items

    return {"doc_title": title, "doc_type": doc_type.strip().lower(),
            "facets": facets, "warnings": warnings}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
