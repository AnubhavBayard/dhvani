# dhvani — voice RAG over Indic MS MARCO

HH Goa 2026 · shortlisting task 2 · deadline 22 Aug 2026, 11:59 pm IST

Speak a question in Hindi, Tamil, Bengali (or English). It transcribes, retrieves
grounded passages from `ai4bharat/MSMARCO-XI`, and answers with citations — or
refuses, on purpose, when the corpus can't answer.

**Live link:** `TBD`

---

## Latency measurement boundary — read this first

The brief says "under 200ms" for the full process. That number is only meaningful
with a stated boundary, so here are all three, measured separately. Nothing is hidden.

| # | Boundary | What's inside | Target |
|---|---|---|---|
| **A** | **`query_path`** — the headline number | final transcript in → context selected out. Query rewrite, hybrid retrieval, rerank pass 1, RM3 expansion, rerank pass 2, context selection, input+scope+retrieval guardrails. | **< 200 ms** |
| B | `ttft` | final transcript in → first generated token out | reported, not targeted |
| C | `wall_clock` | mic release → last answer token rendered | reported, not targeted |

**Why A is the headline.** The brief's own phrasing — "chunking + vector DB
retrieval + everything through to final output" — enumerates the retrieval
pipeline. Boundary A covers every stage the brief lists, end to end, and it is
the only span whose latency is ours to engineer. B and C both contain a network
round trip to an LLM provider plus token generation: from India that is
230–280 ms of network before any compute, so *any* system with a hosted LLM in
it fails a 200 ms wall-clock target, ours included. Claiming otherwise would be
a fabricated benchmark. We report B and C anyway, in full, so the reader can
apply whatever boundary they prefer.

Build-time work (chunking, overlap, embedding, index construction) is amortized
and contributes 0 ms to A. See `docs/LATENCY.md` for the per-stage budget and
`docs/DECISIONS.md#adr-002` for the reasoning.

## Headline results

Per project rule, no number appears in any doc unless it came from a run we
executed. Everything still marked `PLACEHOLDER` is waiting on the run of record
on deploy hardware.

Boundary A, per tier, cache disabled. **Tier rows stay `PLACEHOLDER` because
tiering is not built** — `t_high`/`t_low`/`t_agree` are swept from the eval set,
not guessed, so no query is labelled with a tier yet. The one measured line is
the blended span over the stages that exist:

**`MEASURED 2026-08-19`** — boundary A over stages 4 + 3 + 7 + harness, 500
queries x 3 reps, warmed, cache disabled, 16-core dev box:
**P50 13.50 ms · P70 15.08 ms · P95 18.38 ms · P100 33.44 ms**
([`docs/results/2026-08-19-bench-stage7.json`](docs/results/2026-08-19-bench-stage7.json)).
This is a **floor**, not a target comparison: stages 5 and 6 are not in it, and
it has not run on deploy hardware. See `docs/LATENCY.md` for the per-stage table,
including the two budgets it currently overruns.

| Tier | Share of queries | P50 | P70 | P100 |
|---|---|---|---|---|
| fast path (early exit) | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` |
| standard | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` |
| escalated (LLM rewrite) | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` |
| **blended** | 100% | `PLACEHOLDER` | `PLACEHOLDER` | `PLACEHOLDER` |

Boundaries B and C, and the cache-enabled variant with its hit rate, are in
`docs/LATENCY.md`. Guardrail catch rates are in `docs/GUARDRAILS.md`.

## What is indexed — stated up front

`ai4bharat/MSMARCO-XI` is 11,451,314 rows across 14 languages, 55.6 GB of
parquet. We index a **documented subset**, not the corpus: **15,000 MS MARCO
queries in Hindi, Bengali and Tamil, plus their English originals** — about
599,000 passages. That is **15.3% of one split of a three-language slice**.

The 14 language files are the same MS MARCO rows translated, not different
content (`MEASURED 2026-08-15`, `docs/DATASET.md`), so indexing more languages
would buy script coverage rather than corpus size. Every recall number in this
repo is a subset number and is labelled as one. ADR-012 has the reasoning and
the arithmetic that set the row count.

**Benchmark hygiene:** models are loaded and warmed with throwaway queries before
the measured batch starts, so no cold start lands in the percentiles. Connection
pools to every external API are pre-established at boot for the same reason.
Query count, hardware, and percentile method: `docs/LATENCY.md`.

## Quickstart

`PLACEHOLDER` — filled in Phase 3. Shape it will take:

```bash
cd task-2
uv python install 3.11        # ADR-013 — self-contained CPython, ships pip and headers
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env          # SARVAM_API_KEY, generation provider key
python -m dhvani.build_index  # downloads corpus subset, chunks, embeds, builds HNSW + BM25
python -m dhvani.bench        # writes docs/results/*.json — the source of every number above
uvicorn dhvani.app:app        # serves API + UI on :8000
```

Already runnable today, against the built index:

```bash
uvicorn dhvani.app:app                       # UI + POST /ask on :8000
python -m dhvani.bench.benchmark --reps 3    # writes docs/results/*.json
python -m dhvani.build.probe_dataset --langs hin ben tam   # dataset recon
python -m pytest tests/                      # 124 checks
```

**What answers today:** speak or type → Sarvam STT → stage 4 (query rewrite) →
stage 3 (hybrid retrieve) → stage 7 (context selection) → generation, answering
live in Bengali, Hindi, Tamil and English with citations. Speech is batch, not
streaming (ADR-029); boundary A is unaffected either way, since its clock starts
at the final transcript in both designs. Stages 5 and 6 are specified, deferred and absent from
the span rather than stubbed (ADR-027); `/health` and every `/ask` response
report exactly which stages boundary A covers.

Generation reads `SARVAM_API_KEY` (falling back to `GROQ_API_KEY`) from the
environment — nothing loads `.env` for you, so `set -a; . ./.env; set +a` first.
With no key the pipeline runs to the edge of the call and returns a
`generation_unavailable` refusal, which is designed behaviour, not a crash.

**Roughly half of in-corpus questions currently refuse.** That is recall@10
0.4464 showing up as user-visible behaviour, not a bug in the refusal path, and
it is the strongest argument for the deferred stage 6 rerank.

`task-2/` is self-contained: its own `requirements.txt`, its own venv, its own
config. It shares no imports with the sibling `task-1-frame-id-generator/`.

## Docs

| File | What's in it |
|---|---|
| `docs/PRD.md` | scope, non-goals, requirement traceability, submission checklist |
| `docs/DESIGN.md` | architecture, region choice, interface contracts |
| `docs/RAG_PIPELINE.md` | build-time and query-time stages, per-stage contracts |
| `docs/CHUNKING.md` | strategies, overlap policy, metadata schema |
| `docs/GUARDRAILS.md` | layered chain, thresholds, adversarial eval set |
| `docs/LATENCY.md` | budget table, optimization status, benchmark method |
| `docs/DESIGN_SYSTEM.md` | tokens extracted from hhgoa.com, component rules |
| `docs/DATASET.md` | what the corpus measurably is, and the indexed subset |
| `docs/PROGRESS.md` | day-by-day plan, blockers, change log |
| `docs/DECISIONS.md` | ADR log |
