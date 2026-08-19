"""The benchmark of record: latency percentiles, retrieval quality, ablation.

`LATENCY.md` fixes the methodology; this module implements it and writes the
evidence file that every number in the docs points at.

    python -m dhvani.bench.benchmark --index index/full --reps 3 \
        --out docs/results/2026-08-18-bench-stage3.json

What it measures, and the parts it deliberately does not claim:

* **Boundary A is timed as one span**, not summed from stages, so harness
  overhead is inside the number rather than hidden between the stages. The gap
  between the span and the sum of the stages is reported as `overhead_ms`.
* **Only the stages that exist are in the span.** Today that is stage 3 plus the
  harness, so the reported boundary A is a *floor* on the finished pipeline, and
  the JSON says so in `boundary_a_covers`. It is not comparable to the 200 ms
  target until stages 4–7 are in it.
* **Quality is computed over gold-bearing queries only** (`has_gold`), latency
  over every query, which is the split `LATENCY.md` requires.
* **Percentiles are nearest-rank**, and P100 is the maximum observation rather
  than an interpolated tail.
* **Reps are independent runs of the same fixed query order.** The spread across
  reps is reported, because a single run's P100 is an anecdote.

Determinism: the query file fixes the order, `Stage3Config` fixes every knob,
and nothing here samples. Two runs of one arm differ only by the machine.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from dhvani.harness.contracts import PipelineTrace
from dhvani.retrieve.stage3 import HybridIndex, Stage3Config
from dhvani.retrieve.stage4 import QueryRewriter, Stage4Config
from dhvani.retrieve.stage7 import (ChunkStore, Stage7Config, TokenCounter,
                                    select_context)

# One arm per line: the name that appears in the ablation table, and what it
# changes about the default config. Stage 4–7 arms join this list as those
# stages land; the table's shape does not change.
# `_stage4: False` turns the rewriter off for that arm; everything else is a
# `Stage3Config` field.
ARMS: dict[str, dict] = {
    "full": {},
    "no_stage4": {"_stage4": False},
    "stage4_loose": {"_s4": {"min_phonetic": 0}},
    "stage4_ed1": {"_s4": {"max_edit_distance": 1}},
    "stage4_len3": {"_s4": {"min_term_len": 3}},
    "stage4_ed1_len5": {"_s4": {"max_edit_distance": 1, "min_term_len": 5}},
    "dense_only": {"bm25": False},
    "bm25_only": {"dense": False},
    "ef_search_256": {"ef_search": 256},
    "k_dense_200": {"k_dense": 200, "k_bm25": 200},
    "no_stage7": {"_stage7": False},
    "no_dedupe": {"_s7": {"dedupe": False}},
    "budget_800": {"_s7": {"token_budget": 800}},
    "budget_3000": {"_s7": {"token_budget": 3000}},
    "chunks_3": {"_s7": {"max_chunks": 3}},
}


def percentiles(values: list[float], points=(50, 70, 95, 100)) -> dict[str, float]:
    """Nearest-rank, on the sorted sample. P100 is the maximum observation."""
    if not values:
        return {f"p{p}": 0.0 for p in points}
    s = sorted(values)
    out = {}
    for p in points:
        rank = max(1, int(np.ceil(p / 100 * len(s))))
        out[f"p{p}"] = round(s[rank - 1], 3)
    return out


def ndcg_at_k(relevant: list[bool], n_gold: int, k: int = 10) -> float:
    """Binary-gain nDCG. The ideal ranking puts every gold hit the query could
    have returned at the top, capped at k."""
    if not n_gold:
        return 0.0
    dcg = sum(1.0 / np.log2(i + 2) for i, r in enumerate(relevant[:k]) if r)
    ideal = sum(1.0 / np.log2(i + 2) for i in range(min(n_gold, k)))
    return float(dcg / ideal) if ideal else 0.0


class Labels:
    """Gold labels, addressed by index row.

    Row is the join key rather than `chunk_id`: the same `chunk_id` exists once
    per language corpus, so an id lookup would count a Hindi hit as a Tamil one.
    """

    def __init__(self, index_dir: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        t = pq.read_table(index_dir / "chunks.parquet",
                          columns=["doc_id", "passage_idx", "lang", "is_selected"])
        self.doc = t.column("doc_id").cast(pa.int64()).to_numpy(zero_copy_only=False)
        self.passage = t.column("passage_idx").cast(pa.int32()).to_numpy(zero_copy_only=False)
        self.selected = t.column("is_selected").to_numpy(zero_copy_only=False).astype(bool)
        lang = t.column("lang").combine_chunks().dictionary_encode()
        self.lang_code = lang.indices.to_numpy(zero_copy_only=False)
        self.lang_names = lang.dictionary.to_pylist()

    def gold_count(self, qid: int, lang: str) -> int:
        """Gold *passages* for this query in its own language — the denominator
        nDCG's ideal ranking needs, and never more than the passages exist."""
        code = self.lang_names.index(lang)
        mask = (self.doc == qid) & self.selected & (self.lang_code == code)
        return int(len(np.unique(self.passage[mask])))

    def hits(self, rows: list[int], qid: int, lang: str) -> tuple[list[bool], list[bool]]:
        """(same-language relevance, any-language relevance) per returned row.

        Both are reported. Same-language is the strict reading; any-language
        credits a Hindi query that retrieves the English gold passage, which is
        a feature of a multilingual index rather than a miss, and the gap
        between the two is the cross-lingual transfer this system claims.
        """
        code = self.lang_names.index(lang)
        same, any_lang = [], []
        for r in rows:
            gold = self.doc[r] == qid and bool(self.selected[r])
            any_lang.append(gold)
            same.append(gold and self.lang_code[r] == code)
        return same, any_lang


