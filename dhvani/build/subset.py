"""Choose the indexed subset and write it down.

ADR-012 fixes the subset: Hindi, Bengali and Tamil, plus deduplicated English,
over 15,000 MS MARCO `query_id`s. This module picks those ids once and writes a
manifest, because the honesty claim in the README ("we index a documented
subset") is only true if the subset is a committed artifact rather than whatever
the last build happened to sample.

The 14 language files are row-aligned (`DATASET.md`), so one set of `query_id`s
applies unchanged to every language. That is what makes per-language results
exactly comparable — the same questions, different scripts.

    python -m dhvani.build.subset --rows 15000 --out index/subset.json
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

# ADR-012. Order matters only for reporting.
LANGUAGES = {"hin": "hin_Deva", "ben": "ben_Beng", "tam": "tam_Taml"}
PIVOT = "eng_Latn"

SCRIPT = {"hin": "Deva", "ben": "Beng", "tam": "Taml", "eng": "Latn"}


def _selected_counts(tbl) -> np.ndarray:
    """Number of `is_selected` passages per row, without leaving Arrow."""
    sel = tbl.column("passages").combine_chunks().field("is_selected")
    flat = pc.list_flatten(sel).to_numpy(zero_copy_only=False)
    parent = pc.list_parent_indices(sel).to_numpy(zero_copy_only=False)
    return np.bincount(parent, weights=flat, minlength=len(sel))


def choose(parquet: Path, rows: int, seed: int) -> dict:
    """Stratified by `query_type`, seeded, gold and no-gold rows kept in their
    natural proportion.

    Keeping the no-gold rows matters twice over: they are 45% of the split, and
    they are L3's labelled negative set (`GUARDRAILS.md`). Sampling only
    answerable rows would produce a corpus that makes the abstain floor
    untestable and every latency percentile flattering.
    """
    tbl = pq.read_table(parquet, columns=["query_id", "query_type", "passages"])
    qid = np.asarray(tbl.column("query_id").to_pylist(), dtype=np.int64)
    qtype = np.asarray(tbl.column("query_type").to_pylist())
    has_gold = _selected_counts(tbl) > 0
    del tbl

    rng = np.random.default_rng(seed)
    total = len(qid)
    picked: list[int] = []

    # Proportional allocation per query_type, largest-remainder so the parts sum
    # to `rows` exactly rather than to rows +/- 4.
    types = sorted(set(qtype.tolist()))
    exact = {t: rows * int((qtype == t).sum()) / total for t in types}
    alloc = {t: int(v) for t, v in exact.items()}
    for t in sorted(types, key=lambda t: exact[t] - alloc[t], reverse=True):
        if sum(alloc.values()) >= rows:
            break
        alloc[t] += 1

    for t in types:
        idx = np.flatnonzero(qtype == t)
        take = min(alloc[t], idx.size)
        picked.extend(rng.choice(idx, size=take, replace=False).tolist())

    picked_idx = np.sort(np.asarray(picked, dtype=np.int64))
    chosen = qid[picked_idx]

    return {
        "query_ids": chosen.tolist(),
        "stats": {
            "rows_requested": rows,
            "rows_chosen": int(chosen.size),
            "rows_available": total,
            "share_of_split": round(float(chosen.size) / total, 4),
            "query_type_counts": dict(Counter(qtype[picked_idx].tolist())),
            "query_type_counts_in_split": dict(Counter(qtype.tolist())),
            "with_gold": int(has_gold[picked_idx].sum()),
            "without_gold": int((~has_gold[picked_idx]).sum()),
            "with_gold_share": round(float(has_gold[picked_idx].mean()), 4),
            "with_gold_share_in_split": round(float(has_gold.mean()), 4),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=15000, help="ADR-012 sets 15,000")
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--split", default="validation")
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--anchor-lang", default="hin",
                    help="the file the ids are drawn from; all languages are "
                         "row-aligned so the choice does not affect the result")
    ap.add_argument("--out", default="index/subset.json")
    args = ap.parse_args()

    suffix = "val" if args.split == "validation" else "train"
    parquet = Path(args.data_dir) / args.split / f"{args.anchor_lang}{suffix}.parquet"
    result = choose(parquet, args.rows, args.seed)

    manifest = {
        "chosen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "adr": "ADR-012",
        "dataset": "ai4bharat/MSMARCO-XI",
        "split": args.split,
        "languages": LANGUAGES,
        "pivot": PIVOT,
        "seed": args.seed,
        "anchor_lang": args.anchor_lang,
        **result,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2))

    s = result["stats"]
    print(f"{s['rows_chosen']:,} of {s['rows_available']:,} rows "
          f"({s['share_of_split']:.1%} of the {args.split} split)")
    print(f"  with gold: {s['with_gold']:,} ({s['with_gold_share']:.1%}) "
          f"vs {s['with_gold_share_in_split']:.1%} in the split")
    print(f"  query_type: {s['query_type_counts']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
