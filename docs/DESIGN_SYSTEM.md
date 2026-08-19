# Design system

## Source of the tokens

Every value below was read out of the live site on **14 Aug 2026**:

- `https://hhgoa.com/` — page HTML, inline type specs
- `https://hhgoa.com/_next/static/chunks/3eyy904_fkf59.css` — CSS custom properties

**The footer "Brand Kit" is not a link.** It renders as a `<p>` with no `href`;
`/brand-kit` returns 404. The only Drive asset linked from the site is the
*Task 1 brief PDF*, not a brand kit. So the site's own stylesheet is the source
of truth here, and it is a better one than a PDF anyway — these are the values
the organizers actually ship.

`OPEN — brand kit asset.` If a real brand kit exists somewhere off-site (deck,
Figma, Notion), send it and these tokens get reconciled against it. Nothing is
blocked meanwhile.

**Note on the sibling task.** `../brand/BRAND.md` in this repo carries a
*different* palette — `#074C18` green, Playfair Display, Space Grotesk — sampled
from the printed key art for task 1. Those are poster values, not web values.
This project uses the site values below. The two are not reconciled and should
not be: they describe different artifacts. See `DECISIONS.md#adr-006`.

## Color

Verbatim from the site's CSS custom properties.

| Token | Value | Site role |
|---|---|---|
| `--background` | `#0b6839` | page background |
| `--foreground` | `#fff` | body text on green |
| `--primary` | `#0b6839` | |
| `--primary-foreground` | `#fff` | |
| `--secondary` | `#fee101` | the yellow — headings, ring, muted-foreground |
| `--secondary-foreground` | `#000` | text on yellow |
| `--accent` | `#ff0080` | the magenta |
| `--accent-foreground` | `#fff` | |
| `--muted` | `#0b6839` | |
| `--muted-foreground` | `#fee101` | |
| `--card` | `#0b6839` | |
| `--card-foreground` | `#fff` | |
| `--border` | `#0b6839` | |
| `--input` | `#0b6839` | |
| `--ring` | `#fee101` | focus ring |
| `--destructive` | `#dc2626` | |
| `--chart-1` … `--chart-5` | `#0b6839`, `#fee101`, `#ff0080`, `#fff`, `#ccc` | |

Additional hexes appearing inline in the page markup:

| Value | Occurrences | Observed use |
|---|---|---|
| `#FFFFFF` | 21 | text |
| `#0B6839` | 11 | surfaces |
| `#EDD723` | 8 | a slightly duller yellow than `#fee101` |
| `#FEE101` | 7 | primary yellow |
| `#F9DC01` | 4 | third yellow variant |
| `#fffbe8` | 2 | near-white cream |

Three yellows within eight hex values of each other is what an exported design
file looks like, not an intentional scale. **This app uses `#fee101` as the
single yellow** and `#fffbe8` as the cream. Recorded here so the choice is
visibly a choice.

**Ratio discipline.** Green is the ground and dominates. Yellow carries
structure — headings, focus rings, active states. Magenta `#ff0080` is
punctuation only: refusal states and the recording indicator, nothing else. If
magenta covers more than a few percent of a screen, it stops meaning anything.

## Type

| Token | Value |
|---|---|
| `--font-imbue` | `"Imbue", "Imbue Fallback"` |
| `--font-victor-mono` | `"Victor Mono", "Victor Mono Fallback"` |
| `--default-font-family` | `var(--font-victor-mono)` |
| `--default-mono-font-family` | `var(--font-victor-mono)` |

**Victor Mono is the default UI font for the whole site** — not just for code.
That is the "developers who live in their terminals" identity, and it is the
single most important thing to carry over. Imbue is the display serif, used at
very large sizes only.

Weights present in the CSS: `100, 400, 500, 700, 800`. Weights in use on real
type: Imbue `500`, `700`; Victor Mono `600`, `700`.

Real type specs pulled verbatim from the page:

| Family | Weight | Size | Line height | Tracking | Where |
|---|---|---|---|---|---|
| Imbue | 500 | `132.8px` | `1.1em` | `0` | hero display |
| Imbue | 500 | `64px` | `1.1em` | `0` | section display |
| Imbue | 700 | `67.13px` | `0.77em` | `0` | tight display lockup |
| Victor Mono | 600 | `33.38px` | `0.84em` | `0.5em` | wide-tracked label |
| Victor Mono | 600 | `22px` | `0.84em` / `0.88em` | `0` / `-0.015em` | nav, UI |
| Victor Mono | 600 | `20px` | `0.88em` | `-0.015em` | UI |
| Victor Mono | 600 | `19px` | `0.88em` | `-0.015em` | UI |
| Victor Mono | 600 | `15.89px` | `0.84em` | `0` | footer, small caps |
| Victor Mono | 700 | `22px` | `1.2em` | `0` | body emphasis |
| Victor Mono | 700 | `18px` | `1.2em` / `1.3em` | `0` / `-0.01em` | body |

