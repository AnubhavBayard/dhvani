"""Fetch the index, then serve. The Space's entrypoint.

The index is 2.5 GB and lives in a dataset repo rather than in this Space:
Spaces are for code, and a 2.5 GB git repo makes every push slow and every
rebuild slower. A free Space has no persistent disk, so this runs on every cold
start — measured against the Hub's own network rather than assumed, and printed,
because a judge who hits a sleeping Space should see progress rather than a
blank page.

`chunks.arrow` is deliberately **not** downloaded. It is the mmap'd store that
ADR-033 built to cut 3.88 GB of residency on an 8 GB box; here the box has 16 GB
and the constraint that actually bites is the 2.85 GB it would add to every cold
start. The parquet fallback in `ChunkStore.load` is the right side of that trade
on this host, and it is the reason the fallback exists.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

INDEX_REPO = os.environ.get("DHVANI_INDEX_REPO", "Anubhav100/dhvani-index")
INDEX_DIR = Path(os.environ.get("DHVANI_INDEX_DIR", "/home/user/index"))
# Everything the query path opens, and nothing the build needs.
KEEP = ["hnsw_sq8.faiss", "bm25/*", "chunks.parquet", "phonetic_vocab.json",
        "manifest.json"]


def fetch_index() -> None:
    from huggingface_hub import snapshot_download

    if (INDEX_DIR / "hnsw_sq8.faiss").exists():
        print(f"index already present at {INDEX_DIR}", flush=True)
        return
    t0 = time.perf_counter()
    print(f"downloading {INDEX_REPO} -> {INDEX_DIR}", flush=True)
    snapshot_download(INDEX_REPO, repo_type="dataset", local_dir=str(INDEX_DIR),
                      allow_patterns=KEEP, max_workers=8,
                      token=os.environ.get("HF_TOKEN"))
    size = sum(f.stat().st_size for f in INDEX_DIR.rglob("*") if f.is_file())
    print(f"index ready: {size / 1e9:.2f} GB in {time.perf_counter() - t0:.1f}s",
          flush=True)


def main() -> None:
    fetch_index()
    import uvicorn

    # One worker, deliberately: a second would mmap and load its own copy of a
    # 1.7 GB FAISS index for no throughput a demo will ever use (DESIGN.md).
    uvicorn.run("dhvani.app:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "7860")), workers=1)


if __name__ == "__main__":
    main()
