"""Checks on the chunkers. Indic text is where a Latin-shaped splitter fails
silently, so most of these are script cases rather than English ones."""

from __future__ import annotations

import numpy as np
import pytest

from dhvani.build.chunk import (MAX_CHUNK_CHARS, normalize, s0_fixed_window,
                                s1_passage, s2_sentence_window, s3_semantic,
                                s4_query_context, split_sentences)

META = {"lang": "hin_Deva", "script": "Deva", "query_type": "DESCRIPTION",
        "split": "validation", "is_selected": True}

HIN = ("भारत एक देश है। इसकी राजधानी नई दिल्ली है। "
       "जनसंख्या २०१९ में लगभग १३० करोड़ थी।")
BEN = "ভারত একটি দেশ। এর রাজধানী নয়াদিল্লি। জনসংখ্যা অনেক বেশি।"
TAM = "இந்தியா ஒரு நாடு. அதன் தலைநகரம் புது தில்லி. மக்கள் தொகை அதிகம்."


def test_normalize_folds_indic_digits():
    """BM25 must not treat २०१९ and 2019 as different tokens."""
    assert normalize("२०१९") == "2019"
    assert normalize("২০১৯") == "2019"
    assert normalize("௨௦௧௯") == "2019"


def test_normalize_strips_zero_width_and_collapses_space():
    assert normalize("क‍ा  ​ख") == "का ख"


@pytest.mark.parametrize("text,expected", [(HIN, 3), (BEN, 3), (TAM, 3)])
def test_splitter_is_script_aware(text, expected):
    """The danda for Devanagari and Bengali, the full stop for Tamil. A
    Latin-only splitter returns 1 here for the first two."""
    assert len(split_sentences(normalize(text))) == expected


def test_splitter_offsets_point_at_the_source():
    text = normalize(HIN)
    for sent, start, end in split_sentences(text):
        assert text[start:end] == sent


def test_short_fragments_glue_onto_the_previous_sentence():
    sents = split_sentences(normalize("यह एक वाक्य है। ठीक।"))
    assert len(sents) == 1


def test_s1_is_one_chunk_per_passage():
    chunks = s1_passage(HIN, "q1", 0, META)
    assert len(chunks) == 1
    assert chunks[0].strategy == "s1_passage"
    assert chunks[0].retrieved_text == chunks[0].text


def test_s2_embeds_narrow_and_returns_wide():
    chunks = s2_sentence_window(HIN, "q1", 0, META, window=1)
    assert len(chunks) == 3
    middle = chunks[1]
    assert len(middle.retrieved_text) > len(middle.text)
    assert middle.text in middle.retrieved_text
    # the window is the overlap, and stage 7 needs to see it
    assert set(middle.overlap_with) == {chunks[0].chunk_id, chunks[2].chunk_id}


def test_s2_declines_single_sentence_passages():
    """S1 already covers them; emitting here would double-index identical text."""
    assert s2_sentence_window("एक वाक्य।", "q1", 0, META) == []


def test_s3_needs_vectors_and_does_not_silently_become_s1():
    assert s3_semantic(HIN, "q1", 0, META, sentence_vectors=None) == []


def test_s3_cuts_at_a_similarity_trough():
    text = normalize(HIN)
    n = len(split_sentences(text))
    # sentences 0,1 alike; 2 unrelated -> the trough is between 1 and 2
    vecs = np.array([[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]], dtype=np.float32)[:n]
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    chunks = s3_semantic(text, "q1", 0, META, sentence_vectors=vecs,
                         min_sentences=1, overlap_ratio=0.0)
    assert len(chunks) == 2
    assert chunks[0].strategy == "s3_semantic"


def test_s3_rejects_a_vector_count_mismatch():
    """A splitter divergence between the embed pass and this one would otherwise
    misalign every chunk boundary in the corpus, silently."""
    with pytest.raises(ValueError, match="diverged"):
        s3_semantic(HIN, "q1", 0, META,
                    sentence_vectors=np.eye(9, dtype=np.float32))


