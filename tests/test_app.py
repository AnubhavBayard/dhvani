"""The query path assembled, and the endpoints that serve it.

The index is faked. These check the wiring — event order, boundary accounting,
the refusal paths, SSE framing — not retrieval quality, which `test_stage3.py`
and the benchmark own. Nothing here needs a built index or an API key.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dhvani import app as appmod
from dhvani.generate.client import REFUSAL_TOKEN, GenerationUnavailable
from dhvani.harness.contracts import (ConfidenceSignals, ContextChunk,
                                      PipelineTrace, Query, RetrievalResult,
                                      ScoredChunk, SelectedContext, stage)
from dhvani.pipeline import BOUNDARY_A_COVERS, Dhvani, PipelineConfig, ablate


class FakeRewriter:
    def rewrite(self, raw, cfg=None, trace=None):
        trace = trace or PipelineTrace()
        with stage(trace, "stage4_rewrite", enabled=getattr(cfg, "enabled", True)):
            pass
        return Query(raw=raw, rewritten="fixed " + raw, lang="hin_Deva",
                     method="phonetic", corrections=[("गोवा", "गोआ")]), trace


class FakeIndex:
    def __init__(self, n=2):
        self.n = n

    def search(self, query, cfg=None, trace=None):
        trace = trace or PipelineTrace()
        with stage(trace, "stage3_retrieve"):
            pass
        chunks = [ScoredChunk(chunk_id=f"c{i}", row=i, score=1.0 - i / 10, rank=i)
                  for i in range(self.n)]
        return RetrievalResult(query=query, chunks=chunks,
                               signals=ConfidenceSignals(top1=0.9)), trace


class FakeStore:
    def __len__(self):
        return 99

    def get(self, row):
        return {"chunk_id": f"c{row}", "text": f"passage {row} text",
                "doc_id": "42", "lang": "hin_Deva", "strategy": "s1_passage",
                "overlap_with": []}


class FakeCounter:
    name = "fake"

    @staticmethod
    def count(text):
        return len(text.split())


class FakeGenerator:
    """Yields `pieces`, or raises `error` when set.

    `refused` mirrors the real client's contract: the marker is stripped from
    the stream and the fact of the refusal is reported on the trace, so a fake
    that yielded the raw token would be testing a protocol nothing speaks.
    """

    def __init__(self, pieces=("answer ", "text [1]"), error=None, refused=False):
        self.pieces, self.error, self.refused = pieces, error, refused

    def stream(self, question, ctx, trace=None, lang=None):
        trace = trace or PipelineTrace()
        self.lang = lang
        with stage(trace, "generate") as st:
            if self.error:
                st.degraded = True
                raise self.error
            st.detail["model_refused"] = self.refused
            for p in self.pieces:
                yield p


def build(generator=None, n=2, cfg=None) -> Dhvani:
    return Dhvani(FakeIndex(n), FakeRewriter(), FakeStore(), FakeCounter(),
                  generator or FakeGenerator(), cfg or PipelineConfig())


def events(d: Dhvani, q="गोवा में मौसम", cfg=None):
    return list(d.answer(q, cfg))


def by_type(evs, t):
    return [e for e in evs if e["type"] == t]


# -- the pipeline -----------------------------------------------------------

def test_event_order_and_answer_assembly():
    evs = events(build())
    assert [e["type"] for e in evs] == ["query", "retrieval", "token", "token",
                                        "grounding", "done"]
    assert "".join(e["text"] for e in by_type(evs, "token")) == "answer text [1]"


def test_boundary_a_is_one_span_not_a_sum_and_says_what_it_covers():
    ev = by_type(events(build()), "retrieval")[0]
    assert ev["boundary_a_ms"] >= ev["summed_ms"]      # the gap is harness cost
    assert ev["boundary_a_covers"] == BOUNDARY_A_COVERS
    assert "stage6_rerank" in ev["not_yet_in_boundary_a"]


def test_tier_is_not_claimed_while_the_thresholds_are_open():
    """Every query reports `standard`. A tier split from unmeasured thresholds
    would be a fabricated result table."""
    assert by_type(events(build()), "retrieval")[0]["tier"] == "standard"


def test_citations_resolve_to_chunk_ids():
    done = by_type(events(build()), "done")[0]
    assert done["citations"] == {1: "c0", 2: "c1"}


def test_corrections_reach_the_ui():
    q = by_type(events(build()), "query")[0]
    assert q["corrections"] == [("गोवा", "गोआ")] and q["method"] == "phonetic"


def test_empty_retrieval_refuses_without_calling_generation():
    called = []

    class Boom(FakeGenerator):
        def stream(self, *a, **kw):
            called.append(1)
            yield "should not happen"


    evs = events(build(Boom(), n=0))
    ref = by_type(evs, "refusal")[0]
    assert ref["kind"] == "no_context" and not called
    assert by_type(evs, "done")[0]["ttft_ms"] is None


def test_provider_failure_becomes_a_refusal_not_a_crash():
    evs = events(build(FakeGenerator(error=GenerationUnavailable("all down"))))
    assert by_type(evs, "refusal")[0]["kind"] == "generation_unavailable"
    assert by_type(evs, "done")                       # still reports timings


def test_a_refusal_with_no_answer_left_becomes_a_refusal_event():
    evs = events(build(FakeGenerator(pieces=(), refused=True)))
    assert by_type(evs, "refusal")[0]["kind"] == "model_refused"
    assert not by_type(evs, "token")
    assert by_type(evs, "done")[0]["model_signalled_insufficient"] is True


def test_an_answered_question_survives_a_marker_about_a_sub_question():
    """Measured 2026-08-19: the model answered from three sources and then used
    the marker to decline a sub-question. Refusing there threw away a correct,
    cited answer — so the marker is reported, not obeyed."""
    evs = events(build(FakeGenerator(pieces=("real cited answer [1]",),
                                     refused=True)))
    assert not by_type(evs, "refusal")
    assert by_type(evs, "done")[0]["model_signalled_insufficient"] is True


def test_the_detected_language_is_passed_to_generation():
    """A Bengali question came back in Hindi until the language was stated
    explicitly in the prompt."""
    gen = FakeGenerator()
    events(build(gen))
    assert gen.lang == "hin_Deva"


def test_an_answer_that_is_only_whitespace_is_a_refusal_too():
    """No flag, no content. Rendering an empty answer box is worse than saying
    the sources did not cover it."""
    evs = events(build(FakeGenerator(pieces=("  ", "\n"))))
    assert by_type(evs, "refusal")[0]["kind"] == "model_refused"


def test_ttft_and_wall_clock_are_reported():
    done = by_type(events(build()), "done")[0]
    assert done["ttft_ms"] >= 0
    assert done["wall_clock_ms"] >= done["boundary_a_ms"]


def test_ablation_switches_stages_off_and_keeps_their_rows():
    cfg = ablate(PipelineConfig(), stage4=False, stage7=False)
    evs = events(build(cfg=cfg), cfg=cfg)
    stages = {s["stage"]: s for s in by_type(evs, "retrieval")[0]["stages"]}
    assert stages["stage4_rewrite"]["enabled"] is False
    assert stages["stage7_context"]["enabled"] is False
    # Stage 7 off means no context, which must refuse rather than call a model
    # with nothing in the window.
    assert by_type(evs, "refusal")[0]["kind"] == "no_context"


# -- the endpoints ----------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DHVANI_SKIP_INDEX", "1")
    with TestClient(appmod.app) as c:
        appmod.state["dhvani"] = build()
        yield c


def test_health_reports_what_the_boundary_covers(client):
    body = client.get("/health").json()
    assert body["ok"] and body["chunks"] == 99
    assert body["boundary_a_covers"] == BOUNDARY_A_COVERS


def test_ask_streams_named_sse_frames(client):
    r = client.post("/ask", json={"q": "गोवा में मौसम"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    names = [l[7:] for l in r.text.splitlines() if l.startswith("event: ")]
    assert names == ["query", "retrieval", "token", "token", "grounding", "done"]
    payloads = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert payloads[0]["type"] == "query"
    # ensure_ascii=False — Devanagari must survive the wire as itself
    assert "गोवा" in r.text


def test_ask_rejects_an_over_long_query(client):
    assert client.post("/ask", json={"q": "x" * 501}).status_code == 422
    assert client.post("/ask", json={"q": ""}).status_code == 422


def test_ask_is_503_before_the_index_is_loaded(client):
    appmod.state.pop("dhvani")
    assert client.post("/ask", json={"q": "hi"}).status_code == 503


def test_index_page_and_static_assets_are_served(client):
    assert "<title>dhvani" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# -- the speech endpoint ----------------------------------------------------

class FakeSTT:
    def __init__(self, transcript=None, error=None):
        self.transcript, self.error = transcript, error
        self.providers = {}
        self.seen = None

    def transcribe(self, audio, filename="audio.webm", trace=None):
        from dhvani.harness.contracts import Transcript
        from dhvani.stt.base import STTUnavailable
        self.seen = (audio, filename)
        trace = trace or PipelineTrace()
        with stage(trace, "stt"):
            if self.error:
                raise STTUnavailable(self.error)
        return self.transcript or Transcript(
            text="वाशिंगटन कौन सा शहर है", lang="hin_Deva",
            confidence=0.7, latency_ms=1070, provider="sarvam")


def test_stt_returns_the_transcript_and_its_trace(client):
    fake = FakeSTT()
    appmod.state["stt"] = fake
    body = client.post("/stt", files={"file": ("a.webm", b"audio-bytes",
                                               "audio/webm")}).json()
    assert body["ok"] and body["text"] == "वाशिंगटन कौन सा शहर है"
    assert body["lang"] == "hin_Deva" and body["provider"] == "sarvam"
    assert [s["stage"] for s in body["stages"]] == ["stt"]
    assert fake.seen[0] == b"audio-bytes"


def test_stt_failure_is_a_product_state_not_an_error_status(client):
    """The UI needs to render "couldn't catch that" and keep the text box. A
    5xx would make that indistinguishable from the server being down."""
    appmod.state["stt"] = FakeSTT(error="all providers down")
    r = client.post("/stt", files={"file": ("a.webm", b"x", "audio/webm")})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "down" in r.json()["reason"]


def test_stt_rejects_oversized_audio_at_the_boundary(client):
    from dhvani.stt.base import MAX_AUDIO_BYTES
    appmod.state["stt"] = FakeSTT()
    r = client.post("/stt", files={"file": ("a.webm", b"x" * (MAX_AUDIO_BYTES + 1),
                                            "audio/webm")})
    assert r.status_code == 413


def test_health_reports_which_speech_providers_have_keys(client, monkeypatch):
    from dhvani.stt.base import STT, STTConfig
    monkeypatch.setenv("SARVAM_API_KEY", "k")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    appmod.state["stt"] = STT(STTConfig())
    assert client.get("/health").json()["stt"] == {"sarvam": True,
                                                   "elevenlabs": False}
