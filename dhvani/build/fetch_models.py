"""Fetch the ONNX encoders `dhvani.embed` expects on disk.

`models/` and `*.onnx` are gitignored — the default encoder alone is 135 MB and
the two benchmark alternates are 4 GB between them — so a fresh clone has the
paths in `MODELS` and none of the files behind them. Found by the fresh-clone
verification on 21 Aug: 11 tests failed with `NO_SUCHFILE` and the pipeline could
not embed at all, because nothing in the repo downloaded what every stage needs.

The file list is derived from `MODELS` rather than repeated here. A path in
`embed.py` that this module does not know how to fetch is the exact drift worth
failing on, so `--check` verifies the declared paths after every download.

    python -m dhvani.build.fetch_models            # the default encoder, 135 MB
    python -m dhvani.build.fetch_models --all      # + bge-m3 and LaBSE, ~4.1 GB
    python -m dhvani.build.fetch_models --check    # verify, download nothing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dhvani.embed import DEFAULT_MODEL, MODELS

# The upstream repo each local `models/<key>/` mirrors. The local layout is the
# repo layout, so every path in MODELS is already a repo-relative path once the
# `models/<key>/` prefix is stripped.
REPOS: dict[str, str] = {
    "multilingual-e5-small": "intfloat/multilingual-e5-small",
    "bge-m3": "BAAI/bge-m3",
    "LaBSE": "sentence-transformers/LaBSE",
}


def _declared(key: str) -> list[str]:
    """Repo-relative paths `embed.py` says this model needs."""
    spec = MODELS[key]
    root = f"models/{key}/"
    out = []
    for p in (spec.onnx, spec.tokenizer, spec.dense):
        if p:
            assert p.startswith(root), f"{key}: {p} is not under {root}"
            out.append(p[len(root):])
    return out


def _patterns(key: str) -> list[str]:
    # `onnx/model.onnx*` also catches the external-weights sidecar: bge-m3 keeps
    # 2.27 GB in `model.onnx_data` next to a 725 KB graph, and the graph alone
    # loads as a truncated model rather than as a missing one.
    pats = [p + "*" if p.endswith(".onnx") else p for p in _declared(key)]
    return pats + ["config.json", "tokenizer_config.json"]


def missing(key: str) -> list[str]:
    return [p for p in _declared(key) if not (Path("models") / key / p).exists()]


def fetch(key: str, force: bool = False) -> None:
    gone = missing(key)
    if not gone and not force:
        print(f"{key}: present, skipping")
        return
    from huggingface_hub import snapshot_download

    print(f"{key}: downloading {REPOS[key]} ({len(gone)} file(s) missing)")
    snapshot_download(REPOS[key], local_dir=f"models/{key}",
                      allow_patterns=_patterns(key))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true",
                    help="also fetch the bge-m3 and LaBSE benchmark alternates "
                         "(~4.1 GB). Neither is on the serving path; they exist "
                         "so the encoder choice in ADR-014 has an ablation arm.")
    ap.add_argument("--check", action="store_true",
                    help="report what is missing and exit non-zero if anything "
                         "is, without downloading. This is the fresh-clone check.")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if every declared file is present")
    args = ap.parse_args()

    keys = list(MODELS) if args.all else [DEFAULT_MODEL]
    if args.check:
        bad = {k: missing(k) for k in keys if missing(k)}
        for k, gone in bad.items():
            print(f"{k}: MISSING {', '.join(gone)}", file=sys.stderr)
        if bad:
            print("run: python -m dhvani.build.fetch_models"
                  + (" --all" if args.all else ""), file=sys.stderr)
            return 1
        print(f"all declared files present for: {', '.join(keys)}")
        return 0

    for k in keys:
        fetch(k, force=args.force)
    for k in keys:
        gone = missing(k)
        assert not gone, f"{k}: still missing after download: {gone}"
    print(f"ready: {', '.join(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
