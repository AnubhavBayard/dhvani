"""Why does one embed worker cost 2.3 GB?

`docs/results/2026-08-15-build-memory-300r-8w.json`: a worker holding a 4096-text
shard peaked at 2.33 GB, on a build whose whole corpus is 17,069 chunks. The
model is a 120 MB INT8 file and the shard's own output is 6 MB, so neither is the
term that matters. This isolates the candidates in a single process:

    python -m dhvani.bench.embed_memory --arena 1 --sort 0

`VmHWM` is the kernel's own high-water mark for the process, so the number does
not depend on when a sampler happened to look.
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


def _vm_hwm_gb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return round(int(line.split()[1]) / 1048576, 2)
    return float("nan")


def load_texts(chunks_parquet: Path, n: int) -> list[str]:
    tbl = pq.read_table(chunks_parquet, columns=["text"])
    texts = tbl.column("text").to_pylist()[:n]
    del tbl
    return texts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="index/chunks.parquet")
    ap.add_argument("--n", type=int, default=4096, help="one shard, as the build ships it")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--arena", type=int, default=1,
                    help="onnxruntime CPU memory arena: 1 on (default), 0 off")
    ap.add_argument("--sort", type=int, default=0,
                    help="1 = sort the shard by length before batching")
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    texts = load_texts(Path(args.chunks), args.n)
    baseline = _vm_hwm_gb()
    lengths = np.array([len(t) for t in texts])

    from dhvani.embed import Embedder
    emb = Embedder(threads=args.threads, max_len=args.max_len,
                   cpu_mem_arena=bool(args.arena))
    after_model = _vm_hwm_gb()

    t0 = time.perf_counter()
    vecs = emb.encode(texts, args.batch_size, emb.spec.passage_prefix,
                      sort=bool(args.sort))
    seconds = time.perf_counter() - t0

    report = {
        "config": {"n": len(texts), "batch_size": args.batch_size,
                   "threads": args.threads, "cpu_mem_arena": bool(args.arena),
                   "sorted_by_length": bool(args.sort), "max_len": args.max_len},
        "text_chars": {"p50": int(np.percentile(lengths, 50)),
                       "p95": int(np.percentile(lengths, 95)),
                       "max": int(lengths.max())},
        "peak_rss_gb": {"after_texts": baseline, "after_model": after_model,
                        "after_encode": _vm_hwm_gb()},
        "ru_maxrss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 2),
        "seconds": round(seconds, 1),
        "chunks_per_second": round(len(texts) / max(seconds, 1e-9), 1),
        "vectors": list(vecs.shape),
    }
    print(json.dumps(report, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
