# dhvani — technical overview and dependency register

*Companion to [`PROJECT_EXPLAINED.md`](PROJECT_EXPLAINED.md), which covers the
same system for a non-technical reader. This one assumes you read code.*

Paths below are relative to the repo root. Last updated 21 August 2026.

Every measurement cited here has a JSON file behind it in
[`docs/results/`](results/) and an ADR in [`DECISIONS.md`](DECISIONS.md). Nothing below is estimated. Where a
number does not exist yet, that is stated instead of filled in.

---

## 1. System shape

A voice-in RAG service over `ai4bharat/MSMARCO-XI`, indexed across Hindi,
Bengali, Tamil and the English originals — **3,278,022 chunks over 598,732
passages, 2.49 GB of index**.

The single structural decision everything else follows from: **the entire query
path is in-process.** No hosted vector database, no sidecar service, no network
hop between the final transcript and the selected context (ADR-004). The index is
pinned in RAM in the same process that serves HTTP, which is why the server is a
long-lived uvicorn process and not a serverless function.

```
build time (once, ~3.1 h CPU)         query time (per request, 13.5 ms p50)
─────────────────────────────         ──────────────────────────────────────
parquet subset  ─┐                    transcript
  chunk          │                      ├─ L1 guardrail
  embed (ONNX)   ├─→ index/full/  ←──── ├─ stage 4  query repair
  FAISS HNSWSQ   │                      ├─ stage 3  embed → dense ∥ lexical → RRF
  bm25s          │                      ├─ stage 7  context selection
  phonetic vocab ┘                      └─ generation + L4 streaming grounding
```

`build/` and `retrieve/` are separated by directory, not by convention, because
the build-time/query-time boundary is exactly what ADR-002 is about and the
easiest thing to blur under deadline pressure.

### Latency boundaries

| | Span | Measured |
|---|---|---|
| **A** | final transcript → context selected | **P50 13.50 · P70 15.08 · P95 18.38 · P100 33.44 ms** |
| B | final transcript → first generated token | ~928 ms |
| C | mic release → last token rendered | ~5,536 ms |

`MEASURED 2026-08-19`, 500 queries × 3 reps, 50 warm-up queries discarded,
`docs/results/2026-08-19-bench-stage7.json`, on the same 16-core box that serves
the live link. Boundary A covers stages 4, 3, 7 and the harness; stages 5 and 6
are unbuilt and out of the span rather than stubbed inside it (ADR-027).

A is the headline because it is the only span that is ours to engineer. B and C
both contain a hosted-LLM round trip — 230–280 ms from India before any compute —
so no system with a hosted model in it meets a 200 ms wall-clock target. All
three are published so the reader can pick a boundary.

---

## 2. Dependency register

29 pinned packages. The design goal was **as few moving parts on the query path
as possible**, because every dependency on that path is something that can add
milliseconds or fail at request time.

Pinning policy, stated in `requirements.txt` itself:

> Every version here is one that a measurement in `docs/results/` was produced
> under; bumping one invalidates the numbers until they are re-run.

That is stricter than ordinary reproducibility. The benchmark numbers are the
submission's main evidence, so the environment that produced them is part of the
evidence.

### 2.1 Retrieval runtime — the query path

These five are the only third-party code that runs between a transcript and a
selected context. Everything here was chosen against a latency or memory
constraint, not a convenience one.

---

**`onnxruntime==1.28.0`** — inference runtime for the embedder.
Used by `dhvani/embed.py`.

*Why.* The query path needs to encode one short string in single-digit
milliseconds, in-process, on CPU. ONNX Runtime loads a pre-quantized INT8 graph
and runs it with no Python-side tensor framework underneath.

*Why not PyTorch + `sentence-transformers`.* That is the default way to do this
and it was rejected. It pulls ~2 GB of wheels, holds an fp32 model resident, and
adds a framework whose startup and dispatch overhead is a meaningful fraction of
a 13 ms budget. The measured cost of the ONNX path is **2.86 ms p50** for a
query encode against an 8 ms budget (`2026-08-15-embed-bench.json`). Neither
`torch` nor `sentence-transformers` is installed — verified by `pip list`.

*Its own dial.* `cpu_mem_arena` is exposed on `Embedder` because ONNX Runtime's
memory arena never returned padding memory during the index build. Turning it off
plus sorting batches by length took build peak from 11.34 GB to **7.08 GB** with
a byte-identical index (ADR-018). This is the single most consequential runtime
flag in the project.

