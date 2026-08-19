# Guardrails

The requirement is that the system demonstrably knows *when not to answer*. That
is a measurement problem, not a prompt problem — so every layer emits a logged
pass/fail with a score, and the catch rate is published per category alongside
the false-refusal rate. A system that refuses everything scores 100% on catches
and is useless; both numbers or neither.

## Threat model

What can actually go wrong here, ranked by likelihood in a judge demo:

| # | Threat | Vector | Cost if missed |
|---|---|---|---|
| T1 | Off-topic question | judge asks something MS MARCO doesn't cover | confident answer from irrelevant chunks — the classic RAG failure |
| T2 | Garbled/empty transcript | noisy room, accent, mic cut | retrieval on noise, plausible answer to a question nobody asked |
| T3 | Hallucination beyond context | generator embellishes past the passages | violates the brief's core grounding requirement |
| T4 | Prompt injection via transcript | spoken "ignore previous instructions…" | system prompt override |
| T5 | Prompt injection via **corpus** | a passage contains instruction-like text | retrieved content treated as instruction |
| T6 | Unsafe input | slurs, self-harm, illegal requests | reputational, live in front of judges |
| T7 | Language mismatch | question in Kannada, corpus subset lacks Kannada | silent quality collapse, no error |
| T8 | Ambiguous question | underspecified query | answer to a guessed interpretation |

T5 is the one most systems miss. Retrieved text is data, never instruction — it
is delimited in the prompt and never concatenated into the instruction block.

**T5 cannot be validated on the corpus as shipped.** `MEASURED 2026-08-15`: a
scan of all 977,545 Hindi validation passages found **zero** matches for
injection phrasing (`ignore previous instructions`, `system prompt`,
`you are an AI`). There is no naturally occurring corpus-borne injection here.
The T5 catch rate is therefore measured against **synthetic injected passages we
author and insert into a copy of the index**, and every T5 number in the results
table says so. A catch rate reported against a corpus containing nothing to catch
would be 100% and would mean nothing.

What the scan *did* find is a different problem, and a real one: the corpus is
machine-translated and carries the translator's own artifacts — 101 passages and
75 answers containing English LLM refusals (`I can't fulfill that request.`), and
331 passages left untranslated in ASCII. These are dropped by a build-time filter
(`CHUNKING.md`), which is also the honest place to handle them: filtering at
build time is logged in the build manifest, whereas filtering at query time would
be a guardrail quietly covering for the corpus.

## The chain

```
transcript ──┬── L1 input      (parallel with retrieval)
             │
             └── retrieval ─── L2 scope ─── L3 floor ─── generate ─── L4 output
```

L1 runs *beside* retrieval, not in front of it. A safety check that gates
retrieval serializes two independent operations; if L1 fails, retrieval results
are discarded and nothing was lost but idle CPU we had anyway.

L2, L3, L4 are inherently sequential — each needs the previous stage's output.

## L1 — Input layer

Runs concurrently with stage 4 + stage 3. Budget is off the critical path.

| Check | Method | Threshold | Threat |
|---|---|---|---|
| empty / too short | token count after normalization | `< 2 tokens` → refuse | T2 |
| garbled transcript | Sarvam confidence + OOV ratio against corpus vocab | `conf < 0.5` **or** `oov_ratio > 0.6` → refuse | T2 |
| language detect | script detection (Unicode block histogram) + fastText lid if ambiguous | not in indexed languages → refuse with the "language not indexed" copy | T7 |
| prompt injection | regex/phrase set over known injection patterns in English + the indexed languages, matched on the normalized transcript | any match → refuse | T4 |
| unsafe content | keyword/phrase lists per language + a small local classifier if lists prove too blunt | any match → refuse | T6 |

Deliberately not an LLM moderation call: that is a network round trip, and this
layer runs on every single request including the ones that get refused.

