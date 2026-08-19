"""Checks on the retrieval metrics the model bake-off is decided by.

A recall function that is quietly wrong produces a confident wrong ADR, so the
metric gets a hand-computed case with a known answer before it gets used.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dhvani.bench.embed_bench import build_eval, score

ROOT = Path(__file__).resolve().parents[1]
HIN = ROOT / "data" / "raw" / "validation" / "hinval.parquet"


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def test_score_perfect_and_worst():
    corpus = _unit(np.eye(4))
    assert score(corpus[:2], corpus, [[0], [1]])["recall@1"] == 1.0
    # gold placed last for both queries: found at rank 4, so recall@1 misses,
    # recall@5 hits, MRR is 1/4.
    out = score(corpus[:2], corpus, [[3], [3]], ks=(1, 5))
    assert out["recall@1"] == 0.0
    assert out["recall@5"] == 1.0
    assert out["mrr@10"] == pytest.approx(0.25)


def test_score_counts_a_row_once_when_several_golds_match():
    """3,031 validation rows have more than one selected passage. A row with two
    golds in the top-k is one hit, not two — otherwise recall exceeds 1.0."""
    corpus = _unit(np.eye(4))
    out = score(corpus[:1], corpus, [[0, 1]], ks=(1, 5))
    assert out["recall@1"] == 1.0
    assert out["recall@5"] == 1.0


def test_score_mrr_uses_the_best_gold():
    corpus = _unit(np.eye(4))
    # query is nearest to dim 0; golds at ranks 1 and 3 -> MRR takes rank 1
    assert score(corpus[:1], corpus, [[0, 2]])["mrr@10"] == pytest.approx(1.0)


@pytest.mark.skipif(not HIN.exists(), reason="hinval.parquet not downloaded")
def test_build_eval_only_samples_rows_that_have_a_gold():
    ev = build_eval(HIN, n_queries=25, seed=20260815)
    assert len(ev["queries"]) == 25
    assert ev["rows_total"] == 97_941
    assert ev["rows_with_gold"] == 53_895
    assert all(q["gold"] for q in ev["queries"]), "sampled a row with no gold"
    n = len(ev["corpus"])
    assert len(ev["corpus_en"]) == n
    assert all(0 <= g < n for q in ev["queries"] for g in q["gold"])


@pytest.mark.skipif(not HIN.exists(), reason="hinval.parquet not downloaded")
def test_build_eval_is_deterministic():
    """Ablation deltas are noise without this."""
    a = build_eval(HIN, n_queries=10, seed=7)
    b = build_eval(HIN, n_queries=10, seed=7)
    assert [q["query_id"] for q in a["queries"]] == [q["query_id"] for q in b["queries"]]
    assert a["corpus"] == b["corpus"]
