"""The query path, assembled — stage 4 → stage 3 → stage 7 → generation.

One place where the boundaries README.md defines are actually measured:

* **boundary A** — final transcript in → context selected out. Measured as one
  span around stages 4, 3 and 7, not summed from them, because summing hides
  scheduling overhead that is real latency (LATENCY.md). The gap between
  `boundary_a_ms` and `summed_ms` is the harness cost, and it is visible.
* **boundary B** — `ttft`, first generated token. Starts where A ends.
* **boundary C** — wall clock to the last token.

Stages 5 and 6 are not built yet. They are absent from the trace rather than
faked, and `boundary_a_covers` says so in every response, so no number here can
be read as covering a stage that did not run.

    from dhvani.pipeline import Dhvani
    d = Dhvani.load("index/full")
    for ev in d.answer("गोवा में मौसम कैसा है"):
        print(ev)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from dhvani.generate.client import (REFUSAL_TOKEN, GenerationClient,
                                    GenerationConfig, GenerationUnavailable,
                                    citation_map)
from dhvani.harness.contracts import (PipelineTrace, RetrievalResult,
                                      SelectedContext)
from dhvani.retrieve.stage3 import HybridIndex, Stage3Config
from dhvani.retrieve.stage4 import QueryRewriter, Stage4Config
from dhvani.retrieve.stage7 import ChunkStore, Stage7Config, TokenCounter, select_context

# Stages inside boundary A today. Emitted with every answer so a latency number
# can never be read as covering more than it does.
BOUNDARY_A_COVERS = ["stage4_rewrite", "stage3_embed", "stage3_retrieve",
                     "stage3_fuse", "stage3_signals", "stage7_context", "harness"]
NOT_YET_IN_BOUNDARY_A = ["stage5_expansion", "stage6_rerank",
                         "guardrails_l1_l2_l3"]


@dataclass(frozen=True)
class PipelineConfig:
    stage4: Stage4Config = field(default_factory=Stage4Config)
    stage3: Stage3Config = field(default_factory=Stage3Config)
    stage7: Stage7Config = field(default_factory=Stage7Config)
    generation: GenerationConfig = field(default_factory=GenerationConfig)


@dataclass
class Retrieved:
    """Everything boundary A produced, and what it cost."""
    context: SelectedContext
    result: RetrievalResult
    trace: PipelineTrace
    citations: dict[int, str]


class Dhvani:
    """Loaded once at boot; every query reuses it. Loading is ~5 s and is a
    cold-start number, not a per-query one (README, benchmark hygiene)."""

    def __init__(self, index: HybridIndex, rewriter: QueryRewriter,
                 store: ChunkStore, counter: TokenCounter,
                 generator: GenerationClient, cfg: PipelineConfig | None = None):
        self.index, self.rewriter = index, rewriter
        self.store, self.counter = store, counter
        self.generator = generator
        self.cfg = cfg or PipelineConfig()

    @classmethod
    def load(cls, index_dir: str | Path = "index/full", threads: int = 2,
             cfg: PipelineConfig | None = None) -> "Dhvani":
        cfg = cfg or PipelineConfig()
        d = Path(index_dir)
        return cls(HybridIndex.load(d, threads=threads), QueryRewriter.load(d),
                   ChunkStore.load(d), TokenCounter(),
                   GenerationClient(cfg.generation), cfg)

    def warm(self, query: str = "warmup query") -> None:
        """Throwaway query so no cold start lands in a measured percentile."""
        self.retrieve(query)

    # -- boundary A ---------------------------------------------------------

    def retrieve(self, question: str, cfg: PipelineConfig | None = None,
                 trace: PipelineTrace | None = None) -> Retrieved:
        cfg = cfg or self.cfg
        trace = trace or PipelineTrace()
        t0 = time.perf_counter_ns()
        query, _ = self.rewriter.rewrite(question, cfg.stage4, trace)
        result, _ = self.index.search(query, cfg.stage3, trace)
        context, _ = select_context(result, self.store, self.counter,
                                    cfg.stage7, trace)
        trace.boundary_a_ms = (time.perf_counter_ns() - t0) / 1e6
        # Tier stays "standard" for every query: tiering keys on t_high/t_low/
        # t_agree, which are `OPEN` until they are swept on the eval set
        # (RAG_PIPELINE.md). Labelling queries by a threshold nobody has
        # measured would put a fabricated tier split in the results table.
        return Retrieved(context, result, trace, citation_map(context))

    # -- the whole answer ---------------------------------------------------

    def answer(self, question: str, cfg: PipelineConfig | None = None
               ) -> Iterator[dict]:
        """Yield UI events. The transport (SSE, WebSocket, a test) decides how
        to render them; this decides what they are.

        A refusal is an event, not an exception: the UI has to show *why* it
        refused, and `docs/GUARDRAILS.md` maps `kind` to the copy.
        """
        cfg = cfg or self.cfg
        t_start = time.perf_counter()
        r = self.retrieve(question, cfg)

        yield {"type": "query", "raw": r.result.query.raw,
               "rewritten": r.result.query.rewritten,
               "lang": r.result.query.lang, "method": r.result.query.method,
               "corrections": r.result.query.corrections}
        yield {"type": "retrieval",
               "boundary_a_ms": round(r.trace.boundary_a_ms, 2),
               "summed_ms": round(r.trace.summed_ms, 2),
               "boundary_a_covers": BOUNDARY_A_COVERS,
               "not_yet_in_boundary_a": NOT_YET_IN_BOUNDARY_A,
               "tier": r.trace.tier,
               "stages": [s.model_dump() for s in r.trace.stages],
               "signals": r.result.signals.model_dump(),
               "context": {"tokens": r.context.tokens,
                           "dropped": r.context.dropped,
                           "chunks": [c.model_dump() for c in r.context.chunks]}}

        if r.context.empty:
            yield {"type": "refusal", "kind": "no_context",
                   "reason": "retrieval returned nothing for this query"}
            yield self._done(t_start, None, r)
            return

        text, ttft = [], None
        try:
            for piece in self.generator.stream(question, r.context, r.trace,
                                               r.result.query.lang):
                if ttft is None:
                    ttft = (time.perf_counter() - t_start) * 1000
                text.append(piece)
                yield {"type": "token", "text": piece}
        except GenerationUnavailable as exc:
            yield {"type": "refusal", "kind": "generation_unavailable",
                   "reason": str(exc)}
            yield self._done(t_start, ttft, r)
            return

        gen = r.trace.get("generate")
        marker = bool(gen and gen.detail.get("model_refused"))
        # The marker alone is not a refusal. Measured 2026-08-19: the model
        # answered the question from three sources and *then* used the marker to
        # decline a sub-question it had inferred. Refusing there would have
        # thrown away a correct, cited answer. So the marker is reported as a
        # signal on `done`, and the refusal event fires only when nothing
        # substantive is left to show.
        if not "".join(text).strip():
            # The model was given a way to say "the sources do not answer
            # this" and used it — designed behaviour, so it is a refusal rather
            # than an answer. The marker is already stripped from the stream by
            # `ThinkFilter`: the model does not reliably emit it alone, and
            # prefixed prose to it on the first live run (2026-08-19).
            yield {"type": "refusal", "kind": "model_refused",
                   "reason": "sources do not contain the answer"}
        yield self._done(t_start, ttft, r)

    def _done(self, t_start: float, ttft: float | None, r: Retrieved) -> dict:
        gen = r.trace.get("generate")
        return {"type": "done",
                "boundary_a_ms": round(r.trace.boundary_a_ms, 2),
                "model_signalled_insufficient": bool(
                    gen and gen.detail.get("model_refused")),
                "ttft_ms": round(ttft, 2) if ttft is not None else None,
                "wall_clock_ms": round((time.perf_counter() - t_start) * 1000, 2),
                "citations": r.citations,
                "stages": [s.model_dump() for s in r.trace.stages]}


def ablate(cfg: PipelineConfig, **off: bool) -> PipelineConfig:
    """`ablate(cfg, stage4=False, stage7=False)` — one arm of the ablation
    table. Every stage is toggleable or it cannot appear in that table."""
    out = cfg
    if "stage4" in off:
        out = replace(out, stage4=replace(out.stage4, enabled=off["stage4"]))
    if "stage7" in off:
        out = replace(out, stage7=replace(out.stage7, enabled=off["stage7"]))
    if "generation" in off:
        out = replace(out, generation=replace(out.generation,
                                              enabled=off["generation"]))
    return out
