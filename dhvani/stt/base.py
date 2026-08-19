"""The STT provider interface, and the one place a provider is chosen.

The brief asks for a swappable speech layer, and an interface with a single
implementation is a wrapper rather than an abstraction (ADR-003) — so there are
two, and `tests/test_stt.py` runs the same audio through both.

Every provider is bounded the same way: hard timeout, bounded retries on
transport faults only, and a defined fallback. STT sits at the front of the
pipeline, so an unbounded retry here is a user staring at a dead microphone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from dhvani.harness.contracts import PipelineTrace, Transcript, stage

# Sarvam and ElevenLabs both speak BCP-47; the index speaks the corpus's own
# tags. One mapping, used by every provider, so a language never means two
# different things in one trace.
BCP47_TO_CORPUS = {
    "hi-IN": "hin_Deva", "bn-IN": "ben_Beng", "ta-IN": "tam_Taml",
    "en-IN": "eng_Latn", "en-US": "eng_Latn", "en-GB": "eng_Latn",
    "hin": "hin_Deva", "ben": "ben_Beng", "tam": "tam_Taml", "eng": "eng_Latn",
    "hi": "hin_Deva", "bn": "ben_Beng", "ta": "tam_Taml", "en": "eng_Latn",
}

# Trust boundary. The endpoint is unauthenticated, the payload is user-supplied
# binary, and it is forwarded to a paid API — so it is capped here rather than
# wherever the provider happens to complain. ~60 s of opus is well under this.
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def to_corpus_lang(code: str | None) -> str | None:
    if not code:
        return None
    return BCP47_TO_CORPUS.get(code) or BCP47_TO_CORPUS.get(code.split("-")[0])


class STTUnavailable(RuntimeError):
    """Every provider failed. The UI falls back to the text box — the
    degradation ladder's first rung (DESIGN.md)."""


@dataclass(frozen=True)
class STTConfig:
    enabled: bool = True
    provider: str = "sarvam"
    fallback: str | None = "elevenlabs"
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 30.0
    retries: int = 1
    language: str | None = None      # None = let the provider detect


class STTProvider(Protocol):
    name: str
    env_key: str

    def transcribe(self, audio: bytes, filename: str, cfg: STTConfig,
                   client: httpx.Client) -> Transcript:
        ...


def available(p: STTProvider) -> bool:
    return bool(os.environ.get(p.env_key, "").strip())


class STT:
    """The provider ladder. Chooses, retries, falls back, traces."""

    def __init__(self, cfg: STTConfig | None = None,
                 client: httpx.Client | None = None,
                 providers: dict[str, STTProvider] | None = None):
        self.cfg = cfg or STTConfig()
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self.cfg.read_timeout_s,
                                  connect=self.cfg.connect_timeout_s))
        if providers is None:
            from dhvani.stt.elevenlabs import ElevenLabsSTT
            from dhvani.stt.sarvam import SarvamSTT
            providers = {"sarvam": SarvamSTT(), "elevenlabs": ElevenLabsSTT()}
        self.providers = providers

    def _ladder(self) -> list[STTProvider]:
        names = [self.cfg.provider]
        if self.cfg.fallback and self.cfg.fallback != self.cfg.provider:
            names.append(self.cfg.fallback)
        return [self.providers[n] for n in names if n in self.providers]

    def transcribe(self, audio: bytes, filename: str = "audio.webm",
                   trace: PipelineTrace | None = None) -> Transcript:
        cfg = self.cfg
        trace = trace or PipelineTrace()
        with stage(trace, "stt", enabled=cfg.enabled,
                   provider=cfg.provider) as st:
            if not cfg.enabled:
                st.detail["reason"] = "disabled — text input arm"
                raise STTUnavailable("stt disabled")
            if not audio:
                st.ok = False
                st.error = "empty audio"
                raise STTUnavailable("empty audio")
            if len(audio) > MAX_AUDIO_BYTES:
                st.ok = False
                st.error = f"audio too large: {len(audio)} bytes"
                raise STTUnavailable(st.error)
            st.detail["bytes"] = len(audio)

            errors: dict[str, str] = {}
            for p in self._ladder():
                if not available(p):
                    errors[p.name] = f"{p.env_key} not set"
                    continue
                for attempt in range(cfg.retries + 1):
                    try:
                        tr = p.transcribe(audio, filename, cfg, self._client)
                    except httpx.HTTPStatusError as exc:
                        errors[f"{p.name}#{attempt}"] = str(exc)
                        if exc.response.status_code < 500:
                            break            # 4xx is our bug, not theirs
                        continue
                    except httpx.HTTPError as exc:
                        errors[f"{p.name}#{attempt}"] = f"{type(exc).__name__}: {exc}"
                        continue
                    if not tr.text.strip():
                        # Silence, or the mic recorded nothing usable. Not a
                        # provider fault and not worth a retry — the caller
                        # shows "couldn't catch that" (DESIGN.md ladder).
                        errors[f"{p.name}#{attempt}"] = "empty transcript"
                        break
                    if errors:
                        st.degraded = True
                        st.detail["fell_back_from"] = dict(errors)
                    st.detail.update(provider_used=tr.provider, lang=tr.lang,
                                     chars=len(tr.text),
                                     confidence=tr.confidence)
                    return tr

            st.detail["errors"] = errors
            st.ok = False
            st.error = "; ".join(f"{k}: {v}" for k, v in errors.items())
            raise STTUnavailable(st.error or "no stt provider")