Sub-pixel sizes (`132.8`, `67.129`, `15.886`) are design-tool export artifacts.
The scale below rounds them into something usable and says so.

**App type scale** (derived, rounded — the derivation is the only place this doc
departs from verbatim values):

| Step | Size | Family | Weight | Line height | Tracking | Use |
|---|---|---|---|---|---|---|
| `display` | `clamp(48px, 9vw, 133px)` | Imbue | 500 | `1.1` | `0` | the one hero line |
| `title` | `64px` | Imbue | 500 | `1.1` | `0` | answer heading |
| `label-wide` | `33px` | Victor Mono | 600 | `0.84` | `0.5em` | stage bar labels |
| `body-lg` | `22px` | Victor Mono | 700 | `1.2` | `0` | answer text |
| `body` | `18px` | Victor Mono | 700 | `1.3` | `-0.01em` | transcript, passages |
| `ui` | `20px` | Victor Mono | 600 | `0.88` | `-0.015em` | buttons, controls |
| `caption` | `16px` | Victor Mono | 600 | `0.84` | `0` | timings, chunk ids, footer |

Sentence case and lowercase throughout. Title Case is the corporate-SaaS tell the
site avoids, except in the wide-tracked uppercase labels, where uppercase *is* the
treatment.

### Script coverage — resolved 2026-08-14, and it is a problem

Checked against the Google Fonts CSS API (`unicode-range` per subset):

| Font | Subsets shipped | Indic? |
|---|---|---|
| **Victor Mono** | latin, latin-ext, cyrillic, cyrillic-ext, greek, vietnamese | **no** |
| **Imbue** | latin, latin-ext, vietnamese | **no** |

Neither brand font can render a single character of Devanagari, Tamil, Telugu,
Bengali, Gurmukhi, Gujarati, Kannada, Malayalam, Odia, or Urdu.

This is not a corner case for this app — it is *most of the content*. The
transcript, the retrieved passages, the citations, and the answer are all Indic
text. Left unhandled, every meaningful string on the page renders in whatever
the browser picks, and the brand fonts only ever appear on chrome and numbers.

**Resolution — explicit two-tier stack, not a silent fallback:**

```css
--font-ui:      "Victor Mono", ui-monospace, monospace;         /* chrome, numbers, labels */
--font-display: "Imbue", Georgia, serif;                        /* hero only */
--font-indic:   "Noto Sans Devanagari", "Noto Sans Bengali", "Noto Sans Tamil",
                "Noto Sans", sans-serif;                        /* all corpus + transcript text */
```

Three families, because ADR-012 indexes three scripts: Devanagari (Hindi),
Bengali (Bengali), Tamil (Tamil). Noto Sans Telugu is out of the stack — Telugu
is not in the subset.

Content text gets `--font-indic` **first** in its stack, with Victor Mono ahead
of it only where the text is known-Latin (timings, chunk ids, stage labels).
Doing it the other way — Victor Mono first, Noto as fallback — produces mixed
per-glyph substitution inside a single sentence, which looks broken.

**This fed the subset decision — with one correction.** `CORRECTED 2026-08-15`:
the cost is one woff2 per **script**, not per language. Devanagari alone renders
Hindi, Marathi, Nepali and Sanskrit; the Bengali family renders Bengali and
Assamese. So the font constraint is much weaker than ADR-007 assumed — four
languages could have shared one download.

That cuts the other way for us. ADR-012 chose three *different* scripts on
purpose, because a multilingual retriever validated on one script has not been
validated. Three scripts is therefore the deliberately expensive option on font
payload, and the reason is recorded so nobody later "optimizes" it by collapsing
the subset onto Devanagari.

**Budget: three woff2 subsets.** Latin-range glyphs are stripped from all three
(Victor Mono covers Latin), leaving the Indic block plus common punctuation.

`RESOLVED — Urdu excluded.` Perso-Arabic needs Noto Nastaliq Urdu, a large file
and right-to-left. It is excluded from the subset by ADR-012, on a stronger
reason than font size: `libindic/soundex` produces no phonetic signal at all for
Perso-Arabic, so stage 4 has no correction path for it. No RTL work is needed
anywhere in the UI.

