# Chunking

## The dataset-shaped problem

MSMARCO-XI is not a corpus of documents. Each row is a query with ~10 candidate
passages, of which one or more is marked `is_selected`. MS MARCO passages are
already short — roughly paragraph length — which means the reflexive "split
documents into 512-token windows" does nothing here: most passages are smaller
than the window, so a naive splitter is an identity function that produces one
chunk per passage and calls it a strategy.

So "vast chunking" for this dataset cannot mean *cutting smaller*. It has to mean
choosing granularity in **both** directions — below the passage and above it —
and letting retrieval decide which granularity a query needs.

`RESOLVED 2026-08-15` — the length distribution is measured, in `DATASET.md`,
from `docs/results/2026-08-15-dataset-probe.json`. It resolves in favour of the
premise above: p50 passage length is **285–334 characters** and p95 is
**549–653**, per language, over all 977,545 validation passages. That is inside
a 512-token window for every language measured, so a fixed-token splitter really
is an identity function here and S1 stands as the honest baseline.

Two adjustments the histogram forces:

- **A hard cap is needed.** p99 is under 1,000 characters but the maximum is
  21,390 (Hindi). A few hundred outlier passages would otherwise dominate stage 6
  latency on their own. Cap at **2,000 characters** — above p99 for every
  language, below the tail — and route anything longer through S3, which is the
  strategy built to cut inside a passage.
- **Thresholds are per script, not global.** Tamil runs ~15% longer in
  characters than Hindi for identical content (p50 334 vs 292). Any threshold
  expressed in characters has to be set per script or expressed in tokens.

## Strategies

Four, all indexed into one store, discriminated by the `strategy` metadata field.

### S1 — `passage` (native, baseline)

One chunk per passage, unmodified. This is the dataset's own unit and the unit
its relevance labels are defined on, which makes it the honest baseline every
other strategy has to beat.

Params: none. Expected to win on short factoid queries where the answer is one
passage — MS MARCO's dominant case.

### S2 — `sentence_window`

Chunk = one sentence; retrieved unit = that sentence plus `w` neighbours on each
side. Embed narrow, return wide. The embedding is precise about what the sentence
says; the returned context still carries what surrounds it.

Params: `window: 1`, sentence splitter must be Indic-aware — Devanagari danda
(`।`), Tamil/Telugu punctuation, and the fact that `.` is not the sentence
terminator in several of these scripts. A Latin-only splitter silently produces
one chunk per passage here, which is S1 wearing a hat.

Expected to win on precise factoid queries — `query_type` `NUMERIC` and
`ENTITY` — where the answer is one clause inside a passage full of distractors.

### S3 — `semantic`

Split on embedding-similarity troughs between adjacent sentences rather than on a
character count. Consecutive sentences are embedded, cosine similarity between
neighbours is computed, and a boundary is cut where similarity drops below a
percentile threshold. Chunks follow topic shifts instead of arbitrary offsets.

Params: `breakpoint_percentile: 85`, `min_chunk_sentences: 2`,
`max_chunk_tokens: 384`.

Costs the most to build (an embedding pass over every sentence before chunking).
Expected to win on `DESCRIPTION` queries, where the answer spans several
sentences and cutting mid-explanation loses it.

### S4 — `query_context` (metadata-aware, dataset-specific)

The dataset hands us supervision most corpora don't have: `is_selected` marks
which passage actually answered the query, and `query_type` labels the question
category. S4 exploits it — the selected passage is chunked together with its
`Eng_Query`/`query` as a prefixed header, so the indexed unit carries the
question it is known to answer.

This is a real retrieval advantage, and also a real evaluation hazard: if the
eval queries are the same queries used to build these headers, we are retrieving
by memorized query text and the score is meaningless.

**Mitigation, non-negotiable: S4 header rows and evaluation rows are disjoint by
`query_id`, and the disjointness is asserted in `build_index.py` and tested.**

