"""Calibrate the L2/L3 thresholds instead of guessing them.

`docs/GUARDRAILS.md` leaves `t_scope` and `t_floor` `OPEN` on purpose: a refusal
threshold picked by eye is a number the results table cannot defend. This sweeps
them against the two labelled populations the dataset hands over for free
(`DATASET.md`):

| population | label | the guardrail should |
|---|---|---|
| `has_gold` — ≥1 passage marked `is_selected` | answerable | **pass** |
| `has_gold=False` — MS MARCO's own "No Answer Present." | unanswerable | **refuse** |

One retrieval per query, `dense_top1` recorded, then a sweep over every observed
value. The operating points are chosen by false-refusal rate on the answerable
population — 1% for `t_scope`, 5% for `t_floor` — because a guardrail that
refuses real questions on camera is the failure that matters here.

**What this sweep does not measure.** `has_gold=False` rows are *topically*
in-corpus: MS MARCO says no passage answers the query, not that the query is
about something the index has never seen. So this is the L3 population, and the
AUC it produces is the honest ceiling on separating "answerable" from "near but
not answer-bearing" with a retrieval score alone. Genuinely off-topic queries
(T1) live in `eval/adversarial.jsonl` and are scored separately.

    python -m dhvani.bench.calibrate_guardrails --index index/full \
        --out docs/results/2026-08-19-guardrail-calibration.json
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

import numpy as np

from dhvani.guardrails.checks import GuardrailConfig, l1_input
from dhvani.harness.contracts import PipelineTrace
from dhvani.retrieve.stage3 import HybridIndex, Stage3Config
from dhvani.retrieve.stage4 import QueryRewriter

FRR_TARGETS = {"t_scope": 0.01, "t_floor": 0.05}


def sweep(scores: list[float], labels: list[bool]) -> list[dict]:
    """One row per candidate threshold: refuse below it, pass at or above.

    Candidates are the observed scores themselves — no grid, no interpolation,
    so every row is an operating point the system can actually be set to.
    """
    s = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=bool)
    pos, neg = s[y], s[~y]
    rows = []
    for t in sorted(set(np.round(s, 4).tolist())):
        rows.append({
            "t": float(t),
            # answerable queries refused: the number that keeps us honest
            "false_refusal": float((pos < t).mean()) if pos.size else 0.0,
            # unanswerable queries refused: the catch rate
            "catch": float((neg < t).mean()) if neg.size else 0.0,
            "refused_total": int((s < t).sum()),
        })
    return rows


def auc(scores: list[float], labels: list[bool]) -> float:
    """Rank-based AUC (Mann-Whitney), ties counted as half — the probability
    that an answerable query outscores an unanswerable one."""
    s = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=bool)
    if not y.any() or y.all():
        return float("nan")
    order = s.argsort(kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # average ranks within ties, so a score that cannot separate scores 0.5
    for v in np.unique(s):
        m = s == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def pick(rows: list[dict], max_frr: float) -> dict:
    """Highest threshold whose false-refusal rate is still within budget —
    highest, because among thresholds that are equally safe for real questions
    the one that refuses the most junk is the one worth having."""
    ok = [r for r in rows if r["false_refusal"] <= max_frr]
    return max(ok, key=lambda r: r["t"]) if ok else {"t": 0.0, "false_refusal": 0.0,
                                                     "catch": 0.0, "refused_total": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index/full")
    ap.add_argument("--queries", default="eval/queries.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    d = Path(args.index)
    rows = [json.loads(ln) for ln in Path(args.queries).read_text().splitlines() if ln.strip()]
    if args.limit:
        rows = rows[:args.limit]

    t0 = time.perf_counter()
    index = HybridIndex.load(d, threads=args.threads)
    rewriter = QueryRewriter.load(d)
    print(f"loaded in {time.perf_counter() - t0:.1f}s; {len(rows)} queries", flush=True)

    cfg = Stage3Config()
    gcfg = GuardrailConfig()
    scored = []
    for i, r in enumerate(rows):
        trace = PipelineTrace()
        query, _ = rewriter.rewrite(r["query"], trace=trace)
        result, _ = index.search(query, cfg, trace)
        scored.append({
            "query_id": r["query_id"], "corpus": r.get("corpus"),
            "lang": r.get("lang"), "has_gold": bool(r["has_gold"]),
            "dense_top1": round(result.signals.dense_top1, 6),
            "rrf_top1": round(result.signals.top1, 6),
            "margin_1_5": round(result.signals.margin_1_5, 6),
            "l1": l1_input(r["query"], gcfg).kind,
        })
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(rows)}", flush=True)

    dense = [s["dense_top1"] for s in scored]
    gold = [s["has_gold"] for s in scored]
    rrf = [s["rrf_top1"] for s in scored]
    curve = sweep(dense, gold)
    points = {name: pick(curve, frr) for name, frr in FRR_TARGETS.items()}

    report = {
        "run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index": str(d), "queries": args.queries, "n": len(scored),
        "n_answerable": int(sum(gold)), "n_unanswerable": int(len(gold) - sum(gold)),
        "hardware": {"platform": platform.platform(), "python": platform.python_version()},
        "signal": {
            "dense_top1": {"auc": auc(dense, gold),
                           "mean_answerable": float(np.mean([d for d, g in zip(dense, gold) if g])),
                           "mean_unanswerable": float(np.mean([d for d, g in zip(dense, gold) if not g])),
                           "p05": float(np.percentile(dense, 5)),
                           "p50": float(np.percentile(dense, 50)),
                           "p95": float(np.percentile(dense, 95))},
            # Reported to show *why* the guardrails do not key on it: RRF top1
            # is a rank artefact, so its AUC is the null this replaces.
            "rrf_top1": {"auc": auc(rrf, gold)},
        },
        "operating_points": points,
        "l1_refusals": {k: sum(1 for s in scored if s["l1"] == k)
                        for k in {s["l1"] for s in scored} - {None}},
        "curve": curve,
        "per_query": scored,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}", flush=True)
    print(json.dumps({k: report[k] for k in
                      ("n", "n_answerable", "n_unanswerable", "signal",
                       "operating_points", "l1_refusals")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
