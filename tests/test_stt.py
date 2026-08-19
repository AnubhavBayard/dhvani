"""The speech layer — the provider ladder, the swap, and the bounds.

Driven by `httpx.MockTransport`. The swap test is the point of having two
providers at all (ADR-003): the same audio goes through both and comes back as
the same contract, so "swappable" is a property that is checked rather than
claimed.
"""

from __future__ import annotations

import httpx
import pytest

from dhvani.harness.contracts import PipelineTrace
from dhvani.stt.base import (MAX_AUDIO_BYTES, STT, STTConfig, STTUnavailable,
                             to_corpus_lang)
from dhvani.stt.elevenlabs import ElevenLabsSTT
from dhvani.stt.sarvam import SarvamSTT

AUDIO = b"RIFF....WAVEfake"

SARVAM_BODY = {"transcript": "वाशिंगटन कौन सा शहर है?", "language_code": "hi-IN",
               "language_probability": 0.696}
ELEVEN_BODY = {"text": "वाशिंगटन कौन सा शहर है?", "language_code": "hin",
               "language_probability": 0.91}


def stt_for(handler, **kw) -> STT:
    return STT(STTConfig(**kw), httpx.Client(transport=httpx.MockTransport(handler)),
               {"sarvam": SarvamSTT(), "elevenlabs": ElevenLabsSTT()})


def route(request):
    if "sarvam" in str(request.url):
        return httpx.Response(200, json=SARVAM_BODY)
    return httpx.Response(200, json=ELEVEN_BODY)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-sarvam")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven")


# -- the contract both providers speak --------------------------------------

def test_the_two_providers_return_the_same_contract_for_the_same_audio():
    """ADR-003 chose Sarvam over ElevenLabs. A choice with one option built is
    an assertion, so both are built and both are checked here."""
    out = {}
    for name in ("sarvam", "elevenlabs"):
        t = stt_for(route, provider=name, fallback=None).transcribe(AUDIO)
        out[name] = t
        assert t.text == "वाशिंगटन कौन सा शहर है?"
        assert t.is_final and t.provider == name
        assert t.latency_ms >= 0
    # Both providers report BCP-47 in their own dialect; the pipeline sees one
    # vocabulary, or a language silently means two things in one trace.
    assert out["sarvam"].lang == out["elevenlabs"].lang == "hin_Deva"


@pytest.mark.parametrize("code,want", [
    ("hi-IN", "hin_Deva"), ("bn-IN", "ben_Beng"), ("ta-IN", "tam_Taml"),
    ("en-US", "eng_Latn"), ("hin", "hin_Deva"), ("ta", "tam_Taml"),
    ("xx-YY", None), (None, None), ("", None),
])
def test_language_codes_normalize_to_corpus_tags(code, want):
    assert to_corpus_lang(code) == want


def test_the_provider_sends_its_own_auth_header():
    seen = {}

    def handler(request):
        seen[str(request.url)] = dict(request.headers)
        return route(request)

    stt_for(handler, provider="sarvam", fallback=None).transcribe(AUDIO)
    stt_for(handler, provider="elevenlabs", fallback=None).transcribe(AUDIO)
    sarvam = next(h for u, h in seen.items() if "sarvam" in u)
    eleven = next(h for u, h in seen.items() if "elevenlabs" in u)
    assert sarvam["api-subscription-key"] == "test-sarvam"
    assert eleven["xi-api-key"] == "test-eleven"


# -- bounds at the trust boundary -------------------------------------------

def test_oversized_audio_is_refused_before_any_request():
    called = []

    def handler(request):
        called.append(1)
        return route(request)

    with pytest.raises(STTUnavailable, match="too large"):
        stt_for(handler).transcribe(b"x" * (MAX_AUDIO_BYTES + 1))
    assert not called


def test_empty_audio_is_refused_before_any_request():
    called = []

    def handler(request):
        called.append(1)
        return route(request)

    with pytest.raises(STTUnavailable, match="empty audio"):
        stt_for(handler).transcribe(b"")
    assert not called


# -- the ladder --------------------------------------------------------------

def test_5xx_retries_then_falls_back_to_the_second_provider():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if "sarvam" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json=ELEVEN_BODY)

    trace = PipelineTrace()
    t = stt_for(handler, retries=1).transcribe(AUDIO, trace=trace)
    assert t.provider == "elevenlabs"
    assert sum("sarvam" in u for u in hits) == 2      # bounded: 1 try + 1 retry
    st = trace.get("stt")
    assert st.degraded and st.detail["provider_used"] == "elevenlabs"


def test_4xx_is_not_retried():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(401)

    with pytest.raises(STTUnavailable):
        stt_for(handler, retries=3).transcribe(AUDIO)
    assert len(hits) == 2                             # one per provider


def test_a_provider_with_no_key_is_skipped_without_a_request(monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return route(request)

    t = stt_for(handler).transcribe(AUDIO)
    assert t.provider == "elevenlabs"
    assert len(hits) == 1 and "elevenlabs" in hits[0]


def test_silence_is_not_retried_and_ends_as_unavailable():
    """An empty transcript is the user saying nothing, not the provider
    failing. Retrying the same silence twice helps no one."""
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, json={"transcript": "  ", "text": "  "})

    with pytest.raises(STTUnavailable, match="empty transcript"):
        stt_for(handler, retries=2).transcribe(AUDIO)
    assert len(hits) == 2                             # one per provider, no retries


def test_everything_down_raises_so_the_ui_can_fall_back_to_text():
    trace = PipelineTrace()
    with pytest.raises(STTUnavailable):
        stt_for(lambda r: httpx.Response(500), retries=0).transcribe(AUDIO, trace=trace)
    st = trace.get("stt")
    assert not st.ok and st.detail["errors"]


def test_disabled_arm_emits_its_row():
    trace = PipelineTrace()
    with pytest.raises(STTUnavailable):
        stt_for(route, enabled=False).transcribe(AUDIO, trace=trace)
    assert trace.get("stt").status == "off"


def test_a_forced_language_is_passed_through():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return route(request)

    stt_for(handler, provider="sarvam", fallback=None,
            language="ta-IN").transcribe(AUDIO)
    assert b"ta-IN" in seen["body"]
