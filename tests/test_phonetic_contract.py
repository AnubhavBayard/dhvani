"""The properties of `libindic/soundex` that stage 4 and ADR-012 are built on.

This is a contract test against a third-party library, not a test of our code.
It exists because two decisions rest on the library's behaviour: stage 4 keys its
phonetic vocabulary on `code[1:]` rather than `code`, and ADR-012 excludes Urdu
from the indexed subset. If a library update changes either behaviour, both are
stale and this is where it surfaces.

Documented in RAG_PIPELINE.md, stage 4.
"""

from __future__ import annotations

import pytest

from libindic.soundex import Soundex

SX = Soundex()

# "bhaarat" — the same word, five scripts.
BHARAT = {
    "hin": "भारत", "ben": "ভারত", "tam": "இந்தியா", "tel": "భారత", "kan": "ಭಾರತ",
}


def test_first_character_is_passed_through_verbatim():
    """Why the vocabulary key is `code[1:]`: the code is script-tagged."""
    for lang, word in BHARAT.items():
        assert SX.soundex(word)[0] == word[0], lang


def test_tail_key_agrees_across_devanagari_telugu_kannada():
    """The blocking key works for the scripts it works for. Bengali and Tamil
    diverge on some words, which is why the tail key selects candidates and
    `compare()` scores them — see RAG_PIPELINE.md stage 4."""
    tails = {lang: SX.soundex(w)[1:] for lang, w in BHARAT.items()}
    assert tails["hin"] == tails["tel"] == tails["kan"] == "APK0000"


def test_compare_recognises_cross_language_equality():
    """Returns 2 for phonetically equal strings in different languages."""
    assert SX.compare("भारत", "ভারত") == 2
    assert SX.compare("भारत", "இந்தியா") == -1


def test_urdu_produces_no_phonetic_signal():
    """ADR-012 excludes Urdu on exactly this. Not weak coverage — none."""
    code = SX.soundex("بھارت")
    assert code[1:] == "0" * len(code[1:]), f"Urdu now encodes: {code!r}"


@pytest.mark.parametrize("script,word", [("Beng", "ভাৰত"), ("Deva", "भारत")])
def test_assamese_and_devanagari_do_encode(script, word):
    """The languages that ride on the Bengali and Devanagari mappings work."""
    assert SX.soundex(word)[1:].rstrip("0") != ""
