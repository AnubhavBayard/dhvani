"""Embedding model bake-off: recall and CPU latency on real MSMARCO-XI rows.

Decides the retriever for ADR-007/ADR-011. Brute-force exact cosine on purpose —
this measures the *model*, not the ANN index. Index recall is a separate number
measured after the HNSW build.

Eval construction, from one language's validation parquet:
  * keep rows with at least one `is_selected` passage (only ~55% of rows have one)
  * corpus  = every `Translated_passages` entry of the sampled rows, deduped
  * query   = the target-language `query`
  * gold    = the passages that row marked `is_selected`
so the pool is a realistic hard-negative set: ~10 passages per query, all
retrieved for a sibling query in the same corpus.

    python -m dhvani.bench.embed_bench --lang hin --queries 500 \
        --out docs/results/<date>-embed-bench.json
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

from dhvani.embed import MODELS, Embedder


def build_eval(parquet: Path, n_queries: int, seed: int) -> dict:
    """Queries with a known gold passage, plus the pooled corpus they live in."""
    tbl = pq.read_table(parquet, columns=["query_id", "query", "Eng_Query", "passages"])

    # Stay in Arrow until the rows are chosen. to_pylist() on the full file
    # materializes ~2M Python strings and costs several GB; on the sampled rows
    # it costs nothing.
    sel = tbl.column("passages").combine_chunks().field("is_selected")
    flat = pc.list_flatten(sel).to_numpy(zero_copy_only=False)
    parent = pc.list_parent_indices(sel).to_numpy(zero_copy_only=False)
    n_selected = np.bincount(parent, weights=flat, minlength=len(sel))
    usable = np.flatnonzero(n_selected > 0)

    rng = np.random.default_rng(seed)
    picked = np.sort(rng.choice(usable, size=min(n_queries, len(usable)), replace=False))
    sub = tbl.take(picked)
    del tbl

    qid = sub.column("query_id").to_pylist()
    qry = sub.column("query").to_pylist()
    eqry = sub.column("Eng_Query").to_pylist()
    pas = sub.column("passages").to_pylist()

    corpus: list[str] = []
    corpus_en: list[str] = []
    index_of: dict[str, int] = {}
    queries = []
    for row, p in enumerate(pas):
        gold = []
        for text, en, is_sel in zip(p["Translated_passages"], p["English_passages"], p["is_selected"]):
            j = index_of.get(text)
            if j is None:
                j = index_of[text] = len(corpus)
                corpus.append(text)
                corpus_en.append(en)
            if is_sel:
                gold.append(j)
        queries.append({"query_id": qid[row], "query": qry[row],
                        "eng_query": eqry[row], "gold": gold})

    return {
        "queries": queries,
        "corpus": corpus,
        "corpus_en": corpus_en,
        "rows_with_gold": int(usable.size),
        "rows_total": int(n_selected.size),
    }


def score(qv: np.ndarray, cv: np.ndarray, golds: list[list[int]], ks=(1, 5, 10)) -> dict:
    sims = qv @ cv.T
    order = np.argsort(-sims, axis=1)[:, :max(ks)]
    out = {}
    for k in ks:
        hits = sum(1 for r, g in zip(order, golds) if set(g) & set(r[:k].tolist()))
        out[f"recall@{k}"] = round(hits / len(golds), 4)
    rr = []
    for r, g in zip(order, golds):
        gs = set(g)
        rr.append(next((1 / (rank + 1) for rank, d in enumerate(r.tolist()) if d in gs), 0.0))
    out["mrr@10"] = round(float(np.mean(rr)), 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="hin")
    ap.add_argument("--split", default="validation")
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--queries", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260815)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--threads", type=int, default=2, help="match the 2-vCPU deploy box")
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    ap.add_argument("--out", default=f"docs/results/{date.today()}-embed-bench.json")
    args = ap.parse_args()

    suffix = "val" if args.split == "validation" else "train"
    parquet = Path(args.data_dir) / args.split / f"{args.lang}{suffix}.parquet"
    ev = build_eval(parquet, args.queries, args.seed)
    golds = [q["gold"] for q in ev["queries"]]
    print(f"{len(ev['queries'])} queries, {len(ev['corpus'])} corpus passages "
          f"({ev['rows_with_gold']}/{ev['rows_total']} rows have a gold passage)", flush=True)

    result = {
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {"threads": args.threads, "provider": "CPUExecutionProvider"},
        "eval": {
            "dataset": "ai4bharat/MSMARCO-XI",
            "lang": args.lang, "split": args.split,
            "queries": len(ev["queries"]), "corpus_passages": len(ev["corpus"]),
            "seed": args.seed, "max_len": args.max_len, "batch_size": args.batch_size,
            "rows_with_gold": ev["rows_with_gold"], "rows_total": ev["rows_total"],
        },
        "models": {},
    }

    for key in args.models:
        spec = MODELS[key]
        if not Path(spec.onnx).exists():
            print(f"skip {key}: {spec.onnx} missing", flush=True)
            continue
        print(f"--- {key}", flush=True)
        emb = Embedder(spec, args.threads, args.max_len)

        t0 = time.perf_counter()
        cv = emb.encode_passages(ev["corpus"], args.batch_size)
        t_corpus = time.perf_counter() - t0

        qv = emb.encode([q["query"] for q in ev["queries"]], args.batch_size,
                        spec.query_prefix)
        # cross-lingual: target-language query against the English passages
        cve = emb.encode_passages(ev["corpus_en"], args.batch_size)

        # single-query latency, the number the query path actually pays
        singles = []
        for q in ev["queries"][:100]:
            t0 = time.perf_counter()
            emb.encode_query(q["query"])
            singles.append((time.perf_counter() - t0) * 1000)
        singles.sort()

        result["models"][key] = {
            "model": spec.name,
            "dims": cv.shape[1],
            "notes": spec.notes,
            "onnx_size_mb": spec.size_mb,
            "monolingual": score(qv, cv, golds),
            "cross_lingual_query_to_english": score(qv, cve, golds),
            "latency_ms_single_query": {
                "p50": round(singles[len(singles) // 2], 2),
                "p95": round(singles[int(len(singles) * 0.95)], 2),
                "p100": round(singles[-1], 2),
                "n": len(singles),
            },
            "corpus_encode": {
                "passages": len(ev["corpus"]),
                "seconds": round(t_corpus, 1),
                "passages_per_second": round(len(ev["corpus"]) / t_corpus, 1),
            },
            "index_bytes_per_vector_int8": cv.shape[1],
        }
        print(json.dumps(result["models"][key], indent=2), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        # Each model gets its own process — a 2.3 GB fp32 session alongside the
        # 4 GB parquet read OOM'd a 15 GB box on 15 Aug. Merge so three separate
        # runs still produce one comparable results file, and refuse to merge
        # results measured on a different eval.
        previous = json.loads(out.read_text())
        if previous.get("eval") == result["eval"]:
            merged = dict(previous["models"])
            merged.update(result["models"])
            result["models"] = merged
        else:
            print("eval config changed — not merging with the existing file",
                  flush=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"wrote {out} ({len(result['models'])} model(s))", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