---

**`faiss-cpu==1.15.0`** — approximate nearest-neighbour index.
Used by `dhvani/retrieve/stage3.py`, `dhvani/build/build_index.py`.
Specifically `IndexHNSWSQ(384, QT_8bit, M=16, METRIC_INNER_PRODUCT)`.

*Why.* HNSW graph search in-process with a scalar quantizer in front of the
vectors, and `efSearch` exposed directly as `index.hnsw.efSearch` — the highest-
leverage recall/latency knob available.

*Why not a hosted vector DB* (Pinecone, Qdrant Cloud, Weaviate). A hosted call is
30–80 ms, which is a third of the whole budget spent on HTTP before any search
happens. Even a self-hosted Qdrant on the same box pays serialization, loopback
and a process boundary for what is otherwise a function call (ADR-004).

*Why not `hnswlib`, which was chosen first and then removed.* ADR-004 picked
hnswlib for a smaller install and shipped it. ADR-015 measured it:

| | bytes/vector | 100k index |
|---|---|---|
| hnswlib | **1,684** | 168 MB |
| FAISS `IndexHNSWFlat` (fp32) | 1,680 | 168 MB |
| FAISS `IndexHNSWSQ` (`QT_8bit`) | **528** | 53 MB |

**hnswlib stores float32 and offers no quantization at all** — no int8 mode, no
scalar quantizer, no option. Every sizing figure in two earlier ADRs had assumed
528 bytes. At 1,684 the index would have been roughly 2× the target box. The
comment recording the removal is still in `requirements.txt`, which is the right
place for it.

*Cost accepted.* ~30 MB installed against hnswlib's ~1 MB, and SQ8 is lossy — a
full-precision rescore of the top-50 is specified for exactly that reason and
currently disabled (ADR-017).

---

**`bm25s==0.3.10`** — lexical (sparse) retrieval, the other half of hybrid.
Used by `dhvani/retrieve/stage3.py`, `dhvani/build/build_index.py`.

*Why both halves.* Measured, not assumed — from the 15-arm ablation
(`2026-08-21-bench-ablation.json`, 500 queries, 289 gold-labelled, × 3 reps):

| Arm | recall@10 | Δ | P50 A | P100 A |
|---|---|---|---|---|
| dense only | 0.3668 | −0.0796 | **5.61 ms** | 41.44 ms |
| bm25 only | 0.3875 | −0.0589 | 13.49 ms | 26.20 ms |
| **hybrid (RRF)** | **0.4464** | — | 13.49 ms | 31.59 ms |

Fusion beats either half. Note the trade the table makes visible: dense-only is
by far the fastest arm *and* has the worst P100, so BM25 buys tail stability as
well as recall.

*Where the time actually goes.* `dense_only`'s `stage3_retrieve` p50 is
**0.491 ms** against the hybrid's 8.367 ms — **BM25 is ~94% of the retrieve
stage.** That single number reframes every optimization decision downstream of
it, including the `ef_search` one in §6.

*Two things it needed fixing on.*

1. **Its default tokenizer is wrong for Indic text** (ADR-023). `bm25s.tokenize`
   defaults to `r"(?u)\b\w\w+\b"`; Python's `\w` excludes combining marks
   (category `Mn`), which Devanagari, Bengali and Tamil use for every vowel sign,
   virama and nukta. Result: `'कंप्यूटर क्या है'` tokenized to `['टर']`. Three of
   four corpora were lexically indexed as syllable debris, and every check the
   project had stayed green — the build reported 172,015 vocabulary terms without
   mentioning they were fragments. The fix is one pattern,
   `r"(?u)[\wऀ-෿]{2,}"`, defined once in `dhvani/build/chunk.py` and imported by
   both the build and the query path, because a tokenizer that differs between
   index time and query time produces an index nobody can query.
2. **Top-k selection was over the corpus, not over scored candidates**
   (ADR-022) — the fix took boundary A from 136.47 ms to 13.30 ms. Most of the
   project's headline speed is this one change.

*Load flag.* `bm25s.BM25.load(mmap=True)`, part of the ADR-033 residency work.

---

**`tokenizers==0.23.1`** — HuggingFace fast tokenizer for the encoder.
Used by `dhvani/embed.py`.

