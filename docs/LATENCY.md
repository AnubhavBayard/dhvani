# Latency

## Boundary definition

| # | Name | Starts | Ends | Contains | Target |
|---|---|---|---|---|---|
| **A** | `query_path` | final transcript received | context selected (stage 7 output) | stages 4, 3, 5, 6, 7 + guardrail layers L1–L3 + harness overhead | **< 200 ms** |
| B | `ttft` | final transcript received | first generated token emitted | A + prompt assembly + generation-provider round trip + first token | reported |
| C | `wall_clock` | mic button released | last answer token rendered | B + remaining generation + transport + render | reported |

Build-time work — chunking, overlap, embedding the corpus, HNSW construction —
is outside all three. It runs once, is amortized across every query, and putting
it in a per-query budget would be inventing a number.

**Why A is the target and B/C are not.** From India, a round trip to a hosted LLM
is 230–280 ms of pure network before the model produces a token. That single fact
means no system with a hosted generation call has a wall-clock under 200 ms,
regardless of how good its retrieval is. Targeting C would leave two options:
fabricate the number, or drop generation and stop being a RAG system. Boundary A
covers every stage the brief enumerates — "chunking + vector DB retrieval +
everything through to final output" of the retrieval pipeline — and is the span
whose latency is genuinely ours. B and C are published in full so anyone can
apply a stricter boundary to our own numbers.

If a local generation model on the same box were used, C would collapse toward A.
That is a real option and it is `OPEN`: it trades answer quality for a headline
number, and this project would rather have the honest split.

## Per-stage budget — boundary A

`TARGET` values. Replaced by `MEASURED` after the first `bench` run.

| Stage | fast tier | standard tier | Notes |
|---|---|---|---|
| 4 — query rewrite (phonetic) | 3 | 3 | dict lookup + bounded edit distance, no network |
| 3 — embed query | 8 | 8 | ONNX INT8, single short sequence |
| 3 — dense HNSW ∥ BM25 | 6 | 6 | parallel threads; budget is the max, not the sum |
| 3 — full-precision rescore top-50 | 3 | 3 | only if index is quantized |
| 3 — RRF fusion + signals | 1 | 1 | arithmetic |
| 5 — RM3 expansion + re-retrieve | — | 15 | lexical only by default |
| 6 — cross-encoder rerank, 32 in one batch | — | 60 | dominant cost; first target if measurement overruns |
| 7 — dedupe, order, budget, ids | 5 | 5 | |
| L2 scope + L3 floor | 2 | 2 | threshold comparisons on existing scores |
| harness — validation, tracing, async overhead | 5 | 5 | |
| **total** | **33** | **108** | |
| **headroom to 200 ms** | **167** | **92** | |

### First measured line — `MEASURED 2026-08-15`

One row of that table now has a number behind it.

| stage | budget | measured p50 | p95 | conditions |
|---|---|---|---|---|
| 3 — embed query | 8 ms | **2.86 ms** | 3.96 ms | `multilingual-e5-small` INT8 ONNX, 2 threads, 100 real Hindi validation queries |

Evidence: [`docs/results/2026-08-15-embed-bench.json`](results/2026-08-15-embed-bench.json).

**The budget does not move yet, but this is now the number of record.** It ran
at 2 ONNX threads on the 16-core dev box, and ADR-036 makes that box the
deployment — so the machine that measured this is the machine that serves the
live link. What the figure still does not cover is a warm cache and stages 5/6;
treat the 8 ms budget as met, not as re-derived.

L1 input guardrails are absent from the table because they run concurrently with
stages 4 and 3 and finish inside their shadow. If L1 ever exceeds stage 3's
duration it enters the critical path and moves into this table — the harness
measures it either way rather than assuming.

Escalated tier is over budget by construction: an LLM rewrite is ~250 ms of
network. It is reported as its own tier with its own percentiles and hit rate.

### Boundary A with stage 7 in it — `MEASURED 2026-08-19`

Evidence: [`docs/results/2026-08-19-bench-stage7.json`](results/2026-08-19-bench-stage7.json)
— 500 queries x 3 reps, warmed (50 throwaway queries), cache disabled, 2 ONNX
threads, 16-core dev box — which is the deploy hardware (ADR-036), so this is
the number of record.

