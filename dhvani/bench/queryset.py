"""Sample the benchmark query set once, to a file, so every run uses it.

`LATENCY.md` fixes the sampling rules and this module is the only place they are
implemented:

* **latency** is measured over rows in their natural proportions — a row with no
  gold passage is a real query and its refusal is a fast path, so dropping those
  rows would flatter every percentile;
* **recall and MRR** are computed only over rows that have a gold passage, since
  a recall number over all rows is capped at 0.55 by the dataset (`DATASET.md`);
* **`query_type` is stratified** to the mix measured in the indexed subset, not
  to a guess, and the achieved mix is written next to the queries.

One file holds both populations, with `has_gold` per row, because two files
drift apart the first time one is regenerated and the other is not.

    python -m dhvani.bench.queryset --n 500 --out eval/queries.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

# The corpora as they are indexed: three Indic languages plus the English pivot,
# which lives in the anchor file's English columns (ADR-012).
LANGS = ["hin", "ben", "tam", "eng"]


def _rows(parquet: Path, indexed: set[int], english: bool) -> list[dict]:
    cols = ["query_id", "query_type", "passages", "Eng_Query" if english else "query"]
    t = pq.read_table(parquet, columns=cols).to_pylist()
    field = "English_passages" if english else "Translated_passages"
    out = []
    for r in t:
        if r["query_id"] not in indexed:
            continue
        sel = [i for i, s in enumerate(r["passages"]["is_selected"]) if s]
        text = (r["Eng_Query"] if english else r["query"]) or ""
        if not text.strip() or not r["passages"][field]:
            continue
        out.append({"query_id": str(r["query_id"]), "query": text.strip(),
                    "query_type": r["query_type"], "gold_passages": sel,
                    "has_gold": bool(sel)})
    return out


def stratified(rows: list[dict], n: int, rng: np.random.Generator) -> list[dict]:
    """`n` rows keeping the corpus's own `query_type` mix.

    Quota per type rather than a plain uniform draw: uniform gets the mix right
    in expectation and wrong in any one sample, and the query types have
    genuinely different lengths (`DATASET.md`), so the mix moves the latency
    distribution.
    """
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["query_type"], []).append(r)
    quota = {t: round(n * len(v) / len(rows)) for t, v in by_type.items()}
    picked: list[dict] = []
    for t, v in sorted(by_type.items()):
        take = min(quota[t], len(v))
        idx = rng.choice(len(v), size=take, replace=False)
        picked += [v[i] for i in sorted(idx)]
    # Rounding leaves the total a few either side of n; trim or top up from the
    # largest stratum, which is the one whose proportion moves least.
    if len(picked) > n:
        drop = set(rng.choice(len(picked), size=len(picked) - n, replace=False).tolist())
        picked = [r for i, r in enumerate(picked) if i not in drop]
    return picked


# Combining marks — matras, viramas, nuktas. These are what an STT system drops
# first, and what a Latin-shaped tokenizer treats as a word boundary, so they are
# the realistic corruption to inject when measuring what stage 4 recovers.
_MARKS = tuple(range(0x0900, 0x0904)) + tuple(range(0x093A, 0x0950)) + \
         tuple(range(0x0981, 0x0984)) + tuple(range(0x09BC, 0x09D8)) + \
         tuple(range(0x0B82, 0x0B83)) + tuple(range(0x0BBE, 0x0BD8))


def garble(text: str, rng: np.random.Generator, rate: float = 0.35) -> str:
    """Corrupt a query the way a speech recogniser does: drop marks and letters.

    Not a claim about any particular STT engine's error distribution — it is a
    controlled defect so stage 4's catch rate is a measurement rather than an
    anecdote. Words shorter than 4 characters are left alone; there is nothing
    to recover from a 3-character word missing a character.
    """
    words = text.split()
    out = []
    for w in words:
        if len(w) < 4 or rng.random() > rate:
            out.append(w)
            continue
        marks = [i for i, ch in enumerate(w) if ord(ch) in _MARKS]
        # Prefer dropping a combining mark; fall back to an interior character
        # so Latin queries get corrupted too.
        idx = int(rng.choice(marks)) if marks else int(rng.integers(1, len(w) - 1))
        out.append(w[:idx] + w[idx + 1:])
    return " ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500, help="total queries across all languages")
    ap.add_argument("--subset", default="index/subset.json")
    ap.add_argument("--data-dir", default="data/raw")
    ap.add_argument("--langs", nargs="*", default=LANGS)
    ap.add_argument("--out", default="eval/queries.jsonl")
    ap.add_argument("--garble", type=float, default=0.0,
                    help="corrupt this share of words per query, STT-style. "
                         "Writes the clean text alongside as `clean`, so a run "
                         "over the garbled set can still be compared row for row.")
    ap.add_argument("--seed", type=int, default=0,
                    help="0 = the subset's own seed + 2, so the query set is "
                         "reproducible from the manifest alone")
    args = ap.parse_args()

    manifest = json.loads(Path(args.subset).read_text())
    indexed = set(manifest["query_ids"])
    anchor = manifest["anchor_lang"]
    seed = args.seed or manifest["seed"] + 2
    rng = np.random.default_rng(seed)

    per_lang = args.n // len(args.langs)
    picked: list[dict] = []
    for lang in args.langs:
        english = lang == "eng"
        src = anchor if english else lang
        parquet = Path(args.data_dir) / "validation" / f"{src}val.parquet"
        rows = _rows(parquet, indexed, english)
        chosen = stratified(rows, per_lang, rng)
        for r in chosen:
            r["lang"] = {"hin": "hin_Deva", "ben": "ben_Beng",
                         "tam": "tam_Taml", "eng": "eng_Latn"}[lang]
            r["corpus"] = lang
        picked += chosen
        print(f"{lang}: {len(chosen)} of {len(rows)} indexed rows", flush=True)

    # Fixed order, shuffled once: languages must interleave (a run that does all
    # of one language first measures a warm cache per language, not a pipeline),
    # and the order is then frozen in the file so every run and every ablation
    # arm sees the identical sequence.
    order = rng.permutation(len(picked))
    picked = [picked[i] for i in order]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.garble:
        for r in picked:
            r["clean"], r["query"] = r["query"], garble(r["query"], rng, args.garble)
        changed = sum(r["clean"] != r["query"] for r in picked)
        print(f"garbled {changed} of {len(picked)} queries at rate {args.garble}")

    with out.open("w") as f:
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    meta = {
        "built_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 __import__("time").gmtime()),
        "seed": seed,
        "n": len(picked),
        "subset": args.subset,
        "by_corpus": dict(Counter(r["corpus"] for r in picked)),
        "by_query_type": dict(Counter(r["query_type"] for r in picked)),
        "with_gold": sum(r["has_gold"] for r in picked),
        "with_gold_share": round(sum(r["has_gold"] for r in picked) / len(picked), 4),
        "garble_rate": args.garble,
        "garbled_queries": sum(r.get("clean", r["query"]) != r["query"] for r in picked),
    }
    Path(str(out) + ".meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))
    print(f"wrote {out} and {out}.meta.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
