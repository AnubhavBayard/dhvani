"""Checks on the Day-2 recon: the percentile math, and the evidence file itself.

The evidence test is the one that matters. Every number in DATASET.md and
ADR-012 is read off docs/results/2026-08-15-dataset-probe.json. If that file and
those documents drift apart, the documents are wrong and nothing else notices.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dhvani.build.probe_dataset import LANGS, _lengths

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "docs" / "results" / "2026-08-15-dataset-probe.json"


def test_lengths_percentiles():
    stats = _lengths(["x" * n for n in range(1, 101)])
    assert stats["n"] == 100
    assert stats["min"] == 1 and stats["max"] == 100
    assert stats["p50"] == 51 and stats["p95"] == 96 and stats["p99"] == 100
    assert stats["mean"] == pytest.approx(50.5)


def test_lengths_empty_is_empty():
    assert _lengths([]) == {}


@pytest.fixture(scope="module")
def probe() -> dict:
    if not PROBE.exists():
        pytest.skip(f"{PROBE} not built yet")
    return json.loads(PROBE.read_text())


def test_file_inventory(probe):
    """13 train files and 14 validation files — Telugu has no train split.

    ADR-012 excludes Telugu on exactly this fact, and CHUNKING.md says S4 is
    unavailable for it. If the dataset ever gains teltrain.parquet, both are
    stale and this test is how we find out.
    """
    files = probe["files"]
    train = {k for k in files if k.startswith("train/")}
    val = {k for k in files if k.startswith("validation/")}
    assert len(val) == 14, "expected one validation file per language"
    assert len(train) == 13, "expected Telugu to be missing from train"
    assert "train/teltrain.parquet" not in files
    assert "validation/telval.parquet" in files
    assert set(LANGS) == {k.split("/")[1][:3] for k in val}


def test_row_counts(probe):
    files = probe["files"]
    val = {k: v for k, v in files.items() if k.startswith("validation/")}
    assert {v["num_rows"] for v in val.values()} == {97941}, \
        "every language is the same 97,941 validation rows"
    assert probe["totals"]["num_rows"] == 11_451_314
    assert sum(v["num_rows"] for v in files.values() if v.get("num_rows")) == 11_451_314


def test_single_row_group_per_file(probe):
    """No cheap partial reads. The build pipeline downloads whole files because
    of this; if it ever stops being true, that decision can be revisited."""
    assert all(v["num_row_groups"] == 1 for v in probe["files"].values())


def test_languages_are_the_same_rows(probe):
    """The finding ADR-012 is built on: the language files are row-aligned."""
    samples = probe["samples"]
    assert len(samples) >= 2, "need at least two languages to compare"
    shapes = {
        lang: (s["passages_per_row"]["total_translated"],
               json.dumps(s["query_type_values"], sort_keys=True),
               json.dumps(s["is_selected_per_row"], sort_keys=True),
               json.dumps(s["english_passage_chars"], sort_keys=True))
        for lang, s in samples.items()
    }
    assert len(set(shapes.values())) == 1, f"languages diverged: {shapes.keys()}"


def test_usable_eval_pool(probe):
    """~45% of rows have no gold passage. DATASET.md and ADR-012 both quote
    53,895 usable rows; the eval sampler filters on it."""
    for lang, s in probe["samples"].items():
        zero = s["is_selected_per_row"]["rows_with_zero_selected"]
        assert zero == 44_046, f"{lang}: {zero}"
        assert s["rows_sampled"] - zero == 53_895


def test_passage_length_fits_the_chunking_premise(probe):
    """CHUNKING.md claims a 512-token window is an identity function here, and
    caps chunks at 2,000 characters. Both need p95 well under the cap."""
    for lang, s in probe["samples"].items():
        p95 = s["translated_passage_chars"]["p95"]
        p99 = s["translated_passage_chars"]["p99"]
        assert p95 < 700, f"{lang} p95={p95}"
        assert p99 < 2000, f"{lang} p99={p99} exceeds the CHUNKING.md cap"
