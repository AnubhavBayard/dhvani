---
title: dhvani
emoji: 🎙️
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
short_description: Voice RAG over MSMARCO-XI in Hindi, Bengali, Tamil and English
---

# dhvani

Speak a question in Hindi, Bengali, Tamil or English; get an answer built only
from retrieved passages, with citations you can open — or an explicit refusal
when the passages do not support one.

Built for **Hacker House Goa 2026, Task 2**. Source, every measurement, and every
decision that produced them: [github.com/AnubhavBayard/dhvani](https://github.com/AnubhavBayard/dhvani).

## What you are looking at

| | |
|---|---|
| Index | 3,278,022 chunks over 598,732 MS MARCO-XI passages, four chunking strategies |
| Retrieval | FAISS HNSW (int8) + BM25, fused by RRF, in process — no hosted vector DB |
| **Boundary A** | **P50 13.5 ms** — final transcript in, context selected out |
| Guardrails | injection and out-of-index language refused before retrieval; every answer sentence checked back against the passages |
| Speech | Sarvam AI, with ElevenLabs behind the same interface |

The stage bar across the top is not decoration: every stage reports whether it
ran, how long it took, and whether it degraded. Timings shown are measured on
this box, this request.

## First load takes a minute

A free Space sleeps when nobody visits, and the index is re-downloaded on wake —
2.5 GB from the Hub, then ~4 s to load and warm. If you arrive at a spinner, it
is fetching, not broken. Every request after that is served from a warm process,
which is the whole reason the retrieval numbers look the way they do.

## Known, and written down rather than hidden

**Recall@10 is 0.4464**, so roughly half of in-corpus questions refuse. That is a
retrieval ceiling, not a bug in the refusal path — the reranker that would lift
it is specified, deferred and absent from the trace rather than faked. Questions
verified to answer well are listed in `docs/DEMO_SCRIPT.md` in the repo.

Latency boundaries B (first token) and C (last token) cross the network to a
generation provider and are reported, never targeted. Boundary A is the number
under a budget, and it is the one on screen.
