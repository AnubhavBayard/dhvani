"""Build the index: chunk, embed, FAISS + BM25 + phonetic vocabulary.

Build-time only (ADR-002); contributes 0 ms to boundary A.

    python -m dhvani.build.build_index --dry-run          # chunk yield + sizing
    python -m dhvani.build.build_index --rows 1000        # small end-to-end
    python -m dhvani.build.build_index                    # the real build

The real build embeds one corpus per *part*, checkpointed to disk, and merges
the parts into the index at the end. Parts already on disk are skipped, so the
build can be split across several processes when one process cannot hold the
whole thing — see "parts" below.

Parallelism note (`MEASURED 2026-08-16`): ONNX intra-op threads scale badly on
this model — 36 passages/s at 2 threads, 71 at 8, and *61* at 16, because the
model is small and memory-bound. Data parallelism across processes is the lever
that works, so the embed pass is a process pool of 2-thread workers rather than
one fat 16-thread session.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from dhvani.build import chunk as ch
from dhvani.build.subset import LANGUAGES, SCRIPT
from dhvani.embed import DEFAULT_MODEL, MODELS
from dhvani.harness.contracts import Chunk

# The English pivot is indexed once and shared by every language (ADR-012); the
# 14 language files carry byte-identical `English_passages`.
PIVOT = "eng"

_WORKER = {}


def _init_worker(model_key: str, threads: int, cpu_mem_arena: bool = True) -> None:
    from dhvani.embed import Embedder
    _WORKER["emb"] = Embedder(model_key, threads=threads,
                              cpu_mem_arena=cpu_mem_arena)


def _mem_available_gb() -> float:
    """Headroom left on the box. The build was OOM-killed once (change log,
    15 Aug) and this machine has no swap, so this is the number that decides
    whether a run survives — not RSS."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1048576
    return float("nan")


def _embed_shard(args) -> np.ndarray:
    texts, prefix, batch = args
    return _WORKER["emb"].encode(texts, batch, prefix)


def embed_parallel(texts: list[str], prefix: str, model_key: str, workers: int,
                   threads: int, batch: int = 32, shard: int = 4096,
                   cpu_mem_arena: bool = True,
                   out: np.ndarray | None = None) -> np.ndarray:
    """Embed `texts` with a pool of spawned workers, in input order.

    `out` is an optional preallocated destination — the corpus build passes a
    `np.memmap`, so the vectors land on disk as they are produced instead of
    costing the parent 1.2 GB of anonymous memory it has to hold for the length
    of the pass (see `main`).
    """
    if not texts:
        return np.zeros((0, MODELS[model_key].dims), dtype=np.float32)
    shards = [(texts[i:i + shard], prefix, batch) for i in range(0, len(texts), shard)]
    # spawn, not fork (`MEASURED 2026-08-15`): forked workers inherit the parent's
    # heap after the parquet read, and CPython's refcounting writes to every page
    # it touches, so copy-on-write copies it for real — 4.6 GB per worker, and
    # eight of them OOM-killed a 15 GB box. Spawned workers start empty and
    # receive only their shard.
    with ProcessPoolExecutor(max_workers=workers,
                             mp_context=mp.get_context("spawn"),
                             initializer=_init_worker,
                             initargs=(model_key, threads, cpu_mem_arena)) as pool:
        # Filled in place, not collected then vstacked: `list(pool.map(...))` plus
        # `np.vstack` holds two full copies of the vectors in the parent at once —
        # 1.3 GB each at one corpus of ADR-012's subset, and the parent is what
        # the OOM killer took on 15 Aug (anon-rss 4.5 GB). Shards come back in
        # order, so the write offset is just a running count.
        if out is None:
            out = np.empty((len(texts), MODELS[model_key].dims), dtype=np.float32)
        assert out.shape == (len(texts), MODELS[model_key].dims), \
            f"destination {out.shape} != ({len(texts)}, {MODELS[model_key].dims})"
        done = 0
        t0 = time.perf_counter()
        for i, part in enumerate(pool.map(_embed_shard, shards), start=1):
            out[done:done + len(part)] = part
            done += len(part)
            del part
            if i % 10 == 0 or i == len(shards):
                rate = done / max(time.perf_counter() - t0, 1e-9)
                print(f"  shard {i}/{len(shards)} {done:,}/{len(texts):,} chunks "
                      f"{rate:.0f}/s eta {(len(texts) - done) / max(rate, 1e-9) / 60:.0f}m "
                      f"memavail {_mem_available_gb():.1f}G", flush=True)
        assert done == len(texts), f"shard rows {done} != inputs {len(texts)}"
    return out


