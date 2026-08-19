# RAG pipeline

Two pipelines, not one. The brief's seven stages contain two that belong to
build time; running them per query is repeated work that buys nothing. The
corrected mapping and its rationale are in `DECISIONS.md#adr-002`.

Every number labelled `TARGET` below is a budget, not a measurement. Measured
values replace them, labelled `MEASURED` with a date, once `bench` runs.

---

# Part 1 — Build-time pipeline (offline)

Runs once per corpus version. Contributes **0 ms** to query latency. Output is a
directory of artifacts loaded into RAM at boot.

```
MSMARCO-XI parquet ─▶ subset select ─▶ [stage 1: chunking ×N strategies]
                                              │
                                       [stage 2: overlap]
                                              │
                            ┌─────────────────┼─────────────────┐
                       embed (ONNX)      bm25s index      vocab + phonetic keys
                            │                 │                 │
                       HNSW build             │                 │
                            └─────────────────┴─────────────────┘
                                              ▼
                                        index bundle
```

## Stage 1 — Chunking

**Purpose.** Turn dataset passages into retrievable units under several
strategies at once, so retrieval can pick the granularity a given query needs.

**I/O.** `Passage(doc_id, text, lang, is_selected, rank, query_type)` →
`list[Chunk]`. Chunk schema in `CHUNKING.md`.

**Algorithm.** Each strategy runs over the full subset independently; all outputs
land in one index, discriminated by the `strategy` metadata field. Strategies and
their justification: `CHUNKING.md`.

**Config.** `strategies: [passage, sentence_window, semantic, doc_summary]`,
per-strategy params in `CHUNKING.md`.

**Latency.** 0 ms query-time. Build cost `OPEN` — falls out of subset size.

**Failure.** Malformed/empty passage → skipped, counted, logged. A strategy that
throws fails the build; a partial index is worse than no index.

**Ablation.** Rebuild with a strategy excluded, re-run retrieval quality. Since
strategy is a metadata field, ablation is also possible at query time by
filtering — cheaper, and what the ablation harness actually does.

## Stage 2 — Chunk overlap

**Purpose.** Stop answers from being cut in half at a chunk boundary.

**Why it is build-time.** Overlap is a parameter of how chunks are cut. It is
decided when the index is written and cannot vary per query without re-chunking
the corpus mid-request. Listing it as a query-time stage would put an indexing
parameter in the latency budget, which is how a 200 ms budget gets fictional.

**I/O.** `(list[Chunk], overlap_ratio)` → `list[Chunk]` with overlapping spans
and `overlap_with: list[chunk_id]` recorded so stage 7 can dedupe.

**Algorithm + tuning sweep.** `CHUNKING.md`.

**Config.** `overlap_ratio` default `TARGET 0.15`, resolved by the sweep.

**Latency.** 0 ms query-time. Costs index size, which costs HNSW search time —
the real price of overlap, and why it is swept rather than assumed.

**Failure.** Overlap ≥ 1.0 rejected at config validation.

**Ablation.** Build index variants at ratio ∈ {0, 0.1, 0.15, 0.25}, compare
recall@10 against index size and P50 search time.

---

# Part 2 — Query-time pipeline

Order: **4 → 3 → 5 → 6 → 7**. Numbers are the brief's; the order is the correct one.

```
t=0  final transcript
     ├── [guardrail: input]      ─┐ parallel, does not gate retrieval
     ├── [semantic cache probe]  ─┤
     └── [stage 4: rewrite]      ─┘
             │
        [stage 3: hybrid retrieve + RRF]
             │  signals: margin, rank agreement
        ┌────┴──────────────┐
    early exit          continue
        │                  │
        │        [stage 5: RM3 expansion]
        │                  │
        │        [stage 6: rerank pass 2]
        └────────┬─────────┘
        [guardrail: scope + retrieval floor]
                 │
        [stage 7: context selection]
                 │  ◀── boundary A ends
              generate
```

## Stage 4 — Query rewriting *(runs first)*

**Purpose.** Repair what STT mangled — garbled proper nouns, dropped morphemes,
script inconsistency — before anything touches the index.

**Why first.** Rewriting after retrieval means the first retrieval already ran on
corrupt input, and its results seeded everything downstream. Fixing the query
after that point is repair work applied to a decision already made.

**I/O.** `Transcript` → `Query`.

**Algorithm (default path, no LLM).**
1. Script/language normalization: Unicode NFC, digit normalization
   (Devanagari/Tamil/Bengali numerals → ASCII), zero-width joiner cleanup,
   nukta normalization. Indic text arrives in several equivalent encodings and
   BM25 treats them as different tokens.
