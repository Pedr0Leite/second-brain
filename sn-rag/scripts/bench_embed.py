"""DECISION-2 benchmark: embedding throughput on THIS hardware, real corpus chunks.

Embeds `h_path + "\\n\\n" + chunk_text` (the actual indexed text per spec §5.1),
never synthetic strings. Reports chunks/sec and projected full-corpus wall-clock
against the measured chunk-count projection.

Usage: python3 scripts/bench_embed.py --n 512 [--models a,b,c]
"""
import argparse
import os
import random
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, MANIFEST_DB_PATH, PARENT_CHUNK_MIN_CHARS,
                    PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
from ingest import manifest
from ingest.chunker import chunk_document

DEFAULT_MODELS = [
    "BAAI/bge-small-en-v1.5",
    "snowflake/snowflake-arctic-embed-s",
    "BAAI/bge-base-en-v1.5",
    "BAAI/bge-large-en-v1.5",
]

# From scripts/project_chunks.py (see docs/BUILD-LOG.md Phase 2).
PROJECTED_CHILD_CHUNKS = 525_217
OVERNIGHT_HOURS = 10


def load_real_chunks(n: int, seed: int) -> list[str]:
    """Real child-chunk texts, exactly as they would be embedded."""
    with manifest.connect(MANIFEST_DB_PATH) as conn:
        rows = conn.execute("SELECT rel_path FROM files WHERE status='pending'").fetchall()
    random.seed(seed)
    random.shuffle(rows)
    texts: list[str] = []
    for (rel_path,) in rows:
        try:
            raw = (CORPUS_PATH / rel_path).read_text(encoding="utf-8", errors="replace")
            _, children = chunk_document(raw, rel_path, PARENT_CHUNK_MIN_CHARS,
                                         PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS,
                                         CHILD_CHUNK_OVERLAP)
        except Exception:
            continue
        for c in children:
            texts.append(f"{c.h_path}\n\n{c.text}" if c.h_path else c.text)
            if len(texts) >= n:
                return texts
    return texts


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=512)
    ap.add_argument("--warmup", type=int, default=32)
    ap.add_argument("--seed", type=int, default=20260804)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--models", type=str, default=",".join(DEFAULT_MODELS))
    ap.add_argument("--no-sort", action="store_true",
                    help="disable length-sorted batching (measured 5x slower)")
    args = ap.parse_args()

    from fastembed import TextEmbedding

    texts = load_real_chunks(args.n + args.warmup, args.seed)
    if len(texts) < args.n + args.warmup:
        print(f"only got {len(texts)} chunks, wanted {args.n + args.warmup}", file=sys.stderr)
    warm, bench = texts[:args.warmup], texts[args.warmup:args.warmup + args.n]
    mean_chars = sum(len(t) for t in bench) / len(bench)

    # Transformer batches pad every sequence to the longest member, so mixing a
    # 200-char chunk with an 8,000-char code block wastes most of the compute.
    # Sorting by length before batching measured 5.4x faster (see BUILD-LOG).
    # The production indexer must do the same.
    if not args.no_sort:
        bench = sorted(bench, key=len)

    print(f"host: {os.cpu_count()} logical CPUs | onnxruntime CPU provider")
    print(f"benchmark corpus: {len(bench)} real child chunks, mean {mean_chars:.0f} chars,"
          f" max {max(len(t) for t in bench)} chars")
    print(f"config: seed={args.seed}, batch={args.batch},"
          f" length_sorted={not args.no_sort}")
    print(f"projection basis: {PROJECTED_CHILD_CHUNKS:,} child chunks (Phase 2 measurement)")
    floor = PROJECTED_CHILD_CHUNKS / (OVERNIGHT_HOURS * 3600)
    print(f"overnight floor: {PROJECTED_CHILD_CHUNKS:,} / ({OVERNIGHT_HOURS}h x 3600s)"
          f" = {floor:.1f} chunks/sec\n")

    header = f"{'model':40s} {'dim':>5s} {'load_s':>7s} {'chunks/s':>9s} {'full_corpus':>12s} {'rss_MB':>8s}  verdict"
    print(header)
    print("-" * len(header))

    for name in args.models.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            t0 = time.perf_counter()
            model = TextEmbedding(model_name=name)
            load_s = time.perf_counter() - t0

            list(model.embed(warm, batch_size=args.batch))  # warm caches

            t0 = time.perf_counter()
            vectors = list(model.embed(bench, batch_size=args.batch))
            elapsed = time.perf_counter() - t0

            rate = len(bench) / elapsed
            hours = PROJECTED_CHILD_CHUNKS / rate / 3600
            dim = len(vectors[0])
            verdict = "OK overnight" if hours <= OVERNIGHT_HOURS else f"TOO SLOW ({hours / 24:.1f}d)"
            print(f"{name:40s} {dim:5d} {load_s:7.1f} {rate:9.1f} {hours:11.2f}h {rss_mb():8.0f}  {verdict}")
            del model
        except Exception as exc:
            print(f"{name:40s} FAILED: {exc!r}")


if __name__ == "__main__":
    main()
