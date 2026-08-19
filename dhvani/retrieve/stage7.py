"""Stage 7 — context selection. Boundary A ends here.

Spec: `docs/RAG_PIPELINE.md` stage 7. Selection, not retrieval: stages 3 (and
later 5, 6) decided *what* is relevant; this decides what actually fits in the
generation call and hands every survivor an id a citation can resolve.

Three filters, in this order, each counted separately so the ablation table can
attribute the drop:

1. **Overlap dedupe** — `overlap_with` was written at build time exactly for
   this (CHUNKING.md). Four strategies over one passage guarantee near-copies;
   without this the window fills with the same sentence three times.
2. **Jaccard dedupe** — the net for near-duplicates that share no chunk id,
   which is every cross-strategy pair the build did not link.
3. **Token budget** — counted with a tokenizer, not a word count.

    from dhvani.retrieve.stage7 import ChunkStore, select_context
    store = ChunkStore.load("index/full")
    ctx, trace = select_context(result, store)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dhvani.harness.contracts import (ContextChunk, PipelineTrace,
                                      RetrievalResult, SelectedContext, stage)


@dataclass(frozen=True)
class Stage7Config:
    enabled: bool = True
    max_chunks: int = 6
    token_budget: int = 1500
    dedupe: bool = True
    dedupe_threshold: float = 0.85   # Jaccard over shingles
    shingle: int = 4                 # characters, not words — Indic scripts do
                                     # not put spaces where a word tokenizer
                                     # expects them


def shingles(text: str, n: int) -> set[str]:
    t = " ".join(text.split())
    return {t[i:i + n] for i in range(max(len(t) - n + 1, 1))}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class ChunkStore:
    """Text and metadata for an index row, read lazily off the parquet.

    `memory_map=True` is the whole design. The text column is 1.2 GB and
    `parent_text` another 1.0 GB — resident, that is a quarter of the 8 GB
    deploy box on top of FAISS's 1.7 GB (R5). Mapped, the pages the OS actually
    faults in are the handful of chunks a query cites, and the kernel reclaims
    them under pressure. This is R5's "chunk text to an mmap'd store" lever,
    taken up front because pyarrow gives it away for free.

    Measured on the dev box: 0.3 s to map, 0.06 ms per row lookup.
    """

    COLUMNS = ["chunk_id", "text", "parent_text", "doc_id", "lang", "strategy",
               "overlap_with"]

    def __init__(self, table):
        self.t = table

    @classmethod
    def load(cls, index_dir: str | Path = "index") -> "ChunkStore":
        import pyarrow.parquet as pq
        return cls(pq.read_table(Path(index_dir) / "chunks.parquet",
                                 columns=cls.COLUMNS, memory_map=True))

    def __len__(self) -> int:
        return self.t.num_rows

    def get(self, row: int) -> dict:
        """One row as plain Python. `text` is `retrieved_text`: S2 embeds narrow
        and returns wide, so the unit that was scored is not always the unit
        that goes in the window (contracts.Chunk.retrieved_text)."""
        r = {c: self.t.column(c)[row].as_py() for c in self.COLUMNS}
        r["text"] = r.pop("parent_text") or r["text"]
        r["overlap_with"] = r["overlap_with"] or []
        return r


class TokenCounter:
    """Counts with the embedder's tokenizer, loaded without truncation.

    ponytail: this is a *proxy*. RAG_PIPELINE.md asks for the generation model's
    tokenizer, and Sarvam publishes no local one — so the honest options were a
    proxy that is labelled or a word count that is not. The trace carries the
    tokenizer's name so no table can claim otherwise. Upgrade path: if the
    provider ships a tokenizer.json, point `spec.tokenizer` at it here only.
    """

    def __init__(self, tokenizer_path: str | None = None):
        from tokenizers import Tokenizer

        from dhvani.embed import DEFAULT_MODEL, MODELS
        spec = MODELS[DEFAULT_MODEL]
        self.name = f"{DEFAULT_MODEL} (proxy)"
        self.tok = Tokenizer.from_file(tokenizer_path or spec.tokenizer)

    def count(self, text: str) -> int:
        return len(self.tok.encode(text, add_special_tokens=False).ids)


def select_context(result: RetrievalResult, store: ChunkStore,
                   counter: TokenCounter | None = None,
                   cfg: Stage7Config | None = None,
                   trace: PipelineTrace | None = None
                   ) -> tuple[SelectedContext, PipelineTrace]:
    cfg = cfg or Stage7Config()
    trace = trace or PipelineTrace()
    counter = counter or TokenCounter()
    ctx = SelectedContext()

    with stage(trace, "stage7_context", enabled=cfg.enabled,
               max_chunks=cfg.max_chunks, token_budget=cfg.token_budget,
               dedupe=cfg.dedupe) as st:
        if not cfg.enabled:
            st.detail["reason"] = "disabled — ablation arm '− stage 7'"
            return ctx, trace

        st.detail["tokenizer"] = counter.name
        seen_ids: set[tuple[str, str]] = set()   # (chunk_id, lang) — an id is
        kept_shingles: list[set[str]] = []       # only unique within a corpus
        used = 0

        for cand in result.chunks:
            if len(ctx.chunks) >= cfg.max_chunks:
                ctx.dropped_capped += 1
                continue
            if cand.row is None:
                continue
            row = store.get(cand.row)
            key = (row["chunk_id"], row["lang"])

            if cfg.dedupe:
                if key in seen_ids or any((o, row["lang"]) in seen_ids
                                          for o in row["overlap_with"]):
                    ctx.dropped_overlap += 1
                    continue
                sh = shingles(row["text"], cfg.shingle)
                if any(jaccard(sh, k) >= cfg.dedupe_threshold
                       for k in kept_shingles):
                    ctx.dropped_jaccard += 1
                    continue
            else:
                sh = set()

            tokens = counter.count(row["text"])
            if used + tokens > cfg.token_budget:
                # Skip, do not stop: a long chunk at rank 2 should not evict
                # every shorter one behind it.
                ctx.dropped_budget += 1
                continue

            used += tokens
            seen_ids.add(key)
            if cfg.dedupe:
                kept_shingles.append(sh)
            ctx.chunks.append(ContextChunk(
                chunk_id=row["chunk_id"], row=cand.row, text=row["text"],
                score=cand.score, rank=len(ctx.chunks), lang=row["lang"],
                doc_id=row["doc_id"], strategy=row["strategy"], tokens=tokens))

        ctx.tokens = used
        st.detail.update(kept=len(ctx.chunks), tokens=used,
                         dropped_overlap=ctx.dropped_overlap,
                         dropped_jaccard=ctx.dropped_jaccard,
                         dropped_budget=ctx.dropped_budget,
                         dropped_capped=ctx.dropped_capped,
                         considered=len(result.chunks))
        if ctx.empty:
            # Not an error — stage 3 legitimately returns nothing on an
            # out-of-corpus query. The refusal is the caller's to make; what
            # this guarantees is that it is a visible state, not an empty list
            # quietly handed to a generation call.
            st.degraded = True
            st.detail["reason"] = "no chunks survived — refusal path"

    return ctx, trace
