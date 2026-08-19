"""Typed contracts every stage speaks, and the trace every stage emits.

Two rules from CLAUDE.md are enforced here rather than by convention:

* **Every stage is toggleable, timed, and ablatable.** `StageTrace` carries
  `enabled`, so a stage that was switched off still appears in the trace with a
  reason instead of vanishing — an ablation table needs the row, not a gap.
* **Instrumentation is not a later task.** `stage()` is the only way a stage
  runs, so timing exists from the first commit rather than measuring the code
  written to add timing.
"""

from __future__ import annotations

import time
import traceback
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

Strategy = Literal["s1_passage", "s2_sentence_window", "s3_semantic",
                   "s4_query_context", "s0_fixed_window"]
Tier = Literal["fast", "standard", "escalated"]


# --------------------------------------------------------------------------
# tracing
# --------------------------------------------------------------------------

class StageTrace(BaseModel):
    """One stage's record. Emitted whether the stage succeeded, degraded,
    failed, or was switched off."""

    stage: str
    duration_ms: float = 0.0
    ok: bool = True
    enabled: bool = True
    degraded: bool = False
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def status(self) -> str:
        if not self.enabled:
            return "off"
        if not self.ok:
            return "failed"
        return "degraded" if self.degraded else "ok"


class PipelineTrace(BaseModel):
    """The whole query path. `boundary_a_ms` is measured as one span rather
    than summed from stages — summing hides async scheduling overhead, which is
    real latency (LATENCY.md)."""

    query_id: str | None = None
    tier: Tier = "standard"
    boundary_a_ms: float = 0.0
    stages: list[StageTrace] = Field(default_factory=list)

    def add(self, trace: StageTrace) -> StageTrace:
        self.stages.append(trace)
        return trace

    def get(self, stage: str) -> StageTrace | None:
        return next((s for s in self.stages if s.stage == stage), None)

    @property
    def stage_ms(self) -> dict[str, float]:
        return {s.stage: s.duration_ms for s in self.stages}

    @property
    def summed_ms(self) -> float:
        """Deliberately separate from boundary_a_ms. The gap between them is the
        harness overhead, and it is a number worth being able to see."""
        return sum(s.duration_ms for s in self.stages)


@contextmanager
def stage(trace: PipelineTrace, name: str, enabled: bool = True,
          **detail: Any) -> Iterator[StageTrace]:
    """Run a stage, time it, and record it no matter how it ends.

    Failures are recorded and re-raised — the *degradation* decision belongs to
    the caller, which knows what its fallback is. What this guarantees is that a
    stage never leaves the trace without a row.

        with stage(trace, "stage3_retrieve", enabled=cfg.stage3) as st:
            st.detail["k_dense"] = 100
            ...
    """
    st = StageTrace(stage=name, enabled=enabled, detail=dict(detail))
    trace.add(st)
    if not enabled:
        yield st
        return
    t0 = time.perf_counter_ns()
    try:
        yield st
    except Exception as exc:
        st.ok = False
        st.error = f"{type(exc).__name__}: {exc}"
        st.detail.setdefault("traceback", traceback.format_exc(limit=3))
        raise
    finally:
        st.duration_ms = (time.perf_counter_ns() - t0) / 1e6


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------

class Chunk(BaseModel):
    """The indexed unit. Schema is CHUNKING.md's, kept in sync with it.

    `char_span` exists so a citation can highlight the exact source span in the
    UI rather than pointing vaguely at a chunk.
    """

    chunk_id: str
    text: str
    doc_id: str
    passage_idx: int
    strategy: Strategy
    ordinal: int = 0
    lang: str
    script: str
    is_selected: bool = False
    query_type: str = ""
    split: str = "validation"
    token_count: int = 0
    char_span: tuple[int, int] = (0, 0)
    overlap_with: list[str] = Field(default_factory=list)
    parent_text: str | None = None

    @property
    def retrieved_text(self) -> str:
        """What stage 7 puts in the context window. S2 embeds narrow and returns
        wide, so the returned unit is not always the embedded one."""
        return self.parent_text or self.text


# --------------------------------------------------------------------------
# query path
# --------------------------------------------------------------------------

class Query(BaseModel):
    raw: str
    rewritten: str | None = None
    lang: str | None = None
    script: str | None = None
    method: str = "passthrough"      # phonetic | llm | passthrough
    corrections: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return self.rewritten or self.raw


class ScoredChunk(BaseModel):
    # `row` is the index-store row: chunks.parquet row i is FAISS id i is BM25
    # doc i. It is carried because `chunk_id` is not a lookup key — the same id
    # exists once per language corpus (see the change log, 18 Aug) — and every
    # stage after 3 has to fetch text, metadata or labels for a hit.
    chunk_id: str
    row: int | None = None
    score: float
    rank: int
    dense_rank: int | None = None
    bm25_rank: int | None = None
    stage: str = "stage3"


class ConfidenceSignals(BaseModel):
    """Computed from numbers stage 3 already has in hand, so they cost
    arithmetic rather than a model call. These drive the tier decision."""

    top1: float = 0.0
    margin_1_5: float = 0.0
    kendall_tau: float = 0.0
    n_candidates: int = 0
    dense_bm25_overlap: int = 0


class RetrievalResult(BaseModel):
    query: Query
    chunks: list[ScoredChunk] = Field(default_factory=list)
    signals: ConfidenceSignals = Field(default_factory=ConfidenceSignals)

    @property
    def chunk_ids(self) -> list[str]:
        return [c.chunk_id for c in self.chunks]


class ContextChunk(BaseModel):
    """One chunk as stage 7 hands it to generation: text resolved, id attached.

    `row` travels with it because a citation the UI renders must be resolvable
    back to the index store, and `chunk_id` alone is not a key (see ScoredChunk).
    """

    chunk_id: str
    row: int
    text: str
    score: float
    rank: int
    lang: str = ""
    doc_id: str = ""
    strategy: str = ""
    tokens: int = 0


class SelectedContext(BaseModel):
    """Stage 7 out — boundary A ends here (DESIGN.md).

    The three drop counters are separate on purpose: "dropped: 12" cannot tell
    an ablation table whether dedupe or the token budget did the work.
    """

    chunks: list[ContextChunk] = Field(default_factory=list)
    tokens: int = 0
    dropped_overlap: int = 0
    dropped_jaccard: int = 0
    dropped_budget: int = 0
    dropped_capped: int = 0     # hit `max_chunks`, never weighed against the
                                # token budget — kept apart because otherwise a
                                # budget sweep reads as if the budget did work
                                # the chunk cap did

    @property
    def dropped(self) -> int:
        return (self.dropped_overlap + self.dropped_jaccard
                + self.dropped_budget + self.dropped_capped)

    @property
    def empty(self) -> bool:
        """Zero chunks survived — the refusal path, not an empty generation
        call (RAG_PIPELINE.md stage 7, Failure)."""
        return not self.chunks


class Transcript(BaseModel):
    """What an `STTProvider` returns. Boundary A's clock starts at the first
    `is_final=True` transcript (DESIGN.md, data flow step 3).

    `lang` is the provider's own detection, in its own vocabulary — normalized
    to the corpus tag by the provider adapter, because stage 4 and the
    generation prompt both key on `hin_Deva`, not `hi-IN`.
    """

    text: str
    is_final: bool = True
    confidence: float | None = None
    # Duration of the audio itself. 0 until something decodes the container —
    # the batch path never needs to, and a field holding request latency under
    # this name would be a lie the UI then prints.
    audio_ms: int = 0
    latency_ms: int = 0          # provider round trip, which is what we can see
    lang: str | None = None
    provider: str = ""
