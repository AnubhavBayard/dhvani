# Decisions (ADR log)

Newest last. Each: context, options, decision, consequences, date.

---

## ADR-001 — `task-2/` is isolated from `task-1-frame-id-generator/`

**Date:** 2026-08-14

**Context.** The repo already contains task 1 (Next.js, deployed, its own
`.git`, its own `node_modules`, its own Vercel link). It is submitted work and
must not move or break. It also contains `brand/BRAND.md`, a repo-level shared
brand doc that task 1 reads from.

**Options.**
1. Import shared code and brand tokens across the folder boundary.
2. Copy anything needed into `task-2/`, no cross-boundary imports.
3. Extract a shared package both consume.

**Decision.** Option 2. `task-2/` gets its own `requirements.txt`, its own venv,
its own config, its own `.gitignore`. Nothing outside `task-2/` is imported at
runtime or build time.

**Consequences.** Some duplication, accepted deliberately: a shared import means
an edit on one side silently breaks a submitted project on the other, eight days
before a deadline with no resubmissions. Option 3 is correct engineering for a
long-lived repo and wrong for two one-shot submissions.

**Copies made from the sibling project:** none yet. Task 1 is Next.js/Canvas;
this is Python/RAG. The only genuinely reusable artifact is the brand knowledge,
and that is superseded — see ADR-006. Any future copy gets logged here.

**Repo root:** the root `README.md` is an index of tasks, so a single row is
appended to its table linking `task-2/`. Nothing else at the root is touched.

---

## ADR-002 — Stage reordering: two stages are build-time, and rewriting runs first

**Date:** 2026-08-14

**Context.** The brief numbers seven stages 1–7. Taken as a query-time sequence
in that order, the pipeline does provably wasted work.

**Three problems with the literal ordering.**

1. **Stages 1 and 2 (chunking, overlap) are build-time.** Chunking the corpus
   per query would re-chunk the same passages on every request for an identical
   result. Overlap is a parameter of *how chunks were cut* — it is decided when
   the index is written and cannot vary per query without re-chunking mid-request.
   Both are amortized across every query and contribute 0 ms to the query path.
   Leaving them in the query-time budget would mean either measuring work nobody
   does per query, or quietly not doing them and reporting the difference as
   speed.

2. **Stage 4 (query rewriting) must run before stage 3 (retrieval).** Its job is
   repairing STT damage: garbled proper nouns, dropped morphemes, mixed scripts.
   Retrieving first means the first retrieval ran on corrupt input — and in the
   literal ordering that corrupt retrieval is what seeds everything after it.
   Fixing the query at position 4 is repair applied to a decision already made.

3. **Stage 3's output must feed stage 5.** Rerank pass 1 produces a wide,
   high-recall candidate set. RM3 pseudo-relevance feedback needs exactly that:
   top-k documents as its pseudo-relevant seed. If pass 1's output were discarded
   and stage 5 re-retrieved from scratch, pass 1 would be pure overhead and would
   have to be cut. The 3→5 link is what makes both stages worth their latency.

**Options.**
1. Implement the literal 1→7 order.
2. Split build-time from query-time, run 4 before 3, wire 3's output into 5.
3. Cut stages until the budget fits.

**Decision.** Option 2. Build-time: 1, 2. Query-time: **4 → 3 → 5 → 6 → 7**.

**Consequences.** Every stage the brief asks for is implemented and measured;
none is dropped. The query-path budget contains only work actually done per
query. The 3→5 dependency is load-bearing, so the ablation harness must test
"stage 5 off" (stage 3 output passes through) rather than "stage 3 off with 5 on",
which is not a valid configuration. Option 3 was rejected outright: dropping a
required stage to hit a latency number is the failure mode the brief warns about.

---

## ADR-003 — STT provider: Sarvam AI

**Date:** 2026-08-14

**Context.** The brief allows Sarvam AI or ElevenLabs. Corpus is
`ai4bharat/MSMARCO-XI` — Assamese, Bengali, Gujarati, Hindi, Kannada, Malayalam,
Marathi, Nepali, Odia, Punjabi, Sanskrit, Tamil, Telugu, Urdu. Users and judges
are in India. STT sits on a streaming socket, so its round trip is paid many
times per question, not once.

**Options.**

| | Sarvam AI | ElevenLabs Scribe |
|---|---|---|
| Indic language coverage | built for Indian languages, the stated product focus | broad multilingual, Indic not the focus |
| Hosting | India | US/EU |
| Network from a Mumbai VM | short hop | 230–280 ms RTT, paid repeatedly on a streaming connection |
| Code-mixed Hindi-English | a stated design target | not a stated target |
| Same-vendor generation option | Sarvam-M, same region, one fewer connection pool | n/a |

**Decision.** Sarvam AI on the default path.

**Rationale.** Two independent reasons, either sufficient. First, language: the
corpus is Indic, and an STT model trained for Indic phonology and code-mixing
will mangle fewer proper nouns — which directly reduces the work stage 4 has to
undo. Second, geography: STT is the only external dependency on a *streaming*
connection, so its RTT is multiplied by the number of round trips in a
conversation turn. Co-locating with it in Mumbai is the single highest-leverage
placement decision in the system, and it only pays off if the provider is in
India.

**Consequences.** ElevenLabs is implemented as a second `STTProvider` and
selectable by config, with a test that runs the same audio through both — the
brief asks for a swappable abstraction, and an interface with one implementation
is not an abstraction, it is a wrapper. Vendor risk is concentrated on Sarvam;
mitigated by the circuit breaker (open circuit → ElevenLabs → text input).

**Cost.** Checked 2026-08-14:

| | free tier | STT rate | cost of one benchmark pass (~42 min audio) |
|---|---|---|---|
| Sarvam | ₹100 credits, no expiry | ₹30/hour, billed per second | ~₹21 — inside the free credits |
| ElevenLabs | 10k credits/month | 330 credits/min ≈ 30 min/month | ~13,800 credits — **over** the free tier; needs Starter at $6/mo |

