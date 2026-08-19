"""L4 — is the sentence the model just wrote actually in the passages?

Spec: `docs/GUARDRAILS.md` L4. Step 1 only: character-aware n-gram overlap
between each generated sentence and the selected chunks. Step 2 (a local NLI
cross-encoder for the ambiguous band) is **not built** — it was specified to run
on "the ONNX runtime already warm for the reranker", and ADR-027 deferred the
reranker, so it would be a new model load on the critical path. Ambiguous
sentences are therefore reported as ambiguous and kept, never silently promoted.

**Marked, not buffered.** The obvious implementation holds each sentence until
it is judged. That puts the whole first sentence in front of boundary B, and
`ttft` is a headline number. So tokens stream the moment they arrive, and the
verdict for a sentence is emitted when the sentence closes — the UI marks a
sentence it has already drawn. The one thing enforcement still does at the end
is replace the answer wholesale when most of it is ungrounded, which is exactly
the case where showing it was wrong anyway.

    g = Grounder(ctx.chunks)
    for piece in stream:
        for verdict in g.feed(piece):
            ...
    g.flush(); g.verdict()      # -> Verdict('not_grounded') or PASS
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dhvani.build.chunk import TOKEN_PATTERN, normalize, split_sentences
from dhvani.guardrails.checks import PASS, Verdict
from dhvani.harness.contracts import ContextChunk

_TOKEN_RE = re.compile(TOKEN_PATTERN)
# A generated sentence closes on any of these; the same terminators the chunker
# splits on, so a sentence here means what it means at build time.
_CLOSERS = ".!?।॥…\n"


@dataclass(frozen=True)
class GroundingConfig:
    enabled: bool = True
    n: int = 3                   # n-gram size
    t_high: float = 0.30         # >= this share of n-grams found -> grounded
    # MEASURED 2026-08-19 (`docs/results/2026-08-19-grounding-calibration.json`):
    # swept over 60 real answers scored against their own context and against
    # another query's. Every point in the sweep catches 100% of the mismatched
    # pairs; 0.05 is the point that replaces the fewest real answers (20.0%,
    # against 25.0% at 0.10 and 33.3% at 0.30).
    t_low: float = 0.05          # <= this -> ungrounded
    # Fraction of *judged* sentences that may be ungrounded before the whole
    # answer is replaced. GUARDRAILS.md: "if a majority of sentences are dropped
    # the whole answer is replaced" — a partially hallucinated answer with the
    # hallucinations quietly removed is still a broken answer.
    # Flat across the whole sweep at 0.34-0.90: the answers L4 replaces have no
    # grounded sentence at all, so where the majority line sits changes nothing.
    # It stays at the specified half.
    max_ungrounded: float = 0.50
    # Sentences this short are citations, list markers or "yes." — too short to
    # produce n-grams, and scoring them would put noise in the majority vote.
    min_tokens: int = 6


def ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    toks = _TOKEN_RE.findall(normalize(text).lower())
    if len(toks) < n:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


@dataclass
class SentenceVerdict:
    text: str
    overlap: float
    label: str                   # grounded | ambiguous | ungrounded | skipped
    chunk_id: str | None = None  # best-overlapping chunk, for the citation

    def as_event(self) -> dict:
        return {"type": "grounding", "label": self.label,
                "overlap": round(self.overlap, 4), "chunk_id": self.chunk_id,
                "text": self.text}


class Grounder:
    """Fed the token stream, emits one verdict per completed sentence."""

    def __init__(self, chunks: list[ContextChunk],
                 cfg: GroundingConfig | None = None):
        self.cfg = cfg or GroundingConfig()
        self.chunks = chunks
        # Per chunk, so a grounded sentence can name *which* passage carries it
        # — L4 step 3 is citation enforcement, and "grounded somewhere" is not a
        # citation.
        self._grams = [(c.chunk_id, ngrams(c.text, self.cfg.n)) for c in chunks]
        self._all: set[tuple[str, ...]] = set().union(*[g for _, g in self._grams]) \
            if self._grams else set()
        self._buf = ""
        self.verdicts: list[SentenceVerdict] = []

    # -- streaming ----------------------------------------------------------

    def feed(self, piece: str) -> list[SentenceVerdict]:
        """Buffer, and judge every sentence the buffer now completes."""
        if not self.cfg.enabled:
            return []
        self._buf += piece
        out = []
        while True:
            cut = self._closing_index(self._buf)
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut + 1], self._buf[cut + 1:]
            v = self.judge(sentence)
            if v is not None:
                out.append(v)
        return out

    def flush(self) -> list[SentenceVerdict]:
        """Judge whatever is left when the stream ends without a terminator."""
        if not self.cfg.enabled or not self._buf.strip():
            return []
        v = self.judge(self._buf)
        self._buf = ""
        return [v] if v is not None else []

    @staticmethod
    def _closing_index(buf: str) -> int | None:
        for i, ch in enumerate(buf):
            # A decimal point or an abbreviation is not a sentence end. Splitting
            # on it would judge "3." against the passages and score it 0.
            if ch in _CLOSERS and not (ch == "." and i + 1 < len(buf)
                                       and buf[i + 1].isdigit()):
                return i
        return None

    # -- the check ----------------------------------------------------------

    def judge(self, sentence: str) -> SentenceVerdict | None:
        text = sentence.strip()
        if not text:
            return None
        grams = ngrams(text, self.cfg.n)
        if len(_TOKEN_RE.findall(text)) < self.cfg.min_tokens or not grams:
            v = SentenceVerdict(text, 0.0, "skipped")
            self.verdicts.append(v)
            return v
        best_id, best = None, 0.0
        for chunk_id, chunk_grams in self._grams:
            hit = len(grams & chunk_grams) / len(grams)
            if hit > best:
                best_id, best = chunk_id, hit
        # Scored against every selected chunk at once: a sentence that fuses two
        # passages is grounded, and per-chunk maxima would call it a hallucination.
        union = len(grams & self._all) / len(grams)
        label = ("grounded" if union >= self.cfg.t_high else
                 "ungrounded" if union <= self.cfg.t_low else "ambiguous")
        v = SentenceVerdict(text, union, label, best_id if label != "ungrounded" else None)
        self.verdicts.append(v)
        return v

    # -- step 3: enforcement ------------------------------------------------

    @property
    def judged(self) -> list[SentenceVerdict]:
        return [v for v in self.verdicts if v.label != "skipped"]

    def verdict(self) -> Verdict:
        judged = self.judged
        if not self.cfg.enabled or not judged:
            return PASS
        ungrounded = sum(v.label == "ungrounded" for v in judged)
        share = ungrounded / len(judged)
        if share > self.cfg.max_ungrounded:
            return Verdict("not_grounded",
                           f"{ungrounded}/{len(judged)} sentences below "
                           f"t_low {self.cfg.t_low}",
                           {"ungrounded": ungrounded, "judged": len(judged),
                            "share": round(share, 4)})
        return PASS

    def summary(self) -> dict:
        judged = self.judged
        return {"sentences": len(self.verdicts), "judged": len(judged),
                "grounded": sum(v.label == "grounded" for v in judged),
                "ambiguous": sum(v.label == "ambiguous" for v in judged),
                "ungrounded": sum(v.label == "ungrounded" for v in judged),
                "mean_overlap": round(
                    sum(v.overlap for v in judged) / len(judged), 4) if judged else 0.0}