*Why.* Rust-backed, loads `tokenizer.json` directly, no `transformers` needed.
It is the minimum required to feed ONNX Runtime the right input ids. Installing
`transformers` for this would drag in the framework the ONNX decision exists to
avoid.

---

**`numpy==2.4.6`** — vectors, pooling, percentile math.
Used across `embed.py`, `build/`, `bench/`.

*Why.* It is the array type FAISS, ONNX Runtime and `bm25s` all speak. Not a
choice so much as the shared boundary between them.

---

### 2.2 Storage and data

**`pyarrow==25.0.1`** — parquet reads, and the chunk store.
Used by `build/subset.py`, `build/arrow_store.py`, `build/probe_dataset.py`,
`bench/queryset.py`, and on the query path by `retrieve/stage3.py`.

*Why it is on the query path at all.* The chunk store — the text of 3.28M chunks
— has to be readable per request without being resident. This is the subject of
ADR-033, which is worth reading in full because it corrects an error:

ADR-025 used `pq.read_table(..., memory_map=True)` and claimed the OS would fault
in only the chunks a query cites. Nothing measured it. When measured, the server
held **7.42 GB resident** against an 8 GB target box, of which the `ChunkStore`
was 3.88 GB. **`memory_map=True` maps the file; parquet is compressed, so every
column is decompressed into fresh Arrow buffers on read.** Zero-copy is
impossible by construction — the flag bought a cheaper read, not a lazy store.

The fix: write `chunks.arrow` as **uncompressed Arrow IPC**, where the on-disk
layout *is* the in-memory layout, and map that. Two related fixes from the same
measurement — hold `chunk_ids` as the Arrow column rather than `to_pylist()`
(3.28M Python strings for the ≤50 ids a query reads, 0.82 GB), and mmap `bm25s`.

| | Before | After |
|---|---|---|
| resident, warmed | 7.42 GB | **2.96 GB** |
| index load | 4.0 s | 3.2 s |
| boundary A P50 | 13.50 ms | 13.11 ms |
| row lookup | 0.06 ms | 0.038 ms |

`chunks.parquet` is still written — 337 MB against 2.85 GB — and is what offline
tools read. `ChunkStore.load` falls back to it, so an index predating this still
serves.

**One trap recorded there:** `feather.read_table(columns=[...])` is *not*
zero-copy either. A column projection rebuilds the selected columns into fresh
buffers — 2.86 GB of the saving handed straight back, measured while making the
change. The whole file is mapped and columns are named at lookup time.

---

**`huggingface-hub==1.27.0`** — corpus and model download.
Used by `build/probe_dataset.py` and `build/fetch_models.py`.

*Why.* Both the dataset and the ONNX encoder live on the Hub. `fetch_models.py`
exists because a fresh-clone verification on 21 Aug found nothing fetched the
models: 11 tests died on `NO_SUCHFILE` and nothing could embed (ADR-037). The
file list is derived from `MODELS` in `embed.py` rather than duplicated, so a
model spec and its download cannot drift apart.

**`fsspec==2026.7.0`** — not imported anywhere directly. It is what lets pyarrow
read parquet footers over HTTP range requests, so dataset reconnaissance can
inspect a 55.6 GB corpus without downloading it. Pinned because the behaviour is
load-bearing at build time.

**`safetensors==0.8.0`** — benchmark path only, `embed.py:115`. LaBSE's ONNX
export is the transformer only; its `Dense(768→768)+tanh` head ships as a
safetensors file and has to be applied separately. Not on the serving path — the
deployed encoder is e5-small, which has no such head.

---

### 2.3 Serving

**`fastapi==0.141.1`** + **`uvicorn==0.52.4`** + **`starlette==1.6.0`**
Used by `dhvani/app.py`.

*Why.* `POST /ask` streams over SSE and `POST /stt` takes a multipart upload;
both are one-liners in Starlette's model. FastAPI's Pydantic integration means
the HTTP boundary and the internal stage contracts validate through the same
type system rather than two.

*Why not serverless.* Not available as an option: the index is pinned in RAM
(ADR-004), so the process must be long-lived. This also means the frontend cannot
inherit the sibling task's Vercel deployment story.

**`pydantic==2.13.4`** — stage contracts.
Used by `dhvani/harness/contracts.py` and `app.py`.

*Why.* Every stage takes and returns a typed model, so a stage that silently
returns a differently-shaped dict fails at its boundary instead of three stages
later. This is what makes the harness's per-stage failure injection meaningful —
the degradation ladder in `DESIGN.md` is testable because each rung has a schema.

