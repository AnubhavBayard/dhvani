# Design — dhvani

## Shape of the system

One Python process in Mumbai holding the index in RAM, serving both the API and
the static UI. Two external dependencies: Sarvam for STT, a generation provider
for the final answer. Everything between transcript and context selection is
in-process arithmetic — no sockets, no serialization, no hops.

That is the whole architecture, and it is the architecture *because* of the
latency budget. Any box you could add between "transcript arrives" and "context
selected" costs a network round trip we cannot afford.

```
browser                         mumbai vm (single process)              external
───────                         ──────────────────────────              ────────
 mic ─┐
      │ webm/opus chunks (WS)
      ├──────────────────────▶ /ws/stt ──── stream ──────────────────▶ sarvam
      │                             │                                  saarika
      │ ◀── partial transcripts ────┤ ◀────── partials ────────────────┘
      │                             │
      │                    ┌────────▼─────────┐
      │                    │  harness runner  │  typed stages, timing, retries
      │                    └────────┬─────────┘
      │                             │
      │        ┌────────────────────┼────────────────────┐
      │        │ (parallel)         │                    │ (parallel)
      │   guardrails:          stage 4 rewrite       semantic cache
      │   input layer          phonetic+norm         probe
      │        │                    │                    │
      │        │              ┌─────▼─────┐              │
      │        │              │  stage 3  │  dense HNSW ∥ BM25 → RRF
      │        │              └─────┬─────┘
      │        │                    │ confidence signal (margin, rank agreement)
      │        │           ┌────────┴────────┐
      │        │      early exit         escalate
      │        │           │                 │
      │        │           │           stage 5 RM3 ──▶ stage 6 rerank 2
      │        │           └────────┬────────┘
      │        │                    │
      │        └───▶ guardrails: scope + retrieval floor
      │                             │
      │                       ┌─────▼─────┐
      │                       │  stage 7  │ dedupe, order, token budget, ids
      │                       └─────┬─────┘
      │                             │  ── boundary A ends here ──
      │                             │
      │                             ├──── prompt + context ───────────▶ generation
      │ ◀── SSE: tokens, ────────── ┤ ◀─── stream ────────────────────┘
      │     timings, citations      │
      │                       guardrails: output layer
      │                       (citation overlap, NLI on ambiguous)
      ▼
  render: transcript, answer, citations; stage bar + timings behind a disclosure
```

## Data flow, microphone to rendered answer

1. **Capture.** `MediaRecorder` → opus chunks over a WebSocket. No client-side
   audio processing; every millisecond in the browser is a millisecond of budget.
2. **Streaming STT.** Chunks forwarded to Sarvam. Partial transcripts come back
   and are (a) rendered live and (b) fed to speculative retrieval — stage 3 runs
   on partials so candidates often exist before the user stops talking.
3. **Final transcript.** Boundary A's clock starts here.
4. **Fan-out at t=0.** Three things start simultaneously: input guardrails,
   semantic-cache probe, and stage 4 rewrite. Guardrails gate the *response*, not
   retrieval — serializing a safety check in front of retrieval costs its full
   latency for nothing, since a blocked query simply discards retrieval results.
5. **Retrieval.** Stages 3 → (5, 6 conditionally) → 7, per `RAG_PIPELINE.md`.
6. **Confidence gate.** Score margin and BM25/dense rank agreement decide early
   exit, standard, or escalation. Both signals are arithmetic on numbers already
   computed — zero marginal cost.
7. **Refuse or generate.** Score floor below threshold → refusal, no LLM call.
   Otherwise stream generation with the context and chunk ids.
8. **Output guardrails on the stream.** Citation-span overlap per sentence as it
   arrives; local NLI cross-encoder only where overlap is ambiguous.
9. **Render.** SSE carries tokens, the per-stage timing map, and citation
   anchors. UI paints the stage bar as events land, so the pipeline is visible
   rather than hidden behind a spinner — inside a disclosure closed by default,
   so the answer is what a first-time reader sees and the instrumentation is one
   click away rather than in the way (ADR-035).

## Region choice

This is an architecture decision, not deployment trivia. The pipeline's fixed
cost is dominated by where the compute sits relative to the user and to Sarvam.