# --------------------------------------------------------------------------
# chunk generation
# --------------------------------------------------------------------------

COLUMNS = ["query_id", "query", "Eng_Query", "query_type", "passages"]


def _rows_for(parquet: Path, query_ids: set[int], batch_rows: int = 2048) -> dict:
    """The subset's rows, materialized a batch at a time.

    Reading the whole column set and then `take`-ing the subset materialized all
    97,941 rows of `passages` — a nested struct holding every passage in the file
    — to keep the ~15,000 that are wanted. That read is the bulk of the parent's
    2.73 GB peak (`docs/results/2026-08-15-build-memory-300r-8w.json`), and it
    costs the same at 300 rows as at 15,000, which is why the small build looked
    survivable. Filtering per batch bounds it by `batch_rows` instead of by the
    file.
    """
    wanted = np.fromiter(query_ids, dtype=np.int64)
    cols: dict[str, list] = {k: [] for k in
                             ("query_id", "query", "eng_query", "query_type", "passages")}
    pf = pq.ParquetFile(parquet)
    for batch in pf.iter_batches(batch_size=batch_rows, columns=COLUMNS):
        qid = np.asarray(batch.column("query_id"), dtype=np.int64)
        keep = np.flatnonzero(np.isin(qid, wanted))
        if not len(keep):
            continue
        sub = batch.take(pa.array(keep))
        cols["query_id"] += sub.column("query_id").to_pylist()
        cols["query"] += sub.column("query").to_pylist()
        cols["eng_query"] += sub.column("Eng_Query").to_pylist()
        cols["query_type"] += sub.column("query_type").to_pylist()
        cols["passages"] += sub.column("passages").to_pylist()
    return cols


def chunks_for_corpus(rows: dict, lang_key: str, english: bool,
                      strategies: set[str]) -> tuple[list[Chunk], list[list[str]]]:
    """Chunks for one corpus, plus the sentence lists S3 still needs vectors for.

    S3 is returned unembedded on purpose: sentence embedding is batched across
    the whole corpus in one pass, because doing it per passage is correct and
    roughly 30x slower.
    """
    field = "English_passages" if english else "Translated_passages"
    lang = "eng_Latn" if english else LANGUAGES[lang_key]
    script = SCRIPT["eng"] if english else SCRIPT[lang_key]
    query_field = "eng_query" if english else "query"

    out: list[Chunk] = []
    pending_s3: list[list[str]] = []
    s3_slots: list[tuple[str, int, dict, str]] = []

    for i, p in enumerate(rows["passages"]):
        doc_id = str(rows["query_id"][i])
        qtype = rows["query_type"][i]
        for pidx, (text, sel) in enumerate(zip(p[field], p["is_selected"])):
            meta = {"lang": lang, "script": script, "is_selected": bool(sel),
                    "query_type": qtype, "split": "validation"}
            if "s1_passage" in strategies:
                out += ch.s1_passage(text, doc_id, pidx, meta)
            if "s2_sentence_window" in strategies:
                out += ch.s2_sentence_window(text, doc_id, pidx, meta)
            if "s0_fixed_window" in strategies:
                out += ch.s0_fixed_window(text, doc_id, pidx, meta)
            if "s3_semantic" in strategies:
                sents = ch.split_sentences(ch.normalize(text))
                if len(sents) >= 3:
                    pending_s3.append([s for s, _, _ in sents])
                    s3_slots.append((doc_id, pidx, meta, text))

    return out, (pending_s3, s3_slots)