Both families are open-licensed (SIL OFL). Self-host the woff2 subsets; a
Google Fonts CDN request is a network hop in front of a latency demo.

## Spacing, radius, motion

| Token | Value | Notes |
|---|---|---|
| `--spacing` | `0.25rem` (4px) | base unit; scale is multiples of 4 |
| `--radius` | `0.625rem` (10px) | base |
| `--radius-sm` | `calc(var(--radius) * 0.6)` = 6px | in use on the site |
| `--radius-md` | `calc(var(--radius) * 0.8)` = 8px | in use |
| `--radius-lg` | `calc(var(--radius) * 1.8)` = 18px | in use |
| `--container-sm` | `24rem` | |
| `--container-3xl` | `48rem` | |
| `--container-4xl` | `56rem` | main content column |
| `--leading-tight` | `1.25` | |
| `--leading-snug` | `1.375` | |
| `--leading-relaxed` | `1.625` | |
| `--tracking-tight` | `-0.025em` | |
| `--tracking-normal` | `0em` | |
| `--tracking-wide` | `0.025em` | |
| `--default-transition-duration` | `0.15s` | |
| `--default-transition-timing-function` | `cubic-bezier(.4, 0, .2, 1)` | |
| `--ease-out` | `cubic-bezier(0, 0, .2, 1)` | |

`OPEN — spacing scale beyond the base unit.` The site is an absolutely-positioned
design-tool export, so its gaps are one-off pixel values rather than a scale.
Only the `0.25rem` base is a real token. This app uses a 4px-multiple scale
(4/8/12/16/24/32/48/64) built on that base — derived, not extracted, and flagged
as such.

## Motion

150 ms, `cubic-bezier(.4, 0, .2, 1)`, as the site defines. Restrained — this is a
latency project, and animation that delays information contradicts the pitch.

- stage bar transitions: 150 ms, site easing
- mic press feedback: immediate, no transition — perceived latency starts here
- recording pulse: 1.2 s ease-in-out loop on the mic ring, cream `#fffbe8`
- answer tokens: appear as they stream, no fade. A fade delays the first token to
  look smooth, which is exactly backwards for this app.
- `prefers-reduced-motion: reduce` kills the pulse and all transitions. Stage
  progression stays legible as discrete state changes.

## Component inventory

### 1 — mic control (the hero)

The largest thing on the page. Not an icon in a form.

- Circular, `--secondary` fill on `--background`, `--secondary-foreground` glyph.
- Press-to-talk: `pointerdown` starts, `pointerup` stops. Also toggleable by
  `Space`/`Enter` when focused, because press-and-hold is not keyboard-operable.
- States: `idle` → `listening` (cream `#fffbe8` ring, pulsing — cream not magenta,
  per the contrast audit; magenta may fill inside the ring but never carries the
  state alone) → `processing` (ring becomes the stage bar) → `idle`.
- Focus ring uses `--ring` `#fee101`, 3px, 3px offset — visible, never removed.
- Permission denied → collapses to the text input fallback with a one-line note.
  No modal.

### 2 — transcript view

- `body` type, `--foreground`.
- Partial transcript renders at 60% opacity; finalized text at full. The user can
  see the recognizer changing its mind, which is honest and also the most
  convincing thing in the demo video.
- Corrections from stage 4 are marked: original struck in `--muted-foreground`,
  correction in `--secondary`, `title` attribute giving the method. This is the
  one place the query-rewrite stage is visible to a user, and it is worth showing.
- Serves as the caption track for the transcribed audio.

### 3 — stage progress indicator

Seven-segment horizontal bar, `label-wide` type, uppercase, `0.5em` tracking.

- Segments: `transcribing · rewriting · retrieving · fusing · expanding · reranking · selecting`
- States per segment: pending (`--border`), active (`--secondary`, animated),
  done (`--secondary` solid, elapsed ms in `caption` underneath), skipped
  (dimmed + "skipped" — fast-tier early exit is a *feature*, so it is labelled,
  not hidden), degraded (magenta **fill**, not outline — a magenta line on green
  is 1.82 and invisible; fallback path was taken).
- `aria-live="polite"` announcing stage transitions.
- This component is the differentiator. A spinner would hide the entire thesis of
  the submission.

### 4 — answer panel with citations

- `body-lg` type. Tokens appear as they stream.
- Each sentence carries a superscript chunk marker; click scrolls to and expands
  the source chunk with its `char_span` highlighted.
