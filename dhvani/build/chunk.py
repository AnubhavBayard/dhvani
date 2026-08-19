"""The four chunking strategies, plus the naive baseline they have to beat.

Build-time only (ADR-002). Every strategy takes a passage and returns `Chunk`s
carrying enough metadata to be ablated, deduped and cited later.

Strategies are CHUNKING.md's:
  s1_passage          native passage, the honest baseline
  s2_sentence_window  embed one sentence, return it plus w neighbours
  s3_semantic         cut on embedding-similarity troughs between sentences
  s4_query_context    selected passage prefixed with the query it answers
  s0_fixed_window     the naive fixed-token splitter, off by default, kept so
                      "we tried the obvious thing" is a number and not a claim
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np

from dhvani.harness.contracts import Chunk

# CHUNKING.md: p99 passage length is under 1,000 characters and the maximum is
# 21,390. A handful of outliers would otherwise dominate stage-6 latency.
MAX_CHUNK_CHARS = 2000

# Sentence terminators that actually occur in this corpus. Devanagari and
# Bengali use the danda; Tamil uses ASCII full stops. A Latin-only splitter
# produces one chunk per passage here, which is S1 wearing a hat.
_TERMINATORS = "।॥.!?…"
_SENTENCE_RE = re.compile(rf"[^{re.escape(_TERMINATORS)}]+[{re.escape(_TERMINATORS)}]*")

# Devanagari, Bengali, Tamil and ASCII digits, normalized so BM25 does not treat
# "2019" and "२०१९" as different tokens.
_DIGITS = {}
for _base, _script in ((0x0966, "Deva"), (0x09E6, "Beng"), (0x0BE6, "Taml")):
    for _d in range(10):
        _DIGITS[chr(_base + _d)] = str(_d)
_DIGIT_RE = re.compile("|".join(map(re.escape, _DIGITS)))

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)

# The lexical tokenizer's pattern, used at build time and at query time from
# this one definition. `bm25s`'s default is `\b\w\w+\b`, and Python's `\w`
# excludes combining marks — so every Indic vowel sign and virama is a word
# boundary and "कंप्यूटर क्या है" tokenizes to ["टर"]. Adding the Indic block
# (U+0900–U+0DFF) to the character class keeps the marks inside the token.
# Measured 18 Aug: with the default pattern BM25 indexed fragments for three of
# four corpora (ADR-023).
TOKEN_PATTERN = r"(?u)[\w\u0900-\u0DFF]{2,}"


def normalize(text: str) -> str:
    """NFC, digit folding, zero-width cleanup, whitespace collapse.

    Applied identically at build time and at query time — stage 4 imports this
    function rather than reimplementing it, because a normalization mismatch
    between the index and the query is invisible and destroys lexical recall.
    """
    text = unicodedata.normalize("NFC", text).translate(_ZERO_WIDTH)
    text = _DIGIT_RE.sub(lambda m: _DIGITS[m.group()], text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str, min_chars: int = 15) -> list[tuple[str, int, int]]:
    """Return (sentence, start, end) with offsets into `text`.

    Offsets are kept because `Chunk.char_span` is what lets the UI highlight the
    exact source span rather than pointing vaguely at a chunk. Fragments shorter
    than `min_chars` are glued onto the previous sentence — abbreviations and
    decimals produce them, and a two-character "chunk" embeds to noise.
    """
    out: list[tuple[str, int, int]] = []
    for m in _SENTENCE_RE.finditer(text):
        piece = m.group().strip()
        if not piece:
            continue
        start = m.start() + (len(m.group()) - len(m.group().lstrip()))
        end = start + len(piece)
        if out and len(piece) < min_chars:
            prev, p_start, _ = out[-1]
            out[-1] = (f"{prev} {piece}", p_start, end)
        else:
            out.append((piece, start, end))
    return out or ([(text.strip(), 0, len(text.strip()))] if text.strip() else [])


def _chunk(text: str, *, strategy: str, doc_id: str, passage_idx: int,
           ordinal: int, span: tuple[int, int], meta: dict,
           parent_text: str | None = None) -> Chunk:
    # The language is part of the id, not just a column. `doc_id` is the
    # dataset's `query_id`, which is the *same row* in every language file, so
    # without the prefix the Hindi, Bengali, Tamil and English copies of a
    # passage all share one id — 3.28M chunks collapsed to 969k ids in the
    # 18 Aug build, and any consumer keying on the id (citations, stage 7
    # dedupe, `overlap_with`) could not tell the corpora apart. See ADR-020.
    return Chunk(
        chunk_id=f"{meta['lang']}:{doc_id}:{passage_idx}:{strategy}:{ordinal}",
        text=text[:MAX_CHUNK_CHARS],
        doc_id=doc_id,
        passage_idx=passage_idx,
        strategy=strategy,
        ordinal=ordinal,
        char_span=span,
        parent_text=parent_text,
        token_count=len(text.split()),
        **meta,
    )


# --------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------

def s1_passage(passage: str, doc_id: str, passage_idx: int, meta: dict) -> list[Chunk]:
    """One chunk per passage. The dataset's own unit, and the unit its relevance
    labels are defined on — which is what makes it the baseline to beat."""
    text = normalize(passage)
    if not text:
        return []
    return [_chunk(text, strategy="s1_passage", doc_id=doc_id,
                   passage_idx=passage_idx, ordinal=0,
                   span=(0, len(text)), meta=meta)]


def s2_sentence_window(passage: str, doc_id: str, passage_idx: int, meta: dict,
                       window: int = 1) -> list[Chunk]:
    """Embed narrow, return wide: the chunk is one sentence, the returned unit is
    that sentence plus `window` neighbours on each side."""
    text = normalize(passage)
    sents = split_sentences(text)
    if len(sents) < 2:
        return []  # nothing to window over; S1 already covers it
    chunks = []
    for i, (sent, start, end) in enumerate(sents):
        lo, hi = max(0, i - window), min(len(sents), i + window + 1)
        parent = " ".join(s for s, _, _ in sents[lo:hi])
        c = _chunk(sent, strategy="s2_sentence_window", doc_id=doc_id,
                   passage_idx=passage_idx, ordinal=i, span=(start, end),
                   meta=meta, parent_text=parent)
        chunks.append(c)
    # The window *is* the overlap, so neighbours share returned text and stage 7
    # must be able to see it.
    for i, c in enumerate(chunks):
        lo, hi = max(0, i - window), min(len(chunks), i + window + 1)
        c.overlap_with = [o.chunk_id for o in chunks[lo:hi] if o is not c]
    return chunks


def s3_semantic(passage: str, doc_id: str, passage_idx: int, meta: dict,
                sentence_vectors: np.ndarray | None = None,
                breakpoint_percentile: int = 85, min_sentences: int = 2,
                overlap_ratio: float = 0.15) -> list[Chunk]:
    """Cut where adjacent sentences stop being about the same thing.

    `sentence_vectors` are supplied by the caller because embedding is batched
    across the whole corpus — embedding per passage here would be correct and
    roughly 30x slower.

    With no vectors (or one sentence) this returns nothing rather than silently
    degrading to S1: a strategy that quietly becomes another strategy makes the
    ablation table a lie.
    """
    text = normalize(passage)
    sents = split_sentences(text)
    if len(sents) < min_sentences + 1 or sentence_vectors is None:
        return []
    if len(sentence_vectors) != len(sents):
        raise ValueError(
            f"{doc_id}:{passage_idx} has {len(sents)} sentences but "
            f"{len(sentence_vectors)} vectors — the splitter used to embed and "
            f"the splitter used here have diverged")

    sims = np.sum(sentence_vectors[:-1] * sentence_vectors[1:], axis=1)
    if sims.size == 0:
        return []
    threshold = float(np.percentile(sims, 100 - breakpoint_percentile))
    cuts = [i + 1 for i, s in enumerate(sims) if s <= threshold]

    bounds, prev = [], 0
    for c in cuts + [len(sents)]:
        if c - prev >= min_sentences or c == len(sents):
            bounds.append((prev, c))
            prev = c
    if prev < len(sents):
        bounds[-1] = (bounds[-1][0], len(sents))

    chunks = []
    for ordinal, (lo, hi) in enumerate(b for b in bounds if b[1] > b[0]):
        body = " ".join(s for s, _, _ in sents[lo:hi])
        start, end = sents[lo][1], sents[hi - 1][2]
        if overlap_ratio > 0 and lo > 0:
            # Snap overlap to a sentence boundary. Overlapping at an arbitrary
            # token offset produces half-sentences, which embed poorly.
            carry = sents[lo - 1][0]
            if len(carry) <= overlap_ratio * len(body) * 2:
                body = f"{carry} {body}"
                start = sents[lo - 1][1]
        chunks.append(_chunk(body, strategy="s3_semantic", doc_id=doc_id,
                             passage_idx=passage_idx, ordinal=ordinal,
                             span=(start, end), meta=meta))
    for i, c in enumerate(chunks[1:], start=1):
        c.overlap_with = [chunks[i - 1].chunk_id]
    return chunks


def s4_query_context(passage: str, doc_id: str, passage_idx: int, meta: dict,
                     query: str, query_type: str) -> list[Chunk]:
    """The selected passage, prefixed with the question it is known to answer.

    The dataset hands us supervision most corpora do not have. It also hands us
    an evaluation hazard: if the header query is the query being evaluated, the
    score measures memorized text. The guard is the *split* — S4 chunks are built
    from train rows only, evaluation queries come from validation — and it is
    asserted in `build_index.py`, not left to this function to remember.
    """
    text = normalize(passage)
    if not text or not query.strip():
        return []
    header = f"{query_type}: {normalize(query)}"
    body = f"{header}\n\n{text}"
    return [_chunk(body, strategy="s4_query_context", doc_id=doc_id,
                   passage_idx=passage_idx, ordinal=0,
                   span=(0, len(text)), meta=meta)]


def s0_fixed_window(passage: str, doc_id: str, passage_idx: int, meta: dict,
                    window_chars: int = 512, stride: int = 384) -> list[Chunk]:
    """The naive splitter, off by default. It exists to be measured.

    On this corpus p95 passage length is ~550 characters, so this mostly emits
    one chunk per passage — which is the finding, and is worth a row in the
    ablation table rather than an assertion in a doc.
    """
    text = normalize(passage)
    if not text:
        return []
    chunks = []
    for ordinal, start in enumerate(range(0, max(1, len(text) - stride + 1), stride)):
        piece = text[start:start + window_chars]
        if not piece.strip():
            continue
        chunks.append(_chunk(piece, strategy="s0_fixed_window", doc_id=doc_id,
                             passage_idx=passage_idx, ordinal=ordinal,
                             span=(start, start + len(piece)), meta=meta))
    return chunks