2. Phonetic correction against corpus vocabulary. Every corpus term is keyed at
   build time by a phonetic code; query terms below a frequency floor and outside
   the vocabulary get matched to the nearest vocabulary term within an edit
   distance bound, scored on phonetic key + edit distance.
3. Terms that match vocabulary are left alone. Dense retrievers already handle
   ordinary paraphrase; an aggressive rewriter here destroys more than it fixes.

**Config.** `max_edit_distance: 2`, `min_term_freq: 3`, `enabled: true`,
`llm_rewrite: false` (escalation tier only), `phonetic_scheme` — narrowed
2026-08-14 to two candidates, both purpose-built for Indic scripts (Soundex and
Metaphone are Latin-only and were never viable):

| | `libindic/soundex` | `indic-soundex` (PyPI 0.1.0) |
|---|---|---|
| languages | hi, bn, pa, gu, or, ta, te, kn, ml + English | Indic names generally; explicit aspirated-consonant and compound-character (`ksh`, `gy`) handling, Tamil `zh`↔`l` |
| cross-language matching | **yes** — returns 2 when two strings are phonetically equal across *different* languages | not documented |
| maturity | long-lived, part of the Silpa/libindic project | 0.1.0, single file, zero dependencies |
| deps | project library | pure stdlib |

`libindic/soundex` is the favourite for one specific reason: its cross-language
equivalence result is directly useful on a corpus where the same entity appears
in several scripts. `indic-soundex` is the fallback if the dependency proves
awkward — a zero-dependency single file is cheap to vendor.

### `MEASURED 2026-08-15` — what `libindic/soundex` actually returns

Ran it on the same word in every candidate script. Three things came out that
change how step 2 above has to be built.

**1. The code is script-tagged, because the first character is passed through
verbatim.** `भारत → भAPK0000`, `ভারত → ভAPK0000`, `ಭಾರತ → ಭAPK0000`. The
phonetic body is identical; the leading character is not. So raw soundex output
**cannot be used as a shared vocabulary key across scripts** — the natural
implementation of step 2 (one hash bucket per phonetic code) silently degrades to
per-script buckets.

**2. Dropping the first character gives a usable cross-script blocking key —
usually.** On `code[1:]`, Hindi/Telugu/Kannada/Gujarati/Punjabi/Odia agree
exactly; Tamil and Bengali diverge on some words and not others:

| word | hin | ben | tam | tel | kan |
|---|---|---|---|---|---|
| इंडिया / ইন্ডিয়া / இந்தியா | `NIBOA00` | `LIBOA00` | `LKBOA00` | `NIBOA00` | `NIBOA00` |
| मुंबई / মুম্বাই / மும்பை | `CNMB000` | `CNMAB00` | `CNMD000` | `CNMD000` | `CNMD000` |
| कंप्यूटर / কম্পিউটার / கம்ப்யூட்டர் | `NMOCIP0` | `NMBCIAP` | `NMOCIP0` | `NMOCIP0` | `NMOCIP0` |

**Decision:** `code[1:]` is the **blocking key**, not the match. Look up
candidates by the tail key, then score them with `Soundex.compare()`, which
handles the cross-language case properly (returns `2` for phonetic equality
across different languages, `-1` for no match). That keeps the index lookup O(1)
and the pairwise comparison bounded to a handful of candidates — which is what
the 3 ms budget assumes anyway.

**3. Urdu returns nothing usable.** `بھارت → ب0000000` — the first character,
then zeros. This is not weak coverage, it is no coverage. ADR-007 recommended
excluding Urdu on this suspicion; ADR-012 excludes it on this measurement.
Assamese, Nepali and Sanskrit do ride on the Bengali and Devanagari mappings and
encode normally (`ভাৰত → ভAK00000`).

**Packaging note.** `libindic-soundex==1.0.2` does not declare its dependency on
`libindic-utils`; importing it without that installed raises
`ModuleNotFoundError: No module named 'libindic.utils'`. Both are pinned in
`requirements.txt` with a comment, because this will otherwise be rediscovered on
the deploy box on Day 7.

The experiment below still decides between the two; it no longer has to invent a
scheme.

**Latency.** `TARGET 3 ms`. Dictionary lookups plus bounded edit distance over a
handful of out-of-vocabulary terms.

**Failure.** Vocabulary unloaded or lookup throws → pass the raw transcript
through, mark `method="passthrough"`. Never blocks.