- Chunk panel shows: chunk id, strategy, language, final score, source passage
  with the cited span in `--secondary`.
- Sentences dropped by guardrail L4 never render. Nothing to strike through — the
  user only ever sees grounded text.

### 5 — latency readout

- `caption` type, sits under the answer, collapsed by default to a single line:
  `query path 87ms · tier fast · ttft 340ms`.
- Expands to the per-stage table for this query.
- A persistent corner panel shows the running session P50/P70/P100 and the
  fast-path hit rate, with cache state stated explicitly (`cache: off`). Judges
  should be able to read the numbers without opening the repo — and should be able
  to see we disclose the cache.

### 6 — refusal states

Nine kinds, per `GUARDRAILS.md`, in three visual treatments. Never a toast — a
toast reads as a bug, and these are correct behavior.

| Treatment | Kinds | Look |
|---|---|---|
| **out of scope** | `off_topic`, `unsupported_language` | full panel, `--secondary` rule above, calm. States what the corpus *does* cover. |
| **not confident** | `weak_retrieval`, `not_grounded`, `generation_failed` | panel + the retrieved passages below it. The system shows its work. |
| **declined** | `unsafe`, `injection` | compact magenta `#ff0080` **fill block**, white `body-lg` text (22px/700 clears AA-large at 3.77). Short. No lecture, no explanation of policy. |
| **input problem** | `empty_audio`, `garbled` | inline near the mic, not in the answer area. It's an input issue, so it belongs at the input. |

### 7 — text input fallback

- Always present, never hidden behind a toggle. The mic can fail, STT can fail,
  and a judge on a laptop with no microphone still needs a working demo.
- Same type and border language as the rest; `--input` background, `--ring` focus.

## Accessibility floor

Non-negotiable, and none of it is optional for latency reasons.

- Every control keyboard-operable; mic works via Space/Enter, not press-and-hold only.
- Focus visible everywhere, `--ring`, never `outline: none`.
- `aria-live` regions for transcript (polite) and stage changes (polite).
### Contrast audit — run 2026-08-14

WCAG 2.1 relative-luminance ratios, computed on the extracted hex values.

| Pair | Ratio | AA normal (4.5) | AA large (3.0) | AAA (7.0) |
|---|---|---|---|---|
| `#fee101` yellow on `#0b6839` | **5.23** | pass | pass | fail |
| `#ffffff` white on `#0b6839` | **6.88** | pass | pass | fail |
| `#fffbe8` cream on `#0b6839` | **6.62** | pass | pass | fail |
| `#edd723` dull yellow on `#0b6839` | **4.70** | pass | pass | fail |
| `#000000` on `#fee101` yellow | **15.98** | pass | pass | pass |
| `#ffffff` on `#ff0080` magenta | **3.77** | **fail** | pass | fail |
| **`#ff0080` magenta on `#0b6839`** | **1.82** | **fail** | **fail** | fail |

Two findings.

**Yellow on green passes AA at every size** — 5.23. The worry about `caption`-size
yellow was unfounded; no size restriction needed. AAA is out of reach for the
brand's own palette, which is a property of the brand, not something to fix here.

**Magenta on green is unusable — 1.82.** It fails not just the 4.5 text threshold
but the 3.0 threshold for non-text UI components. So magenta cannot be a rule, a
border, a ring, or an outline **on the green ground**, which is exactly where the
design was going to use it (refusal rules, recording indicator, degraded-stage
outlines). This is the design-system rule that comes out of it:

- **Magenta is a fill, never a mark on green.** `#ff0080` as a background block
  with `#fff` on it reads at 3.77 — AA-large only, so text on magenta must be
  ≥24px, or ≥18.66px bold. The `body-lg` step (22px / weight 700) qualifies.
- **Recording indicator:** the pulsing ring is `#fffbe8` cream (6.62) — that ring
  carries the state. Magenta may sit inside it as a fill. State is never conveyed
  by magenta alone.
- **Degraded-stage marker:** magenta fill on the segment, not a magenta outline.
- **Declined-refusal treatment:** magenta fill block, white `body-lg` text.

Rewriting the component rules above accordingly; the `#ff0080` "rule above the
panel" idea is dropped — it would have been invisible.
- Transcript doubles as captions for the audio.
- `prefers-reduced-motion` respected.

## Budget

Total page weight target: **< 50 KB gzipped**, excluding self-hosted font subsets.
No framework, no build step, no runtime CSS-in-JS. A hydration bundle in front of
a 40 ms retrieval path would undercut the entire submission.