`OPEN — unsafe-content wordlists for 3 Indic languages.` Narrowed from 14 by
ADR-012: the indexed subset is Hindi, Bengali and Tamil, so L1 needs quality
lists for **Devanagari, Bengali and Tamil script only** — anything else is
refused by L2 as an out-of-subset language before content ever matters. That is a
material scope reduction on the hardest `OPEN` item in this document.
Quality lists in Tamil are still not something to improvise. Experiment: evaluate
the available multilingual toxicity lists and a small multilingual classifier on
a hand-built 100-item set, pick on precision — a false positive here refuses a
legitimate judge query on camera.

## L2 — Scope layer

**Question.** Is this question inside what the corpus can answer at all?

**Method.** No extra model. The signal is already computed: after stage 3, if the
best fused score sits below the corpus-coverage threshold, the question is
outside coverage. Calibrated by scoring a set of known-in-corpus queries and a
set of known-out-of-corpus queries (general knowledge, current events, questions
about the system itself) and placing the threshold at the crossover.

**Threshold.** `t_scope` `OPEN` — calibration run, target: false-refusal rate on
in-corpus queries below 5%.

**Out-of-subset languages are a named, testable case (T7).** ADR-012 indexes
Hindi, Bengali and Tamil. The dataset contains eleven more — Assamese, Gujarati,
Kannada, Malayalam, Marathi, Nepali, Odia, Punjabi, Sanskrit, Telugu, Urdu — and
a question in any of them must be refused with "that language isn't in this
index", not answered from the nearest Hindi passage. Because the language files
are row-aligned (`DATASET.md`), the eval set for this is exact rather than
approximate: take a query that *is* answerable in Hindi, and its Assamese
translation is the same question, same `query_id`, guaranteed out of subset. That
is a clean positive/negative pair per language, and it is free.

Detection is script-based first (a Kannada query is unambiguous from its code
points) and score-based second, for the three languages sharing Devanagari with
Hindi — Marathi, Nepali and Sanskrit are the hard cases, and they are the ones
the calibration run has to be honest about.

**Distinct from L3** because the refusal copy differs. Out-of-scope means "this
corpus is about X, ask me about X." Weak retrieval means "I found something but
not confidently enough." Collapsing them produces one vague error message, which
is exactly the outcome the brief calls out.

## L3 — Retrieval floor

**Question.** Is the best chunk good enough to generate from?

**Method.** Post-stage-6 top-1 score against a floor. Refuse *before* generating.
Costs nothing and prevents the most expensive path in the system — the cheapest
guardrail we have is the one that skips the LLM call.

**Threshold.** `t_floor` `OPEN` — swept against the answerable-query set,
choosing the point where refusals correlate with actually-wrong answers.

**The sweep has a labelled negative set, free.** `MEASURED 2026-08-15`
(`DATASET.md`): **44,046 of 97,941 validation rows per language have no passage
marked `is_selected`**, and 43,991 of them carry the literal answer
`No Answer Present.` These are queries the dataset itself declares unanswerable
from its own passages. They are exactly what L3 exists to catch, and there are
tens of thousands of them per language rather than the handful a hand-built set
would contain.

So `t_floor` is swept on two real populations rather than one and a guess:

| population | rows per language | L3 should |
|---|---|---|
| ≥1 `is_selected` passage | 53,895 | pass |
| 0 `is_selected`, answer = "No Answer Present." | 43,991 | **refuse** |

That turns the threshold sweep into a straightforward operating-point choice with
a real ROC behind it, and it means the L3 catch rate in the results table is
measured on ~44k labelled negatives, not on the ~20 refusal items in
`eval/adversarial.jsonl`. The adversarial set still covers the categories this
one cannot — injection, out-of-scope language, unsafe content.

## L4 — Output layer

Runs **on the token stream**, per sentence, as generation streams. Checking after
generation completes adds its full cost to the tail.

**Step 1 — citation-span overlap (~5 ms).** For each generated sentence, compute
n-gram overlap (n=3, script-aware tokenization) against the selected chunks.
- overlap ≥ `t_high_overlap` → grounded, attach chunk id, done.
- overlap ≤ `t_low_overlap` → ungrounded, flag.
- between → ambiguous, escalate to step 2.

