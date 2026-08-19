# PRD — dhvani

## Problem

MS MARCO-style question answering works well in English and badly in Indic
languages, and voice is how most Indian users would actually ask. Two things
break at once: STT mangles proper nouns and morphology in Hindi/Tamil/Bengali,
and English-first retrievers score translated passages poorly. A RAG system that
ignores either failure returns fluent, confident, ungrounded answers.

The second problem is latency. Voice sets a hard perceptual bar — a pause over
~300 ms reads as the system being broken — and the naive seven-stage RAG
pipeline is 4+ sequential network calls, each 100 ms+ from India.

## Target user

The judge evaluating this submission, and behind them the archetype: a
Hindi/Tamil/Bengali speaker asking a factual question aloud and expecting either
a cited answer or an honest "I don't have that."

## Scope

In scope:

- Voice question in the dataset's Indic languages + English, via browser mic.
- Transcription through Sarvam AI, behind a swappable `STTProvider` interface.
- Hybrid retrieval (dense HNSW + BM25) over a chunked subset of
  `ai4bharat/MSMARCO-XI`, index pinned in process RAM.
- Seven-stage pipeline with build-time/query-time split, every stage toggleable,
  timed, and ablatable.
- Tiered execution with early exit and confidence-triggered escalation.
- Four-layer guardrail chain with distinct refusal states.
- Web UI showing live transcription, stage progression, per-stage timings,
  running P50/P70/P100, and clickable citations.
- Benchmark harness producing every number in every doc.

Explicit non-goals:

- **Not indexing the full dataset.** `MEASURED 2026-08-15`: 27 parquet files,
  55.6 GB, **11,451,314 rows** — 13 train files at 3.3–4.0 GB and 14 validation
  files at 419–494 MB. Full-corpus embedding is days of compute and tens of GB of
  RAM. We index a documented subset — **15,000 queries in Hindi, Bengali and
  Tamil plus their English originals, ~599,000 passages, 15.3% of one split of a
  three-language slice** (ADR-012) — and state its size everywhere. Scale is not
  what this task tests; retrieval engineering is.

  The subset is smaller than it sounds, and honestly so: the 14 language files
  are the *same* MS MARCO rows translated, not different content, so indexing
  more languages would buy script coverage rather than corpus size.
- **No multi-turn conversation.** Single question, single answer. Conversational
  state adds a query-rewrite dependency that fights the latency budget for zero
  brief credit.
- **No TTS.** The brief requires speech *in*, not out. Voice output adds a second
  network round trip after generation and is worth nothing to the requirements.
- **No user accounts, persistence, or history.** Stateless per request.
- **No hosted vector DB.** Deliberate — see `docs/DECISIONS.md#adr-004`.
- **No fine-tuning.** Off-the-shelf multilingual retrievers, benchmarked not assumed.
- **Not production-hardened.** Single region, single process, no autoscaling. A
  judge-facing demo with honest instrumentation.

## Requirement traceability

Every line of the brief, with where it is satisfied.

**1. Speech-to-text — Sarvam AI or ElevenLabs**
- [ ] Sarvam AI chosen and justified — `DECISIONS.md#adr-003`
- [ ] `STTProvider` interface with ElevenLabs as a second implementation
- [ ] Both implementations exercised by a test that swaps providers via config

**2. Dataset — `ai4bharat/MSMARCO-XI`**
- [ ] Real splits/languages/fields inspected before design — `docs/DATASET.md` (Phase 2)
- [ ] Chunking and embedding choices revised against actual data
- [ ] Subset selection documented with its size and language coverage

**3. Vast chunking**
- [ ] ≥3 strategies implemented, all indexed into one store — `docs/CHUNKING.md`
- [ ] Overlap policy tuned offline with a documented sweep
- [ ] Metadata-aware chunking (language, query_type, is_selected, passage rank)
- [ ] Per-strategy win/loss evidence by query type

**4. Latency < 200 ms**
- [ ] Boundary defined at top of `README.md` and in `docs/LATENCY.md`
- [ ] Per-stage budget table summing under target
- [ ] Measured against the budget, per stage
- [ ] Full wall-clock reported separately, not hidden

**5. P50 / P70 / P100 analytics**
- [ ] ≥500 queries, warmed, cold start excluded
- [ ] Reported per tier, not only blended
- [ ] Cache-enabled and cache-disabled numbers both published, with hit rate
- [ ] Percentiles surfaced live in the UI

**6. Harness**
- [ ] Typed structured I/O at every stage boundary
- [ ] Bounded retry with backoff + circuit breaker on external calls
- [ ] Graceful degradation: STT→text input, reranker→fusion order, generation→passages
- [ ] Per-stage structured traces from the first commit
- [ ] Deterministic replay mode for reproducible benchmarks

**7. Guardrails**
- [ ] Input layer: unsafe, injection, language detect, empty/garbled transcript
- [ ] Scope layer: off-topic vs corpus coverage
- [ ] Retrieval layer: relevance-score floor, refuse before generating
- [ ] Output layer: groundedness + citation enforcement
- [ ] Distinct refusal copy and distinct UI state per failure mode
- [ ] Adversarial eval set built, catch rate measured and published

**Cross-cutting**
- [ ] Ablation table: retrieval quality + latency, each stage on/off
- [ ] UI matches hhgoa.com tokens — `docs/DESIGN_SYSTEM.md`
- [ ] `task-2/` builds from a fresh clone with no file outside it

## Success criteria

1. Boundary-A P100 under 200 ms on the fast path, with the fast-path hit rate
   published. If unreachable, `docs/LATENCY.md` says so with measured evidence
   and names the closest honest alternative.
2. Ablation table shows every stage either earns its milliseconds in retrieval
   quality or is cut. A stage that costs latency and moves no metric gets deleted,
   and the deletion is documented.
3. Adversarial catch rate published per category. Refusals are demonstrably
   correct, not blanket caution — false-refusal rate on answerable queries reported too.
4. Every claim in a generated answer maps to a chunk id the user can click.
5. A judge opening the live link can, without reading the repo, speak a question,
   watch the stages, see the timings, and see a citation.

## Submission checklist

- [ ] Public GitHub repo
- [ ] Live link reachable by judges (not localhost, not password-gated)
- [ ] Video 1 — 90 s, team and process, **not** the product
- [ ] Video 2 — end-to-end product demo
- [ ] Form submitted: https://forms.gle/MNvCjcv23Hn2Eeu58
- [ ] **Per member**, both videos posted to Instagram, X, and LinkedIn
- [ ] ≥1 Instagram post public
- [ ] Every post tagged `#RAGInGoa`
- [ ] Post URLs collected into `docs/SUBMISSION.md` before form submission

`OPEN — team roster.` The social requirement is per individual member. Solo or a
team? If a team, names and handles are needed by Day 7 so the posting checklist
can enumerate them. Blocks nothing until then.

**No resubmissions.** Everything above is verified by 21 Aug so Day 8 is buffer.