**Ablation.** `off` (raw transcript), `phonetic` (default), `llm`. Report
retrieval quality per mode, and specifically on the STT-corrupted subset of the
eval set — the only place this stage can show value.

**Experiment resolving `phonetic_scheme`:** take 100 eval queries, corrupt them
with real STT output (speak them, transcribe), and measure recall@10 recovered
by each scheme against the uncorrupted baseline.

## Stage 3 — Reranking pass 1 (cheap, high-recall fusion)

**Purpose.** Get a wide, high-recall candidate set cheaply, and produce the
pseudo-relevant seed that stage 5 consumes.

**I/O.** `Query` → `RetrievalResult` (top-k after fusion) + `ConfidenceSignals`.

**Algorithm.**
1. Embed the rewritten query once, ONNX INT8.
2. In parallel threads: HNSW dense search and `bm25s` lexical search. Two
   independent CPU-bound operations; running them sequentially wastes the
   smaller one's entire duration. (Dimension truncation was planned here via
   Matryoshka — dropped, neither candidate model supports it. See `LATENCY.md`.)
3. Fuse with Reciprocal Rank Fusion. RRF over score normalization because dense
   cosine and BM25 scores are not on comparable scales and any normalization
   scheme needs per-corpus tuning that RRF doesn't.
4. Rescore top-50 with full-precision vectors if the index is quantized.
5. Compute confidence signals: `margin_1_5` and Kendall tau between the BM25 and
   dense orderings. Both are arithmetic over numbers already in hand.

**The stage-5 dependency.** Top-3 fused documents are RM3's pseudo-relevant seed.
If that link were cut, this pass would be pure overhead and the stage would have
to go. It is the load-bearing connection between 3 and 5.

**Config.** `k_dense: 100`, `k_bm25: 100`, `k_out: 50`, `rrf_k: 60`,
`ef_search: 64` fast tier / `256` escalated, `rescore_top: 0`.

Step 4 is **disabled**, not dropped: the build persists no fp32 vectors and
keeping them costs 6.1 GB against an 8 GB box (**ADR-017**). `stage3_rescore`
still emits a trace row with `enabled=False`, so the ablation table has the row.

**Latency.** `TARGET 18 ms` = embed 8 + max(dense 6, bm25 4) + rescore 3 + fuse 1.

The embed term is now measured. `MEASURED 2026-08-15`, dev box, 2 ONNX threads
to match the deploy box's vCPU count, `multilingual-e5-small` INT8, 100
single-query encodes over real Hindi validation queries
(`docs/results/2026-08-15-embed-bench.json`):

| | p50 | p95 | p100 |
|---|---|---|---|
| query embed | **2.86 ms** | 3.96 ms | 4.99 ms |

That is 5 ms under budget, on a dev box rather than the deploy box — same thread
count, faster cores, so it is an optimistic bound and not the number of record.
The budget line stays at 8 ms until Day 7 re-measures it on Lightsail. The
headroom is noted because stage 6 (`TARGET 60 ms`) is the stage that will need
it.

**Failure.** Dense path fails → BM25 only, flagged degraded. BM25 fails → dense
only. Both fail → refuse with the retrieval-failure message. Never a silent
empty result set.

**Ablation.** dense-only / bm25-only / fused. Fusion has to beat both single
retrievers or RRF is complexity for nothing.

## Stage 5 — Multi-query expansion (RM3 pseudo-relevance feedback)

**Purpose.** Recover documents the original phrasing missed, by expanding the
query with terms drawn from what already looks relevant.

**Why RM3 rather than LLM fan-out.** An LLM generating paraphrases is ~250 ms of
network for a problem classical IR solved with term statistics. RM3 does it in
in-process arithmetic. The LLM version stays implemented, benchmarked, and lives
on the escalation tier and in the ablation table.

**I/O.** `RetrievalResult` (from stage 3) → `RetrievalResult` (fused, wider).

**Algorithm.**
1. Take top-3 documents from stage 3 as the pseudo-relevant set.
2. Extract candidate expansion terms, score by RM3 relevance-model weight
   (term probability in the feedback set × query likelihood), filter by IDF so
   corpus-common terms are not "expansion".
3. Build 2–3 expanded query variants: original + top expansion terms, weighted.
4. Re-retrieve lexically per variant. Dense re-retrieval is optional and off by
   default — it means re-embedding, and expansion terms move BM25 far more than
   they move a dense vector.
5. Fuse all result sets with the stage-3 set via RRF.

**Config.** `fb_docs: 3`, `fb_terms: 10`, `n_variants: 3`, `orig_weight: 0.6`,
`min_idf: 2.0`, `dense_reretrieve: false`, `llm_expansion: false`.