Constraints:
- Sarvam AI is India-hosted. From a Mumbai VM the RTT is a LAN-ish hop; from
  US-east it is a 230–280 ms round trip **each way through the audio stream**.
- Groq and Cerebras are US-hosted. Fast generation, far away.
- Judges are in India.

Options considered:

| Option | STT hop | Generation hop | User hop | Verdict |
|---|---|---|---|---|
| **All Mumbai, Indian generation provider** | short | short | short | **chosen** |
| Mumbai app + US generation (Groq) | short | 230–280 ms | short | fallback if Indian generation quality fails |
| US app + Sarvam STT | 230–280 ms ×2 on a streaming socket | short | 230–280 ms | rejected — pays the worst hop on the highest-volume link |
| Split: STT proxy in India, retrieval in US | short | short | mixed | rejected — adds an inter-region hop inside boundary A |

**Decision: single region, `ap-south-1` / Mumbai, co-located with Sarvam.**
Boundary A never leaves the process, so region choice affects only STT and
generation; putting the app next to the *streaming* dependency (STT, many
round trips) beats putting it next to the *one-shot* dependency (generation).

Generation provider is `OPEN` — see below. If it lands US-hosted, boundary A is
unaffected and only B and C move, which is exactly why the boundary is drawn
where it is.

## Technology choices

Each row: what we picked, what we rejected, and the reason in latency or risk terms.

| Concern | Chosen | Rejected | Why |
|---|---|---|---|
| Backend language | Python 3.11 | Node, Rust | onnxruntime, hnswlib, bm25s, HF datasets are all first-class in Python. Rust is faster and 8 days is not enough to write an HNSW pipeline in it. |
| Web framework | FastAPI + uvicorn | Flask, Django | native async (parallel stage fan-out is the whole design), WebSocket + SSE built in, Pydantic gives the harness its typed stage contracts for free |
| Vector index | hnswlib, in-process | FAISS; Pinecone/Qdrant/Weaviate | hosted DB = 30–80 ms HTTP per query, a third of the budget on a socket. hnswlib over FAISS for a smaller install and a directly exposed `ef` dial, which is the highest-leverage latency knob we have. FAISS re-evaluated in Phase 2 if PQ support matters. |
| Lexical index | `bm25s` | Elasticsearch, rank_bm25 | in-memory, sparse-matrix scoring, no server. `rank_bm25` is a Python loop and too slow at corpus scale. |
| Embedder runtime | ONNX Runtime, INT8 | PyTorch | no CUDA on the target VM; ORT INT8 on CPU is the fast path. PyTorch also drags ~2 GB of deps into a container we want small. |
| Embedding model | `OPEN` — Phase 2 bench | `bge-small-en` | corpus is Indic. An English-only model is disqualified before benchmarking. Candidates: `multilingual-e5-small`, `bge-m3`, `LaBSE`. |
| Reranker | `OPEN` — multilingual cross-encoder, Phase 2 | `ms-marco-MiniLM` (English) | same reason |
| STT | Sarvam AI | ElevenLabs | `DECISIONS.md#adr-003` |
| Generation | `OPEN` | — | see below |
| Frontend | vanilla JS + CSS, no build step | Next.js, React | task-1 is Next.js and that was right for it. Here the pitch is latency; shipping a hydration bundle in front of a 40 ms retrieval path undercuts the entire submission. Target < 50 KB gzipped total. |
| Transport | WebSocket (audio up), SSE (answer down) | polling, WS both ways | SSE is one-directional and simpler for token streaming; the browser reconnect story is free |
| Deployment | single VM, Mumbai | serverless | the index must stay pinned in RAM. Cold-start a serverless function and you pay index load on the request that measures P100. |

## Interface contracts

Every stage boundary is a Pydantic model. The harness validates on the way in
and out, so a stage that returns garbage fails at its own boundary rather than
three stages later.