def run_arm(index: HybridIndex, labels: Labels, queries: list[dict],
            cfg: Stage3Config, reps: int, k: int = 10,
            rewriter: QueryRewriter | None = None,
            s4: Stage4Config | None = None,
            store: ChunkStore | None = None,
            counter: TokenCounter | None = None,
            s7: Stage7Config | None = None) -> dict:
    s4 = s4 or Stage4Config()
    s7 = s7 or Stage7Config()
    per_rep = []
    quality_seen = False
    for rep in range(reps):
        lat, stage_ms, overhead = [], {}, []
        recall = mrr = ndcg = 0.0
        recall_any = 0.0
        n_gold_queries = 0
        rewritten = corrections = 0
        ctx_tokens, ctx_kept, ctx_drop = [], [], {"overlap": 0, "jaccard": 0,
                                                  "budget": 0, "capped": 0}
        for q in queries:
            t0 = time.perf_counter_ns()
            if rewriter is not None:
                # One trace across both stages, so boundary A is a single span
                # over the pipeline as it exists rather than a sum of two runs.
                query, trace = rewriter.rewrite(q["query"], s4, PipelineTrace())
                result, trace = index.search(query, cfg, trace)
                rewritten += query.method == "phonetic"
                corrections += len(query.corrections)
            else:
                result, trace = index.search(q["query"], cfg)
            if store is not None:
                # Inside the span: stage 7 is where boundary A ends (DESIGN.md),
                # so timing it outside would report a boundary that stops one
                # stage early.
                ctx, trace = select_context(result, store, counter, s7, trace)
                ctx_tokens.append(ctx.tokens)
                ctx_kept.append(len(ctx.chunks))
                ctx_drop["overlap"] += ctx.dropped_overlap
                ctx_drop["jaccard"] += ctx.dropped_jaccard
                ctx_drop["budget"] += ctx.dropped_budget
                ctx_drop["capped"] += ctx.dropped_capped
            boundary_a = (time.perf_counter_ns() - t0) / 1e6
            lat.append(boundary_a)
            overhead.append(boundary_a - trace.summed_ms)
            for name, ms in trace.stage_ms.items():
                stage_ms.setdefault(name, []).append(ms)

            if not q["has_gold"]:
                continue
            n_gold_queries += 1
            rows = [c.row for c in result.chunks[:k]]
            same, any_lang = labels.hits(rows, int(q["query_id"]), q["lang"])
            recall += any(same)
            recall_any += any(any_lang)
            mrr += next((1.0 / (i + 1) for i, r in enumerate(same) if r), 0.0)
            ndcg += ndcg_at_k(same, labels.gold_count(int(q["query_id"]), q["lang"]), k)
        quality_seen = quality_seen or bool(n_gold_queries)
        per_rep.append({
            "n": len(lat),
            "latency_ms": percentiles(lat),
            "mean_ms": round(float(np.mean(lat)), 3),
            "overhead_ms": percentiles(overhead, (50, 100)),
            "stage_ms": {s: percentiles(v, (50, 100)) for s, v in stage_ms.items()},
            "stage4": {"queries_rewritten": rewritten, "corrections": corrections},
            "stage7": ({"tokens": percentiles(ctx_tokens, (50, 100)),
                        "chunks_kept": percentiles(ctx_kept, (50, 100)),
                        "empty_context": sum(1 for c in ctx_kept if not c),
                        "dropped": ctx_drop} if store is not None else None),
            "quality": {
                "n_gold_queries": n_gold_queries,
                f"recall@{k}": round(recall / n_gold_queries, 4) if n_gold_queries else 0.0,
                f"recall@{k}_any_lang": round(recall_any / n_gold_queries, 4) if n_gold_queries else 0.0,
                f"mrr@{k}": round(mrr / n_gold_queries, 4) if n_gold_queries else 0.0,
                f"ndcg@{k}": round(ndcg / n_gold_queries, 4) if n_gold_queries else 0.0,
            },
        })
    p50s = [r["latency_ms"]["p50"] for r in per_rep]
    return {
        "config": cfg.__dict__ if hasattr(cfg, "__dict__") else vars(cfg),
        "reps": per_rep,
        # The spread is the honest error bar on a single run's percentile.
        "p50_spread_ms": round(max(p50s) - min(p50s), 3),
        "quality_stable": quality_seen and len({json.dumps(r["quality"], sort_keys=True)
                                                for r in per_rep}) == 1,
    }