Sources: [docs.sarvam.ai pricing](https://docs.sarvam.ai/api-reference-docs/pricing),
[elevenlabs.io/pricing](https://elevenlabs.io/pricing). Sarvam's marketing page
advertises ₹1,000 free credits against the docs' ₹100; planning against ₹100.

Neither requires a card up front, so this is no longer a paid-dependency
approval — it is a signup. The replay harness caches transcripts, so audio is
transcribed once and every re-benchmark and ablation run costs zero STT. Real
spend is one pass over the eval audio plus demo and video takes.

ElevenLabs' free tier is enough to run the same audio through both providers in
the swap test, which is all the second implementation needs to prove.

---

## ADR-004 — In-process index, no hosted vector database

**Date:** 2026-08-14

**Context.** Boundary A budget is 200 ms. A hosted vector DB call is 30–80 ms.

**Options.** Pinecone / Qdrant Cloud / Weaviate; self-hosted Qdrant on the same
box; hnswlib or FAISS in-process.

**Decision.** hnswlib in-process, plus `bm25s` in-process for lexical. No
network hop anywhere between transcript and context selection.

> **Amended 2026-08-16 — the library is now FAISS `IndexHNSWSQ`, see ADR-015.**
> hnswlib turned out to have no quantization of any kind: it stores float32, at
> a measured 1,684 bytes per vector against the 528 every sizing figure in this
> project assumed. The escape hatch below was the right one and it was taken.
> Everything else in this ADR stands unchanged — in-process, no hosted vector
> DB, no network hop, and the `ef` dial still directly exposed.

**Rationale.** A hosted DB spends a third of the budget on HTTP before any search
happens. Even a self-hosted Qdrant on the same machine pays serialization,
loopback, and a process boundary for a search that is a function call in-process.
hnswlib over FAISS for a smaller install and a directly exposed `ef` dial — the
highest-leverage recall/latency knob available, and one this design tunes per
tier.

**Consequences.** The index must fit in the VM's RAM, which bounds corpus size
(ADR-007). No horizontal scaling and no index updates without a restart —
neither matters for a single-region demo. Re-evaluate FAISS in Phase 2 if
product quantization turns out to matter more than the `ef` dial.

---

## ADR-005 — Latency boundary A is the headline number

**Date:** 2026-08-14

**Context.** The brief asks for "under 200ms" for "chunking + vector DB retrieval
+ everything through to final output". From India, a round trip to any hosted LLM
is 230–280 ms of network before a token is generated. A wall-clock target under
200 ms is therefore unreachable for any RAG system with a hosted generator.

**Options.**
1. Report wall clock and fail the target visibly.
2. Report a narrow retrieval-only boundary and not mention wall clock.
3. Define three boundaries, target the defensible one, publish all three.
4. Drop hosted generation for a local model to make wall clock fit.

**Decision.** Option 3. Boundary A (transcript → context selected) is targeted
and headlined; B (TTFT) and C (full wall clock) are measured and published beside
it. The boundary is stated at the top of the README, before any number.

**Rationale.** Option 2 is the dishonest one and the easiest to get caught at.
Option 1 discards the real engineering result — that the retrieval pipeline
genuinely is fast. Option 4 trades answer quality for a headline and is kept on
the table as a documented alternative if the numbers argue for it.

**Consequences.** The headline number requires an explanation, which costs a
paragraph in the README. Worth it: a stated boundary with all three numbers
published cannot be attacked as hiding anything, and an unexplained "under 200ms"
from India invites exactly that attack.

---

## ADR-006 — Design tokens come from the live site, not the sibling brand doc

**Date:** 2026-08-14

**Context.** `../brand/BRAND.md` holds a palette sampled from the printed key art
for task 1: `#074C18` green, `#F8E206` yellow, Playfair Display, Space Grotesk.
The live site at hhgoa.com ships different values: `#0b6839` green, `#fee101`
yellow, Imbue and Victor Mono. The footer "Brand Kit" is a `<p>` with no `href`;
`/brand-kit` 404s; the only Drive asset linked from the site is the task 1 brief.

**Options.** Reuse the sibling's poster palette; extract from the live site;
reconcile the two into one system.

**Decision.** Extract from the live site's shipped CSS. Documented in
`DESIGN_SYSTEM.md` with the source URL and extraction date.

**Rationale.** The site is what judges see, and its stylesheet is more
authoritative than a sampling of a printed poster. Reconciling them would be
wrong — they describe different artifacts (print key art vs web product), and
averaging two brand systems produces neither.

**Consequences.** Task 1 and task 2 look related but not identical. Acceptable:
they are separate submissions judged separately. Victor Mono as the default UI
font is the main carry-over, and it is the right one — it is where the
terminal-native identity actually lives.

---

## ADR-007 — Index a documented subset, not the full corpus

**Date:** 2026-08-14

**Context.** Measured from the HF API on 2026-08-14: 28 parquet files (14
languages × train/validation), each ~419–494 MB, tagged `10M<n<100M` rows. Each
row carries ~10 passages in both English and the target language. Full-corpus
embedding is days of GPU time and tens of GB of index. The dev box has 15 GB RAM.

**Options.** Full corpus; one language; a stratified multi-language subset.

**Decision.** A stratified subset across a chosen set of languages, with its
exact size, language mix, and row count stated in `DATASET.md`, the README, and
the UI. Selection criteria fixed in Phase 2 after measuring embedding throughput
and per-chunk index memory.

**Rationale.** The task tests retrieval engineering, not corpus scale. A subset
that is stated everywhere is honest; an unstated one is the same thing as a lie
about scale. Multi-language rather than Hindi-only because the multilingual
retriever choice is a central decision and cannot be validated on one script.

**Consequences.** Scope guardrail L2 must refuse questions outside the indexed
subset, including questions in languages present in the dataset but not indexed —
which makes the subset decision a guardrail requirement, not just a resourcing
one. Recall numbers are subset numbers and are labelled as such everywhere.

**Two further constraints on language selection, found 2026-08-14** — neither is
about compute, and both were invisible when this ADR was written:

1. **Font payload.** Neither brand font covers any Indic script, so each indexed
   language costs a separate Noto woff2 download in front of a latency demo
   (`DESIGN_SYSTEM.md`).
2. **Phonetic coverage.** Stage 4's correction depends on an Indic phonetic
   library, and neither candidate covers Urdu's Perso-Arabic script.

**Recommendation: exclude Urdu.** It is the one language that fails both — no
phonetic correction path, and a large right-to-left font requiring layout work
nothing else on the page needs. Final language list still set in Phase 2, but it
is now chosen against three constraints rather than one.

---

## ADR-008 — Vanilla frontend, no framework

**Date:** 2026-08-14

**Context.** Task 1 is Next.js/React and that was right for it. This project's
entire claim is latency.

**Options.** Next.js (familiar, deploys to Vercel); Svelte; vanilla JS + CSS, no
build step.

**Decision.** Vanilla. Target < 50 KB gzipped.

**Rationale.** A hydration bundle in front of a 40 ms retrieval path is a
self-inflicted wound on the one axis being judged. The UI needs a WebSocket, an
EventSource, and DOM updates — none of which needs a framework. No build step
also means no build-step failure at 11 pm on 21 August.

**Consequences.** More hand-written DOM code. Bounded: the UI is one page with
seven components. The backend cannot be serverless anyway (ADR-004: the index is
pinned in RAM), so the frontend cannot inherit Vercel's deployment story from
task 1 regardless — see `DESIGN.md` deployment topology.

---

## ADR-009 — Generation provider: Sarvam

**Date:** 2026-08-14

**Context.** Boundary A ends before generation, so this choice cannot break the
headline number — but it sets boundaries B and C, and it decides whether the
answer is any good in Indic languages. Left `OPEN` when `DESIGN.md` was written.

**Options.** Prices checked 2026-08-14.

| | region | input / 1M | output / 1M | cached input | free tier |
|---|---|---|---|---|---|
| **Sarvam 105B** | India | ₹29.28 | ₹73.2 | ₹10.98 (−62%) | shares the ₹100 signup credits |
| Groq Llama 3.3 70B | US | $0.59 (~₹52) | $0.79 (~₹70) | −50% on repeated prefixes | 30 req/min, 1000 req/day, no card |
| Groq Llama 3.1 8B | US | $0.05 | $0.08 | as above | as above |

Sources: [docs.sarvam.ai pricing](https://docs.sarvam.ai/api-reference-docs/pricing),
[Groq pricing 2026 summary](https://www.cloudzero.com/blog/groq-pricing/).

**Decision.** Sarvam, same vendor as STT.

**Rationale.** Four reasons, in order of weight.

1. **Region.** India-hosted. Boundaries B and C stay defensible instead of
   carrying a 230–280 ms trans-Pacific round trip we would then have to explain.
2. **One vendor.** One API key, one connection pool to pre-warm at boot, one
   circuit breaker, one set of credits. Optimization 6 is about killing
   connection overhead; halving the number of external hosts is the cheapest
   version of that.
3. **Indic quality.** The corpus is Indic and the answers are Indic. A vendor
   whose entire product is Indian languages is the right prior, though it is a
   prior and not a measurement.
4. **Prompt caching is priced in.** ₹10.98 vs ₹29.28 on cached input is a 62%
   discount on the static system prompt, which is re-sent on every request. The
   optimization the brief asks for has a published rate here.

**Cost check.** 500 benchmark queries × (~1500 context + ~200 output) ≈ 750k
input + 100k output ≈ ₹22 + ₹7 = **₹29 per full run**, less with the cached
prefix. Plus ~₹21 of STT. Roughly **₹50 of the ₹100 signup credits** for one
benchmark run of record plus demo and video takes. Fits, without much room —
if it runs short, the replay harness makes re-runs cost nothing on STT, and
generation can be topped up cheaply.

**Consequences.** Vendor risk concentrates further on Sarvam: if it is down,
both STT and generation are down. Mitigated by the degradation ladder — STT
failure falls back to text input, generation failure falls back to returning the
retrieved passages, and the retrieval half of the system (the part being judged
on latency) keeps working through both. Groq stays configured as the secondary
generation provider; its free tier at 1000 req/day would cover a benchmark run
at zero cost if credits run out, at the price of a slower boundary C.

**This closes blocker B2.** It is a signup, not a purchase.

---

## ADR-010 — Deploy target: AWS Lightsail, Mumbai

**Date:** 2026-08-14

**Context.** ADR-004 pins the index in process RAM, so this needs a real VM, and
ADR-003 puts co-location with Sarvam ahead of everything else.

**Index sizing** — arithmetic, not a measurement, using 384-dim vectors
(`multilingual-e5-small`), hnswlib at `M=16`, and ~400 bytes of chunk text each:

| chunks | int8 index | fp32 index |
|---|---|---|
| 500k | 0.46 GB | 1.04 GB |
| 1M | 0.92 GB | 2.07 GB |
| 2M | 1.84 GB | 4.14 GB |
| 5M | 4.60 GB | 10.36 GB |

Plus ONNX embedder, reranker, and NLI models resident, plus the BM25 index, plus
the process. An 8 GB box holds a few million chunks comfortably. This is far less
constraining than expected, and it means the subset can be generous.

> **Superseded 2026-08-15 — the table above is wrong and the conclusion under it
> is wrong.** It assumed ~400 bytes of chunk text per chunk. Indic scripts are 3
> bytes per character in UTF-8; the measured mean is 822–1,022 bytes per passage
> (`DATASET.md`). It also counted one chunk per passage, where four chunking
> strategies produce roughly 6.8. The corrected figure is ~7.9 KB per source
> passage, not 0.92 KB — **8.6× higher** — and the subset is consequently tight
> rather than generous. See **ADR-012**. The choice of host is unaffected; only
> the sizing arithmetic that reassured us about it was.

**Options.** Prices checked 2026-08-14.

| | India region | 8 GB | 16 GB |
|---|---|---|---|
| **AWS Lightsail** | **Mumbai, ap-south-1** | $44/mo (2 vCPU) | $84/mo (4 vCPU) |
| DigitalOcean | **Bangalore only — no Mumbai** | $48/mo (4 vCPU) | $96/mo (8 vCPU) |

Sources: [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/),
[DigitalOcean droplet pricing](https://www.digitalocean.com/pricing/droplets).

**Decision.** Lightsail 8 GB in Mumbai, $44/mo. Resize to 16 GB if Phase 2
measures the index larger than the arithmetic suggests.

**Rationale.** DigitalOcean has no Mumbai region, only Bangalore. Intra-India
Bangalore↔Mumbai is tens of milliseconds — irrelevant to most projects and not
irrelevant to this one, where the entire STT stream crosses that link and the
whole placement argument (ADR-003) is about being next to Sarvam. Lightsail is
also marginally cheaper at 8 GB. DigitalOcean's better vCPU-per-dollar would
matter if this were compute-bound; it is latency-bound.

**Consequences.** Lightsail's Mumbai bundles ship half the data transfer
allowance of standard regions — irrelevant at demo traffic, noted so it is not a
surprise. One VM, no redundancy: if it dies during judging, the live link is down.
Accepted for a demo; the mitigation is deploying on Day 7 rather than Day 8, so
there is a day to notice.

**This closes blocker B4**, at $44 for one month.

---

## ADR-011 — Matryoshka embeddings dropped

**Date:** 2026-08-14

**Context.** The plan called for searching at 256 truncated dimensions on the
wide pass and rescoring at full dimensions on the narrow set, on the premise that
`bge-m3` supports Matryoshka representation learning.

**Finding.** It does not. Checked both candidate model cards on 2026-08-14:
neither `BAAI/bge-m3` nor `intfloat/multilingual-e5-small` mentions Matryoshka or
MRL. bge-m3's advertised "multi-functionality" is dense, sparse, and ColBERT
multi-vector retrieval — multi-*function*, not multi-*resolution*. Truncating a
non-MRL embedding degrades recall unpredictably, because nothing trained the
prefix dimensions to stand alone.

**Decision.** Drop the optimization. Do not substitute an unverified claim.

**Consequences.** Very little, as it turns out. `multilingual-e5-small` is 384
dimensions natively — smaller than the 256-dim search step this was going to
truncate *down to* from 1024. The problem it solved does not arise on the
favoured model. Scalar quantization and per-tier `ef_search` remain, and those
are the larger levers anyway.

Revisit only if Phase 2's recall benchmark forces `bge-m3` and its 1024 dims.

**Also found in the same check** — inputs to the Phase 2 model benchmark, not a
decision yet:

| | dims | layers | ONNX | note |
|---|---|---|---|---|
| `multilingual-e5-small` | 384 | 12 | **pre-quantized INT8 published** (`model_qint8_avx512_vnni.onnx`) | exactly our runtime, zero quantization work |
| `bge-m3` | 1024 | 24 | fp32 with external data file | 2× depth, 2.7× index size, no INT8 build |
| `LaBSE` | 768 | 12 | bare fp32 | 501k vocab — a very large embedding table |

`multilingual-e5-small` is the strong favourite before a single recall number
exists. It still gets benchmarked — the favourite losing on recall is exactly
what the benchmark is for — but the deployment story is decided either way.

---

## ADR-012 — Indexed subset: 3 languages, 15,000 shared rows, English as a deduplicated pivot

**Date:** 2026-08-15

**Context.** ADR-007 committed to "a stratified subset" and deferred the numbers
to Phase 2. Phase 2 measured them, and two of its findings changed the shape of
the decision rather than just filling in blanks.

**Finding 1 — the 14 language files are the same rows.** `query_id` sequence,
`query_type` distribution, passage counts, and `is_selected` labels are identical
across files, and `English_passages` is byte-identical between them
(`DATASET.md`, `MEASURED 2026-08-15`). Language count buys script coverage, not
corpus size. It also means one sampled set of `query_id`s applies unchanged to
every language, so per-language results are exactly comparable and the English
side is embedded once for the whole index.

**Finding 2 — ADR-010 undersized the chunk text by 2.1–2.6×.** It assumed ~400
bytes per chunk. Indic scripts are 3 bytes per character in UTF-8, and the
measured mean is 822 bytes (Hindi), 834 (Bengali), 1,022 (Tamil). Chunk text is
the largest single line item in index memory, so the sizing table in ADR-010 is
wrong and is superseded here.

**Corrected sizing**, per source passage, all four chunking strategies indexed:

| line item | bytes | basis |
|---|---|---|
| chunk text, all strategies | ~2,500 | `MEASURED` 822 B/passage × ~3 strategies that re-split the same text |
| vectors, int8 × 384 dims | ~2,600 | `MEASURED` byte count × `TARGET` 6.8 chunks/passage |
| hnswlib graph, `M=16` | ~980 | arithmetic, 145 B/node |
| BM25 postings | ~1,800 | `TARGET`, from token counts |
| **total** | **~7.9 KB** | |

The 6.8 chunks-per-passage multiplier is a `TARGET`. It is the one number here
that is not measured, because it cannot be until the chunkers exist. It is
re-measured on Day 3 and the row count is the dial that absorbs the difference.

**Budget.** 8 GB box (ADR-010), minus OS, FastAPI, and three resident ONNX
models — embedder, reranker, NLI — leaves ~5 GB for indexes. At 7.9 KB per
source passage that is **~630,000 source passages**.

**Decision.**

| | value |
|---|---|
| target languages | **Hindi (`hin_Deva`), Bengali (`ben_Beng`), Tamil (`tam_Taml`)** |
| pivot language | English, deduplicated — indexed once, shared by all three |
| split indexed | validation |
| rows | **15,000 `query_id`s**, sampled once, applied identically to all four files |
| sampling | stratified by `query_type`, seeded, gold and no-gold rows kept in their natural 55/45 proportion |
| resulting source passages | ~149,700 per language × 4 = **~599,000** |
| projected index | **~4.8 GB** `TARGET` |

**Why these three languages.**

- **Three scripts, three Noto woff2 files.** The font constraint from ADR-007 is
  per *script*, not per language — Devanagari alone would have covered Hindi,
  Marathi, Nepali and Sanskrit for one download. Choosing three distinct scripts
  is therefore the expensive option on payload and the right one on evidence:
  a multilingual retriever validated on one script has not been validated.
- **Two language families.** Hindi and Bengali are Indo-Aryan, Tamil is
  Dravidian. Tamil also has the longest passages measured (p50 334 chars vs
  Hindi's 292), so it is the script that stresses the stage-6 budget hardest.
- **All three have a train split**, so chunking strategy S4 works for all three
  and the ablation table has no empty cells.

**Excluded, with reasons.**

- **Urdu.** `libindic/soundex` returns `ب0000000` for Perso-Arabic input — the
  first character and then nothing. There is no phonetic signal at all, so stage
  4 has no correction path for it. It is also the only right-to-left script here
  and would need layout work nothing else on the page needs. Confirms the
  recommendation ADR-007 made on weaker evidence.
- **Telugu.** The dataset ships no `teltrain.parquet` — 13 train files, 14
  validation files. S4 headers may only be built from train, so indexing Telugu
  means a documented hole in the ablation table.
- **The other nine.** No evidence against them; they simply do not add a script
  or a family that the chosen three do not already cover, and each one costs
  ~1.2 GB of index for content already present in another language.

**Consequences.**

- The subset is **15.3% of one split of a 14-language corpus**, and that number
  goes in the README, in `DATASET.md`, and in the UI. ADR-007's rule stands: an
  unstated subset is a lie about scale.
- Guardrail L2 must refuse questions in Assamese, Gujarati, Kannada, Malayalam,
  Marathi, Nepali, Odia, Punjabi, Sanskrit, Telugu and Urdu — languages present
  in the dataset and absent from the index. That refusal is a feature and needs
  its own adversarial eval category.
- **The row count is the dial, not the language list.** If the Day-3 chunk
  multiplier comes in above 6.8, rows drop; if below, rows rise. Changing the
  language list would invalidate the font subsets and the guardrail eval set, so
  it is the expensive dial and is fixed here.
- Two levers held in reserve, in order of preference: move chunk text out of RAM
  to an mmap'd store (~30% of index memory, no recall cost), then resize the box
  to 16 GB ($84/mo, ADR-010).

---

## ADR-013 — Python 3.11 comes from a uv-managed standalone build

**Date:** 2026-08-15

**Context.** `CLAUDE.md` specifies Python 3.11 and an own venv at `task-2/.venv`.
The dev box has system Python 3.10.12 and 3.13.15, no 3.11, and neither system
interpreter can create a working venv unaided: `ensurepip` is absent from both
(Debian/Ubuntu splits it into `python3-venv`). Only 3.10 has development headers
installed, and `hnswlib` (ADR-004) publishes no wheels at all — it compiles from
source and needs headers.

**Options.**
1. `apt install python3.11 python3.11-venv python3.11-dev` — needs sudo.
2. Use system 3.10 with `--without-pip` and bootstrap pip by hand — deviates
   from the stated version, and still needs `python3.10-venv` for a clean venv.
3. Use system 3.13 — no headers, so `hnswlib` cannot build.
4. `uv python install 3.11` — downloads a self-contained CPython 3.11.15 build
   that ships pip and headers, then `uv venv --python 3.11 .venv`.

**Decision.** Option 4.

**Rationale.** It is the only option that hits the version `CLAUDE.md` specifies
without sudo and without a broken venv. It also makes the interpreter itself
reproducible: the version is pinned by the tool rather than by whatever the host
distribution happens to ship, which is the same property the pinned
`requirements.txt` is there for. `uv` was already installed on the box.

**Consequences.** `uv` becomes a build prerequisite and is stated as one in the
README's setup steps — the fresh-clone verification on Day 7 has to install it
first. The Lightsail box gets the same standalone 3.11, so dev and deploy run
the same interpreter build rather than merely the same version number.

---

## ADR-014 — Embedding model: `multilingual-e5-small` INT8

**Date:** 2026-08-16

**Context.** ADR-011 called `multilingual-e5-small` the favourite before any
recall number existed, and said the favourite losing on recall was exactly what
the benchmark was for. The benchmark ran on 15 Aug.

**Measured** — 300 Hindi validation queries with a gold passage, 2,996-passage
pool, brute-force exact cosine, 2 ONNX threads
(`docs/results/2026-08-15-embed-bench.json`):

| | dims | ONNX | recall@10 | recall@5 | MRR@10 | cross-lingual recall@10 | query p50 |
|---|---|---|---|---|---|---|---|
| **`multilingual-e5-small`** | 384 | **118 MB INT8** | **0.890** | 0.777 | 0.482 | 0.837 | **2.86 ms** |
| `bge-m3` | 1024 | 2,267 MB fp32 | not completed | | | | |
| `LaBSE` | 768 | 1,882 MB fp32 | not completed | | | | |

**The comparison is incomplete, and that is stated rather than papered over.**
Both fp32 models OOM'd a 15 GB dev box when their session coexisted with the
parquet read, and one bge-m3 pass costs ~60 minutes at 2 threads against
e5-small's 3. `embed_bench.py` now runs one model per process and merges
results, so the run can be completed; it is queued to run alongside the index
build rather than in front of it.

**Decision.** `multilingual-e5-small` INT8, now, without waiting for the other
two.

**Rationale.** The deployment argument was already decisive and independent of
recall, and ADR-011 said so before the numbers existed:

1. **It is the only candidate with a published pre-quantized INT8 ONNX build**
   (`model_qint8_avx512_vnni.onnx`) matching the runtime this project deploys.
   The others ship fp32 and would need quantization work we have not budgeted.
2. **384 dims against 1024 and 768.** At 528 bytes per vector in the index
   (ADR-015), bge-m3 would cost 2.7× the index memory of the entire subset — and
   ADR-012 shows the memory budget is the binding constraint, not recall.
3. **118 MB against 2.3 GB resident.** The 8 GB box holds three models at once —
   embedder, reranker, NLI.
4. Measured query latency of **2.86 ms p50** against an 8 ms budget.

**What would reverse this.** Only recall@10 materially above 0.890 from bge-m3
on the completed run — "materially" meaning enough to justify 2.7× index memory,
which given the sizing in ADR-012 means the subset would have to shrink by more
than half to pay for it. That is a high bar and it is stated in advance so the
result cannot be rationalized after the fact. If it clears, this ADR is amended
and the index is rebuilt; the build pipeline is model-agnostic (`dhvani/embed.py`)
precisely so that stays a config change.

**Consequences.** The cross-lingual number — 0.837 recall@10 for a Hindi query
against English passages, against 0.890 monolingual — is high enough that the
deduplicated English pivot (ADR-012) is worth its index space: a Hindi question
can be answered from an English passage at 94% of monolingual recall.

---

## ADR-015 — FAISS `IndexHNSWSQ` replaces hnswlib

**Date:** 2026-08-16

**Context.** ADR-004 chose hnswlib in-process and left an explicit escape hatch:
"Re-evaluate FAISS in Phase 2 if product quantization turns out to matter more
than the `ef` dial." ADR-010 and ADR-012 both sized the index assuming int8
vectors at 384 bytes each.

**Finding.** `MEASURED 2026-08-16`, 100,000 random 384-dim vectors, `M=16`:

| | bytes/vector | 100k index |
|---|---|---|
| hnswlib | **1,684** | 168 MB |
| FAISS `IndexHNSWFlat` (fp32) | 1,680 | 168 MB |
| FAISS `IndexHNSWSQ` (`QT_8bit`) | **528** | 53 MB |

**hnswlib stores float32 and offers no quantization at all.** There is no int8
mode, no scalar quantizer, no option. Every sizing figure in ADR-010 and ADR-012
assumed something the chosen library cannot do — 1,684 bytes against an assumed
528 is **3.2× over**, which on top of the text-size error ADR-012 already
corrected would have put the index at roughly 2× the 8 GB box.

**Decision.** `faiss.IndexHNSWSQ(384, QT_8bit, M=16, METRIC_INNER_PRODUCT)`.

**Rationale.** It is the same HNSW graph with a scalar quantizer in front of the
vectors, so ADR-004's actual argument — in-process, no network hop, a directly
exposed `ef` dial — is untouched. `efSearch` and `efConstruction` are exposed as
`index.hnsw.efSearch` / `.efConstruction`, so per-tier `ef` tuning survives
verbatim. 528 bytes/vector is exactly the figure ADR-012 budgeted against, so
the subset does not have to shrink.

**Consequences.**

- `faiss-cpu` replaces `hnswlib` in `requirements.txt`. Larger install (~30 MB
  against ~1 MB) and it ships wheels, so ADR-013's "hnswlib compiles from source"
  argument for headers no longer applies — the uv-managed 3.11 stands on its own
  merits, which were never only about hnswlib.
- SQ8 is lossy. Recall against an exact search is measured on the real index and
  reported in `LATENCY.md`, and the full-precision rescore of the top-50
  (RAG_PIPELINE.md stage 3) is what it is there for. The comparison on random
  vectors showed no degradation, but random vectors are not evidence about this
  corpus and are not cited as such.
- ADR-004 is amended, not overturned: in-process ANN, no hosted vector DB. Only
  the library changed, and it changed for the reason ADR-004 named in advance.

---

## ADR-016 — S4's headers come from held-out validation rows, not the train split

**Date:** 2026-08-16

**Context.** `CHUNKING.md` guards strategy S4 against label leakage with: "S4
headers are built only from the train split; every evaluation query comes from
the validation split." The mechanism is a split boundary; the property it buys
is that no evaluation query is ever the header of a chunk it retrieves.

**What it costs.** Every file in this dataset is a single parquet row group
(`DATASET.md`), so there is no partial read — using train means downloading
3.7 GB per language, 11 GB for three, to extract 15,000 rows each. The download
stalled twice against the hub and had produced 0 bytes after seven minutes.

**Finding.** The split boundary is not the only way to get the property, and it
is not the most direct. The validation split has 97,941 rows and ADR-012 indexes
15,000 of them; **82,941 rows are unused and already on disk.**

**Decision.** S4 headers are built from a held-out slice of validation, chosen
disjointly from the indexed subset by `query_id`, and the disjointness is
asserted in the build.

**Rationale.** The assertion moves from a proxy to the thing itself. "These rows
came from a different split" implies non-overlap; `set(s4_ids) & set(eval_ids)
== ∅` **is** non-overlap, checked directly, and it fails loudly if a later change
to the sampler breaks it. It is also the same distribution — the eval queries and
the S4 headers are drawn from one population rather than two, so S4's measured
contribution is not confounded by a train/validation distribution shift.

**Consequences.**

- No train-split download. `data/raw` stays at 1.4 GB.
- `CHUNKING.md`'s S4 section is rewritten: the guard is `query_id` disjointness,
  the assertion lives in `build_index.py`, and it runs as a test.
- The honest limitation is unchanged and worth restating: S4 can only help when
  an evaluation query is semantically near the *header of a different row*.
  It cannot retrieve its own memorized query, by construction. If the ablation
  shows S4 winning no cell, that is a real result and the strategy gets cut per
  `CHUNKING.md`'s stated decision rule.
- Telugu's missing train split (ADR-012) stops being a reason to exclude Telugu.
  Telugu stays excluded on the other grounds — script coverage is already served
  and each language costs ~1.2 GB of index — but the record should say which
  argument survives.

---

## ADR-017 — Stage 3's full-precision rescore is specified but disabled

**Date:** 2026-08-15

**Context.** `RAG_PIPELINE.md` stage 3 step 4 says "rescore top-50 with
full-precision vectors if the index is quantized". The index *is* quantized —
ADR-015 chose `IndexHNSWSQ` with an 8-bit scalar quantizer — so the step is live
by its own condition. Rescoring needs the fp32 vectors, and a scalar-quantized
FAISS index cannot reconstruct them; they would have to be persisted separately.

**Finding.** ADR-012 sizes the subset at ~4M chunks. Keeping fp32 vectors beside
the index costs `4M x 384 x 4 B = 6.1 GB`, against an 8 GB box that already
holds the index, the chunk text, three ONNX sessions and the process. The
rescore is not affordable at the subset size the memory budget already forced.

**Decision.** `Stage3Config.rescore_top = 0`. The stage still emits a
`stage3_rescore` trace row with `enabled=False` and the reason, so it appears in
the ablation table as an off row rather than as a gap.

**Rationale.** The rescore buys back quantization error in the *ordering* of the
top-50, and stage 6's cross-encoder reranks that same top-50 from the text
anyway — it is a second, stronger ordering pass over the same candidates. Paying
6.1 GB to improve an ordering that is about to be discarded is the wrong trade
at this memory budget. If the SQ8 recall gap turns out to be large enough to
lose candidates *before* stage 6 sees them, that is a recall problem measured
against the brute-force baseline, and the lever is `ef_search`, not the rescore.

**Consequences.**

- Index recall against exact search gets measured on Day 3 and is the number
  that would reopen this. A gap under ~1 point at `ef_search=64` closes it.
- If it does reopen, the affordable form is fp32 for a *slice* — the S1 chunks
  only, ~15% of the corpus — not the whole index.
- `RAG_PIPELINE.md` stage 3's config block records `rescore_top: 0` with a
  pointer here, so the spec and the code do not disagree silently.

## ADR-018 — Embed batches are sorted by length; the build's memory dial was the padding, not the worker count

**Date:** 2026-08-15

**Context.** The full-subset build was OOM-killed twice (change log, 15 Aug).
Both post-mortems read the kernel's message and concluded that `--workers 8` was
too many for a 15 GB swapless box, leaving worker count and row count as two
unset dials and a night of wall clock to gamble on them. Neither dial had been
measured; the 300-row build was treated as the safe reference.

**Finding.** `dhvani/bench/build_memory.py` samples `VmRSS` for the build
process and every descendant, plus `MemAvailable`, for the length of a run. The
300-row reference build —
[`docs/results/2026-08-15-build-memory-300r-8w.json`](results/2026-08-15-build-memory-300r-8w.json)
— peaked at **11.61 GB with MemAvailable bottoming at 0.18 GB**. It did not
survive because it was small. It survived by 180 MB.

Worker footprint is what dominates, and it does not scale with rows: the shard
is fixed at 4,096 texts, so the 300-row build and the 15,000-row build ask the
same thing of a worker. `dhvani/bench/embed_memory.py` isolates one worker on
one shard —
[`docs/results/2026-08-15-embed-shard-memory.json`](results/2026-08-15-embed-shard-memory.json):

| arena | length-sorted | peak worker | rate |
|---|---|---|---|
| on | no (as built) | 2.30 GB | 45.9/s |
| off | no | 1.86 GB | 41.4/s |
| on | **yes** | 1.69 GB | **141.3/s** |
| off | **yes** | 1.69 GB | 127.9/s |

Sorting is the win, and it is unambiguous. The arena columns are *not* decided
by this table — one shard is too short a run to show what the arena does; see
the decision below.

The tokenizer pads each batch to its longest member. Chunk texts are p50 91
chars against a 2,000-char cap, so an unsorted batch of 32 pads thirty short
chunks up to one outlier and pays for a full-width forward pass on all of them.

**Decision.** `Embedder.encode` sorts its input by length, batches, and inverts
the permutation before returning.

The onnxruntime CPU arena is **off for the build**, on for the query path. The
single-shard table above says the arena is free once batches are sorted — 1.69 GB
either way — and that reading was wrong, because it gave a worker *one* shard.
The build hands each worker ~27 in sequence, and the arena never returns memory
to the OS, so a worker ratchets up to its largest allocation and holds it for the
rest of the run. Measured at the scale that actually exposes it, 2,000 rows x 4
corpora x 4 workers
([on](results/2026-08-15-build-memory-2000r-4corpora-4w.json),
[off](results/2026-08-15-build-memory-2000r-4corpora-4w-noarena.json)):

| | arena on | arena off |
|---|---|---|
| peak worker | 2.63 GB | 1.88 GB |
| peak workers combined | 9.87 GB | 5.79 GB |
| peak total | 11.34 GB | 7.08 GB |
| MemAvailable floor | 0.44 GB | 4.22 GB |
| embed rate | 294.1/s | 286.6/s |
| chunks / index bytes | 414,210 / 319,952,221 | identical |

2.5% throughput for 9.6x the headroom, and the index is byte-identical, so this
is a pure memory trade with no retrieval consequence. The query path keeps the
arena: one session answering one query at a time is exactly the case the arena is
good at. `--cpu-mem-arena` re-enables it for the build as this measurement's
ablation arm.

**The dial, set.** The constraint is
`parent + workers x per-worker < ~14 GB` with no swap. At `--workers 4` with the
arena off the full-subset build projects to 3.11M chunks and **~3.7 h** — from
414,210 chunks in 1,790 s at 2,000 rows, scaled by 7.5. More workers do not buy
throughput on this model, which is memory-bound rather than core-bound (the
module docstring's 36/71/61 passages-per-second thread sweep is the same
finding); 8 workers measured *slower* per chunk than 4. So the answer to "spend a
night on the full subset or cut rows" is neither: the full 15,000 rows fit in an
evening, and the thing that was actually wrong was never the row count.

Two parent-side reads were fixed in the same pass, both visible in the same
evidence file:

- `_rows_for` read the whole column set and then `take`-d the subset,
  materializing all 97,941 rows of the `passages` struct to keep ~15,000. Now
  filtered per `iter_batches` batch, so the cost is bounded by the batch rather
  than by the file — and it was a *fixed* cost, identical at 300 rows and at
  15,000, which is precisely why the small build looked survivable.
- The chunk-text accumulator was held across every corpus for BM25 and the
  phonetic vocabulary, ~1.1 GB at ADR-012's size, live in the parent during the
  exact window when eight workers are also resident. Read back from
  `chunks.parquet` after the workers exit instead. Parquet preserves write
  order, so the list is the same list in the same order the FAISS rows are in,
  asserted against `n_chunks`.

**Measured, 300 rows / 8 workers / hin, same box, before and after**
([after](results/2026-08-15-build-memory-300r-8w-sorted.json)):

| | before | after |
|---|---|---|
| peak parent | 2.73 GB | 0.93 GB |
| peak single worker | 2.33 GB | 1.73 GB |
| peak total | 11.61 GB | 8.62 GB |
| MemAvailable floor | 0.18 GB | 3.36 GB |
| embed rate | 98.3/s | 259.5/s |
| wall clock | 215 s | 90.6 s |

**Consequences.**

- **The chunk count moved once: 17,069 -> 17,153**, entirely in `s3_semantic`
  (3,346 -> 3,430). S3 cuts on similarity troughs between sentence vectors, and
  INT8 output shifts ~1e-2 with a batch's padding, so changing batch composition
  moves a few cut points. This is not a misalignment — `tests/test_embed_sort.py`
  pins row identity through the permutation — it is S3's documented sensitivity
  to what it is batched with.
- **Determinism holds where the ablation table needs it.** Same config twice is
  byte-identical across all 17,153 chunk texts, and `--workers 4` and
  `--workers 8` produce identical chunks, because shard boundaries are fixed at
  4,096 and the sort is deterministic within a shard. Worker count is not a
  variable the chunk store depends on.
- Any `MEASURED` chunk-count or index-size number taken before this change is
  superseded; the reference build in `index/` predates it.
- The rate improvement is not a wall-clock projection for the full subset until
  it is measured at 4 corpora — see the scaling run.

---

## ADR-019 — The build is checkpointed per corpus, and the index is assembled in a separate merge pass

**Date:** 2026-08-17

**Context.** The full-subset build has now been OOM-killed three times. The third
attempt (`index/full-build-2026-08-15T1526.log`, launched 15:26 on 15 Aug with
ADR-018's settings) got much further than the first two — two corpora finished
and the third died at shard 140 of 192 — and it died with the same signature the
log had been showing all along: `MemAvailable` starting at 6.2 GB on corpus 1,
2.7 GB on corpus 2, and 1.3 GB by the middle of corpus 3. ADR-018 fixed what a
*worker* costs. What was left is a different quantity: what the **parent**
accumulates as corpora go by.

Three things grew across the loop and none of them were freed until the process
exited:

1. the FAISS index, added to per corpus — 528 bytes/vector, so 1.6 GB by the
   last corpus of ADR-012's subset, resident while the workers are live;
2. the corpus's vector array, 1.2 GB of anonymous memory the parent holds for
   the whole embed pass;
3. whatever the allocator had already ratcheted up to and had no reason to give
   back — the same class of behaviour as onnxruntime's arena in ADR-018, one
   level up.

None of these is a per-corpus cost that a smaller `--rows` fixes. They are a
cost that grows with *how much of the build one process has already done*, which
is why the run survives corpus 1, gets tighter through corpus 2, and dies in
corpus 3 — and why "cut the row count" would have bought the wrong thing.

**Decision.** The build embeds one corpus per **part**, checkpointed to disk, and
assembles FAISS, the chunk store, BM25 and the phonetic vocabulary in a **merge
pass** that runs after every embed worker has exited. Parts already on disk are
skipped, so the build can be split across as many processes as it needs:

```
python -m dhvani.build.build_index --langs hin --no-merge --out index/full
python -m dhvani.build.build_index --langs ben --no-merge --out index/full
python -m dhvani.build.build_index --langs tam --no-merge --out index/full
python -m dhvani.build.build_index --out index/full        # eng, then merge
```

The plain single command still builds everything in one process. The split is
available, not mandatory, and both routes run the same code — the single-process
build writes and merges the same parts.

What the split actually buys is the one thing no amount of in-process care can:
**a process exit returns memory to the kernel.** Every corpus starts with the
same headroom as the first, rather than with whatever the previous three left
behind. The failure mode that has killed this build three times is cumulative,
so the fix has to be a boundary the accumulation cannot cross.

**Details that had to be right.**

- **Vectors go to a `np.memmap`, not an array.** `embed_parallel` takes an
  optional preallocated destination and the corpus build passes a memmap, so the
  1.2 GB lands in page cache — evictable — instead of in the parent's heap next
  to four ~1.9 GB workers.
- **Parts are published atomically.** Both files are written under a `.tmp` name
  and renamed. A run the OOM killer takes mid-corpus leaves nothing the next run
  would mistake for a finished part, and `state.json` records a corpus only after
  its files are on disk. This is the property that makes a killed build cost one
  corpus instead of all of them.
- **Merge order is the language order, never the build order.** Row order is the
  join key across all three artifacts — chunks row *i* is FAISS id *i* is BM25
  doc *i* (`retrieve/stage3.py`) — so a merge that ordered parts by whichever
  finished first would build a clean index that retrieves the wrong passage.
  `tests/test_build_parts.py` asserts hin-then-ben and ben-then-hin produce the
  same ordering.
- **The SQ8 quantizer now trains on a stride across every part.** It previously
  trained on the head of the first corpus, which is one language — the code
  carried a comment flagging exactly that. Free to fix once every part is on
  disk before training starts.

**Measured, 2026-08-17.** 60 rows x {hin, ben}, `--workers 2`: one process, and
the same build split across two processes, produce **byte-identical**
`parts/*.npy`, `chunks.parquet`, `phonetic_vocab.json` and BM25 index, and the
same 7,173 chunks / 1,198 passages / 5,792,087 index bytes. The split is
invisible in the artifacts, which is the only claim that makes it safe to use.

**Known non-determinism, and it is not the split's.** Two merges of the *same*
parts in the *same* process produce different `hnsw_sq8.faiss` bytes (same size,
same `ntotal`). FAISS's HNSW construction is not deterministic under its
parallel `add`. Pre-existing, unrelated to parts, and it does not affect the
chunk store or BM25 — but ADR-018's "byte-identical index" claim covers the
chunk store, not the graph, and the ablation table needs to know that dense
recall can wobble between builds of identical inputs. Left open: if the ablation
deltas turn out to be within that wobble, `add` gets pinned to one thread.

**Consequences.**

- The parts are ~4.8 GB of fp32 vectors on disk at ADR-012's subset size, kept
  after the merge. They cost nothing at query time and make a re-merge free —
  which matters, because merge parameters (`M`, `ef_construction`) are now
  changeable without re-embedding.
- `--rebuild` re-embeds a corpus that already has a part; without it, parts are
  skipped. A change to chunking or the model needs `--rebuild` or a clean
  `--out`, and forgetting that yields an index built from stale vectors. The
  manifest records the model, which is the check that catches it.
- ADR-017 stands: those fp32 vectors are a build-time artifact on disk, not a
  query-time rescore in RAM.

## ADR-020 — `chunk_id` carries the language; the index row is the join key

**Date:** 2026-08-18

**Context.** `chunk_id` was `doc_id:passage_idx:strategy:ordinal`, and `doc_id`
is the dataset's `query_id` — which identifies *the same row in every language
file*. MSMARCO-XI is parallel: row 290643 exists in hin, ben, tam and the English
pivot, with the same passage index and the same strategies applied to it. So the
four corpora produced four chunks with one id.

Found on 18 Aug while wiring gold-label matching into the benchmark harness. The
full index holds **3,278,022 chunks and 969,298 distinct `chunk_id`s** — every id
addresses 3.4 rows on average.

Nothing about retrieval was wrong: stage 3 joins on row order, and row order was
correct (`test_build_parts.py`). What was wrong is the *identifier stage 3 hands
back*. Downstream, `chunk_id` is what stage 7 dedupes on, what a citation points
at, and what `overlap_with` names — and all three would have treated the Hindi,
Bengali, Tamil and English copies of a passage as one chunk. A citation would
have resolved to four passages in four scripts, and stage 7's dedupe would have
collapsed genuine cross-lingual evidence into a single entry.

**Decision, in two parts.**

1. **The language is part of the id**: `lang:doc_id:passage_idx:strategy:ordinal`,
   built in `chunk.py:_chunk` from the meta the chunker already carries. Ids
   inside `overlap_with` get the same prefix — neighbours are always in the same
   corpus as the chunk that names them.
2. **The index row, not the id, is the join key inside the pipeline.**
   `ScoredChunk` now carries `row`, and stage 3 fills it. An id is a stable,
   human-readable *name* for a chunk, safe to log, cite and compare across
   builds; a row is what fetches its text, metadata or label. Conflating the two
   is what made the collision invisible for three days — every internal lookup
   happened to use rows already, so nothing failed loudly.

**Why not re-embed.** The ids are a deterministic function of columns already in
the parquet, and row order is untouched by renaming a string. A migration pass
rewrote `chunk_id` and `overlap_with` in the four parts and the merged chunk
store, streamed row group at a time and published atomically. 3.1 h of embedding
was not spent again to change a prefix. A fresh build produces exactly the same
ids from `chunk.py` — the migration is for the artifacts already on disk, not a
permanent code path, so it is not committed.

**What the tests now hold.** `test_chunk.py` asserts the same dataset row in two
languages produces two ids, and that `overlap_with` carries the prefix.
`test_build_parts.py` asserts the built index has no duplicate id at all — the
invariant, checked against the real artifact rather than a synthetic one.

**Consequences.**

- Ids are ~9 characters longer, which the chunk store's zstd absorbs.
- Any result file or log written before 18 Aug quotes short ids. None had been
  published outside `docs/results/` at the time of the fix.
- The benchmark harness computes gold matches by row and reports same-language
  and any-language recall separately. That distinction only exists because the
  collision forced the question of what a hit in another language *means* — and
  it turns out to be one of the more interesting numbers a multilingual index
  can report.

## ADR-021 — Boundary A is published as a floor while stages are missing, and recall is published twice

**Date:** 2026-08-18

**Context.** The first benchmark of record ran on 18 Aug against the full index,
with only stage 3 built. Two reporting questions had to be answered before any
number left the JSON, and both have a comfortable dishonest answer.

**Decision 1 — a partial pipeline reports a floor, never a headline.** Boundary A
is defined as the span from final transcript to selected context, over stages 4,
3, 5, 6, 7 plus guardrails. Today it contains stage 3 and the harness. The
measured 136.47 ms P50 is therefore a *lower bound* on the finished pipeline and
is labelled as one, in the evidence file (`boundary_a_covers`,
`not_yet_in_boundary_a`) and everywhere it is quoted. It is not compared to the
200 ms target, and no "we are at 136 ms of our 200 ms budget" claim is made from
it — the stages still to be added include the reranker, which the budget expects
to be the single most expensive one.

The comfortable alternative is to quote the number and mention the caveat
quietly. A judge who notices a 200 ms target being met by a pipeline missing
four of its five stages will discount every other number in the submission, and
they would be right to.

**Decision 2 — recall is reported same-language and any-language, both.** The
index is parallel: the same passage exists in Hindi, Bengali, Tamil and English.
A Hindi query that retrieves the English copy of its gold passage has done
something useful and something different from retrieving the Hindi one. Neither
number alone is the truth:

- same-language only understates a multilingual retriever;
- any-language flatters it, and would let a system that ignores the query
  language entirely score well.

Both are computed over the same run and printed side by side (`recall@10`,
`recall@10_any_lang`). The gap between them — 0.0173 fused, 0.0311 dense-only,
0.0000 for BM25 — is itself the measurement of cross-lingual transfer, and it is
more informative than either column.

**Decision 3 — quality is computed over gold-bearing queries, latency over all
of them.** 44.97% of validation rows have no selected passage (`DATASET.md`).
Including them in recall caps it at 0.55 by construction; excluding them from
latency removes the refusal path, which is a real and fast query shape. The
query set is one file with `has_gold` per row and the harness applies the split,
so the two populations cannot drift apart the way two files would.

**Consequences.** The results section of `LATENCY.md` will grow rather than be
rewritten: each stage that lands adds its rows and moves boundary A up. The
floor labelling is removed only when boundary A contains every stage its
definition names.

## ADR-022 — BM25 top-k is selected over scored candidates, not over the corpus

**Date:** 2026-08-18

**Context.** The first benchmark of record put boundary A at **136.47 ms P50**,
and **134.0 ms of it was `bm25s`** while dense search over the same 3.28M
documents took 0.43 ms. A three-hundred-fold gap between two retrievers over one
corpus is not a property of lexical search; it is a bug somewhere.

A profile found it in one line. `bm25s.retrieve` computes scores quickly
(`get_scores` ≈ 2 ms) and then calls `bm25s.selection._topk_numpy`, which runs
`np.argpartition` over **the entire score array — all 3,278,022 documents** —
and allocates a 26 MB index array to do it. 117 of 126 profiled ms were that one
call.

**What it was not.** `n_threads` was pinned to 1 in `_lexical` and that looked
like the obvious dial. Measured: 1 / 2 / 4 / 8 threads gave 134 / 140 / 137 /
132 ms. `bm25s` parallelizes *across queries in a batch*, and a live query is a
batch of one, so the knob does nothing for this workload. Changing it and
declaring victory would have shipped a no-op with a plausible story attached.

**Decision.** `HybridIndex._lexical` calls the library's own `get_scores`, then
selects the top-k over `np.flatnonzero(scores)` — the ~114,000 documents the
query's terms actually touch — instead of over the full array.

Scoring is unchanged and still the library's. Only the selection is ours, and it
is the same algorithm applied to a smaller set: `argpartition` on the candidates,
then a sort of the k survivors.

**Measured, 2026-08-18** (500 queries, 3 reps, warm, dev box —
`docs/results/2026-08-18-bench-stage3{,-bm25fix}.json`):

| | before | after |
|---|---|---|
| boundary A P50 | 136.47 ms | **13.30 ms** |
| boundary A P100 | 198.53 ms | **21.60 ms** |
| stage 3 retrieve P50 | 132.46 ms | **9.29 ms** |
| first query, unwarmed | 179.06 ms | **48.09 ms** |

**Two behaviour changes, both deliberate.**

1. **Zero-score documents are dropped.** `bm25s` fills its k with documents that
   matched nothing when fewer than k match. Those entered the RRF fusion as
   candidates and displaced real ones. Removing them is why recall@10 rose
   0.4048 → 0.4118 and MRR@10 0.1840 → 0.1917 in a change that was supposed to
   be latency-only. `tests/test_stage3.py` compares against the library's
   non-zero-scoring results specifically, so the padding is excluded on purpose
   rather than by accident.
2. **Ties break by ascending row id.** `bm25s` leaves tied documents in
   arbitrary order. The set is identical either way, but a fixed order is what
   makes two runs of one config produce the same fused ranking — which
   `LATENCY.md`'s determinism claim and every ablation delta depend on.

**Why not a different library, or a sparse index of our own.** Neither is
needed: the scores were already fast and correct, and the corpus is unchanged. A
swap would have cost a dependency, a rebuild, and a new set of numbers to
justify, to fix a selection step that is ten lines. If lexical latency becomes
the constraint again — it is still 70% of stage 3, at 9.2 ms against a 6 ms
budget — the next lazy step is a smaller candidate pool via static pruning of
the highest-document-frequency terms, measured before it is claimed.

## ADR-023 — The lexical tokenizer's pattern is ours, and it includes the Indic block

**Date:** 2026-08-18

**Context.** `bm25s.tokenize` defaults to `token_pattern=r"(?u)\b\w\w+\b"`.
Python's `\w` matches letters, digits and underscore — **not** combining marks
(category `Mn`), which is what Devanagari, Bengali and Tamil use for every vowel
sign, virama and nukta. Each mark therefore ended a token:

```
'कंप्यूटर क्या है'      -> ['टर']
'मुंबई में कितने लोग'    -> ['बई', 'तन']
'সৌরজগতের গ্রহ কয়টি'  -> ['রজগত', 'রহ', 'কয']
'கம்ப்யூட்டர் என்றால்'   -> ['கம', 'டர', 'என']
'what is a corporation' -> ['what', 'corporation']      ← English was fine
```

Three of the four corpora were lexically indexed as syllable debris. This was
invisible in every check the project had: the build reported 172,015 vocabulary
terms without saying they were fragments, BM25 returned results for every query,
and the fixture retrieval test passes on English. It surfaced only when stage 4
needed to ask "is this term in the corpus vocabulary?" and the answer for
Devanagari was gibberish.

**Decision.** `TOKEN_PATTERN = r"(?u)[\wऀ-෿]{2,}"`, defined in
`dhvani/build/chunk.py` beside `normalize()` and imported by both the build
(`build_bm25`) and the query path (`stage3._lexical`).

One definition, for the same reason `normalize()` has one: a tokenizer that
differs between index time and query time produces an index nobody can query and
no error message anywhere. The Unicode range covers Devanagari through Sinhala,
which is every Indic script this project could plausibly add, not just the three
it indexes.

**Measured, 2026-08-18** (500 queries, 3 reps, BM25-only arm):

| | before | after |
|---|---|---|
| recall@10 | 0.2284 | **0.3875** (+70%) |
| MRR@10 | 0.1238 | **0.2101** (+70%) |
| nDCG@10 | 0.2469 | **0.4007** (+62%) |
| vocabulary terms | 172,015 | **779,413** |
| BM25 index | 378 MB | 535 MB |

Fused retrieval went 0.4118 → 0.4567 recall@10 on the same change.

**Cost.** A 36-second rebuild of the BM25 index from `chunks.parquet`. No
re-embedding, no re-chunking, no FAISS rebuild — which is a property of ADR-019's
merge split, where each artifact is built from the parts independently.

**Consequences.**

- Every BM25 number measured before 18 Aug 21:10 is void. The pre-fix result
  files stay on disk labelled as such, because the delta is the evidence.
- The earlier reading that "lexical retrieval transfers across scripts not at
  all" survives the fix — it was true for the right reason, not because of the
  bug.
- `tests/test_chunk.py` now asserts the pattern keeps Indic words whole. A
  tokenizer regression is otherwise silent, which is exactly how this one lasted
  three days.

## ADR-024 — Stage 4's defaults come from its own ablation, and it does not yet pay for itself

**Date:** 2026-08-18

**Context.** Stage 4 rewrites the query before retrieval: Unicode and digit
normalization, then phonetic correction of out-of-vocabulary terms against the
corpus vocabulary keyed by `soundex(term)[1:]`. The design is
`RAG_PIPELINE.md`'s. What it is *worth* was never measured, and the ablation
harness exists precisely so that question gets an answer instead of an argument.

Measuring it needed a corrupted query set — the dataset's queries are clean text,
and a repair stage tested on undamaged input measures only its own side effects.
`queryset.py --garble` drops combining marks and interior characters from 35% of
words, corrupting 393 of 500 queries.

**Measured, 2026-08-18** (500 queries, 3 reps, `min_term_len` 5 vs stage 4 off):

| | stage 4 on | off | delta |
|---|---|---|---|
| clean, recall@10 | 0.4464 | **0.4567** | −0.0103 |
| clean, MRR@10 | 0.2323 | **0.2342** | −0.0019 |
| garbled, recall@10 | **0.3599** | 0.3564 | +0.0035 |
| garbled, MRR@10 | **0.1813** | 0.1709 | +0.0104 |
| garbled, nDCG@10 | **0.3783** | 0.3447 | +0.0336 |

**Decision 1 — `min_term_len` is 5, from the sweep and not from the spec.** Four
configurations were run on both sets:

| config | clean recall@10 | garbled recall@10 | corrections (clean/garbled) |
|---|---|---|---|
| `min_term_len` 3 (the spec's implicit default) | 0.4360 | 0.3495 | 152 / 429 |
| `min_term_len` 3, `max_edit_distance` 1 | 0.4394 | 0.3495 | 102 / 322 |
| **`min_term_len` 5** | **0.4464** | **0.3599** | 82 / 245 |
| `min_term_len` 5, `max_edit_distance` 1 | 0.4464 | 0.3599 | 67 / 204 |

Short tokens are where a phonetic code carries the least signal and a wrong
correction costs the most. The edit-distance bound turned out not to matter once
the length floor was in place, so it stays at the spec's 2.

A `min_phonetic` floor was also added and measured: it changes nothing, because
every candidate that wins on edit distance already passes `Soundex.compare()`.
It is kept as an explicit knob rather than removed, because "we checked and it
was already true" is worth being able to re-check when the vocabulary changes.

**Decision 2 — the honest verdict is published, not buried.** On clean
transcripts stage 4 *costs* 0.0103 recall@10. Every correction applied to a
correct query is damage, and an out-of-vocabulary term in clean text is usually a
rare proper noun that was already right. On corrupted transcripts it buys
0.0035 recall@10 and 0.0104 MRR@10 — real, small, and one-tenth of what
`ef_search` 256 buys for free.

So the stage stays implemented, stays in the ablation table, and its default-path
membership is **`OPEN` until Day 5**, when Sarvam's output replaces a synthetic
garbler. The corruption model here is a controlled defect chosen to be
recoverable; a real STT error distribution may be kinder or much worse, and that
measurement is the one that decides.

**Known ceiling, marked in the code.** Blocking is on the exact tail code, so a
matra dropped mid-word changes the code itself (`कंप्यटर` → `NMOIP00` against
`कंप्यूटर` → `NMOCIP0`) and the term is never a candidate. The upgrade is a
deletion-neighbourhood index over the codes, SymSpell-style, at roughly 2M extra
keys. It is not built, because the measured catch rate does not yet justify the
memory — and building it first would have been optimizing a stage whose value is
still `OPEN`.

---

## ADR-025 — Chunk text is read through an mmap'd parquet column, never held resident

**Date:** 2026-08-19

**Context.** Stage 7 needs the text of a retrieved row, and so does every
citation the UI renders. `chunks.parquet` is 352 MB on disk but 1.20 GB of text
and 0.98 GB of `parent_text` uncompressed (`MEASURED 2026-08-19`, from the
parquet column metadata). R5 already flagged that the 8 GB Lightsail box is
tight, not generous: FAISS alone is 1.73 GB.

Three ways to get text for a row:

1. **Load the text column into RAM at boot.** Simplest. Costs 2.2 GB resident on
   top of FAISS, before the process has answered anything.
2. **Read the row group on demand.** Costs nothing resident, but a row group is
   50,000 rows and `read_row_group` measured **36.2 ms**. Six citations can land
   in six different groups, which is 217 ms — boundary A's whole budget, spent
   on text lookup.
3. **Map the file and let the kernel page in what is touched.**
   `pq.read_table(..., memory_map=True)`.

**Decision.** Option 3. R5 listed "chunk text to an mmap'd store" as a held
lever for when memory got tight; pyarrow gives it away for a keyword argument,
so it is taken now rather than kept in reserve. Resident cost is the handful of
pages a query actually touches, and the kernel reclaims them under pressure
instead of the process being OOM-killed — which this project has already been
three times (ADR-018, ADR-019).

**Measured, 2026-08-19** (dev box, `index/full`, 3,278,022 rows, 500 queries x
3 reps, warmed): 0.3 s to map the table, and stage 7 end to end at **P50
1.31 ms / P100 20.14 ms** against its `TARGET 5 ms`
([`2026-08-19-bench-stage7.json`](results/2026-08-19-bench-stage7.json)).

**Consequence — the tail is real and it is not a cold start.** P50 is four times
inside the target; **P100 is four times outside it**. The first guess was that
this was the page cache warming, and that guess was wrong: the P100 is
20.1 / 23.7 / 20.1 ms across the three reps, so it recurs in every warmed rep
rather than decaying. The cause is the design working as described — 500
distinct queries touch a wide scatter of a 2.2 GB mapped region, so some lookups
fault from disk no matter how warm the process is. Option 1 would not have this
tail; it would have 2.2 GB of resident memory instead, on a box that has been
OOM-killed three times.

This is a deliberate trade of P100 for headroom, and it is published rather than
smoothed: **stage 7 misses its own TARGET at P100.** Boundary A absorbs it —
13.50 ms P50 / 33.44 ms P100 with stage 7 against 12.14 / 18.29 without it,
still an order of magnitude inside the 200 ms target — so the tail is affordable
today. If it stops being affordable, the lever is to hold only the `text` column
resident (1.20 GB) and leave `parent_text` mapped, which is one argument.

---

## ADR-026 — The token budget is counted with a proxy tokenizer, and it is labelled as one

**Date:** 2026-08-19

**Context.** `RAG_PIPELINE.md` stage 7 says the token budget is counted "with the
generation model's tokenizer, not a word-count approximation" — because a word
count is wrong by a factor that varies by script, and Devanagari and Tamil are
exactly where it is worst.

Sarvam publishes no downloadable tokenizer for `sarvam-m`. So the instruction
cannot be followed literally. The options were a word count (wrong, and the doc
already rejects it), a remote token-count call (a network round trip inside
boundary A — absurd), or a different tokenizer that is at least a real subword
tokenizer trained on these scripts.

**Decision.** Count with `multilingual-e5-small`'s tokenizer, which is already
loaded in-process for the embedder, and **carry the name in the trace** as
`multilingual-e5-small (proxy)`. Every response and every benchmark row says
which tokenizer produced the count, so no table can imply the generation model's
own.

**Consequence.** The budget is approximate. It is a *budget*, not a hard provider
limit — overshooting costs generation latency, not a rejected request — so the
approximation is affordable in a way it would not be if we were packing to the
model's context ceiling. `max_tokens` on the provider call is the real bound.

**Upgrade path.** One line: if a provider ships a `tokenizer.json`, point
`TokenCounter` at it. The label changes with it.

---

## ADR-027 — The end-to-end slice ships before stages 5 and 6

**Date:** 2026-08-19

**Context.** On 19 Aug, with three days left, the project had an excellent
retrieval core and no product: no context selection, no generation, no UI, no
deployed link. Boundary A was 12.37 ms against a 200 ms target — sixteen times
under — while three submission requirements (a live link, a demo video, social
posts of that video) had nothing to point at. Stages 5 (RM3 expansion) and 6
(cross-encoder rerank) were the next items in the plan's order.

**Decision.** Build stage 7 + generation + `/ask` + the UI first, and defer
stages 5 and 6 behind them.

**Reasoning.** The remaining work divides into things that improve a number
already sixteen times inside its target, and things that turn zero into
non-zero. Stage 6's own budget line is 60 ms — it would spend a fifth of the
target to raise recall from 0.4913, and it cannot be demonstrated, filmed, or
submitted. The slice can. A demo video is unfilmable without a UI, and it is a
hard requirement with a fixed date; recall is a soft number with no floor in the
brief.

The order also de-risks: deploying on Day 7 as originally planned means the
first deployment attempt happens with one day of slack. Deploying a working
slice on 20 Aug means the deployment problems surface with three.

**Consequence.** The published pipeline is 4 → 3 → 7, and `boundary_a_covers`
says exactly that in `/health`, in every `/ask` response, and in every benchmark
file. The measured boundary A remains a **floor**, not a target comparison
(ADR-021), and the ablation table will have fewer rows than `RAG_PIPELINE.md`
describes. Stages 5 and 6 stay specified, toggleable and unbuilt; if a day
frees, 6 lands first, because R2 already holds its levers.

**Rejected.** Building stage 6 first "because the pipeline should be complete
before it is shown". A complete pipeline nobody can see is worth less at a
deadline than an incomplete one that answers questions on a public URL.

---

## ADR-028 — What contact with the live providers changed

**Date:** 2026-08-19

**Context.** The generation client was written and fully tested against
`httpx.MockTransport` while B1 was open. The keys arrived on 19 Aug. Every
assumption the mock could not test turned out to be wrong, and this ADR records
all five rather than quietly patching them.

**1. Both model ids were dead.** `sarvam-m` (from ADR-003's era) returns
`Model 'sarvam-m' has been deprecated`. Groq no longer serves
`llama-3.3-70b-versatile` at all. Ids are now taken from each provider's live
`/v1/models` and the ones in use are `sarvam-105b-conversations` and
`qwen/qwen3.6-27b`. Note that ADR-009 costed "Sarvam 105B" all along — the
client's default was simply the wrong string.

**2. Auth: both headers are accepted, B5 closes.** Sarvam's OpenAI-compatible
route takes `Authorization: Bearer`. Sending `api-subscription-key` alongside it
is ignored rather than rejected, so the belt-and-braces header stays — it costs
nothing and covers Sarvam's other endpoints when STT lands.

**3. Reasoning is the dominant latency term, and it is per-provider.**
`sarvam-105b` reasons before every answer and cannot be told not to:
`reasoning_effort: low` and `chat_template_kwargs: {enable_thinking: false}` are
both accepted and both ignored. It spent 2,384 characters and **5.5 s** before
the first visible token, and at the original `max_tokens: 512` it spent the
entire budget on the scratchpad and returned **no answer at all**.

| | model | reasoning | first live answer |
|---|---|---|---|
| Sarvam | `sarvam-105b` | always on, not disableable | 5.5 s, or nothing at 512 tokens |
| **Sarvam** | **`sarvam-105b-conversations`** | **none** | **0.94 s** |
| Groq | `qwen/qwen3.6-27b` | on by default | ~10 s |
| **Groq** | **same + `reasoning_effort: "none"`** | **none** | **0.34–0.61 s** |

So: the conversations variant on Sarvam, and a per-provider `extra_body` on the
`Provider` record carrying Groq's switch. `max_tokens` also went 512 → 2048, so
that a model which reasons anyway still has room to answer afterwards.

The client keeps handling reasoning in both forms regardless — Sarvam streams it
in a separate `reasoning_content` field, Qwen inlines `<think>` tags in
`content` — because a model that starts reasoning again after a provider-side
change must not put its scratchpad on screen.

**4. The refusal marker is not emitted alone, and its presence is not a
refusal.** The system prompt says to reply with exactly `INSUFFICIENT_CONTEXT`
and nothing else. Observed, in one afternoon: a sentence of prose *then* the
marker; the marker wrapped as `<INSUFFICIENT_CONTEXT>`, which stripping the bare
token left on screen as `<>`; and — the interesting one — **a complete, correctly
cited answer followed by the marker used to decline a sub-question the model had
inferred on its own.**

The first two are stripping bugs and are fixed (bracketed variants first, and
the strip runs before the stream's holdback cut rather than after, so a marker
straddling the cut is still caught). The third changed a decision: the marker is
now a **reported signal**, not an instruction. It appears on the `done` event as
`model_signalled_insufficient`, and the refusal *event* fires only when no
substantive answer survives. Obeying the marker unconditionally threw away a
correct three-source answer.

A marker-only stream is still a first-class refusal and must not be mistaken for
an empty one — otherwise the ladder falls through to the fallback provider and a
correct refusal gets reported as an outage.

**5. "Answer in the language of the question" does not hold on its own.** A
Bengali question came back answered in Hindi, because the retrieved passages
were mostly Hindi and the model followed the context rather than the question.
The detected language from stage 4 is now named explicitly in the user turn
("Answer in Bengali (Bengali script), regardless of which language the sources
are written in"). Verified: the same question now answers in Bengali.

**Consequence — a latency risk worth watching.** Sarvam's spread is wide:
0.95 s, 4.70 s and 16.04 s on three consecutive identical requests. Groq's was
0.34–0.61 s. This affects boundaries B and C only, which are reported and not
targeted, so nothing about the headline number changes — but a 16 s demo take is
a bad demo take, and the read timeout is 25 s. If the spread persists on the
Mumbai box, the honest options are to raise Groq to the default path (against
ADR-009's one-vendor and region reasoning) or to publish the spread. **Not
decided today** — one afternoon of samples from a dev box is not evidence.

---

## ADR-029 — Batch speech-to-text now, streaming deferred; and a first-token deadline

**Date:** 2026-08-19

### Batch, not streaming

**Context.** `DESIGN.md` specifies `WS /ws/stt`: `MediaRecorder` opus chunks over
a WebSocket, partial transcripts rendered live and fed to speculative retrieval,
so candidates exist before the user stops talking. That is the right design and
it is not what shipped today.

**Decision.** `POST /stt` takes the whole recording once the user stops.
Streaming stays specified and unbuilt.

**Reasoning.** What streaming buys is overlap between talking and retrieving —
entirely a boundary C improvement, and boundary C is reported, not targeted.
What it costs is a second transport, partial-transcript state, cancellation of
speculative work when the transcript changes, and a UI that renders text which
may be retracted. Boundary A does not move by a microsecond either way: its
clock starts at the *final* transcript in both designs.

With three days left and the demo video unshot, the correct order is the one
that makes voice work at all. `Transcript.is_final` already exists, so the
streaming path adds a value to a field rather than a field to a contract.

**Consequence.** The user waits for the whole clip to upload and transcribe.
Measured live: **1.7 s for a 4-second Hindi clip, 2.3 s for English** including
network. Perceptible, not painful. Streaming would hide most of it.

**Also decided here:** the transcript lands in the text box and is *shown* before
anything is asked, rather than firing straight into `/ask` invisibly. STT gets
proper nouns wrong — that is the entire reason stage 4 exists — and a user who
can see "heard: X" can fix it. This costs nothing and is the difference between
a demo that recovers from a mis-hear and one that does not.

### A first-token deadline, because both providers have heavy tails

**Context.** Generation latency was not the stable number the first samples
suggested. Across the afternoon, the same request:

| | samples | observed |
|---|---|---|
| Sarvam | 3 + 6 | 0.95 s, 4.70 s, 16.04 s, 33.65 s … then six runs at 1.03–1.44 s |
| Groq | 3 + 6 | 0.34–0.61 s … then a 15.79 s outlier in six runs |

The first three samples said "Sarvam is erratic, Groq is steady" and that was
about to become a recommendation to change the default provider. Six more
samples each said the opposite for Groq. **Both providers have fat tails, and a
vendor flip would have been a decision taken on nine samples of noise.** ADR-009
stands untouched.

**Decision.** Bound it instead. `read_timeout_s` 25 s → **10 s**. httpx's read
timeout is per-read, so in a streaming call it behaves as a first-token
deadline: nothing is read until the provider emits its first frame, and once
tokens flow the inter-token gap is milliseconds. A provider that has produced
nothing in 10 s raises `ReadTimeout` while `first` is still true, and the ladder
falls through to the other provider cleanly — the mechanism already existed and
was simply tuned too loose to fire.

**Consequence.** Worst case for a user is now ~10 s to first token plus the
fallback's own time, instead of an unbounded stare. A genuine 10-second gap
*mid*-answer still fails the stream rather than retrying, because a retry would
duplicate text already on screen (ADR-028). The 33.65 s observation is what a
25 s timeout looks like when the provider is slow rather than broken: it was
inside the budget, so nothing fired.

**Not decided.** Whether either tail is a Mumbai-network artifact of this dev
box. Re-measure on Lightsail; that is the run of record.