Boundary A now covers stages **4, 3 and 7** plus the harness. Stages 5 and 6 are
deferred (ADR-027) and **absent from the span**, so this remains a *floor* on the
finished pipeline and is not comparable to the 200 ms target (ADR-021).

| arm | boundary A P50 | P70 | P95 | P100 | stage 7 P50 | stage 7 P100 |
|---|---|---|---|---|---|---|
| stages 4 + 3 (the 18 Aug span) | 12.14 | 13.52 | 16.53 | 18.29 | — | — |
| **+ stage 7** | **13.50** | **15.08** | **18.38** | **33.44** | **1.31** | **20.14** |

| stage | budget | measured P50 | P100 | verdict |
|---|---|---|---|---|
| 4 — query rewrite | 3 ms | **0.03 ms** | 1.33 ms | inside |
| 3 — embed query | 8 ms | **3.27 ms** | 6.49 ms | inside |
| 3 — dense ∥ BM25 | 6 ms | **8.38 ms** | 23.11 ms | **40% over at P50** |
| 3 — RRF fusion | 1 ms | **0.14 ms** | 0.30 ms | inside |
| 3 — confidence signals | (in the 1 ms above) | **0.04 ms** | 0.21 ms | inside |
| 3 — fp32 rescore top-50 | 3 ms | **off** | off | not built — no fp32 vectors persisted (ADR-017) |
| 7 — dedupe, order, budget, ids | 5 ms | **1.31 ms** | **20.14 ms** | inside at P50, **4x over at P100** |
| harness overhead | 5 ms | **0.13 ms** | 21.76 ms | inside at P50 |

**Stage 3's retrieval step is 40% over its own 6 ms line at P50** and has been
since the BM25 selection fix (ADR-022) took it from 136 ms to this. The budget
was written before anything was measured; 8.38 ms for a dense HNSW search and a
BM25 scan over 3.28M documents, in parallel, is not a number worth optimizing
against a 186 ms surplus. It is listed as over rather than quietly rebudgeted.

**Stage 7's P100 misses its budget, and the explanation for it was wrong twice.**
The first was page-cache warm-up; the measurement refuted it — P100 is
20.1 / 23.7 / 20.1 ms across three *warmed* reps, so it recurs rather than
decays. The second was page faults against an mmap'd store, which held until
ADR-033 measured the process and found the "mapped" parquet store fully resident
at 3.88 GB. There were no faults to blame, because nothing was being paged.

The store is genuinely mapped now (uncompressed Arrow IPC, 2.96 GB resident
against 7.42 GB). Re-benchmarked the same day, 500 queries × 3 reps
([`2026-08-19-bench-arrowstore.json`](results/2026-08-19-bench-arrowstore.json)):

| Rep | Boundary A P50 | stage7 P50 | stage7 P100 |
|---|---|---|---|
| 1 | 15.92 ms | 1.85 ms | 25.4 ms |
| 2 | 13.77 ms | 1.24 ms | 20.4 ms |
| 3 | 13.73 ms | 1.26 ms | 20.6 ms |

Reps 2 and 3 land on the pre-change numbers (13.70, 13.52) and recall@10 is
identical at 0.4464, so **paging the store costs nothing in the steady state**.
Rep 1 is 2.2 ms slower because it is the one paying to fault 2.85 GB of store in:
the harness's 50-query warmup touches a few hundred rows, which does not warm a
file this size. That is a real cold-start cost and it belongs to the first
queries after a deploy, not to the percentile.

So the P100 is not paging — it was 20.1 ms when nothing was mapped and it is
20.4 ms now that everything is. Not warm-up either. What is left is the row-group
layout and `parent_text` for the widest S2 windows, unmeasured — on the same box
that now serves the deployment (ADR-036), so there is no later hardware to blame
it on.

**Both overruns are affordable and neither is hidden.** Boundary A sits at
13.50 ms P50 against a 200 ms target — **186 ms of headroom** — so a stage that
is 15 ms over its own line at P100 changes nothing that matters yet. They are
listed because a budget table that only reports the rows that passed is not a
budget table.

## Optimization status

`planned` until implemented and measured. No row moves to `done` without a
benchmark number behind it.

### 1 — LLM out of the hot path