def hardware() -> dict:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        mem_kb = next(int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines()
                      if l.startswith("MemTotal:"))
    except OSError:
        mem_kb = 0
    import os
    return {"cpu": cpu, "cores": os.cpu_count(), "mem_total_gb": round(mem_kb / 1048576, 2),
            "platform": platform.platform(), "python": platform.python_version()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index/full")
    ap.add_argument("--queries", default="eval/queries.jsonl")
    ap.add_argument("--arms", nargs="*", default=list(ARMS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="cap the query set; 0 = all")
    ap.add_argument("--warmup", type=int, default=50,
                    help="throwaway queries before the measured batch; no cold "
                         "start lands in a reported percentile (LATENCY.md)")
    ap.add_argument("--threads", type=int, default=2,
                    help="embedder threads; 2 matches the deploy box (ADR-010)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--no-stage7", action="store_true",
                    help="stop boundary A at stage 3, as runs before 19 Aug did")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    index_dir = Path(args.index)
    queries = [json.loads(l) for l in Path(args.queries).read_text().splitlines() if l.strip()]
    if args.limit:
        queries = queries[:args.limit]

    t0 = time.perf_counter()
    index = HybridIndex.load(index_dir, threads=args.threads)
    labels = Labels(index_dir)
    rewriter = QueryRewriter.load(index_dir)
    store = None if args.no_stage7 else ChunkStore.load(index_dir)
    counter = None if args.no_stage7 else TokenCounter()
    load_s = time.perf_counter() - t0

    # Cold start is measured before the warm-up rather than discarded with it:
    # a benchmark that only publishes warm numbers has hidden its worst one.
    t0 = time.perf_counter()
    index.search(queries[0]["query"])
    cold_ms = (time.perf_counter() - t0) * 1000

    for q in queries[:args.warmup]:
        index.search(q["query"])

    report = {
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index": str(index_dir),
        "queries": args.queries,
        "n_queries": len(queries),
        "n_gold_queries": sum(q["has_gold"] for q in queries),
        "reps": args.reps,
        "warmup": args.warmup,
        "k": args.k,
        "hardware": hardware(),
        "boundary_a_covers": ["stage4_rewrite", "stage3_embed", "stage3_retrieve",
                              "stage3_fuse", "stage3_signals", "harness"]
                             + ([] if args.no_stage7 else ["stage7_context"]),
        # The guardrail layers are inside boundary A in `pipeline.py`, but this
        # harness assembles the stages itself and does not run them, so they are
        # outside *this* span. Their cost is measured separately (GUARDRAILS.md:
        # L1 P50 0.016 ms, L2/L3 arithmetic on signals stage 3 already has).
        "not_yet_in_boundary_a": ["stage5_expansion", "stage6_rerank",
                                  "guardrail_l1", "guardrail_l2", "guardrail_l3"]
                                 + (["stage7_context"] if args.no_stage7 else []),
        "cold_start": {"index_load_s": round(load_s, 2),
                       "first_query_ms": round(cold_ms, 2)},
        "arms": {},
    }

    for name in args.arms:
        knobs = dict(ARMS[name])
        s4 = Stage4Config(enabled=knobs.pop("_stage4", True),
                          **knobs.pop("_s4", {}))
        s7 = Stage7Config(enabled=knobs.pop("_stage7", True),
                          **knobs.pop("_s7", {}))
        cfg = replace(Stage3Config(), **knobs)
        t0 = time.perf_counter()
        report["arms"][name] = run_arm(index, labels, queries, cfg, args.reps,
                                       args.k, rewriter, s4, store, counter, s7)
        r0 = report["arms"][name]["reps"][0]
        print(f"{name:14s} s4 {r0['stage4']['queries_rewritten']:3d}q/"
              f"{r0['stage4']['corrections']:3d}c  "
              f"p50 {r0['latency_ms']['p50']:7.2f}ms  "
              f"p100 {r0['latency_ms']['p100']:8.2f}ms  "
              f"recall@{args.k} {r0['quality'][f'recall@{args.k}']:.4f}  "
              f"mrr {r0['quality'][f'mrr@{args.k}']:.4f}  "
              f"({time.perf_counter() - t0:.0f}s)", flush=True)

    out = Path(args.out) if args.out else Path(
        f"docs/results/{time.strftime('%Y-%m-%d')}-bench-stage3.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