**Step 2 — local NLI cross-encoder (~15 ms), ambiguous sentences only.** Premise
= concatenated selected chunks, hypothesis = the sentence. Entailment below
threshold → ungrounded. Runs on the same ONNX runtime already warm for the
reranker, so there is no new model load and no new dependency.

Not an LLM groundedness judge: that is another 200 ms+ per request for a check
these two steps do locally.

**Step 3 — citation enforcement.** Every claim maps to a chunk id. Sentences
with no chunk id are dropped from the answer, and if a majority of sentences are
dropped the whole answer is replaced with the not-grounded refusal. A partially
hallucinated answer with the hallucinations quietly removed is still a broken
answer.

`OPEN — thresholds t_high_overlap, t_low_overlap, t_entail.` Set on a labelled
set of 100 (answer, context) pairs, half grounded and half deliberately
hallucinated, choosing the operating point on the precision/recall curve.

## Refusal copy

Lowercase, sentence case, no exclamation marks — house voice per
`DESIGN_SYSTEM.md`. Each says what happened and what to do next. None of them
apologize twice.

| Kind | Trigger | Copy |
|---|---|---|
| `empty_audio` | L1 empty | "didn't catch anything — try holding the mic button while you speak." |
| `garbled` | L1 confidence/OOV | "the transcription came back unclear. try again, or type it instead." |
| `unsupported_language` | L1 language | "that sounded like {lang}. right now the index covers {langs}." |
| `unsafe` | L1 unsafe | "not going to answer that one." |
| `injection` | L1 injection | "that reads like an instruction rather than a question. ask me something about the corpus." |
| `off_topic` | L2 | "that's outside what this corpus covers. it's ms marco — general web questions, in {langs}." |
| `weak_retrieval` | L3 | "found some passages but none confidently enough to answer from. closest matches below." |
| `not_grounded` | L4 | "i drafted an answer but couldn't tie it back to the retrieved passages, so i'm not showing it. here's what was retrieved." |
| `generation_failed` | harness | "couldn't generate the summary. the retrieved passages are below." |

`weak_retrieval`, `not_grounded`, and `generation_failed` all still show the
retrieved passages. A refusal that shows its work is more useful than one that
shows nothing, and it demonstrates the retrieval half of the system still worked.

## Adversarial eval set

`OPEN — final size.` Target ~140 items, ~20 per category, hand-built and
committed as `eval/adversarial.jsonl` with the expected verdict per item so the
catch rate is a test rather than a judgement call.

| Category | n | Expected | Notes |
|---|---|---|---|
| off-topic | 20 | refuse `off_topic` | current events, questions about this system, personal questions |
| injection | 20 | refuse `injection` | direct, roleplay-framed, and corpus-embedded (T5) |
| unanswerable-from-corpus | 20 | refuse `off_topic`/`weak_retrieval` | plausible topic, absent from the indexed subset |
| ambiguous | 20 | answer with hedge **or** refuse — both acceptable, silent guessing is not | |
| non-English | 20 | answer if indexed, refuse `unsupported_language` if not | spread across indexed and non-indexed languages |
| silent/noisy audio | 20 | refuse `empty_audio`/`garbled` | real recordings: silence, room noise, cut-off speech |
| adversarial-but-benign | 20 | **answer** | the false-positive control: questions that trip keyword filters but are legitimate (medical terms, violence in a historical passage). Without this category the catch rate is unfalsifiable. |

## Metrics

Published in `README.md` and shown in the UI's guardrails panel:

- **catch rate per category** — refused when it should have refused
- **false-refusal rate** — refused an answerable query. The number that keeps the
  catch rate honest.
- **grounding precision** — of sentences kept by L4, how many are actually
  supported, on a hand-labelled sample
- **layer attribution** — which layer caught each item. A layer that never fires
  is dead code; a layer that catches everything means the ones before it are.
- **latency per layer**, measured, since L1–L3 sit in boundary A

Every request logs a `GuardrailVerdict` per layer, pass or fail, with its score
and elapsed time. The log is the evidence.