```python
class Transcript(BaseModel):
    text: str
    lang: str                 # BCP-47, from Sarvam or fallback detector
    is_final: bool
    confidence: float | None
    audio_ms: int

class Query(BaseModel):        # stage 4 out
    raw: str
    rewritten: str
    lang: str
    corrections: list[tuple[str, str]]   # (before, after) — shown in the UI
    method: Literal["phonetic", "llm", "passthrough"]

class Candidate(BaseModel):
    chunk_id: str
    text: str
    score: float
    source: Literal["dense", "bm25", "rrf", "rm3", "rerank"]
    lang: str
    doc_id: str               # query_id from the dataset
    strategy: str             # which chunking strategy produced it

class RetrievalResult(BaseModel):   # stages 3, 5, 6 all return this
    candidates: list[Candidate]
    signals: ConfidenceSignals

class ConfidenceSignals(BaseModel):
    top1: float
    margin_1_5: float         # top1 - top5, normalized
    rank_agreement: float     # Kendall tau, BM25 vs dense top-k
    tier: Literal["fast", "standard", "escalated"]

class SelectedContext(BaseModel):   # stage 7 out — boundary A ends here
    chunks: list[Candidate]
    tokens: int
    dropped: int              # deduped or budget-trimmed, for the ablation table

class GuardrailVerdict(BaseModel):
    layer: Literal["input", "scope", "retrieval", "output"]
    passed: bool
    reason: str | None        # maps to refusal copy, see GUARDRAILS.md
    score: float
    elapsed_ms: float

class StageTrace(BaseModel):
    stage: str
    elapsed_ms: float
    ok: bool
    retries: int
    fallback_used: bool
    meta: dict

class Answer(BaseModel):
    text: str
    citations: list[Citation]  # (claim span, chunk_id)
    refused: bool
    refusal_kind: str | None
    traces: list[StageTrace]
    signals: ConfidenceSignals
```

**Harness guarantees around every stage:**
- hard timeout, per-stage, from config
- bounded retry with exponential backoff — bounded because an unbounded retry
  policy looks responsible and destroys P100
- circuit breaker per external dependency; open circuit skips straight to fallback
- structured trace emitted whether the stage succeeded, retried, or fell back
- replay mode: a recorded trace re-runs the pipeline deterministically, which is
  what makes the benchmark reproducible and the ablation table honest

**Degradation ladder:**

| Failure | Fallback | User sees |
|---|---|---|
| STT down / mic denied | text input | fallback input focused, one-line note |
| STT partial garbage | phonetic rewrite still runs; if confidence floor missed, refuse | "couldn't catch that" refusal state |
| Reranker OOM/timeout | RRF fusion order from stage 3 | answer, timing panel marks stage 6 degraded |
| RM3 fails | stage 3 candidates pass through | same |
| Generation down | render retrieved passages with citations, no prose | "here's what I found, couldn't summarize" |
| Index missing at boot | process refuses to start | — (never a runtime path) |

## Deployment topology

```
[browser] ──https──▶ [caddy/nginx :443] ──▶ [uvicorn :8000, N workers = 1]
                                                 │
                                                 ├─ hnswlib index    (RAM, pinned)
                                                 ├─ bm25s index      (RAM)
                                                 ├─ ONNX embedder    (warmed at boot)
                                                 ├─ ONNX reranker    (warmed at boot)
                                                 ├─ ONNX NLI         (warmed at boot)
                                                 └─ httpx pools      (opened + warmed at boot)
```

One worker, deliberately: multiple workers each mmap their own copy of the index
and multiply RAM for no throughput we need at demo scale. Boot sequence is
strictly: load index → load models → warm models with throwaway queries → open
and warm HTTP/2 connections to Sarvam and the generation provider → *only then*
bind the port. Nothing is lazy-loaded, so no user request ever pays initialization.

**Host: AWS Lightsail 8 GB, Mumbai `ap-south-1`, $44/mo** — ADR-010. DigitalOcean
was rejected because it has no Mumbai region, only Bangalore, and the whole
placement argument is about sitting next to Sarvam. Index sizing arithmetic puts
1M chunks at ~0.92 GB int8, so 8 GB is generous; resize to 16 GB ($84/mo) if
Phase 2 measures otherwise.

**Generation: Sarvam** — ADR-009. India-hosted, same vendor as STT (one key, one
pool to warm, one circuit breaker), and it publishes a cached-input rate, which
is the prompt-caching optimization priced in rather than assumed. Groq stays
configured as the secondary provider; its free tier would cover a benchmark run
at zero cost if credits run out, at the price of a slower boundary C.

Both are signups against free credits, not purchases. Estimated total spend for
the project: ~₹50 of Sarvam's ₹100 signup credits, plus $44 for one month of VM.
