"""Calibrate L4's thresholds on real answers instead of guessing them.

`docs/GUARDRAILS.md` leaves `t_low`, `t_high` and the replacement fraction
`OPEN`, to be set "on a labelled set of 100 (answer, context) pairs, half
grounded and half deliberately hallucinated". Hand-writing hallucinated answers
is slow and biased toward what the author imagines a hallucination looks like.
This builds the same labelled set mechanically and for free:

* **positive** — a real generated answer scored against **its own** context;
* **negative** — the same real answer scored against **another query's** context,
  which is a guaranteed mismatch and needs no invention.

The sweep then reports, for every candidate operating point, how often an answer
is replaced when the context was right (false refusal) against how often it is
replaced when the context was wrong (catch).

Answers are generated once and cached in the evidence file, so re-sweeping costs
nothing and never calls a provider twice.

    python -m dhvani.bench.calibrate_grounding --n 60 \\
        --out docs/results/2026-08-19-grounding-calibration.json
    python -m dhvani.bench.calibrate_grounding --replay <that file>   # free
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from dhvani.guardrails.grounding import Grounder, GroundingConfig
from dhvani.harness.contracts import ContextChunk
from dhvani.pipeline import Dhvani

T_LOW = [0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]
MAX_UNGROUNDED = [0.34, 0.50, 0.60, 0.67, 0.75, 0.90]


def collect(index: str, queries: str, n: int, threads: int) -> list[dict]:
    """One live answer per query, with the context it was generated from."""
    rows = [json.loads(l) for l in Path(queries).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["has_gold"]][:n]
    d = Dhvani.load(index, threads=threads)
    d.warm()
    out = []
    for i, r in enumerate(rows):
        text, ctx = [], []
        for ev in d.answer(r["query"]):
            if ev["type"] == "retrieval":
                ctx = ev["context"]["chunks"]
            elif ev["type"] == "token":
                text.append(ev["text"])
        answer = "".join(text).strip()
        if answer and ctx:
            out.append({"query_id": r["query_id"], "query": r["query"],
                        "lang": r["lang"], "answer": answer, "context": ctx})
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(rows)} ({len(out)} usable)", flush=True)
    return out


def replaced(answer: str, chunks: list[dict], cfg: GroundingConfig) -> bool:
    """Would L4 replace this answer against this context?"""
    g = Grounder([ContextChunk(**c) for c in chunks], cfg)
    g.feed(answer)
    g.flush()
    return bool(g.verdict())


def sweep(pairs: list[dict], rng: random.Random) -> dict:
    """Every answer scored against its own context and against a shifted one.

    The shift is by one rather than a shuffle so no answer can draw its own
    context back, and so the negative set is reproducible from the file alone.
    """
    neg_ctx = [pairs[(i + 1) % len(pairs)]["context"] for i in range(len(pairs))]
    grid = []
    for t_low in T_LOW:
        for frac in MAX_UNGROUNDED:
            cfg = GroundingConfig(t_low=t_low, t_high=max(t_low * 3, 0.30),
                                  max_ungrounded=frac)
            fr = sum(replaced(p["answer"], p["context"], cfg) for p in pairs)
            catch = sum(replaced(p["answer"], c, cfg)
                        for p, c in zip(pairs, neg_ctx))
            grid.append({"t_low": t_low, "max_ungrounded": frac,
                         "false_refusal": round(fr / len(pairs), 4),
                         "catch": round(catch / len(pairs), 4),
                         "separation": round((catch - fr) / len(pairs), 4)})
    return {"grid": grid,
            # The operating point: the largest separation, ties broken toward
            # the lower false-refusal rate. A guardrail that replaces correct
            # answers is the failure that shows up on camera.
            "chosen": max(grid, key=lambda g: (g["separation"], -g["false_refusal"]))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index/full")
    ap.add_argument("--queries", default="eval/queries.jsonl")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--replay", default="", help="re-sweep a previous run's "
                                                 "cached answers; no provider calls")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.replay:
        pairs = json.loads(Path(args.replay).read_text())["pairs"]
        print(f"replaying {len(pairs)} cached answers", flush=True)
    else:
        pairs = collect(args.index, args.queries, args.n, args.threads)

    result = sweep(pairs, random.Random(20260819))
    report = {"run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "n_pairs": len(pairs), "replayed": bool(args.replay),
              "negatives": "each answer against the next query's context",
              **result, "pairs": pairs}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"wrote {args.out}", flush=True)
    print(json.dumps({k: report[k] for k in ("n_pairs", "chosen")}, indent=2))
    top = sorted(result["grid"], key=lambda g: -g["separation"])[:8]
    for g in top:
        print(f"  t_low {g['t_low']:.2f} frac {g['max_ungrounded']:.2f} "
              f"-> FRR {g['false_refusal']:.3f} catch {g['catch']:.3f} "
              f"sep {g['separation']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
