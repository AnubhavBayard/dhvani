"""Stage 3 — cheap, high-recall hybrid retrieval with RRF fusion.

Spec: `docs/RAG_PIPELINE.md` stage 3. Dense (FAISS `IndexHNSWSQ`, ADR-015) and
lexical (`bm25s`) run concurrently, then fuse by Reciprocal Rank Fusion — dense
cosine and BM25 scores are not on comparable scales and normalizing them needs
per-corpus tuning RRF does not (ADR-004).

Row order is the join key: `chunks.parquet` row i is FAISS id i is BM25 doc i,
because `build_index.py` writes all three from the same `all_chunks` list.

Rescoring the top-50 with full-precision vectors is specified but **off** — the
build persists no fp32 vectors, and 4M x 384 x 4 B is 6.1 GB against an 8 GB box
(ADR-012). It appears in the trace as a disabled stage with that reason rather
than vanishing, so the ablation table keeps its row.

    from dhvani.retrieve.stage3 import HybridIndex, Stage3Config
    idx = HybridIndex.load("index")
    result, trace = idx.search("गोवा में मौसम कैसा है")
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dhvani.build.chunk import TOKEN_PATTERN
from dhvani.embed import DEFAULT_MODEL, Embedder
from dhvani.harness.contracts import (ConfidenceSignals, PipelineTrace, Query,
                                      RetrievalResult, ScoredChunk, stage)


@dataclass(frozen=True)
class Stage3Config:
    k_dense: int = 100
    k_bm25: int = 100
    k_out: int = 50
    rrf_k: int = 60
    ef_search: int = 64          # 256 on the escalated tier (RAG_PIPELINE.md)
    dense: bool = True           # ablation: dense-only / bm25-only / fused
    bm25: bool = True
    rescore_top: int = 0         # 0 = off; no fp32 vectors persisted (ADR-017)


def rrf(rankings: list[list[int]], k: int) -> dict[int, float]:
    """Reciprocal Rank Fusion over 1-based ranks. A doc missing from a ranking
    contributes nothing from it rather than a penalty term."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            fused[doc] = fused.get(doc, 0.0) + 1.0 / (k + rank)
    return fused


def kendall_tau(a: list[int], b: list[int]) -> float:
    """Rank correlation between two orderings, over the docs both returned.

    Agreement between the dense and lexical orderings is a confidence signal:
    when both retrievers independently rank the same docs the same way, the
    fused top is trustworthy; when they disagree the query is a tier-escalation
    candidate.

    ponytail: O(n^2) over at most `k_out` ids — a few thousand comparisons,
    cheaper than importing scipy for `kendalltau`. Revisit if k_out grows past
    a few hundred.
    """
    rank_b = {d: i for i, d in enumerate(b)}
    common = [d for d in a if d in rank_b]
    if len(common) < 2:
        return 0.0
    rank_a = {d: i for i, d in enumerate(a)}
    concordant = discordant = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            x, y = common[i], common[j]
            s = (rank_a[x] - rank_a[y]) * (rank_b[x] - rank_b[y])
            if s > 0:
                concordant += 1
            elif s < 0:
                discordant += 1
    n = concordant + discordant
    return (concordant - discordant) / n if n else 0.0


