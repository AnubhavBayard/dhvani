"""Benchmark harness — the arithmetic, and one end-to-end arm on a real index.

The arithmetic tests run anywhere. What they guard is the part of a benchmark
nobody notices being wrong: a percentile that interpolates, an nDCG whose ideal
ranking is not the achievable one, or a gold match done by `chunk_id` — which
would silently credit a Hindi query for retrieving the Tamil copy of the same
passage, because the id is identical across corpora.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dhvani.bench.benchmark import ARMS, Labels, ndcg_at_k, percentiles, run_arm
from dhvani.bench.queryset import stratified

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "index" / "full"
QUERIES = ROOT / "eval" / "queries.jsonl"
HAS_FULL = (FULL / "hnsw_sq8.faiss").exists() and QUERIES.exists()


# -- percentiles ------------------------------------------------------------

def test_percentiles_are_nearest_rank_and_p100_is_the_maximum():
    v = [float(x) for x in range(1, 11)]           # 1..10
    p = percentiles(v)
    assert p["p50"] == 5.0                         # ceil(0.5*10) = 5th value
    assert p["p70"] == 7.0
    assert p["p100"] == 10.0                       # the maximum, not interpolated
    assert percentiles([])["p50"] == 0.0


def test_percentile_never_invents_a_value_between_observations():
    # An interpolating percentile would return 1.5 here. P100 must be an
    # observation, or the reported worst case is one nobody measured.
    assert percentiles([1.0, 2.0], (50, 100)) == {"p50": 1.0, "p100": 2.0}


# -- nDCG -------------------------------------------------------------------

def test_ndcg_is_one_when_every_gold_is_at_the_top():
    assert ndcg_at_k([True, True, False], n_gold=2) == pytest.approx(1.0)


def test_ndcg_discounts_by_position_against_the_achievable_ideal():
    # One gold passage exists and it came back second: ideal is rank 1.
    assert ndcg_at_k([False, True], n_gold=1) == pytest.approx(1 / np.log2(3))


def test_ndcg_is_zero_without_gold_rather_than_dividing_by_zero():
    assert ndcg_at_k([True], n_gold=0) == 0.0


# -- query set sampling -----------------------------------------------------

def test_stratified_keeps_the_type_mix_and_the_count():
    rows = ([{"query_type": "DESCRIPTION"}] * 540 + [{"query_type": "NUMERIC"}] * 253
            + [{"query_type": "ENTITY"}] * 207)
    got = stratified(rows, 100, np.random.default_rng(0))
    assert len(got) == 100
    mix = {t: sum(r["query_type"] == t for r in got) for t in
           ("DESCRIPTION", "NUMERIC", "ENTITY")}
    # Within a rounding step of the corpus proportions, not merely "random".
    assert abs(mix["DESCRIPTION"] - 54) <= 2
    assert abs(mix["NUMERIC"] - 25) <= 2
    assert abs(mix["ENTITY"] - 21) <= 2


# -- against the real index -------------------------------------------------

@pytest.mark.skipif(not HAS_FULL, reason="full index or query set not built")
def test_gold_match_is_by_row_not_chunk_id_so_languages_do_not_cross():
    """The same `chunk_id` exists in all four corpora. Matching by id would make
    a same-language recall number indistinguishable from an any-language one."""
    labels = Labels(FULL)
    q = next(json.loads(l) for l in QUERIES.read_text().splitlines()
             if json.loads(l)["has_gold"])
    qid = int(q["query_id"])
    gold_rows = np.flatnonzero((labels.doc == qid) & labels.selected)
    assert len(gold_rows) > 0

    same, any_lang = labels.hits(list(gold_rows), qid, q["lang"])
    assert all(any_lang)                       # every row is this query's gold
    assert any(same) and not all(same)         # but only one corpus is its own
    assert labels.gold_count(qid, q["lang"]) >= 1


@pytest.mark.skipif(not HAS_FULL, reason="full index or query set not built")
def test_one_arm_runs_end_to_end_and_reports_every_field_the_docs_quote():
    from dataclasses import replace

    from dhvani.retrieve.stage3 import HybridIndex, Stage3Config

    queries = [json.loads(l) for l in QUERIES.read_text().splitlines()][:4]
    index = HybridIndex.load(FULL)
    arm = run_arm(index, Labels(FULL), queries,
                  replace(Stage3Config(), **ARMS["full"]), reps=1)
    rep = arm["reps"][0]
    assert rep["n"] == len(queries)
    assert rep["latency_ms"]["p100"] >= rep["latency_ms"]["p50"] > 0
    assert "stage3_embed" in rep["stage_ms"]
    assert set(rep["quality"]) >= {"recall@10", "mrr@10", "ndcg@10"}
    # Boundary A is a span, so it cannot be smaller than the stages inside it.
    assert rep["overhead_ms"]["p50"] >= 0
