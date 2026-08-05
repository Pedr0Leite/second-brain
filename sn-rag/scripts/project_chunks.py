"""Project full-corpus chunk counts from a random sample. Shows its arithmetic.

Usage: python3 scripts/project_chunks.py [--sample N] [--seed S]
"""
import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, MANIFEST_DB_PATH, PARENT_CHUNK_MIN_CHARS,
                    PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
from ingest import manifest
from ingest.chunker import chunk_document


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260804)
    args = ap.parse_args()

    with manifest.connect(MANIFEST_DB_PATH) as conn:
        rows = conn.execute("SELECT rel_path, source, bytes FROM files WHERE status='pending'").fetchall()

    population = len(rows)
    random.seed(args.seed)
    sample = rows if population <= args.sample else random.sample(rows, args.sample)

    parents_total = children_total = child_chars_total = 0
    per_file_children, by_source, failures = [], {}, []
    t0 = time.perf_counter()

    for rel_path, source, _ in sample:
        try:
            text = (CORPUS_PATH / rel_path).read_text(encoding="utf-8", errors="replace")
            parents, children = chunk_document(text, rel_path, PARENT_CHUNK_MIN_CHARS,
                                               PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS,
                                               CHILD_CHUNK_OVERLAP)
        except Exception as exc:
            failures.append((rel_path, repr(exc)))
            continue
        parents_total += len(parents)
        children_total += len(children)
        child_chars_total += sum(len(c.text) for c in children)
        per_file_children.append(len(children))
        agg = by_source.setdefault(source, {"files": 0, "parents": 0, "children": 0})
        agg["files"] += 1
        agg["parents"] += len(parents)
        agg["children"] += len(children)

    elapsed = time.perf_counter() - t0
    n = len(per_file_children)
    if n == 0:
        print("no files chunked")
        sys.exit(1)

    parents_per_file = parents_total / n
    children_per_file = children_total / n
    scale = population / n

    print(f"population (status=pending)   = {population}")
    print(f"sample size (seed={args.seed}) = {n}   [failures: {len(failures)}]")
    print(f"chunking wall-clock           = {elapsed:.2f}s  ({n / elapsed:.0f} files/sec, chunking only)")
    print()
    print(f"sample parents                = {parents_total}")
    print(f"sample children               = {children_total}")
    print(f"parents/file                  = {parents_total} / {n} = {parents_per_file:.3f}")
    print(f"children/file                 = {children_total} / {n} = {children_per_file:.3f}")
    print(f"children/file median          = {statistics.median(per_file_children)}")
    print(f"children/file p90             = {sorted(per_file_children)[int(n * 0.9)]}")
    print(f"children/file max             = {max(per_file_children)}")
    print()
    print(f"PROJECTED parents  = {parents_per_file:.3f} x {population} = {round(parents_per_file * population):,}")
    print(f"PROJECTED children = {children_per_file:.3f} x {population} = {round(children_per_file * population):,}")
    print(f"  (scale factor = {population} / {n} = {scale:.2f})")
    print()
    mean_child_chars = child_chars_total / children_total
    print(f"mean child chunk size = {child_chars_total} / {children_total} = {mean_child_chars:.1f} chars")
    print(f"PROJECTED text to embed = {round(children_per_file * population):,} x {mean_child_chars:.1f} chars"
          f" = {round(children_per_file * population * mean_child_chars / 1e9, 2)} GB")
    print()
    print("by source (sample):")
    for source, agg in sorted(by_source.items()):
        print(f"  {source:12s} files={agg['files']:5d} parents/file={agg['parents'] / agg['files']:6.2f}"
              f" children/file={agg['children'] / agg['files']:6.2f}")

    if failures:
        print(f"\nfailures ({len(failures)}):")
        for rel_path, exc in failures[:10]:
            print(f"  {rel_path}: {exc}")


if __name__ == "__main__":
    main()
