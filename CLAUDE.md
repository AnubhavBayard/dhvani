# Conventions — task-2

Repo conventions for this project. `task-2/` is the project root; every path here
is relative to it.

## Isolation from the sibling project

`../task-1-frame-id-generator/` is submitted work. Do not read it at runtime, do
not import from it, do not edit it, do not refactor it. If something there is
useful, **copy it in** and log the copy in `docs/DECISIONS.md`. Nothing outside
`task-2/` may be required to build or run this project — verified by a fresh
clone before deployment.

The one permitted change outside this folder: a single row appended to the root
`README.md` table, which is an index of tasks. Already done.

## Layout

```
task-2/
  README.md              boundary statement + headline numbers, first thing a judge reads
  CLAUDE.md              this file
  requirements.txt       pinned
  .env.example           every key, no values
  docs/                  see README table
  dhvani/
    app.py               FastAPI: WS /ws/stt, POST /ask (SSE), static UI
    harness/             runner, contracts, retries, circuit breaker, traces, replay
    build/               chunking strategies, embed, index build — build-time only
    retrieve/            stages 4,3,5,6,7 — one module per stage
    guardrails/          l1_input, l2_scope, l3_floor, l4_output
    stt/                 provider interface + sarvam + elevenlabs
    generate/            provider client, prompting, streaming
    bench/               benchmark + ablation harness, percentile math
  web/                   index.html, app.js, style.css, self-hosted font subsets
  eval/                  query sets, adversarial.jsonl, labelled groundedness pairs
  index/                 built artifacts — gitignored
  docs/results/          benchmark JSON — committed; this is the evidence
  tests/
```

Query-time code lives in `retrieve/`, build-time code in `build/`. The separation
is enforced by directory, because it is exactly the distinction ADR-002 is about
and the one easiest to blur under deadline pressure.

## Language and frameworks

- Python 3.11, FastAPI, uvicorn, Pydantic v2 for every stage contract
- hnswlib + `bm25s` + onnxruntime — all in-process, no hosted vector DB (ADR-004)
- Frontend: vanilla JS/CSS, no build step, < 50 KB gzipped (ADR-008)
- Own venv at `task-2/.venv`. Never install into the sibling's environment.

## The rules that matter

**Latency numbers are measured, never estimated.** Any latency, recall, accuracy,
or catch-rate number in any document must come from a run that was executed.
Aspirational values are labelled `TARGET`. Measured values are labelled
`MEASURED` with the date and a pointer to the `docs/results/*.json` that produced
them. A number without one of those two labels is a bug in the docs.

**Every stage is toggleable, timed, and ablatable.** A stage that cannot be
turned off cannot appear in the ablation table, and the ablation table is one of
the strongest things this submission has.

**Instrumentation is not a later task.** Every stage emits a `StageTrace` from
its first commit. Timing added afterwards measures the code you wrote to add
timing.

**Retrieved text is data, never instruction.** Corpus content is delimited in the
prompt and never concatenated into the instruction block. Corpus-borne prompt
injection is threat T5 in `docs/GUARDRAILS.md`.

**Bounded everything.** Every external call has a hard timeout, a bounded retry
count, and a defined fallback. An unbounded retry policy looks responsible and
destroys P100.

**Nothing is complete without a test proving it.** Not a suite per function — one
runnable check that fails if the logic breaks. Retrieval stages get a fixture
query with a known expected chunk. Guardrails get the adversarial eval set.

**Ask before adding a paid API dependency.** Currently two: Sarvam STT, and the
generation provider. Both flagged as blockers in `docs/PROGRESS.md`.

## Finishing a task

Not done until:

1. the code works and has a check that proves it
2. `docs/PROGRESS.md` is updated — task status, any new blocker, and an appended
   entry in the change log for today
3. `docs/DECISIONS.md` has an ADR for any choice a future reader would ask "why"
   about
4. any number the change affects has been re-measured, not adjusted by hand

Steps 2 and 3 are part of the task, not paperwork after it. `PROGRESS.md` is the
source of truth for project state; if it is stale, the project state is unknown.

## Testing

- `pytest`, in `tests/`
- retrieval: fixture queries with known relevant chunk ids
- harness: failure injection per stage — the degradation ladder in `DESIGN.md` is
  a promise and needs a test each
- guardrails: `eval/adversarial.jsonl` runs as a test with per-category assertions
- benchmarks are not tests. They live in `dhvani/bench/` and write to
  `docs/results/`. Determinism comes from replay mode, so two runs of the same
  config produce the same numbers — without that, ablation deltas are noise.

## Commits

`type: lowercase summary` — `feat`, `fix`, `perf`, `docs`, `test`, `chore`.
`perf` commits state the before and after numbers in the body, from a run:

```
perf: batch reranker candidates in one forward pass

stage 6 p50 210ms -> 58ms (500 queries, warmed, dev box)
docs/results/2026-08-17-rerank-batch.json
```

Small commits. A commit that touches a stage and its doc and its test together is
correct; a commit that touches three stages is not.