def test_s4_carries_the_question_it_answers():
    chunks = s4_query_context(HIN, "q1", 0, META,
                              query="भारत की राजधानी क्या है?",
                              query_type="LOCATION")
    assert len(chunks) == 1
    assert chunks[0].text.startswith("LOCATION: भारत की राजधानी क्या है?")
    assert normalize(HIN) in chunks[0].text


def test_s4_declines_an_empty_query():
    assert s4_query_context(HIN, "q1", 0, META, query="  ", query_type="X") == []


def test_s0_degenerates_to_one_chunk_on_a_typical_passage():
    """The measured finding, as a test: p95 passage length is ~550 chars, so the
    naive window is close to an identity function on this corpus."""
    assert len(s0_fixed_window("क" * 400, "q1", 0, META)) == 1
    assert len(s0_fixed_window("क" * 1200, "q1", 0, META)) > 1


def test_every_strategy_caps_chunk_length():
    long = "भारत। " * 4000
    vecs = np.ones((len(split_sentences(normalize(long))), 2), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    produced = (s1_passage(long, "q", 0, META)
                + s2_sentence_window(long, "q", 0, META)
                + s3_semantic(long, "q", 0, META, sentence_vectors=vecs)
                + s4_query_context(long, "q", 0, META, query="क्या?", query_type="X")
                + s0_fixed_window(long, "q", 0, META))
    assert produced
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in produced)


def test_chunk_ids_are_unique_and_stable():
    a = s2_sentence_window(HIN, "q1", 0, META)
    b = s2_sentence_window(HIN, "q1", 0, META)
    ids = [c.chunk_id for c in a]
    assert ids == [c.chunk_id for c in b]
    assert len(set(ids)) == len(ids)


def test_chunk_id_is_unique_across_languages_for_the_same_dataset_row():
    """`doc_id` is the dataset's `query_id` and it is the same row in every
    language file. Without the language in the id, the Hindi and Bengali copies
    of one passage collide — which is what happened in the 18 Aug build
    (3,278,022 chunks, 969,298 distinct ids) and what ADR-020 fixes."""
    hin = s1_passage(HIN, "1234", 0, META)
    ben = s1_passage(BEN, "1234", 0, {**META, "lang": "ben_Beng", "script": "Beng"})
    assert hin[0].chunk_id != ben[0].chunk_id
    assert hin[0].chunk_id.startswith("hin_Deva:")
    assert hin[0].doc_id == ben[0].doc_id == "1234"


def test_overlap_ids_carry_the_language_of_the_chunk_that_names_them():
    chunks = s2_sentence_window(HIN, "1234", 0, META)
    named = {o for c in chunks for o in c.overlap_with}
    assert named and all(o.startswith("hin_Deva:") for o in named)
    assert named <= {c.chunk_id for c in chunks}


def test_token_pattern_keeps_indic_words_whole():
    """The bug ADR-023 fixes: `\\w` excludes combining marks, so `bm25s`'s
    default pattern cut every Indic word at its first matra and BM25 indexed
    syllable fragments. A regression here is silent — the index still builds,
    still answers, and is still wrong."""
    import re

    from dhvani.build.chunk import TOKEN_PATTERN

    assert re.findall(TOKEN_PATTERN, "कंप्यूटर क्या है") == ["कंप्यूटर", "क्या", "है"]
    assert re.findall(TOKEN_PATTERN, "মুম্বাই শহর") == ["মুম্বাই", "শহর"]
    assert re.findall(TOKEN_PATTERN, "கம்ப்யூட்டர் என்றால்") == ["கம்ப்யூட்டர்", "என்றால்"]
    assert re.findall(TOKEN_PATTERN, "what is a corporation 2019") == \
        ["what", "is", "corporation", "2019"]


def test_build_and_query_tokenize_through_the_same_pattern():
    """One definition, or the index and the query disagree invisibly."""
    import inspect

    from dhvani.build import build_index
    from dhvani.retrieve import stage3

    assert "ch.TOKEN_PATTERN" in inspect.getsource(build_index.build_bm25)
    assert "TOKEN_PATTERN" in inspect.getsource(stage3.HybridIndex._lexical)
