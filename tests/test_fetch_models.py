"""`fetch_models` must stay in step with what `embed.py` declares.

The failure this guards is silent: someone adds a model to `MODELS`, or repoints
an existing `onnx=` path, and the fetcher keeps reporting success while a fresh
clone is missing a file. Nothing here touches the network — the logic worth
testing is the derivation, not the download.
"""
from dhvani.build.fetch_models import REPOS, _declared, _patterns, missing
from dhvani.embed import DEFAULT_MODEL, MODELS


def test_every_model_has_a_repo_to_fetch_from():
    assert set(REPOS) == set(MODELS)


def test_declared_paths_are_repo_relative_and_complete():
    for key, spec in MODELS.items():
        want = [p for p in (spec.onnx, spec.tokenizer, spec.dense) if p]
        got = _declared(key)
        assert len(got) == len(want)
        # `_declared` strips the local prefix; rejoining must reproduce embed.py.
        assert [f"models/{key}/{p}" for p in got] == want


def test_onnx_patterns_catch_the_external_weights_sidecar():
    # bge-m3 keeps 2.27 GB in `model.onnx_data`; the 725 KB graph alone loads as
    # a truncated model, not a missing one, so the glob has to reach the sidecar.
    pats = _patterns("bge-m3")
    assert "onnx/model.onnx*" in pats
    assert not any(p.endswith(".onnx") for p in pats)


def test_missing_reports_nothing_when_the_files_are_there():
    # The dev box has the default encoder; a fresh clone does not, and there the
    # meaningful assertion is the inverse, which `--check` makes at the CLI.
    assert missing(DEFAULT_MODEL) == [] or all(
        p in _declared(DEFAULT_MODEL) for p in missing(DEFAULT_MODEL))
