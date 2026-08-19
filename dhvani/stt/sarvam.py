"""Sarvam speech-to-text — the default provider (ADR-003).

Batch, not streaming. `DESIGN.md` specifies a WebSocket carrying opus chunks
with partial transcripts feeding speculative retrieval; that is an optimization
of boundary C, and boundary C is reported rather than targeted. What it buys is
overlap between the user still talking and retrieval already running. What it
costs is a second transport, partial-transcript state, and cancellation — on a
three-day budget with the demo video unshot. So: batch now, streaming behind it
(ADR-029), and `Transcript.is_final` already exists so the streaming path adds a
value rather than a field.

Verified live 2026-08-19: `POST /speech-to-text`, multipart `file`, 1.07 s for a
4-second Hindi clip, auto language detection returning `hi-IN` at 0.696.
"""

from __future__ import annotations

import time

import httpx

from dhvani.harness.contracts import Transcript
from dhvani.stt.base import STTConfig, to_corpus_lang

ENDPOINT = "https://api.sarvam.ai/speech-to-text"


class SarvamSTT:
    name = "sarvam"
    env_key = "SARVAM_API_KEY"

    def transcribe(self, audio: bytes, filename: str, cfg: STTConfig,
                   client: httpx.Client) -> Transcript:
        import os

        data = {}
        if cfg.language:
            data["language_code"] = cfg.language
        t0 = time.perf_counter()
        r = client.post(ENDPOINT,
                        headers={"api-subscription-key": os.environ[self.env_key]},
                        files={"file": (filename, audio, "application/octet-stream")},
                        data=data)
        r.raise_for_status()
        body = r.json()
        return Transcript(
            text=body.get("transcript", ""),
            is_final=True,
            # Sarvam reports confidence in its *language* detection, not in the
            # transcript. Carried as-is and named for what it is, because a
            # number labelled "confidence" that means something else is worse
            # than no number.
            confidence=body.get("language_probability"),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            lang=to_corpus_lang(body.get("language_code")),
            provider=self.name)
