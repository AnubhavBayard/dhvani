"""Pick the questions the demo video should actually use, by running them.

Recall@10 is 0.4464, so roughly half of in-corpus questions refuse — and which
half is not predictable by looking at them. The giraffe question refused on the
typed phrasing and answered on the spoken one within the same hour (19 Aug).
Pointing a camera at an unvetted question list is how a take dies.

So the shot list is measured, not chosen: every candidate is asked against the
live pipeline, and a question earns its place only if it

* answers rather than refuses,
* has **no ungrounded sentence** (L4 would mark it on screen),
* carries at least one `[n]` citation — criterion 4 is that every claim maps to
  a chunk the user can click,
* and does all of that **on every repeat**. Generation samples at temperature
  0.2, so a question that worked once is an anecdote; `--reps` decides how many
  times it has to work.

Refusals are shot list material too, not failures: "demonstrably knows when not
to answer" is a scored requirement, and a refusal chosen in advance is a feature
being demonstrated rather than an accident being survived.

    python -m dhvani.bench.demo_script --per-corpus 30 --finalists 6 --reps 3 \\
        --out docs/results/2026-08-19-demo-script.json \\
        --script docs/DEMO_SCRIPT.md
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from dhvani.pipeline import Dhvani

CITATION = re.compile(r"\[\d+\]")
# Two per category is a shot list, not a survey — the video has 90 seconds.
REFUSAL_PICKS = {"injection": 2, "unsupported_language": 2, "off_topic": 3,
                 "unanswerable": 3}
LANG_NAME = {"eng_Latn": "English", "hin_Deva": "Hindi", "ben_Beng": "Bengali",
             "tam_Taml": "Tamil"}


def ask(d: Dhvani, q: str) -> dict:
    """One question, everything the shot list needs to judge it by."""
    text, refusal, done, ctx = [], None, {}, 0
    for ev in d.answer(q):
        t = ev["type"]
        if t == "token":
            text.append(ev["text"])
        elif t == "refusal" and refusal is None:
            refusal = ev
        elif t == "retrieval":
            ctx = len(ev["context"]["chunks"])
        elif t == "done":
            done = ev
    answer = "".join(text).strip()
    g = done.get("grounding") or {}
    return {"answer": answer, "refusal_kind": refusal["kind"] if refusal else None,
            "refusal_copy": refusal.get("copy", "") if refusal else "",
            "citations": len(set(CITATION.findall(answer))),
            "chunks": ctx,
            "grounded": g.get("grounded", 0), "judged": g.get("judged", 0),
            "ungrounded": g.get("ungrounded", 0),
            "mean_overlap": g.get("mean_overlap", 0.0),
            "boundary_a_ms": done.get("boundary_a_ms"),
            "ttft_ms": done.get("ttft_ms"),
            "wall_clock_ms": done.get("wall_clock_ms")}


def is_demo_answer(r: dict) -> bool:
    return (r["refusal_kind"] is None and r["answer"] != ""
            and r["ungrounded"] == 0 and r["judged"] >= 1
            and r["citations"] >= 1)


def rank_key(r: dict) -> tuple:
    """Best first: fully grounded, well supported, and quick to retrieve."""
    return (-r["grounded"], -r["mean_overlap"], r["boundary_a_ms"] or 1e9,
            len(r["answer"]))


def candidates(queries: Path, adversarial: Path, per_corpus: int) -> list[dict]:
    rows = [json.loads(l) for l in queries.read_text().splitlines() if l.strip()]
    by_corpus: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["has_gold"] and len(by_corpus[r["corpus"]]) < per_corpus:
            by_corpus[r["corpus"]].append(
                {"text": r["query"], "lang": r["lang"], "kind": "answer",
                 "corpus": r["corpus"], "id": r["query_id"]})
    out = [c for v in by_corpus.values() for c in v]

    picked: dict[str, int] = defaultdict(int)
    for line in adversarial.read_text().splitlines():
        if not line.strip():
            continue
        a = json.loads(line)
        cat = a["category"]
        if picked[cat] < REFUSAL_PICKS.get(cat, 0):
            picked[cat] += 1
            out.append({"text": a["text"], "lang": a["lang"], "kind": "refusal",
                        "category": cat, "id": a["id"]})
    return out


def stable(runs: list[dict], want_answer: bool) -> bool:
    """Same verdict every time, or it does not go in front of a camera."""
    if want_answer:
        return all(is_demo_answer(r) for r in runs)
    kinds = {r["refusal_kind"] for r in runs}
    return len(kinds) == 1 and None not in kinds


def write_script(path: Path, answers: list[dict], refusals: list[dict],
                 reps: int) -> None:
    lines = [
        "# Demo shot list",
        "",
        f"`MEASURED {time.strftime('%Y-%m-%d')}` — every line below was asked "
        f"against the live pipeline **{reps} times** and behaved the same way "
        "each time. Generation samples at temperature 0.2, so anything that "
        "passed once and not three times is not here.",
        "",
        "Evidence: [`results/" + path.stem.replace("DEMO_SCRIPT", "") +
        "2026-08-19-demo-script.json`](results/2026-08-19-demo-script.json).",
        "",
        "**Before the take.** Read each line into the mic once — the transcript "
        "appears in the box *before* it is asked and is editable, so a mis-hear "
        "is a recoverable take rather than a dead one (ADR-029). Speak the "
        "question as written; a paraphrase is an unvetted question.",
        "",
        "## Questions that answer",
        "",
        "| # | Language | Say this | Cites | Boundary A | TTFT | On screen |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(answers, 1):
        amb = r["judged"] - r["grounded"]
        marks = (f"{amb} sentence marked `?` (ambiguous)" if amb
                 else "all sentences clean")
        lines.append(
            f"| {i} | {LANG_NAME.get(r['lang'], r['lang'])} | {r['text']} | "
            f"{r['citations']} | {r['boundary_a_ms']:.1f} ms | "
            f"{r['ttft_ms'] / 1000:.1f} s | {marks} |")
    lines += ["",
              "**Pick four, not eleven** — one per language, and read each one "
              "aloud first. These come from MS MARCO's machine-translated query "
              "set, so a few are oddly phrased even though they answer well; a "
              "line that sounds strange in your mouth will sound strange on "
              "camera. The measurement says they *work*, not that they are "
              "good television.",
              "",
              "An `ambiguous` mark is not a defect to avoid — it is L4 grading "
              "its own sentence in public, and worth one line of narration if "
              "the take allows.",
              "",
              "What to point at, in order: the stage bar filling, the "
              "boundary-A readout, the citation numbers in the answer, then one "
              "citation clicked open. That is success criterion 5 in four "
              "moves.", "", "### The answers they gave", ""]
    for i, r in enumerate(answers, 1):
        lines += [f"**{i}. {r['text']}**", "",
                  f"> {r['answer'][:400]}", "",
                  f"*{r['grounded']}/{r['judged']} sentences grounded, mean "
                  f"overlap {r['mean_overlap']:.2f}.*", ""]

    lines += ["## Refusals worth showing", "",
              "Not failures — \"demonstrably knows when not to answer\" is "
              "scored. Injection and out-of-index language catch **1.00** of "
              "their adversarial categories and are decided by L1 alone, with "
              "no model in the loop, which is why they repeat identically. A "
              "`not_grounded` line is a different demonstration: L4 reading the "
              "answer back against the passages, from the category the "
              "adversarial set catches 0.45 of (`GUARDRAILS.md`).", "",
              "| # | Say this | Refuses as | What the judge sees |",
              "|---|---|---|---|"]
    for i, r in enumerate(refusals, 1):
        lines.append(f"| R{i} | {r['text']} | `{r['refusal_kind']}` | "
                     f"{r['refusal_copy']} |")
    lines += ["", "The injection line is the one to spend time on: it refuses "
              "**before retrieval runs**, so the stage bar shows one green "
              "guardrail cell and nothing else — the cheapest possible refusal, "
              "visible as such.", ""]
    if not any(r.get("category") == "off_topic" for r in refusals):
        lines += [
            "**No off-topic line survived the repeats, and that is the true "
            "state of the system rather than a gap in this list.** Off-topic "
            "questions are the category L2 was built to catch, and ADR-030 "
            "switched L2 off because a retrieval score does not separate them "
            "on this corpus (AUC 0.581). What is left to catch them is the "
            "model's own judgement, which is sampled — \"who won the cricket "
            "world cup in 2026\" refused, answered, then refused again across "
            "three asks. Do not put one in the video: the take is a coin flip. "
            "If asked about it live, the honest answer is the interesting one, "
            "and it is written up in `GUARDRAILS.md`.", ""]
    path.write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index/full")
    ap.add_argument("--queries", default="eval/queries.jsonl")
    ap.add_argument("--adversarial", default="eval/adversarial.jsonl")
    ap.add_argument("--per-corpus", type=int, default=30)
    ap.add_argument("--finalists", type=int, default=2,
                    help="per corpus, carried into the repeat rounds")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("--script", default="")
    ap.add_argument("--replay", default="", help="rewrite the script from a "
                                                 "previous run; asks nothing")
    args = ap.parse_args()

    if args.replay:
        rep = json.loads(Path(args.replay).read_text())
        write_script(Path(args.script), rep["answers"], rep["refusals"],
                     rep["reps"])
        print(f"wrote {args.script} from {args.replay}", flush=True)
        return 0

    cands = candidates(Path(args.queries), Path(args.adversarial),
                       args.per_corpus)
    d = Dhvani.load(args.index, threads=args.threads)
    d.warm()
    print(f"screening {len(cands)} candidates", flush=True)

    screened = []
    for i, c in enumerate(cands):
        r = ask(d, c["text"])
        screened.append({**c, **r, "runs": [r]})
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(cands)}", flush=True)

    # Finalists: the best few per corpus that passed the screen, plus every
    # refusal candidate that refused at all.
    per_corpus: dict[str, list[dict]] = defaultdict(list)
    for r in screened:
        if r["kind"] == "answer" and is_demo_answer(r):
            per_corpus[r["corpus"]].append(r)
    finalists = [r for rows in per_corpus.values()
                 for r in sorted(rows, key=rank_key)[:args.finalists]]
    finalists += [r for r in screened
                  if r["kind"] == "refusal" and r["refusal_kind"]]
    print(f"{len(finalists)} finalists into {args.reps - 1} more rounds",
          flush=True)

    for rep in range(args.reps - 1):
        for r in finalists:
            r["runs"].append(ask(d, r["text"]))
        print(f"  round {rep + 2}/{args.reps} done", flush=True)

    keep = [r for r in finalists
            if stable(r["runs"], want_answer=r["kind"] == "answer")]
    answers = sorted([r for r in keep if r["kind"] == "answer"], key=rank_key)
    refusals = [r for r in keep if r["kind"] == "refusal"]
    # Last run wins for the numbers printed in the script: it is the one taken
    # with everything warm, which is the state the demo is filmed in.
    for r in keep:
        r.update(r["runs"][-1])

    report = {"run_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "index": args.index, "reps": args.reps,
              "screened": len(screened), "finalists": len(finalists),
              "stable": len(keep),
              "screen_pass_rate": round(
                  sum(r["kind"] == "answer" and is_demo_answer(r)
                      for r in screened)
                  / max(1, sum(r["kind"] == "answer" for r in screened)), 4),
              "answers": answers, "refusals": refusals,
              "all_screened": screened}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"wrote {args.out}", flush=True)
    if args.script:
        write_script(Path(args.script), answers, refusals, args.reps)
        print(f"wrote {args.script}", flush=True)
    print(json.dumps({k: report[k] for k in
                      ("screened", "finalists", "stable", "screen_pass_rate")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