def s4_chunks(rows: dict, lang_key: str, english: bool) -> list[Chunk]:
    """Selected passages only, prefixed with the query they answer (ADR-016)."""
    field = "English_passages" if english else "Translated_passages"
    lang = "eng_Latn" if english else LANGUAGES[lang_key]
    script = SCRIPT["eng"] if english else SCRIPT[lang_key]
    query_field = "eng_query" if english else "query"

    out: list[Chunk] = []
    for i, p in enumerate(rows["passages"]):
        doc_id = str(rows["query_id"][i])
        for pidx, (text, sel) in enumerate(zip(p[field], p["is_selected"])):
            if not sel:
                continue
            meta = {"lang": lang, "script": script, "is_selected": True,
                    "query_type": rows["query_type"][i], "split": "s4_holdout"}
            out += ch.s4_query_context(text, doc_id, pidx, meta,
                                       query=rows[query_field][i],
                                       query_type=rows["query_type"][i])
    return out


def finish_s3(pending, workers: int, threads: int, model_key: str,
              batch: int, cpu_mem_arena: bool = True) -> list[Chunk]:
    """One batched sentence-embedding pass, then cut on the troughs."""
    sentence_lists, slots = pending
    if not sentence_lists:
        return []
    flat = [s for group in sentence_lists for s in group]
    vecs = embed_parallel(flat, MODELS[model_key].passage_prefix, model_key,
                          workers, threads, batch, cpu_mem_arena=cpu_mem_arena)
    out, at = [], 0
    for group, (doc_id, pidx, meta, text) in zip(sentence_lists, slots):
        v = vecs[at:at + len(group)]
        at += len(group)
        out += ch.s3_semantic(text, doc_id, pidx, meta, sentence_vectors=v)
    return out


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

CHUNK_COLUMNS = ["chunk_id", "text", "doc_id", "passage_idx", "strategy",
                 "ordinal", "lang", "script", "is_selected", "query_type",
                 "split", "token_count", "parent_text"]


def chunks_table(chunks: list[Chunk]) -> pa.Table:
    cols = {c: [] for c in CHUNK_COLUMNS}
    cols["char_start"], cols["char_end"], cols["overlap_with"] = [], [], []
    for c in chunks:
        for k in CHUNK_COLUMNS:
            cols[k].append(getattr(c, k))
        cols["char_start"].append(c.char_span[0])
        cols["char_end"].append(c.char_span[1])
        cols["overlap_with"].append(c.overlap_with)
    return pa.table(cols)


def write_chunks(chunks: list[Chunk], path: Path) -> None:
    pq.write_table(chunks_table(chunks), path, compression="zstd")


