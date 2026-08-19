"""Stage 4 — script detection, bounded edit distance, and phonetic repair.

The arithmetic runs anywhere. The repair test needs a built index, because the
thing worth testing is not that the rewriter runs — it is that it corrects a
term the corpus knows and leaves alone a term the corpus already contains. Both
of those are properties of the vocabulary, not of the code.
"""

from __future__ import annotations

import numpy as np
import pytest

from dhvani.bench.queryset import garble
from dhvani.retrieve.stage4 import (QueryRewriter, Stage4Config, detect_script,
                                    edit_distance_within)

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "index" / "full"
HAS_FULL = (FULL / "phonetic_vocab.json").exists() and (FULL / "bm25").exists()


# -- script detection -------------------------------------------------------

def test_script_detection_covers_the_four_indexed_scripts():
    assert detect_script("मुंबई में") == ("Deva", "hin_Deva")
    assert detect_script("সৌরজগতের") == ("Beng", "ben_Beng")
    assert detect_script("கம்ப்யூட்டர்") == ("Taml", "tam_Taml")
    assert detect_script("what is a corporation") == ("Latn", "eng_Latn")


def test_an_indic_character_wins_over_a_latin_one():
    """Mixed script is the normal case for a spoken query — an English brand
    name inside a Hindi sentence. The language is the Indic one."""
    assert detect_script("Mumbai में कितने") == ("Deva", "hin_Deva")


def test_a_query_with_no_letters_is_undetermined_rather_than_english():
    assert detect_script("2019 !!") == ("Zyyy", "und")


# -- bounded edit distance --------------------------------------------------

def test_edit_distance_returns_none_past_the_bound():
    assert edit_distance_within("कंप्यटर", "कंप्यूटर", 2) == 1
    assert edit_distance_within("abc", "xyz", 2) is None
    assert edit_distance_within("a", "abcd", 2) is None          # length gate
    assert edit_distance_within("same", "same", 2) == 0


# -- the garbler the stage is measured against ------------------------------

def test_garble_corrupts_long_words_and_leaves_short_ones():
    rng = np.random.default_rng(0)
    text = "मुंबई में कितने लोग रहते हैं"
    out = garble(text, rng, rate=1.0)
    assert out != text
    assert len(out.split()) == len(text.split())      # words drop chars, not words
    short = garble("a bc de", rng, rate=1.0)
    assert short == "a bc de"


# -- against the real vocabulary --------------------------------------------

@pytest.fixture(scope="module")
def rewriter():
    return QueryRewriter.load(FULL)


@pytest.mark.skipif(not HAS_FULL, reason="full index not built")
def test_a_term_the_corpus_knows_is_never_touched(rewriter):
    """The rewriter's failure mode is over-correction: 'what' → 'that' is worse
    than leaving a garbled term alone, because it happens to every query."""
    q, _ = rewriter.rewrite("what is a corporation")
    assert q.corrections == []
    assert q.method == "passthrough"
    assert q.text == "what is a corporation"


@pytest.mark.skipif(not HAS_FULL, reason="full index not built")
def test_a_dropped_matra_is_repaired_from_the_phonetic_vocabulary(rewriter):
    q, trace = rewriter.rewrite("मुंबइ में")
    assert ("मुंबइ", "मुंबई") in q.corrections
    assert q.method == "phonetic"
    assert trace.get("stage4_rewrite").detail["corrections"] == 1


@pytest.mark.skipif(not HAS_FULL, reason="full index not built")
def test_indic_digits_are_folded_the_way_the_index_folded_them(rewriter):
    """Same `normalize()` as the build. If these drift, lexical recall dies
    silently — BM25 sees २०१९ and 2019 as different terms."""
    q, _ = rewriter.rewrite("२०१९ में")
    assert q.text.startswith("2019")


@pytest.mark.skipif(not HAS_FULL, reason="full index not built")
def test_the_disabled_arm_passes_the_raw_transcript_through_with_a_trace_row(rewriter):
    """Ablation needs the row, not a gap — and the arm has to be a real
    ablation: stage 3 receives the raw text, not a normalized version of it."""
    raw = "२०१९ मुंबइ"
    q, trace = rewriter.rewrite(raw, Stage4Config(enabled=False))
    st = trace.get("stage4_rewrite")
    assert st is not None and st.enabled is False and st.status == "off"
    assert q.text == raw and q.corrections == []