**Latency.** `TARGET 15 ms`.

**Failure.** Empty feedback set or all terms filtered → pass stage 3's results
through unchanged. Degrades to a no-op, never an error.

**Ablation.** off / RM3 / LLM-fanout. Report recall@10 delta and latency delta
for each. If RM3 moves recall by less than a point, it is cut and the cut is
documented.

## Stage 6 — Reranking pass 2 (precise, expensive)

**Purpose.** Order the narrowed set accurately with a cross-encoder that reads
query and passage together.

**I/O.** `RetrievalResult` → `RetrievalResult` (top-n, cross-encoder scored).

**Algorithm.** Take top-32 fused candidates, score all of them in **one batched
ONNX forward pass**. One pass, not a loop — per-item inference wastes the batch
dimension and turns a 60 ms stage into a 400 ms stage. Truncate passages to a
fixed token length so the batch is rectangular and does not blow up on one long
passage.

**Config.** `rerank_top_in: 32`, `rerank_top_out: 8`, `max_len: 256`,
`model: OPEN` (multilingual cross-encoder, Phase 2), `batch: single`.

**Latency.** `TARGET 60 ms`. The most expensive stage in the budget and the one
to attack first if measurement overruns — via smaller `rerank_top_in`, shorter
`max_len`, or a smaller model.

**Failure.** Timeout or model error → fall back to RRF fusion order from stages
3/5, mark degraded in the trace and in the UI timing panel.

**Ablation.** off (fusion order) / on. Expect the largest quality gain and the
largest latency cost of any stage — this is the central trade the ablation table
exists to show.

## Stage 7 — Context selection

**Purpose.** Assemble the final context window. Selection, not retrieval —
retrieval already happened in 3 and 5.

**I/O.** `RetrievalResult` → `SelectedContext`.

**Algorithm.**
1. Dedupe overlapping chunks using the `overlap_with` metadata written at build
   time — the reason stage 2 records it. Multiple strategies over the same
   passage guarantee near-duplicates; without this the context window fills with
   the same sentence three times.
2. Order by final score.
3. Apply the token budget, counting with the generation model's tokenizer, not a
   word-count approximation.
4. Attach chunk ids so every citation resolves to a retrievable span.

**Config.** `token_budget: 1500`, `max_chunks: 6`, `dedupe_threshold: 0.85`
(Jaccard over shingles), `order: score_desc`.

**Latency.** `TARGET 5 ms`.

**Failure.** Zero chunks survive → refusal path, not an empty generation call.

**Ablation.** dedupe on/off, budget ∈ {800, 1500, 3000}. Measures whether more
context helps or just costs generation time.

---

# Tiering

| Tier | Trigger | Path | Budget (boundary A) |
|---|---|---|---|
| **fast** | `top1 > t_high` after stage 3 | 4 → 3 → 7 | `TARGET 33 ms` |
| **standard** | default | 4 → 3 → 5 → 6 → 7 | `TARGET 108 ms` |
| **escalated** | `margin_1_5 < t_low` or `rank_agreement < t_agree` | LLM rewrite + wide fan-out + `ef_search 256` + full rerank | **exceeds 200 ms by design** |

Thresholds `t_high`, `t_low`, `t_agree` are `OPEN` — set by sweeping them on the
eval set against the recall/latency curve, not guessed.

Escalation is reported separately and honestly. A tier that is slower on purpose,
with a published hit rate, is a stronger result than a blended number that hides
which queries were hard.

## Budget summary — boundary A

| Stage | fast | standard |
|---|---|---|
| 4 rewrite (phonetic) | 3 | 3 |
| 3 hybrid + fuse | 18 | 18 |
| 5 RM3 | — | 15 |
| 6 rerank 2 | — | 60 |
| 7 context selection | 5 | 5 |
| guardrails (scope + floor; input runs parallel) | 2 | 2 |
| harness overhead (validation, tracing) | 5 | 5 |
| **total** | **`TARGET 33 ms`** | **`TARGET 108 ms`** |
| headroom vs 200 ms | 167 | 92 |

Headroom is deliberate. The budget is built to survive measurement, since the
first real numbers always land above the estimate.

## Ablation harness

One run, one config. Config is a flat dict of stage toggles, so the ablation
matrix is generated rather than hand-written. Output for each configuration:
recall@10, MRR@10, nDCG@10, P50/P70/P100 of boundary A, and the tier
distribution. Deterministic replay guarantees two runs of the same config
produce the same numbers — without it, ablation deltas are indistinguishable
from noise.
