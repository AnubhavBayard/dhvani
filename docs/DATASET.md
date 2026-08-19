# Dataset — `ai4bharat/MSMARCO-XI`

Everything on this page is `MEASURED 2026-08-15` unless labelled otherwise.
Evidence: [`docs/results/2026-08-15-dataset-probe.json`](results/2026-08-15-dataset-probe.json),
produced by `python -m dhvani.build.probe_dataset`. Re-run it and the numbers
below must reproduce.

Method: parquet footers read over HTTP range requests (no download); field
distributions computed locally on downloaded validation files.

## What the dataset actually is

MS MARCO v2.1 machine-translated into 14 Indian languages. Each row is one
English query and its ~10 candidate passages, with the query, the answer, and
every passage rendered in the target language alongside the English original.

The `meta` column records the translation run itself — `model_name`,
`temperature`, `top_p`. The corpus is LLM output, and it carries LLM artifacts
(see *Data quality* below).

### Row schema — 10 logical fields, 17 parquet leaf columns

| field | type | notes |
|---|---|---|
| `query_id` | int64 | MS MARCO query id — stable across every language file |
| `query` | string | target-language query |
| `Eng_Query` | string | English original |
| `Answer` | string | target-language answer |
| `Eng_Answer` | string | English original |
| `query_type` | string | `DESCRIPTION`/`NUMERIC`/`ENTITY`/`PERSON`/`LOCATION` |
| `source_lang` | string | always `eng_Latn` |
| `target_lang` | string | FLORES code, e.g. `hin_Deva` |
| `passages` | struct of 3 lists | `English_passages`, `Translated_passages`, `is_selected` |
| `meta` | struct of 6 | translation-run parameters |

The datasets-server reports 10 columns (top-level features); the parquet files
report 17 (nested struct fields as leaves). Both are right; they count different
things.

## Files, splits, row counts

27 parquet files, 55.6 GB, **11,451,314 rows**. Every file is a **single parquet
row group** — which means there is no cheap partial read. Sampling a file over
HTTP downloads the whole file, so the build pipeline downloads once and works
locally.

### Validation — 14 files, 1,371,174 rows

Every language file has **exactly 97,941 rows**. All 14 languages present.

### Train — 13 files, 10,080,140 rows

| languages | rows each |
|---|---|
| asm, ben, guj, hin, kan, mal, pan, san, tam | 778,638 |
| ori | 782,282 |
| urd | 770,089 |
| mar | 765,873 |
| nep | 754,154 |

**Telugu has no train file.** `telval.parquet` exists; `teltrain.parquet` does
not. This is a property of the dataset, not of our download. It has a direct
consequence for chunking strategy S4, whose headers may only be built from the
train split (`CHUNKING.md`): **S4 is unavailable for Telugu.** Indexing Telugu
means one of the four strategies has a hole in it, and the ablation table has to
say so.

## The finding that shapes everything: the language files are row-aligned

The 14 language files are the *same* MS MARCO rows, translated. Verified on
`hin` vs `ben`:

- `query_id` sequence identical
- `query_type` distribution identical to the row
- `passages_per_row` identical (mean 9.98, min 1, max 27, 977,545 passages)
- `is_selected` distribution identical
- `English_passages` **byte-identical** in all 99 sampled positions

Consequences, all of them load-bearing:

1. **Indexing k languages does not give k× unique content.** It gives the same
   corpus k times over. Language count buys multilingual coverage, not corpus
   size, and the README must not imply otherwise.
2. **The English side is embedded once, not k times.** Deduplicating
   `English_passages` across languages is free and saves (k−1) × 977,545 vectors.
3. **Cross-lingual retrieval is measurable for free** — every target-language
   query has its English passage set in the same row. `embed_bench.py` reports it
   as a second recall column.

## Field distributions — validation split

Identical across languages (they are the same rows).

### `query_type`

| type | rows | share |
|---|---|---|
| DESCRIPTION | 52,912 | 54.02% |
| NUMERIC | 24,741 | 25.26% |
| ENTITY | 8,427 | 8.60% |
| PERSON | 6,206 | 6.34% |
| LOCATION | 5,655 | 5.77% |

### Relevance labels — 45% of rows have no gold passage

| | rows | share |
|---|---|---|
| ≥1 passage marked `is_selected` | 53,895 | 55.03% |
| **0 passages selected** | **44,046** | **44.97%** |
| >1 passage selected | 3,031 | 3.09% |

Mean selected passages per row: 0.587.

Matching this, **43,991 rows (44.92%) have `Eng_Answer` = "No Answer Present."**
(`कोई उत्तर नहीं मिला।` in Hindi). The two sets are effectively the same rows.

This is the single most important number for evaluation design: the usable
retrieval eval pool is **53,895 rows per language, not 97,941**. Any recall
figure computed over all rows would be capped at 0.55 by construction and would
be measuring the dataset, not the retriever. `embed_bench.py` filters to rows
with a gold passage before sampling, and the filter is reported in every result
file.