`REVISED 2026-08-16 (ADR-016).` This was previously specified as "headers from
the train split, eval queries from validation". The split boundary was only ever
a *proxy* for disjointness, and an expensive one — every file in this dataset is
a single parquet row group, so using train meant an 11 GB download to extract
45,000 rows. Validation has 97,941 rows and ADR-012 indexes 15,000; the other
82,941 are already on disk and are disjoint by construction. The assertion is now
on the property itself:

```python
assert not (s4_query_ids & indexed_query_ids)
```

which fails loudly if a later change to the sampler breaks it, where a split-name
check would not. Stated at length because a chunking strategy that leaks labels
would invalidate every retrieval number in the submission.

**The honest limitation.** S4 can only ever help when an evaluation query is
semantically near the header of a *different* row. It cannot retrieve its own
query — that is the whole point of the guard. So S4's ceiling is real but modest,
and if the ablation shows it winning no cell, the decision rule at the bottom of
this document applies and it gets cut.

Params: `header_template: "{query_type}: {query}\n\n{passage}"`,
`selected_only: true`, `source: validation rows held out from the index`.

~~**S4 is unavailable for Telugu.**~~ `WITHDRAWN 2026-08-16` — this followed from
S4 needing the train split, and it no longer does (ADR-016). The underlying fact
stands (`MEASURED 2026-08-15`: 13 train files, 14 validation files, no
`teltrain.parquet`), but it is no longer an argument about strategy coverage.
Telugu remains out of the subset on the grounds in ADR-012 that survive: script
coverage is already served by the chosen three, and each additional language
costs ~1.2 GB of index for content that is a translation of what is already
there.

**`selected_only: true` covers only 55% of rows.** `MEASURED 2026-08-15`: 44,046
of 97,941 validation rows have no `is_selected` passage at all, and the train
split has the same shape. S4 therefore indexes roughly half the corpus by
construction — it is a precision strategy layered over S1, never a replacement
for it.

### Deliberately rejected

- **Fixed-token sliding window.** The brief rejects it, and on this dataset it
  degenerates to S1 for most passages. It is implemented anyway, off by default,
  purely as the ablation baseline — "we tried the naive thing and here is the
  number" is worth more than asserting it would have been worse.
- **Recursive character splitting.** Its separator hierarchy is Latin-centric.
  S3 does the same job on semantics rather than on punctuation guesses.
- **Whole-row concatenation** (all 10 passages of a query as one chunk). Blows
  the token budget and buries the selected passage among nine distractors.

## Build-time filters

Applied before any strategy runs, so every strategy sees the same corpus.

| filter | drops | why |
|---|---|---|
| passage is entirely ASCII | 331 per language (0.034%) | untranslated source text; noise in an Indic index |
| passage matches an LLM refusal (`I can't fulfill…`, `as an AI…`) | 101 per language (0.010%) | translation-model artifact, not corpus content |
| passage longer than 2,000 characters | above p99 for every language | stage-6 latency outliers |
| duplicate `English_passages` across language files | (k−1) × 977,545 | the 14 language files are the same rows; the English side is byte-identical |

Counts are `MEASURED 2026-08-15` on the Hindi validation split
(`docs/results/2026-08-15-dataset-probe.json` and `DATASET.md`). Total dropped by
the first two filters is under 0.05% — small enough that the filter is honest
housekeeping rather than a thumb on the scale, and it is logged in the build
manifest either way.

The English dedup is the one that matters for sizing: without it, indexing k
languages embeds the same English corpus k times.

## Overlap policy

Overlap applies to S2 and S3, the two strategies that cut *inside* a passage and
can therefore sever an answer. S1 has no internal boundary to protect; S4
inherits S1's.

- S2: overlap is inherent — the window is the overlap. `w=1` means adjacent
  chunks share two thirds of their returned text.
- S3: `overlap_ratio` of the preceding chunk's trailing tokens is prepended to
  each chunk, snapped to a sentence boundary. Overlapping at an arbitrary token
  offset produces half-sentences that embed poorly.