| Item | Status |
|---|---|
| stage 4 → phonetic + script normalization | planned |
| stage 5 → RM3 pseudo-relevance feedback | planned |
| LLM rewrite kept for escalation tier + ablation | planned |
| LLM multi-query kept for ablation | planned |

### 2 — Speculative parallel execution

| Item | Status |
|---|---|
| streaming STT, speculative retrieval on partial transcripts | planned |
| dual-fire raw + rewritten retrieval, cancel the loser on overlap | planned |
| L1 guardrails parallel with retrieval | planned |

### 3 — Tiered pipeline with free confidence signals

| Item | Status |
|---|---|
| score margin top1–top5 | planned |
| BM25/dense rank agreement (Kendall tau) | planned |
| early exit when top1 > `t_high` (skips 5 and 6) | planned |
| escalation to LLM rewrite + wider fan-out + `ef_search` 256 | planned |
| per-tier percentiles + fast-path hit rate reported | planned |

### 4 — No network hops between stages

| Item | Status |
|---|---|
| hnswlib in-process, index pinned in RAM | planned |
| no hosted vector DB anywhere in the path | planned |
| ONNX Runtime INT8 for embedder and reranker | planned |
| models warmed at boot, never lazy-loaded | planned |
| dense and lexical search on parallel threads | planned |
| reranker: one batched forward pass, no loop | planned |
| `bm25s` in-memory lexical index | planned |

### 5 — Index-level compression

| Item | Status |
|---|---|
| scalar quantization (int8) on HNSW + full-precision rescore top-50 | planned |
| ~~Matryoshka truncated dims — search 256, rescore full~~ | **dropped 2026-08-14 — not supported by the candidate models** |
| `ef_search` tuned per tier | planned |

**On Matryoshka.** Checked the model cards on 2026-08-14: neither `BAAI/bge-m3`
nor `intfloat/multilingual-e5-small` mentions Matryoshka or MRL anywhere. bge-m3's
"multi" is multi-*functionality* — dense, sparse, and ColBERT multi-vector
retrieval — not multi-*resolution* embeddings. Truncating a non-MRL embedding
degrades recall unpredictably because the model never trained the prefix
dimensions to be self-sufficient.

So this optimization is off the table for the leading candidates, and saying
otherwise would be an unverified claim in a doc. The dimension-reduction benefit
it was going to deliver is covered instead by scalar quantization plus the
smaller model: `multilingual-e5-small` is 384 dims natively, which is *below* the
256-dim search step this optimization was going to truncate down to on a 1024-dim
model. The problem it solved does not arise.

If Phase 2's benchmark forces `bge-m3` (1024 dims) on recall grounds, revisit —
either find an MRL-trained multilingual model or accept the larger index.

### 6 — Connection overhead

| Item | Status |
|---|---|
| persistent HTTP/2 pools with keep-alive for every external API | planned |
| pools pre-established and warmed at boot, before first request | planned |
| hard timeout + defined fallback on every external call | planned |
| bounded, budgeted retries | planned |

### 7 — Cheap guardrails

| Item | Status |
|---|---|
| citation-span n-gram overlap (~5 ms) | planned |
| local NLI cross-encoder on ambiguous cases (~15 ms) | planned |
| relevance-score floor before generation | planned |

### 8 — Caching, disclosed

| Item | Status |
|---|---|
| semantic cache on embedded query, similarity threshold | planned |
| hit rate published | planned |
| cache-disabled numbers published alongside | planned |

### Generation-side

| Item | Status |
|---|---|
| prompt caching for the static system prompt | planned, provider-dependent (`OPEN`) |
| streaming generation, TTFT reported as user-perceived latency | planned |

## Benchmark methodology

**Query set.** ≥ 500 queries drawn from the validation split, stratified by
language and `query_type`. 500 is the floor for a stable P100: below that the
maximum is one unlucky GC pause.

`RESOLVED 2026-08-15` — the sampling frame is now a measured quantity, not an
assumption. Per language the validation split holds 97,941 rows, of which
**53,895 have a gold passage and 44,046 do not** (`DATASET.md`). Two rules
follow, and both are enforced in the sampler:

- **Latency** queries are drawn from all 97,941 rows in their natural
  proportions. Refusals are part of the latency distribution — an L3 refusal is a
  fast path, and excluding no-answer rows would flatter every percentile.
