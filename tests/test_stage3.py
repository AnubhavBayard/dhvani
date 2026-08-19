"""Stage 3 — fusion arithmetic, and a fixture query against a real index.

The arithmetic tests run anywhere. The retrieval test needs a built index and
skips without one, the same way `test_embed_bench.py` skips without the parquet.

What the fixture test is really guarding is the row-order join: `chunks.parquet`
row i must be FAISS id i must be BM25 doc i. If that ever drifts, retrieval
still returns confident results — they just point at the wrong text, which is
the failure mode most expensive to find later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dhvani.retrieve.stage3 import HybridIndex, Stage3Config, kendall_tau, rrf

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index"
HAS_INDEX = (INDEX / "hnsw_sq8.faiss").exists() and (INDEX / "chunks.parquet").exists()


# -- fusion arithmetic ------------------------------------------------------

def test_rrf_rewards_agreement_over_any_single_top_hit():
    dense = [10, 20, 30]
    lexical = [40, 20, 50]
    fused = rrf([dense, lexical], k=60)
    # 20 is second in both; 10 and 40 are first in one and absent from the other.
    assert max(fused, key=fused.get) == 20
    assert fused[10] == fused[40] == pytest.approx(1 / 61)


def test_rrf_of_one_ranking_keeps_that_ranking():
    fused = rrf([[7, 8, 9]], k=60)
    assert sorted(fused, key=lambda d: -fused[d]) == [7, 8, 9]


def test_kendall_tau_bounds_and_partial_overlap():
    assert kendall_tau([1, 2, 3], [1, 2, 3]) == 1.0
    assert kendall_tau([1, 2, 3], [3, 2, 1]) == -1.0
    assert kendall_tau([1, 2, 3], [9]) == 0.0          # nothing in common
    # only 1 and 2 are shared, and they agree
    assert kendall_tau([1, 2, 3], [1, 2, 9]) == 1.0


# -- against a real index ---------------------------------------------------

@pytest.fixture(scope="module")
def index():
    return HybridIndex.load(INDEX)


@pytest.fixture(scope="module")
def probe():
    """A deterministic indexed chunk, and a query taken from its own text."""
    import pyarrow.parquet as pq
    tbl = pq.read_table(INDEX / "chunks.parquet",
                        columns=["chunk_id", "text", "doc_id", "strategy", "lang"])
    rows = [r for r in tbl.to_pylist()
            if r["strategy"] == "s1_passage" and r["lang"] == "hin_Deva"
            and len(r["text"]) > 200]
    rows.sort(key=lambda r: r["chunk_id"])
    row = rows[len(rows) // 2]
    return row | {"query": row["text"][:160]}


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_index_rows_align_with_vectors(index):
    assert index.faiss.ntotal == len(index.chunk_ids)


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_fused_retrieval_finds_the_source_chunk(index, probe):
    result, trace = index.search(probe["query"])
    assert result.chunks, "empty result set"
    found = [c.chunk_id for c in result.chunks[:5]]
    assert probe["chunk_id"] in found, f"{probe['chunk_id']} not in top-5: {found}"
    assert result.signals.n_candidates > 0
    assert -1.0 <= result.signals.kendall_tau <= 1.0
    assert all(s.ok for s in trace.stages), trace.stage_ms


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
@pytest.mark.parametrize("cfg", [
    Stage3Config(bm25=False),           # dense only
    Stage3Config(dense=False),          # lexical only
], ids=["dense_only", "bm25_only"])
def test_each_retriever_alone_still_finds_it(index, probe, cfg):
    """The ablation arms have to work before the ablation table means anything."""
    result, _ = index.search(probe["query"], cfg)
    assert probe["chunk_id"] in [c.chunk_id for c in result.chunks[:10]]


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_dense_failure_degrades_to_lexical_instead_of_failing(index, probe, monkeypatch):
    """DESIGN.md's degradation ladder, rung one: dense down, answer still served
    and flagged degraded."""
    monkeypatch.setattr(index, "_dense",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("faiss down")))
    result, trace = index.search(probe["query"])
    st = trace.get("stage3_retrieve")
    assert result.chunks, "degraded to nothing"
    assert st.degraded and st.ok and "dense" in st.detail["errors"]


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_every_stage_leaves_a_trace_row_including_the_disabled_one(index, probe):
    _, trace = index.search(probe["query"])
    names = [s.stage for s in trace.stages]
    assert names == ["stage3_embed", "stage3_retrieve", "stage3_fuse",
                     "stage3_signals", "stage3_rescore"]
    assert trace.get("stage3_rescore").status == "off"
    assert trace.get("stage3_embed").duration_ms > 0


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_lexical_topk_matches_the_library_it_replaces(index):
    """The fast path must return bm25s' own answer, not an approximation of it.

    Selection is done over scored candidates rather than the whole corpus
    (ADR-022). That is a latency change, so the set it returns has to be the set
    `bm25s.retrieve` returns, or the ablation table is comparing two retrievers
    and calling it one.
    """
    import bm25s

    from dhvani.retrieve.stage3 import Stage3Config

    cfg = Stage3Config()
    for text in ("what is a corporation", "population of india", "cost of a visa"):
        mine = index._lexical(text, cfg)
        ids, scores = index.bm25.retrieve(bm25s.tokenize([text], show_progress=False),
                                          k=min(cfg.k_bm25, len(index.chunk_ids)),
                                          show_progress=False)
        # bm25s pads its k with zero-score documents when fewer than k match;
        # those are noise in an RRF fusion, so the fast path drops them and the
        # comparison is against the documents that actually scored.
        assert set(mine) == {int(i) for i, sc in zip(ids[0], scores[0]) if sc > 0}
        # Ties aside, the head must agree in order too. A query nothing in this
        # index matches is a valid outcome, not a skipped assertion.
        if mine:
            top = {int(i) for i, sc in zip(ids[0], scores[0]) if sc == scores[0][0]}
            assert mine[0] in top


@pytest.mark.skipif(not HAS_INDEX, reason="no index built")
def test_a_query_with_no_indexable_terms_degrades_to_dense_instead_of_raising(index):
    """`bm25s` drops stopwords and single characters, so a query can tokenize to
    nothing. That is a lexical miss, not an error: dense still answers."""
    from dhvani.retrieve.stage3 import Stage3Config

    assert index._lexical("a", Stage3Config()) == []
    result, trace = index.search("a")
    st = trace.get("stage3_retrieve")
    assert st.detail["bm25_hits"] == 0
    assert st.ok and result.chunks          # dense carried it