**`python-multipart==0.0.32`** — required by FastAPI for the `UploadFile` on
`POST /stt`. Not optional, not directly imported.

**`httpx==0.28.1`** — the only HTTP client, used for every external call.
Used by `stt/base.py`, `stt/sarvam.py`, `stt/elevenlabs.py`, `generate/client.py`.

*Why one client.* Every external call in this project has a hard timeout, a
bounded retry count and a defined fallback — a project rule, because an unbounded
retry policy looks responsible and destroys P100. One client library means one
place where timeout and pool configuration is set, and connection pools are
pre-established at boot so no request pays a TLS handshake inside a measured
span.

**`annotated-doc==0.0.5`** — FastAPI transitive, pinned only for reproducibility.

---

### 2.4 Stage 4 — phonetic query repair

**`libindic-soundex==1.0.2`** + **`libindic-utils==1.0.3`**
Used by `dhvani/retrieve/stage4.py`, `dhvani/build/build_index.py`.

*Why.* Speech recognition mangles proper nouns, and a mangled noun is invisible
to lexical retrieval. Soundex buckets terms by pronunciation, so a misheard term
can be matched against the corpus vocabulary phonetically and repaired before
retrieval. `libindic`'s implementation handles Indic scripts, which is the whole
requirement.

*Two notes.* `libindic-utils` is an **undeclared dependency** of
`libindic-soundex` — it is pinned explicitly because pip will not install it for
you. And the bucket key is `soundex(term)[1:]`, not `soundex(term)`, because the
library passes the first character through verbatim: keeping it would bucket by
first letter and defeat the point.

*The honest part.* **This stage currently costs recall.** Turning it off is worth
**+0.0103 recall@10**, and no threshold setting beats leaving it alone — looser
thresholds are identical to the default, tighter ones are worse (ADR-024, and the
21 Aug ablation confirms it). It rewrote 75 of 500 queries and made 82
corrections. It costs 0.03 ms, so this is not a latency problem, it is a
correctness one. It ships on, and the number arguing against it is on the front
page of the README.

---

### 2.5 Development

**`pytest==9.1.1`** — the whole test story. **196 passed, 17 skipped** from a
fresh clone; the 17 want a built index.

Project rule: nothing is complete without a check that fails if the logic breaks
— not a suite per function, one runnable check. Retrieval stages get fixture
queries with known chunk ids. The harness gets per-stage failure injection.
Guardrails run `eval/adversarial.jsonl` as a test with per-category assertions.

**Benchmarks are explicitly not tests.** They live in `dhvani/bench/` and write
to `docs/results/`. Determinism comes from replay mode, so two runs of the same
config produce the same numbers — without that, ablation deltas are noise.

---

### 2.6 Transitive pins

`certifi`, `filelock`, `flatbuffers`, `hf-xet`, `idna`, `packaging`, `protobuf`,
`pyyaml`, `tqdm`, `typing-extensions`.

Nothing imports these directly. They are pinned because the pinning policy is
about the environment a measurement was produced under, and a transitive that
floats can change behaviour under a fixed direct dependency — `protobuf` and
`flatbuffers` in particular sit underneath ONNX Runtime's graph loading.

---

## 3. Dependencies deliberately absent

The negative space is a design statement, so it is written down.

| Not used | Why not |
|---|---|
| **LangChain / LlamaIndex** | The pipeline is nine stages with typed contracts and per-stage traces. A framework would abstract exactly the seams this project needs to measure, toggle and ablate. Every stage must be switchable to appear in the ablation table. |
| **PyTorch / `sentence-transformers`** | ~2 GB of wheels and an fp32 model resident, to do what a 118 MB INT8 ONNX graph does in 2.86 ms. §2.1. |
| **`transformers`** | `tokenizers` alone covers what the ONNX path needs. |
| **Any hosted vector DB** | 30–80 ms of HTTP against a 200 ms budget, before any search (ADR-004). |
| **React / Next.js / any frontend framework** | A hydration bundle in front of a 13 ms retrieval path is a self-inflicted wound on the axis being judged. The UI needs a WebSocket, an EventSource and DOM updates. No build step also means no build-step failure at 11 pm on 21 August (ADR-008). Target < 50 KB gzipped. |
| **`python-dotenv`** | One line of shell does it: `set -a; . ./.env; set +a`. Nothing in `dhvani/` reads `.env`, deliberately — on a server the variables belong in the unit file. |
| **`websockets`** | Reserved for `WS /ws/stt` with streaming partials and speculative retrieval. Deferred (ADR-029); `POST /stt` batch ships instead. Boundary A is unaffected either way, since its clock starts at the final transcript in both designs. The line is still in `requirements.txt`, commented, with its ADR. |
| **Redis / any cache** | There is no cache. "Cache disabled" in the benchmarks names the only mode that exists, and the cache-hit-rate row in the results plan is **void, not pending**. |