- **Recall and MRR** are computed only over the 53,895 rows that have a gold
  passage. A recall number over all rows is capped at 0.55 by construction and
  measures the dataset rather than the retriever.

The real `query_type` mix, identical in every language file, is the
stratification target: DESCRIPTION 54.0%, NUMERIC 25.3%, ENTITY 8.6%,
PERSON 6.3%, LOCATION 5.8%.

**Warm-up.** Before the measured batch:
1. index loaded, models loaded
2. 50 throwaway queries through the full pipeline, results discarded
3. HTTP/2 pools opened to every external endpoint and exercised once

No cold start lands in the reported percentiles. Stated in `README.md` too,
because a benchmark that doesn't disclose its warm-up is not a benchmark.

**Cold-start numbers are reported separately** — boot time, first-query latency —
so the warm-up is a disclosure rather than a hiding place.

**Timing method.** `time.perf_counter_ns()` at every stage boundary, captured in
the harness rather than in the stages, so instrumentation cost is uniform and
measured once. Boundary A is a single span, not a sum of stage timings — summing
stages hides async scheduling overhead, which is real latency.

**Percentiles.** Nearest-rank on the sorted sample. P100 is the maximum
observation, not an interpolated 99.9th. Reported per tier and blended, with n
per tier, since a P100 over 6 escalated queries means nothing without the 6.

**Repetitions.** 3 independent runs; the spread across runs is reported. A single
run's P100 is not a measurement, it is an anecdote.

**Hardware.** `OPEN` — recorded verbatim at run time (CPU model, cores, RAM,
whether the run is on the dev box or the deploy VM). Both are reported: the dev
box is a 16-core x86_64 with 15 GB RAM; the deploy VM is likely smaller, and the
number that matters to a judge is the one from the box behind the live link.

**Determinism.** Replay mode fixes the query order and every random seed, so the
ablation deltas are the stages and not the scheduler.

## Results

### `MEASURED 2026-08-18` — stages 4 + 3 on the full index

Evidence: [`2026-08-18-bench-stage4.json`](results/2026-08-18-bench-stage4.json)
(clean transcripts) and
[`2026-08-18-bench-stage4-garbled.json`](results/2026-08-18-bench-stage4-garbled.json)
(STT-style corruption). Earlier runs are kept because the deltas are the
evidence: [pre-BM25-fix](results/2026-08-18-bench-stage3.json),
[post-BM25-fix](results/2026-08-18-bench-stage3-bm25fix.json).