The no-answer rows are not waste — they are the natural negative set for
guardrail L3 (the abstain floor). A system that answers confidently on a query
whose own dataset says "No Answer Present" is hallucinating, and we now have
44,046 labelled examples of that per language.

### Passage and query lengths (characters)

Per language, over all 977,545 passages of the validation split.

| | p25 | p50 | p75 | p90 | p95 | p99 | max | mean |
|---|---|---|---|---|---|---|---|---|
| passages, hin | 242 | 292 | 349 | 472 | 552 | 708 | 21,390 | 324.0 |
| passages, ben | 236 | 285 | 343 | 467 | 549 | 722 | 12,127 | 313.9 |
| passages, tam | 275 | 334 | 404 | 552 | 653 | 995 | 12,844 | 379.4 |
| passages, English | 248 | 295 | 344 | 491 | 562 | 693 | 1,391 | 316.2 |
| queries, hin | 26 | 34 | 45 | 58 | 67 | 93 | 9,721 | 43.1 |
| queries, ben | 24 | 33 | 43 | 55 | 64 | 87 | 4,093 | 37.8 |
| queries, tam | 32 | 42 | 54 | 69 | 80 | 110 | 3,721 | 49.7 |
| answers, hin | 20 | 20 | 79 | 147 | 197 | 314 | 9,529 | 67.1 |
| answers, ben | 15 | 16 | 79 | 143 | 192 | 305 | 4,095 | 60.0 |

`CHUNKING.md` held this open pending measurement. It resolves in its favour:
**p95 passage length is ~550–650 characters** — comfortably inside a 512-token
window for every language measured. A fixed-token sliding window really is an
identity function on this corpus, so S1 stands as the baseline and the
sub-passage strategies (S2, S3) are where the chunking work is. The tail is
real but thin: p99 is under 1,000 characters and the outliers (max 21,390) are a
few hundred rows that need a hard cap, not a strategy.

Tamil runs ~15% longer in characters than Hindi for the same content, which
matters for the cross-encoder budget (stage 6 scales with sequence length) and
is why the reranker latency target must be measured per script, not once.

### Bytes on disk, not characters — the number ADR-010 got wrong

Indic scripts are 3 bytes per character in UTF-8. Chunk text is the largest
line item in index memory, so this is not a footnote.

| | mean UTF-8 bytes/passage | p95 | total for 977,545 passages |
|---|---|---|---|
| hin | 822 | 1,404 | 804 MB |
| ben | 834 | 1,457 | 815 MB |
| tam | 1,022 | 1,761 | 999 MB |
| English | 317 | — | 310 MB |

ADR-010 sized the index using "~400 bytes of chunk text each". The measured
figure is **2.1–2.6× that** for Indic text. See ADR-012 for the corrected
sizing and what it does to the subset.

## Data quality — the corpus is LLM output and shows it

Scanned all 977,545 Hindi validation passages and all 97,941 answers.

| pattern | passages | answers |
|---|---|---|
| English LLM refusal (`I can't fulfill…`, `as an AI…`) | 101 (0.010%) | 75 (0.077%) |
| leading apology (`I'm sorry`, `Sorry,`) | 3 | 0 |
| entirely ASCII — untranslated | 331 (0.034%) | 784 (0.800%) |
| prompt-injection phrasing (`ignore previous instructions`, `system prompt`) | **0** | **0** |

68 rows have the literal answer `I can't fulfill that request.` — the
translation model refused and the refusal was written into the corpus.

Two things follow:

1. **A build-time filter is justified and cheap.** Refusal artifacts and
   untranslated ASCII passages are ~0.04% of the corpus and are pure noise in an
   Indic retrieval index. Dropping them is a documented build step, not silent
   data massaging.
2. **Threat T5 needs synthetic adversarial data.** There is no naturally
   occurring corpus-borne prompt injection in this dataset — zero matches. The
   guardrail cannot be validated on the corpus as shipped, so `eval/adversarial.jsonl`
   must contain injected passages we author ourselves, and `GUARDRAILS.md` must
   say that the T5 catch-rate is measured against synthetic injections placed in
   a copy of the index, not against found examples.

## What we downloaded

`data/raw/validation/{hin,ben,tam}val.parquet` — 1.39 GB, gitignored. Chosen as
three different scripts (Devanagari, Bengali, Tamil) and two language families
(Indo-Aryan, Dravidian) so that every measurement above has a cross-script
check rather than a single-language result generalised.

## Open — closed on Day 3

- Per-language index memory once the four chunking strategies multiply the chunk
  count. ADR-012 sizes it from the measured bytes above; the multiplier itself is
  measured when the chunkers exist.
- Embedding throughput on the build host at full core count. The 2-thread figure
  in `docs/results/2026-08-15-embed-bench.json` is the deploy-box number, not the
  build-box number.