---

## 4. Models are dependencies too

Not pip packages, but runtime requirements with the same reproducibility
problem. `models/` is gitignored; `python -m dhvani.build.fetch_models` pulls
them (135 MB, ~35 s).

| Model | Role | Size | Dims |
|---|---|---|---|
| **`multilingual-e5-small`** | deployed encoder | **118 MB INT8 ONNX** | 384 |
| `bge-m3` | benchmark alternate | 2,267 MB fp32 | 1024 |
| `LaBSE` | benchmark alternate | 1,882 MB fp32 | 768 |

**Why e5-small** (ADR-014). Four reasons, three of them independent of recall:

1. **It is the only candidate with a published pre-quantized INT8 ONNX build**
   (`model_qint8_avx512_vnni.onnx`) matching the deployed runtime. The others
   ship fp32 and would need quantization work that was not budgeted.
2. **384 dims against 1024 and 768.** At 528 bytes/vector, bge-m3 would cost 2.7×
   the index memory of the entire subset — and memory is the binding constraint,
   not recall.
3. **118 MB resident against 2.3 GB.** The target box has to hold three models.
4. Measured **2.86 ms p50** query encode against an 8 ms budget.

Measured recall, 300 Hindi validation queries against a 2,996-passage pool,
brute-force exact cosine (`2026-08-15-embed-bench.json`): **recall@10 0.890,
cross-lingual recall@10 0.837.**

That cross-lingual figure is load-bearing: a Hindi question answered from an
English passage retrieves at **94% of monolingual recall**, which is what
justifies the deduplicated English pivot in the indexed subset (ADR-012).

**The comparison is incomplete and labelled as such.** Both fp32 alternates OOM'd
a 15 GB box when their session coexisted with the parquet read, and one bge-m3
pass costs ~60 minutes at 2 threads. `embed_bench.py` now runs one model per
process. ADR-014 states in advance what would reverse the decision — recall@10
materially above 0.890 from bge-m3, "materially" meaning enough to justify 2.7×
index memory — so the result cannot be rationalized after the fact.

**Query and passage prefixes are part of the model contract.** e5 requires
`"query: "` and `"passage: "`; they are in the `ModelSpec`, not sprinkled at call
sites.

---

## 5. External services

Two, both bounded, both with a defined fallback.

| Service | Role | Fallback |
|---|---|---|
| **Sarvam AI** | STT (`POST /stt`) and generation | ElevenLabs Scribe implements the same `STTProvider`; Groq backs generation |
| **Groq** | generation fallback | `generation_unavailable` refusal |

*Why Sarvam for both* (ADR-003, ADR-009). Region is the first argument: STT and
generation are both India-hosted, so boundaries B and C do not carry a 230–280 ms
trans-Pacific round trip. Second, one vendor is one API key, one connection pool
to pre-warm, one circuit breaker, one set of credits. Third, the corpus and the
answers are Indic and Sarvam's stated design target is Indic phonology and
code-mixed Hindi-English — an STT model that mangles fewer proper nouns directly
reduces the work stage 4 has to undo.

`ElevenLabs` is implemented rather than merely specified, and
`tests/test_stt.py` checks both providers against the same audio.

**With no key at all the pipeline runs to the edge of the call and returns a
`generation_unavailable` refusal.** That is designed behaviour, not a crash.

**Retrieved corpus text is data, never instruction.** Passages are fenced in a
delimited block that the instruction block cannot reach — corpus-borne prompt
injection is threat T5 in `docs/GUARDRAILS.md`, and injection catch rate is
**1.00** over the 105-item adversarial set.

---

## 6. Two measured changes, deliberately not applied

Both come from `2026-08-21-bench-ablation.json` and both are recorded rather than
quietly applied, because retuning against a run and then reporting that same run
is how a benchmark becomes fabrication.