500 queries (289 with a gold passage), 3 reps, 50 warm-up queries discarded,
`index/full` — 3,278,022 chunks over four corpora. Dev box: i5-13450HX, 16
cores, 15.25 GB, embedder pinned to 2 threads (ADR-010's deploy shape).

**Boundary A here covers stages 4 and 3 plus the harness.** Stages 5–7 and the
guardrail layers are not built, so this is a **floor on the finished pipeline,
not a comparison against the 200 ms target** (ADR-021).

#### Clean transcripts

| Arm | P50 | P95 | P100 | recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|
| full (stage 4 + dense + BM25) | 12.37 | 17.03 | 20.93 | 0.4464 | 0.2323 | 0.5111 |
| − stage 4 | **11.91** | 16.28 | 18.59 | 0.4567 | 0.2342 | 0.5165 |
| stage 4, `min_term_len` 3 | 12.09 | 16.17 | 20.53 | 0.4360 | 0.2286 | 0.4979 |
| dense only | **3.83** | 4.91 | 6.30 | 0.3668 | 0.2332 | 0.4853 |
| BM25 only | 12.05 | 16.44 | 18.86 | 0.3875 | 0.2101 | 0.4007 |
| `ef_search` 256 | 12.31 | 16.62 | 18.50 | **0.4913** | **0.2705** | **0.5860** |

#### Garbled transcripts (35% of words corrupted, marks dropped first)

| Arm | P50 | P100 | recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|
| full (stage 4 + dense + BM25) | 12.27 | 20.63 | **0.3599** | **0.1813** | **0.3783** |
| − stage 4 | 12.02 | 18.50 | 0.3564 | 0.1709 | 0.3447 |
| stage 4, `min_term_len` 3 | 12.49 | 19.21 | 0.3495 | 0.1777 | 0.3664 |
| dense only | 4.09 | 7.47 | 0.2630 | 0.1593 | 0.3341 |
| BM25 only | 12.29 | 19.50 | 0.2976 | 0.1547 | 0.3093 |
| `ef_search` 256 | 12.43 | 19.48 | **0.4187** | **0.2253** | **0.4541** |

P50 spread across the 3 reps: 0.03–0.35 ms. Quality bit-identical in all three
reps of every arm.

### Budget vs measured — boundary A

| Stage | budget | measured P50 | measured P100 | delta |
|---|---|---|---|---|
| 4 — query rewrite (phonetic) | 3 | **0.03** | 0.9 | −2.97 |
| 3 — embed query | 8 | **3.40** | 7.4 | −4.60 |
| 3 — dense HNSW ∥ BM25 | 6 | **8.40** | 15.9 | **+2.40** |
| 3 — RRF fusion | 1 | 0.13 | 0.6 | −0.87 |
| 3 — signals | — | 0.04 | 0.2 | — |
| harness — tracing, validation | 5 | **0.11** | 0.2 | −4.89 |
| **measured total** | **23** | **12.37** | **20.93** | **−10.6** |

Dense search over 3.28M vectors is 0.40 ms; the parallel span is BM25's 8.4 ms,
still the only line over budget.

### The Indic tokenizer bug — BM25 was indexing syllable debris

Found 18 Aug while building stage 4's vocabulary check. `bm25s`'s default token
pattern is `\b\w\w+\b`, and Python's `\w` excludes combining marks, so every
matra and virama was a word boundary:

```
'कंप्यूटर क्या है'      -> ['टर']
'मुंबई में कितने लोग'    -> ['बई', 'तन']
'সৌরজগতের গ্রহ কয়টি'  -> ['রজগত', 'রহ', 'কয']
'கம்ப்யூட்டர் என்றால்'   -> ['கம', 'டர', 'என']
'what is a corporation' -> ['what', 'corporation']
```

Three of four corpora were lexically indexed as fragments. The pattern now
includes the Indic block (ADR-023) and is defined once for the build and the
query side, like `normalize()` — a tokenizer mismatch between index and query is
invisible.

| BM25-only | before | after |
|---|---|---|
| recall@10 | 0.2284 | **0.3875** (+70%) |
| MRR@10 | 0.1238 | **0.2101** (+70%) |
| nDCG@10 | 0.2469 | **0.4007** (+62%) |
| vocabulary terms | 172,015 | **779,413** |
| index size | 378 MB | 535 MB |

Rebuild cost 36 s from the chunk store; no re-embedding.

### Stage 4 does not yet pay for itself

The ablation is the point of having one, so: **phonetic rewriting costs recall on
clean transcripts and buys a little on corrupted ones.**

| | stage 4 on | off | delta |
|---|---|---|---|
| clean, recall@10 | 0.4464 | **0.4567** | −0.0103 |
| clean, MRR@10 | 0.2323 | **0.2342** | −0.0019 |
| garbled, recall@10 | **0.3599** | 0.3564 | +0.0035 |
| garbled, MRR@10 | **0.1813** | 0.1709 | +0.0104 |
| garbled, nDCG@10 | **0.3783** | 0.3447 | +0.0336 |

On a clean query every correction is damage: an out-of-vocabulary term there is
usually a rare proper noun that was already right. Four configurations were swept
before this default was set (ADR-024) — `min_term_len` 5 halves the corrections
and is the only setting where the garbled delta is positive.

The stage costs 0.03 ms and stays in the pipeline as an ablation arm either way.
**Whether it belongs in the default path is a Day 5 question**, when real STT
output replaces a synthetic garbler: the corruption model here is a controlled
defect, not Sarvam's error distribution.

### Cross-lingual transfer

Recall@10 counting a hit in *any* language against a hit in the query's own:
0.4567 vs 0.4464 fused, 0.3979 vs 0.3668 dense-only, and 0.3875 vs 0.3875 for
BM25 — lexical retrieval still transfers across scripts not at all, even with the
tokenizer fixed, which is what a script-sensitive index predicts. The dense
retriever finds a cross-language copy about 3% of the time.

### Cold start

| Metric | value |
|---|---|
| index load (FAISS + BM25 + chunk ids + phonetic vocabulary, 3.0 GB) | **3.07 s** |
| first query, unwarmed | **18.56 ms** |

Load time doubled when the rewriter joined it — the phonetic vocabulary and the
BM25 vocabulary are 57 MB of JSON between them. Boot cost, paid once.

### Not yet measured

Tier percentiles (tiering is stage 5–6 work), cache hit rate — **there is no
cache in `dhvani/` at all**, so "cache disabled" throughout this file describes
the only mode that exists rather than a switch that was flipped — and the stage
5/6 ablation rows, those stages being unbuilt (ADR-027). The tier and cache
tables below keep their shape and stay empty until those stages exist.

The **stage 4 and stage 7 ablation rows are measured** as of 21 Aug: all 15 arms
in one run, at the bottom of this file.


### Boundary A, cache disabled

| Tier | n | hit rate | P50 | P70 | P100 |
|---|---|---|---|---|---|
| fast | | | | | |
| standard | | | | | |
| escalated | | | | | |
| blended | | 100% | | | |

### Boundary A, cache enabled

| Tier | n | cache hit rate | P50 | P70 | P100 |
|---|---|---|---|---|---|

### Boundaries B and C

| Boundary | P50 | P70 | P100 |
|---|---|---|---|
| B `ttft` | | | |
| C `wall_clock` | | | |

### Budget vs measured, per stage

| Stage | budget | measured P50 | measured P100 | delta |
|---|---|---|---|---|

### Ablation — quality and latency per stage

**`MEASURED 2026-08-21`** — all 15 arms in one run, 500 queries (289 with gold
labels) × 3 reps, 50 throwaway warmup queries per arm, 2 ONNX threads, 16-core
dev box, which is the deployment (ADR-036).
[`docs/results/2026-08-21-bench-ablation.json`](results/2026-08-21-bench-ablation.json).
Every cell is the median across the three reps. Latency is boundary A.

| Config | recall@10 | Δ | MRR@10 | nDCG@10 | P50 A | P95 A | P100 A |
|---|---|---|---|---|---|---|---|
| **full pipeline** | **0.4464** | — | 0.2323 | 0.5111 | 13.48 | 18.29 | 31.59 |
| `ef_search` 256 | **0.4913** | **+0.0449** | **0.2705** | **0.5860** | 13.65 | 18.34 | **24.83** |
| − stage 4 (raw transcript) | 0.4567 | +0.0103 | 0.2342 | 0.5165 | 13.39 | 17.96 | 30.77 |
| stage 4, `min_phonetic` 0 | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 13.48 | 18.37 | 31.14 |
| stage 4, edit distance 1 | 0.4464 | +0.0000 | 0.2306 | 0.5082 | 13.37 | 18.22 | 30.84 |
| stage 4, edit 1 + min len 5 | 0.4464 | +0.0000 | 0.2306 | 0.5082 | 13.48 | 18.45 | 31.70 |
| stage 4, min term len 3 | 0.4360 | −0.0104 | 0.2286 | 0.4979 | 13.68 | 18.45 | 31.88 |
| `k_dense`/`k_bm25` 200 | 0.4325 | −0.0139 | 0.2274 | 0.5036 | 13.67 | 18.42 | 32.02 |
| bm25 only | 0.3875 | −0.0589 | 0.2101 | 0.4007 | 13.49 | 18.03 | 26.20 |
| dense only | 0.3668 | −0.0796 | 0.2332 | 0.4853 | **5.61** | **7.92** | 41.44 |
| − stage 7 (no selection) | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 12.26 | 16.44 | 18.99 |
| − stage 7 dedupe | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 13.26 | 17.96 | 28.68 |
| stage 7, budget 800 tok | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 13.58 | 18.54 | 23.84 |
| stage 7, budget 3000 tok | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 13.76 | 18.26 | 23.20 |
| stage 7, max 3 chunks | 0.4464 | +0.0000 | 0.2323 | 0.5111 | 12.70 | 17.02 | 21.39 |

Stages 5 and 6 have no rows because they are not built (ADR-027). The
LLM-rewrite and LLM-multi-query arms are specified in `RAG_PIPELINE.md` and are
not implemented, so they are absent rather than empty.

**Read the stage 7 rows as latency, not quality.** recall@10 measures retrieval,
and stage 7 runs after retrieval to select which of the retrieved chunks reach
the prompt — so it *cannot* move recall@10 by construction, and the six
identical `0.4464` rows are the harness being correct rather than the stage
doing nothing. What those rows do show is the latency it costs: removing stage 7
entirely takes P100 from 31.59 ms to 18.99 ms, the single largest tail
contribution in the pipeline. Its quality effect needs a groundedness metric,
which is `calibrate_grounding.py`'s job, not this table's.

**Three things this run says out loud.**

1. **`ef_search` 64 is too low, and the fix is free.** 256 buys +0.0449 recall@10
   (+10% relative), +0.0382 MRR and +0.0749 nDCG for +0.17 ms of P50 — and it
   *lowers* P100, 31.59 → 24.83 ms. This is the strongest single result in the
   table and it argues for changing the default, which would re-baseline every
   number of record in this file. Not done in the same commit that measured it.

   **Where the P50 cost is, and why it is so small.** `ef_search` scales the
   dense half only, and the dense half is not where the time goes: `dense_only`
   spends **0.491 ms** in `stage3_retrieve` against the hybrid's 8.367 ms, so
   **BM25 is ~94% of the stage** and the widened graph search is scaling ~3.6% of
   boundary A. `stage3_retrieve` P50 is 8.367 → 8.354 ms, which is to say
   unresolvable at this precision.

   **Where the P100 saving is, corrected.** This entry first attributed it to "a
   more stable candidate set for fusion to work on". Fusion is not where it
   lands, and the widened search does cost what physics says it should — median
   of 3 reps:

   | | `full` | `ef_search` 256 | Δ |
   |---|---|---|---|
   | `stage3_fuse` P100 | 0.421 ms | 0.362 ms | −0.06 |
   | `stage3_retrieve` P100 | 15.497 ms | **16.799 ms** | **+1.30** |
   | `stage7_context` P100 | 20.413 ms | **9.444 ms** | **−10.97** |
   | boundary A P100 | 31.595 ms | 24.828 ms | −6.77 |

   Fusion is sub-millisecond in both arms and cannot account for a 6.77 ms
   difference. The retrieve tail goes **up**, which is the honest cost of a wider
   graph search. The saving is downstream, in **stage 7**: better candidates make
   context selection's drop loops terminate sooner, and `stage7.dropped.budget`
   falls 96 → 51 identically in all three reps. So the change trades +1.30 ms of
   retrieve tail for −10.97 ms of context-selection tail. The effect is real and
   replicated; the mechanism first published here was wrong.
2. **Phonetic query repair is net negative.** Turning stage 4 off is worth
   +0.0103 recall@10. It rewrote 75 of 500 queries (82 corrections) and no
   setting of it beats leaving it alone: looser thresholds are identical to the
   default, tighter ones are worse. This confirms and extends the 18 Aug finding
   already recorded above — the stage costs 0.03 ms, so it is not a latency
   problem, it is a correctness one.
3. **Hybrid fusion earns its place.** Dense alone is −0.0796, BM25 alone is
   −0.0589, and neither half reaches the fused score. Worth noting the trade the
   table makes visible: `dense_only` is by far the fastest arm (P50 5.61 ms
   against 13.48) and has the *worst* P100 (41.44 ms), so BM25 is buying tail
   stability as well as recall.

**One honest artifact.** The `full` arm's P50 spread across its three reps is
4.86 ms; every other arm is under 1.6 ms. `full` runs first, and 50 warmup
queries did not fully settle the first arm. The median across reps is reported
for exactly this reason, and re-running with `--arms full` alone would confirm
it — the number to trust for `full` is the one already measured on 19 Aug.

### Cold start

| Metric | value |
|---|---|
| boot to ready | |
| first query, unwarmed | |

**If the target is missed.** Then this document says so, with the measured
per-stage evidence, and proposes the closest honest alternative — most likely a
narrower boundary A, a cheaper stage 6, or a published fast-path-only number
with its hit rate. A rigorous account of what is physically achievable beats a
number nobody believes.