class HybridIndex:
    """The query-time view of a built index. Loading is build-time work done
    once at startup; nothing here reads parquet or embeds a corpus."""

    def __init__(self, chunk_ids: list[str], faiss_index, bm25, embedder: Embedder):
        self.chunk_ids = chunk_ids
        self.faiss = faiss_index
        self.bm25 = bm25
        self.embedder = embedder
        self._pool = ThreadPoolExecutor(max_workers=2)

    @classmethod
    def load(cls, index_dir: str | Path = "index", model: str = DEFAULT_MODEL,
             threads: int = 2) -> "HybridIndex":
        import bm25s
        import faiss
        import pyarrow.parquet as pq

        d = Path(index_dir)
        chunk_ids = pq.read_table(d / "chunks.parquet",
                                  columns=["chunk_id"]).column("chunk_id").to_pylist()
        index = faiss.read_index(str(d / "hnsw_sq8.faiss"))
        retriever = bm25s.BM25.load(str(d / "bm25"), load_corpus=False)
        if index.ntotal != len(chunk_ids):
            raise ValueError(f"index/chunk mismatch: {index.ntotal} vectors, "
                             f"{len(chunk_ids)} chunks — rebuild")
        return cls(chunk_ids, index, retriever, Embedder(model, threads=threads))

    # -- the two retrievers -------------------------------------------------

    def _dense(self, vector: np.ndarray, cfg: Stage3Config
               ) -> tuple[list[int], list[float]]:
        """Ids *and* similarities. The scores are dropped by RRF, which fuses on
        rank alone, but the guardrails need a number on a comparable scale: an
        RRF top1 is ~1/(k+1) for every query whether the hit is relevant or not
        (GUARDRAILS.md L2/L3). The index is inner-product over L2-normalized
        vectors, so these are cosines in [-1, 1]."""
        self.faiss.hnsw.efSearch = cfg.ef_search
        sims, ids = self.faiss.search(vector[None, :], cfg.k_dense)
        keep = [(int(i), float(s)) for i, s in zip(ids[0], sims[0]) if i >= 0]
        return [i for i, _ in keep], [s for _, s in keep]

    def _lexical(self, text: str, cfg: Stage3Config) -> list[int]:
        """BM25 top-k, selected over the documents the query terms actually
        touch rather than over the whole corpus.

        `bm25s.retrieve` scores in ~2 ms and then spends ~130 ms in
        `np.argpartition` over all 3.28M documents — measured 18 Aug, 97% of
        boundary A (ADR-022). Scoring is unchanged: this calls the library's own
        `get_scores`, and only the selection is done on the ~114k candidates
        with a non-zero score instead of on the full array.

        Ties are broken by ascending row id, which `bm25s` leaves arbitrary. The
        set is identical either way; fixing the order is what makes two runs of
        one config produce the same fused ranking (LATENCY.md, determinism).
        """
        import bm25s

        words = bm25s.tokenize([text], show_progress=False, return_ids=False,
                               token_pattern=TOKEN_PATTERN)[0]
        if not words:
            # Every term was a stopword or below the tokenizer's 2-character
            # floor. Lexical retrieval has nothing to say; dense still does.
            return []
        scores = self.bm25.get_scores(words)
        cand = np.flatnonzero(scores)
        k = min(cfg.k_bm25, len(cand))
        if not k:
            return []
        if len(cand) > k:
            cand = cand[np.argpartition(scores[cand], -k)[-k:]]
        return [int(i) for i in cand[np.lexsort((cand, -scores[cand]))][:k]]

    # -- the stage ----------------------------------------------------------

    def search(self, query: Query | str, cfg: Stage3Config | None = None,
               trace: PipelineTrace | None = None
               ) -> tuple[RetrievalResult, PipelineTrace]:
        cfg = cfg or Stage3Config()
        q = Query(raw=query) if isinstance(query, str) else query
        trace = trace or PipelineTrace()

        with stage(trace, "stage3_embed") as st:
            vector = self.embedder.encode_query(q.text)
            st.detail["dims"] = int(vector.shape[0])

        dense_ids: list[int] = []
        dense_scores: list[float] = []
        bm25_ids: list[int] = []
        with stage(trace, "stage3_retrieve", k_dense=cfg.k_dense,
                   k_bm25=cfg.k_bm25, ef_search=cfg.ef_search) as st:
            # Two CPU-bound searches; running them in sequence wastes the
            # smaller one's entire duration (RAG_PIPELINE.md stage 3).
            fut_dense = self._pool.submit(self._dense, vector, cfg) if cfg.dense else None
            fut_bm25 = self._pool.submit(self._lexical, q.text, cfg) if cfg.bm25 else None

            errors = {}
            for name, fut in (("dense", fut_dense), ("bm25", fut_bm25)):
                if fut is None:
                    continue
                try:
                    ids = fut.result()
                except Exception as exc:  # noqa: BLE001 — one half may fail alone
                    errors[name] = f"{type(exc).__name__}: {exc}"
                    continue
                if name == "dense":
                    dense_ids, dense_scores = ids
                else:
                    bm25_ids = ids

            # Degradation ladder: one retriever down is a degraded answer, both
            # down is a failure the caller must refuse on. Never a silent empty.
            if errors:
                st.degraded = True
                st.detail["errors"] = errors
            if len(errors) == 2 or (not dense_ids and not bm25_ids and not errors
                                    and (cfg.dense or cfg.bm25)):
                st.ok = False
                st.error = "; ".join(errors.values()) or "both retrievers returned nothing"
            st.detail["dense_hits"] = len(dense_ids)
            st.detail["bm25_hits"] = len(bm25_ids)

        with stage(trace, "stage3_fuse", rrf_k=cfg.rrf_k, k_out=cfg.k_out) as st:
            rankings = [r for r in (dense_ids, bm25_ids) if r]
            fused = rrf(rankings, cfg.rrf_k)
            order = sorted(fused, key=lambda d: (-fused[d], d))[:cfg.k_out]
            dense_rank = {d: i for i, d in enumerate(dense_ids)}
            bm25_rank = {d: i for i, d in enumerate(bm25_ids)}
            chunks = [ScoredChunk(chunk_id=self.chunk_ids[d], row=d,
                                  score=fused[d], rank=r,
                                  dense_rank=dense_rank.get(d), bm25_rank=bm25_rank.get(d))
                      for r, d in enumerate(order)]
            st.detail["fused"] = len(fused)
            st.detail["returned"] = len(chunks)

        with stage(trace, "stage3_signals") as st:
            scores = [c.score for c in chunks]
            overlap = set(dense_ids) & set(bm25_ids)
            signals = ConfidenceSignals(
                top1=scores[0] if scores else 0.0,
                dense_top1=dense_scores[0] if dense_scores else 0.0,
                # gap between the best and the fifth: a flat top-5 means the
                # retriever has no opinion, which is what tiering keys on.
                margin_1_5=(scores[0] - scores[4]) if len(scores) >= 5 else 0.0,
                kendall_tau=kendall_tau(dense_ids, bm25_ids),
                n_candidates=len(fused),
                dense_bm25_overlap=len(overlap),
            )
            st.detail.update(signals.model_dump())

        # Specified, not built: no fp32 vectors are persisted (ADR-017). The row
        # stays in the trace so the ablation table has it.
        with stage(trace, "stage3_rescore", enabled=bool(cfg.rescore_top)) as st:
            st.detail["reason"] = "fp32 vectors not persisted — ADR-017"

        return RetrievalResult(query=q, chunks=chunks, signals=signals), trace