**1. `ef_search` 64 → 256.** The strongest single result in the table, and it
replicates across two independent runs:

| Run | recall@10 @64 | recall@10 @256 | Δ |
|---|---|---|---|
| 21 Aug ablation | 0.4464 | **0.4913** | +0.0449 |
| 18 Aug, clean transcripts | 0.4464 | **0.4913** | +0.0449 |
| 18 Aug, **garbled transcripts** | 0.3599 | **0.4187** | **+0.0588** |

The garbled row is the one that matters for a voice product — garbled *is* what
STT emits — and the gain is larger there. Cost is **+0.163 ms P50**
(13.485 → 13.648, median of 3 reps), which is below the rep-to-rep spread of the
arms themselves. It is nearly free because `ef_search` scales the dense half, and
the dense half is 0.491 ms of an 8.367 ms retrieve stage.

*Where the P100 saving comes from.* `LATENCY.md` first attributed it to "a more
stable candidate set for **fusion** to work on", which is wrong and is now
corrected there. Median of 3 reps:

| | `full` | `ef_search` 256 | Δ |
|---|---|---|---|
| `stage3_fuse` P100 | 0.421 ms | 0.362 ms | −0.06 |
| `stage3_retrieve` P100 | 15.497 ms | **16.799 ms** | **+1.30** |
| `stage7_context` P100 | 20.413 ms | **9.444 ms** | **−10.97** |
| boundary A P100 | 31.595 ms | 24.828 ms | −6.77 |

Fusion is sub-millisecond in both arms and cannot account for a 6.77 ms
difference. The retrieve tail goes **up**, which is the honest cost of a wider
graph search. The saving is downstream in **stage 7**: better candidates make
context selection's drop loops terminate sooner, and `stage7.dropped.budget`
falls 96 → 51 identically in all three reps. The trade is +1.30 ms of retrieve
tail for −10.97 ms of context-selection tail.

**2. Stage 4 off** — +0.0103 recall@10. §2.4.

Applying either means re-running the run of record and re-deriving every number
from the new file, not editing numbers by hand.

---

## 7. Known gaps

Stated plainly.

- **Stages 5 (RM3 expansion) and 6 (rerank) are specified and unbuilt** (ADR-027).
  They are absent from boundary A rather than stubbed inside it, and `/health`
  reports exactly which stages the span covers, per response.
- **recall@10 is 0.4464.** Roughly half of in-corpus questions refuse. That is
  the recall number showing up as user-visible behaviour, not a bug in the
  refusal path, and it is the strongest argument for the deferred stage 6.
- **Two of four guardrail layers ship switched off** (ADR-030). A retrieval-score
  threshold separates answerable from unanswerable at **AUC 0.581** on this
  corpus — general web text means something is always nearby. They are built,
  traced and calibrated, one config value from live. Refusal is carried by L1
  (script, injection) and L4 (per-sentence grounding).
- **Adversarial: 0.7746 overall catch, 0.35 false-refusal**, injection and
  out-of-index language both 1.00. Both numbers always published together — a
  system that refuses everything scores 100% on catches and is useless.
- **Tiering is not built**, so the tier rows in `README.md` are `PLACEHOLDER`.
  Thresholds are meant to be swept from the eval set, not guessed.
- **The deployment is a laptop behind an ngrok static domain** (ADR-036). Four
  hosting attempts fell through — Lightsail (card), HF Spaces (docker went
  PRO-only), Azure for Students (written, never run). Single point of failure,
  named and accepted.
- **The full-precision rescore** of the top-50 that SQ8's lossiness argues for is
  specified and disabled (ADR-017).

---

## 8. Reading order

For a reviewer with an hour:

1. [`README.md`](../README.md) — the boundary statement first, then the headline
   numbers.
2. [`docs/PROGRESS.md`](PROGRESS.md) — the day-by-day change log. The most useful
   document in the project; it records the failures at the time they happened.
3. [`docs/DECISIONS.md`](DECISIONS.md) — 37 ADRs. Read **ADR-015** (hnswlib removed),
   **ADR-023** (the Indic tokenizer bug), and **ADR-033** (the mmap that was not
   one). All three are the same shape: a plausible mechanism that had never been
   checked against a running process.
4. [`docs/LATENCY.md`](LATENCY.md) — the full 15-arm ablation table and the per-stage
   budget, including the two budgets it currently overruns.
