"""The chunk store as Arrow IPC, which is the only format that is actually lazy.

ADR-025 chose `pq.read_table(..., memory_map=True)` and claimed the pages a query
touches are the only ones resident. Measured 19 Aug on the full index, that is
false: **3.88 GB resident**. `memory_map` maps the *file*, but parquet is
compressed, so every column is decompressed into fresh Arrow buffers on read —
zero-copy is impossible by construction.

Uncompressed Arrow IPC is the format where the claim holds, because the file
layout on disk *is* the in-memory layout. Same table, measured the same way:
**0.006 GB resident, 0.038 ms per row lookup** (the parquet store's documented
figure was 0.06 ms).

Parquet stays as the build artifact — 337 MB against 2.5 GB, and it is what the
per-corpus parts and every offline tool read. This file is written beside it for
serving.

    python -m dhvani.build.arrow_store --index index/full
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ARROW_NAME = "chunks.arrow"


def write_arrow_store(parquet: Path, arrow: Path, batch_size: int = 50_000) -> dict:
    """Stream parquet row groups into an uncompressed IPC file.

    Streamed, not `read_table` then `write_feather`: the whole point is a store
    that never has to be resident, and materializing it here to write it would
    need the 3.9 GB this exists to avoid.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.perf_counter()
    pf = pq.ParquetFile(parquet)
    rows = 0
    with pa.OSFile(str(arrow), "wb") as sink:
        writer = None
        for batch in pf.iter_batches(batch_size=batch_size):
            if writer is None:
                writer = pa.ipc.new_file(sink, batch.schema)
            writer.write_batch(batch)
            rows += batch.num_rows
        if writer is not None:
            writer.close()
    return {"rows": rows, "bytes": arrow.stat().st_size,
            "seconds": round(time.perf_counter() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index/full")
    args = ap.parse_args()
    d = Path(args.index)
    report = write_arrow_store(d / "chunks.parquet", d / ARROW_NAME)
    print(f"{report['rows']:,} rows -> {d / ARROW_NAME} "
          f"({report['bytes'] / 1e9:.2f} GB, {report['seconds']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
