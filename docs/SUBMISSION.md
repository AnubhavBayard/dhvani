# Submission

The one place the live URL is written down. If the hostname ever changes, this
file is the single edit — everything else links here rather than repeating it.

## Live link

**https://monday-elite-sustainer.ngrok-free.dev**

Verified end to end through the public hostname on **2026-08-21**, not just
locally:

| Check | Result |
|---|---|
| `GET /` | 200, real page, **no ngrok interstitial** |
| `GET /health` | `ok: true`, **3,278,022 chunks**, Sarvam STT live |
| `POST /ask` (SSE) | streamed 109 token events, 3 sentences, **all grounded** (overlap 0.775 / 0.647 / 0.381), citations rendered |
| Boundary A | **24.05 ms** on that ask, through the tunnel |
| `ttft` / wall clock | 928 ms / 5,536 ms |

HTTPS is real, which is the part that matters for the demo: `getUserMedia` only
exists in a secure context, so the microphone works.

**What this link is.** An ngrok tunnel to the dev box, which is the deployment
(ADR-036). The hostname is a claimed free-tier **static** domain, so it survives
`ngrok` restarts — unlike the Cloudflare quick tunnel it replaced, which issued a
new hostname per process.

**What it is not.** Independent of one machine. The origin is a laptop: if it
sleeps or `uvicorn` stops, the URL is up but the app behind it is not. Accepted
trade, argued in ADR-036.

To bring it back up:

```bash
cd task-2
set -a; . ./.env; set +a
.venv/bin/uvicorn dhvani.app:app --host 127.0.0.1 --port 8000 &
ngrok http 8000 --url monday-elite-sustainer.ngrok-free.dev &
```

## Repo

**https://github.com/AnubhavBayard/dhvani** — public, and verified from a fresh
clone on 2026-08-21: new directory, no sibling checkout, nothing on the box but
the repo. Clone → `uv venv` → install → `fetch_models` → `pytest` is about a
minute and gives **196 passed, 17 skipped** (the 17 want a built index). See
`README.md`'s Quickstart, which is the transcript of that run.

## Headline numbers

Every number in this repo is `MEASURED` with a date and a
`docs/results/*.json` behind it, or labelled `TARGET`. The two to quote:

- **Boundary A P50 13.50 ms** (P70 15.08 · P95 18.38 · P100 33.44), 500 queries
  × 3 reps, warmed, on the box that serves the link above —
  `docs/results/2026-08-19-bench-stage7.json`
- **Ablation, 15 arms in one run** — `docs/results/2026-08-21-bench-ablation.json`.
  Hybrid fusion beats either half (dense only −0.0796 recall@10, bm25 only
  −0.0589); two arms argue against current defaults and were deliberately not
  applied in the run that measured them.

## Checklist

- [x] Public GitHub repo
- [x] Live link reachable by judges — not localhost, not password-gated
- [ ] Video 1 — 90 s, team and process, **not** the product
- [ ] Video 2 — end-to-end product demo
- [ ] Form submitted: https://forms.gle/MNvCjcv23Hn2Eeu58
- [ ] Both videos posted to Instagram, X and LinkedIn
- [ ] ≥1 Instagram post public
- [ ] Every post tagged `#RAGInGoa`

Team is **solo** (B3, closed 19 Aug), so "per member" is one person across three
platforms.

## Post URLs

Filled in before the form is submitted; the form asks for them.

| Platform | Video 1 | Video 2 |
|---|---|---|
| Instagram | | |
| X | | |
| LinkedIn | | |