**Tuning method.** Sweep `overlap_ratio ∈ {0, 0.10, 0.15, 0.25}`, rebuild the S3
index at each, and record recall@10, index size, and P50 HNSW search time on the
same query set. Choose the knee, not the maximum: overlap buys recall by
inflating the index, and index size is paid at every query for the rest of the
project. Default `TARGET 0.15` pending the sweep.

Every overlapping pair is recorded in `overlap_with`, which is what stage 7 uses
to dedupe. Overlap without dedupe just fills the context window with repeats.

## Chunk metadata schema

```python
class Chunk(BaseModel):
    chunk_id: str            # f"{doc_id}:{strategy}:{ordinal}" — stable, human-readable
    text: str                # what gets embedded and returned
    doc_id: str              # dataset query_id
    passage_idx: int         # position within the row's passage list
    strategy: str            # s1_passage | s2_sentence_window | s3_semantic | s4_query_context
    ordinal: int             # position within (doc, passage, strategy)
    lang: str                # BCP-47; row language
    script: str              # Deva | Taml | Telu | Beng | Latn | ...
    is_selected: bool        # dataset relevance label
    query_type: str          # DESCRIPTION | NUMERIC | ENTITY | LOCATION | PERSON
    split: str               # train | validation — enforces the S4 leak guard
    token_count: int
    char_span: tuple[int, int]   # offsets into the source passage, for citation highlighting
    overlap_with: list[str]      # chunk_ids sharing text, for stage-7 dedupe
    parent_text: str | None      # S2 only: the wider window returned at retrieval time
```

`char_span` exists so a citation can highlight the exact source span in the UI
rather than pointing vaguely at a chunk. Grounding that a user can see is worth
more than grounding they must trust.

## How four strategies coexist in one index

One HNSW index, one BM25 index, one chunk store. Strategy is a metadata field,
not a separate index.

Why one index rather than four:
- Four indexes = four searches per query, either serialized (4× latency) or
  parallel (4× memory, and a fusion problem anyway).
- Fusion across strategies is the *point*. A single ranked list lets S2's precise
  sentence and S3's spanning chunk compete directly on score.
- Ablation stays cheap: filtering by `strategy` at query time turns a strategy
  off without a rebuild.

Cost of the single-index choice: duplicated text across strategies inflates the
index, which costs HNSW search time. Bounded by (a) stage 7's dedupe, and (b) the
per-strategy ablation, which cuts any strategy that fails to earn its share of
the index. If total index size threatens the latency budget, the fix is dropping
a strategy, not sharding the index.

## Experiment — which strategy wins on which query type

**Setup.** Validation split, `OPEN — N` queries (sized in Phase 2 by what the
index build can cover), stratified by `query_type` and by language.

**Procedure.** For each query, retrieve with the index filtered to exactly one
strategy, then again unfiltered. Record recall@10, MRR@10, nDCG@10 per
`(strategy × query_type × language)` cell, plus P50 latency per strategy — a
strategy whose chunks are longer costs more in stage 6, where cross-encoder time
scales with passage length.

**Output table** (shape; filled by the run):

| strategy | DESCRIPTION | NUMERIC | ENTITY | LOCATION | PERSON | P50 (ms) |
|---|---|---|---|---|---|---|
| s1_passage | `PLACEHOLDER` | | | | | |
| s2_sentence_window | | | | | | |
| s3_semantic | | | | | | |
| s4_query_context | | | | | | |
| all fused | | | | | | |

**Decision rule, stated before seeing the data:** a strategy stays only if it
wins at least one `(query_type × language)` cell by a margin larger than the
run-to-run variance, or if fused retrieval measurably degrades when it is
removed. Strategies that survive on vibes get cut.

**Secondary output:** if a strategy wins cleanly on a query type that is cheap to
detect at query time, routing becomes possible — filter the index by strategy per
query. Only worth doing if the classifier is essentially free; a model call to
pick a chunking strategy costs more than the strategy saves.
