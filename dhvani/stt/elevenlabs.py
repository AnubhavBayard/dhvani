"""ElevenLabs Scribe — the second provider.

It exists so the swap is real: ADR-003 chose Sarvam on Indic coverage and Mumbai
proximity, and a choice with only one option built is an assertion. This is also
the first rung of the degradation ladder when Sarvam's circuit opens.

Its free tier is ~30 minutes of audio a month against a benchmark pass needing
~42, so it is never the default path — it transcribes the swap-test clip and
stands by (ADR-003, cost table).
"""

from __future__ import annotations

import os
import time

import httpx

from dhvani.harness.contracts import Transcript
from dhvani.stt.base import STTConfig, to_corpus_lang

ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v1"


class ElevenLabsSTT:
    name = "elevenlabs"
    env_key = "ELEVENLABS_API_KEY"

    def transcribe(self, audio: bytes, filename: str, cfg: STTConfig,
                   client: httpx.Client) -> Transcript:
        data = {"model_id": MODEL}
        if cfg.language:
            data["language_code"] = cfg.language.split("-")[0]
        t0 = time.perf_counter()
        r = client.post(ENDPOINT,
                        headers={"xi-api-key": os.environ[self.env_key]},
                        files={"file": (filename, audio, "application/octet-stream")},
                        data=data)
        r.raise_for_status()
        body = r.json()
        return Transcript(
            text=body.get("text", ""),
            is_final=True,
            confidence=body.get("language_probability"),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            lang=to_corpus_lang(body.get("language_code")),
            provider=self.name)