def new_faiss(dims: int, m: int, ef_construction: int):
    import faiss
    index = faiss.IndexHNSWSQ(dims, faiss.ScalarQuantizer.QT_8bit, m,
                              faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = ef_construction
    return index


def write_faiss(index, m: int, ef_construction: int, path: Path,
                build_s: float) -> dict:
    import faiss
    faiss.write_index(index, str(path))
    size = path.stat().st_size
    return {"vectors": int(index.ntotal), "dims": int(index.d), "M": m,
            "ef_construction": ef_construction,
            "build_seconds": round(build_s, 1),
            "bytes": size, "bytes_per_vector": round(size / max(1, index.ntotal), 1)}


def build_faiss(vectors: np.ndarray, m: int, ef_construction: int, path: Path) -> dict:
    """Whole-array build. Kept for small runs and tests; the main build adds
    per corpus instead (see `main`), because one array of every vector is
    5.2 GB at the subset size ADR-012 fixed."""
    index = new_faiss(vectors.shape[1], m, ef_construction)
    t0 = time.perf_counter()
    index.train(vectors)
    index.add(vectors)
    return write_faiss(index, m, ef_construction, path, time.perf_counter() - t0)


def build_bm25(texts: list[str], out_dir: Path) -> dict:
    import bm25s
    tokens = bm25s.tokenize(texts, show_progress=False,
                            token_pattern=ch.TOKEN_PATTERN)
    retriever = bm25s.BM25()
    t0 = time.perf_counter()
    retriever.index(tokens, show_progress=False)
    build_s = time.perf_counter() - t0
    retriever.save(str(out_dir))
    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    return {"documents": len(texts), "build_seconds": round(build_s, 1),
            "bytes": size}


def build_phonetic_vocab(texts: list[str], min_freq: int, path: Path) -> dict:
    """Corpus vocabulary keyed by phonetic code for stage 4.

    The key is `soundex(term)[1:]`, not `soundex(term)`. The library passes the
    input's first character through verbatim, so the full code is script-tagged
    and would silently bucket per script (RAG_PIPELINE.md stage 4,
    `tests/test_phonetic_contract.py`).
    """
    from libindic.soundex import Soundex
    sx = Soundex()

    freq: Counter[str] = Counter()
    for t in texts:
        freq.update(w for w in t.split() if len(w) > 2)

    buckets: dict[str, list[str]] = {}
    kept = 0
    for term, n in freq.items():
        if n < min_freq:
            continue
        try:
            code = sx.soundex(term)[1:]
        except Exception:  # noqa: BLE001 - a term the library cannot encode
            continue
        if not code or code.strip("0") == "":
            continue
        buckets.setdefault(code, [])
        if len(buckets[code]) < 32:  # bounded: stage 4 has a 3 ms budget
            buckets[code].append(term)
        kept += 1
    path.write_text(json.dumps(buckets, ensure_ascii=False))
    return {"terms_seen": len(freq), "terms_kept": kept,
            "buckets": len(buckets), "min_freq": min_freq,
            "bytes": path.stat().st_size}


# --------------------------------------------------------------------------
# parts: one corpus at a time, one process at a time
# --------------------------------------------------------------------------
#
# The build embeds one corpus per part and checkpoints it to disk, so the whole
# thing can be run as several processes instead of one long-lived one. That is
# the only fix available for the failure mode this build keeps hitting: peak
# memory is not a per-corpus cost, it is a cost that *accumulates* across
# corpora — the FAISS index grows as it is added to, and nothing an allocator
# hands back to a still-running process is really returned. A part per process
# gives every corpus the same starting headroom, because the previous corpus's
# memory went back to the kernel when its process exited.
#
#     python -m dhvani.build.build_index --langs hin --no-merge --out index/full
#     python -m dhvani.build.build_index --langs ben --no-merge --out index/full
#     python -m dhvani.build.build_index --langs tam --no-merge --out index/full
#     python -m dhvani.build.build_index --langs eng --out index/full   # + merge
#
# Parts already on disk are skipped, so the plain command resumes rather than
# restarts, and an interrupted run costs one corpus rather than all of them.


def _parts_dir(out: Path) -> Path:
    d = out / "parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_state(out: Path) -> dict:
    f = _parts_dir(out) / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _write_state(out: Path, state: dict) -> None:
    (_parts_dir(out) / "state.json").write_text(json.dumps(state, indent=2))


def part_paths(out: Path, lang_key: str) -> tuple[Path, Path]:
    d = _parts_dir(out)
    return d / f"{lang_key}.npy", d / f"{lang_key}.parquet"


def part_complete(out: Path, lang_key: str, state: dict) -> bool:
    vecs, table = part_paths(out, lang_key)
    return lang_key in state and vecs.exists() and table.exists()


def write_part(out: Path, lang_key: str, chunks: list[Chunk], model_key: str,
               workers: int, threads: int, batch: int,
               cpu_mem_arena: bool) -> float:
    """Embed one corpus straight to disk, then publish it atomically.

    Vectors go into a `np.memmap` rather than an in-memory array: at one corpus
    of ADR-012's subset that is 1.2 GB the parent would otherwise hold for the
    whole embed pass, next to four workers at ~1.9 GB each.

    Both files are written under a `.tmp` name and renamed once complete, so a
    run the OOM killer takes mid-corpus leaves no half-written part that the
    next run would mistake for a finished one.
    """
    vecs, table = part_paths(out, lang_key)
    tmp_vecs = vecs.with_suffix(".tmp.npy")
    tmp_table = table.with_suffix(".tmp.parquet")

    t0 = time.perf_counter()
    dest = np.lib.format.open_memmap(
        tmp_vecs, mode="w+", dtype=np.float32,
        shape=(len(chunks), MODELS[model_key].dims))
    embed_parallel([c.text for c in chunks], MODELS[model_key].passage_prefix,
                   model_key, workers, threads, batch,
                   cpu_mem_arena=cpu_mem_arena, out=dest)
    dest.flush()
    del dest
    elapsed = time.perf_counter() - t0

    write_chunks(chunks, tmp_table)
    os.replace(tmp_table, table)
    os.replace(tmp_vecs, vecs)
    return elapsed


def merge_parts(out: Path, lang_order: list[str], state: dict, m: int,
                ef_construction: int, min_term_freq: int,
                train_sample: int = 200_000, block: int = 100_000) -> dict:
    """Build FAISS, the chunk store, BM25 and the phonetic vocabulary from parts.

    Merge order is `lang_order`, not the order the parts happened to be built
    in, because row order is the join key across all three artifacts
    (`retrieve/stage3.py`): chunks row i is FAISS id i is BM25 doc i. Building
    hin then ben must produce the same index as ben then hin.
    """
    langs = [l for l in lang_order if part_complete(out, l, state)]
    if not langs:
        return {}
    import faiss

    dims = None
    report: dict = {}
    t_faiss = time.perf_counter()

    # The quantizer is trained on a stride across every part, not on the head of
    # the first one. A part is one language; training on part 0 alone fits the
    # SQ8 range to Hindi and then quantizes Tamil against it.
    total = sum(state[l]["chunks"] for l in langs)
    sample = []
    for l in langs:
        mm = np.load(part_paths(out, l)[0], mmap_mode="r")
        dims = mm.shape[1]
        take = max(1, int(train_sample * len(mm) / max(total, 1)))
        step = max(1, len(mm) // take)
        sample.append(np.array(mm[::step][:take]))
        del mm
    train = np.concatenate(sample) if sample else np.zeros((0, dims), np.float32)
    del sample

    index = new_faiss(dims, m, ef_construction)
    index.train(train)
    del train

    # Added in blocks: a slice of the memmap is copied, added, and dropped, so
    # the resident cost is the index plus one block rather than the index plus
    # every vector in the corpus.
    for l in langs:
        mm = np.load(part_paths(out, l)[0], mmap_mode="r")
        for i in range(0, len(mm), block):
            index.add(np.array(mm[i:i + block]))
        del mm
        print(f"merge: {l} added, {index.ntotal:,} vectors", flush=True)
    faiss_s = time.perf_counter() - t_faiss

    # Chunk store: streamed row group at a time. Reading four part tables and
    # concatenating them holds every chunk's text twice.
    writer = None
    n_chunks = 0
    for l in langs:
        pf = pq.ParquetFile(part_paths(out, l)[1])
        for batch in pf.iter_batches(batch_size=50_000):
            table = pa.Table.from_batches([batch])
            if writer is None:
                writer = pq.ParquetWriter(out / "chunks.parquet", table.schema,
                                          compression="zstd")
            writer.write_table(table)
            n_chunks += table.num_rows
    writer.close()
    assert n_chunks == index.ntotal, \
        f"chunk store has {n_chunks}, FAISS has {index.ntotal}"

    report["chunk_store"] = {"bytes": (out / "chunks.parquet").stat().st_size}
    report["faiss"] = write_faiss(index, m, ef_construction,
                                  out / "hnsw_sq8.faiss", faiss_s)
    del index

    texts = pq.read_table(out / "chunks.parquet",
                          columns=["text"]).column("text").to_pylist()
    report["bm25"] = build_bm25(texts, out / "bm25")
    report["phonetic_vocab"] = build_phonetic_vocab(texts, min_term_freq,
                                                    out / "phonetic_vocab.json")
    report["merged_langs"] = langs
    return report


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", default="index/subset.json")
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--out", default="index")
    ap.add_argument("--rows", type=int, default=0,
                    help="cap rows for a fast end-to-end run; 0 = the whole subset")
    ap.add_argument("--langs", nargs="*", default=list(LANGUAGES) + [PIVOT])
    ap.add_argument("--strategies", nargs="*",
                    default=["s1_passage", "s2_sentence_window", "s3_semantic",
                             "s4_query_context"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--ef-construction", type=int, default=100)
    ap.add_argument("--min-term-freq", type=int, default=3)
    ap.add_argument("--cpu-mem-arena", action="store_true",
                    help="re-enable onnxruntime's CPU arena in the embed "
                         "workers. Off by default for the build: the arena "
                         "never returns memory to the OS, so a worker ratchets "
                         "up to its largest allocation and holds it across "
                         "every shard it is handed. Measured at 2,000 rows x 4 "
                         "corpora x 4 workers, turning it off moved peak worker "
                         "2.63 -> 1.88 GB and the MemAvailable floor 0.44 -> "
                         "4.22 GB for 2.5% throughput, with a byte-identical "
                         "index (ADR-018). Kept as a flag because it is that "
                         "measurement's ablation arm.")
    ap.add_argument("--no-merge", action="store_true",
                    help="build this run's corpus parts and stop. Use for every "
                         "part but the last when splitting the build across "
                         "processes; the final run merges every part on disk.")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-embed corpora that already have a part on disk "
                         "instead of skipping them")
    ap.add_argument("--dry-run", action="store_true",
                    help="chunk and report the yield and projected size; no embedding")
    args = ap.parse_args()

    manifest = json.loads(Path(args.subset).read_text())
    indexed_ids = manifest["query_ids"]
    if args.rows:
        indexed_ids = indexed_ids[:args.rows]
    indexed = set(indexed_ids)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    strategies = set(args.strategies)
    t_start = time.perf_counter()

    # ---- S4 holdout (ADR-016) --------------------------------------------
    s4_ids: set[int] = set()
    if "s4_query_context" in strategies:
        anchor = Path(args.data_dir) / "validation" / f"{manifest['anchor_lang']}val.parquet"
        all_ids = pq.read_table(anchor, columns=["query_id"]).column("query_id").to_pylist()
        pool = [q for q in all_ids if q not in indexed]
        rng = np.random.default_rng(manifest["seed"] + 1)
        take = min(len(indexed), len(pool))
        s4_ids = set(rng.choice(pool, size=take, replace=False).tolist())
        # The guard S4 exists under. Not a proxy for disjointness — the thing.
        assert not (s4_ids & indexed), "S4 headers overlap the indexed subset"
        print(f"s4 holdout: {len(s4_ids):,} rows, disjoint from the {len(indexed):,} indexed")

    # Streaming build: chunks are embedded and checkpointed one corpus at a time,
    # then dropped. Accumulating every corpus first needs one 5.2 GB array of
    # vectors plus ~3.4M live Chunk objects at ADR-012's subset size, on a box
    # that OOM-killed this build twice already (change log, 15 Aug). FAISS, BM25
    # and the phonetic vocabulary are all built in the merge pass, after every
    # embed worker has exited — during the embed there is nothing resident but
    # the corpus being worked on.
    all_chunks: list[Chunk] = []       # dry runs only; the real build streams
    state = {} if args.dry_run else _read_state(out)
    per_corpus = {}

    for lang_key in args.langs:
        if not args.dry_run and not args.rebuild and part_complete(out, lang_key, state):
            print(f"{lang_key}: part exists, skipping "
                  f"({state[lang_key]['chunks']:,} chunks)", flush=True)
            continue
        english = lang_key == PIVOT
        src = manifest["anchor_lang"] if english else lang_key
        parquet = Path(args.data_dir) / "validation" / f"{src}val.parquet"
        if not parquet.exists():
            print(f"skip {lang_key}: {parquet} not downloaded")
            continue

        t0 = time.perf_counter()
        rows = _rows_for(parquet, indexed)
        base, pending = chunks_for_corpus(rows, src, english, strategies)
        s3 = ([] if args.dry_run or "s3_semantic" not in strategies
              else finish_s3(pending, args.workers, args.threads, args.model,
                             args.batch_size, cpu_mem_arena=args.cpu_mem_arena))
        if args.dry_run and "s3_semantic" in strategies:
            # No vectors in a dry run, so S3's count is projected from its inputs
            # rather than reported as measured.
            s3 = []

        s4 = []
        if s4_ids:
            s4_rows = _rows_for(parquet, s4_ids)
            s4 = s4_chunks(s4_rows, src, english)

        produced = base + s3 + s4
        embed_s = 0.0
        if args.dry_run:
            all_chunks += produced
        else:
            embed_s = write_part(out, lang_key, produced, args.model,
                                 args.workers, args.threads, args.batch_size,
                                 args.cpu_mem_arena)
            print(f"{lang_key}: embedded {len(produced):,} chunks in "
                  f"{embed_s:.0f}s -> {part_paths(out, lang_key)[0]}", flush=True)

        per_corpus[lang_key] = {
            "rows": len(rows["query_id"]),
            "passages": sum(len(p["English_passages" if english else "Translated_passages"])
                            for p in rows["passages"]),
            "chunks": len(produced),
            "by_strategy": dict(Counter(c.strategy for c in produced)),
            "s3_passages_pending": len(pending[0]),
            "embed_seconds": round(embed_s, 1),
            "seconds": round(time.perf_counter() - t0, 1),
        }
        print(f"{lang_key}: {per_corpus[lang_key]['passages']:,} passages -> "
              f"{len(produced):,} chunks {per_corpus[lang_key]['by_strategy']}",
              flush=True)
        del produced, base, s3, s4, rows, pending

        if not args.dry_run:
            # Recorded only once the part's files are on disk, so state.json
            # never claims a corpus the next run cannot actually read.
            state[lang_key] = per_corpus[lang_key]
            _write_state(out, state)

    # Corpora built by earlier processes count towards the report, in the merge
    # order rather than the order they were built in.
    if not args.dry_run:
        per_corpus = {l: state[l] for l in list(LANGUAGES) + [PIVOT] if l in state}
    n_chunks = sum(c["chunks"] for c in per_corpus.values())
    embed_s = sum(c.get("embed_seconds", 0.0) for c in per_corpus.values())
    total_chunks = len(all_chunks) if args.dry_run else n_chunks
    total_passages = sum(c["passages"] for c in per_corpus.values())
    report = {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": MODELS[args.model].name,
        "subset": {"rows_indexed": len(indexed), "s4_holdout_rows": len(s4_ids)},
        "corpora": per_corpus,
        "totals": {"passages": total_passages, "chunks": total_chunks,
                   "chunks_per_passage": round(total_chunks / max(1, total_passages), 2)},
        "config": {"strategies": sorted(strategies), "M": args.m,
                   "ef_construction": args.ef_construction,
                   "workers": args.workers, "threads": args.threads,
                   "cpu_mem_arena": args.cpu_mem_arena},
    }

    if args.dry_run:
        text_bytes = sum(len(c.text.encode()) for c in all_chunks)
        vec_bytes = len(all_chunks) * (MODELS[args.model].dims + 144)  # SQ8 + graph
        report["projected"] = {
            "chunk_text_mb": round(text_bytes / 1e6, 1),
            "faiss_mb": round(vec_bytes / 1e6, 1),
            "note": "BM25 excluded; measured on a real build",
        }
        print(json.dumps(report["totals"] | report["projected"], indent=2))
        (out / "dry-run.json").write_text(json.dumps(report, indent=2))
        return 0

    # ---- finish -----------------------------------------------------------
    if not per_corpus:
        print("nothing built — no corpus produced chunks", flush=True)
        return 1

    report["embed"] = {
        "chunks": n_chunks, "seconds": round(embed_s, 1),
        "chunks_per_second": round(n_chunks / max(embed_s, 1e-9), 1),
        "workers": args.workers, "threads_per_worker": args.threads,
    }
    print(f"embedded {n_chunks:,} chunks in {embed_s:.0f}s "
          f"({n_chunks/max(embed_s,1e-9):.0f}/s)", flush=True)

    if args.no_merge:
        print(f"parts written to {_parts_dir(out)}; merge skipped "
              f"(--no-merge). Run without it to build the index.", flush=True)
        return 0

    # FAISS, BM25 and the phonetic vocabulary are built here, from the parts,
    # after every embed worker has exited — the one window in the build with the
    # whole box available. All three need the corpus at once, and doing any of
    # them inside the corpus loop is what made peak memory grow with every
    # corpus rather than stay flat.
    report |= merge_parts(out, list(LANGUAGES) + [PIVOT], state, args.m,
                          args.ef_construction, args.min_term_freq)
    report["total_seconds"] = round(time.perf_counter() - t_start, 1)
    report["index_bytes"] = (report["chunk_store"]["bytes"] + report["faiss"]["bytes"]
                             + report["bm25"]["bytes"] + report["phonetic_vocab"]["bytes"])

    (out / "manifest.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in
                      ("totals", "embed", "faiss", "bm25", "phonetic_vocab",
                       "index_bytes", "total_seconds")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
