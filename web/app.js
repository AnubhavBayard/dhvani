/* dhvani UI — vanilla, no build step, no dependencies (ADR-008).
 *
 * The transport is fetch + a stream reader rather than EventSource, because
 * EventSource is GET-only and DESIGN.md puts the question in a POST body.
 * That costs the ~20 lines of frame parsing below and buys not having to
 * URL-encode a 500-character Devanagari question into a query string.
 *
 * The stage bar is the point of this page, not decoration: it is the shot the
 * demo video is built around, so it renders every stage the server reports,
 * including the ones that were switched off, with their real timings.
 */

const $ = (id) => document.getElementById(id);
const form = $("ask"), input = $("q"), go = $("go");
const mic = $("mic"), micLabel = $("mic-label"), micNote = $("mic-note");
const els = {
  stages: $("stages"), boundary: $("boundary"), answer: $("answer"),
  refusal: $("refusal"), sources: $("sources"), list: $("source-list"),
  corrections: $("corrections"), heard: $("heard"), details: $("details"),
};

/* The server sends its own copy with every refusal (guardrails/checks.py), so
 * these are the fallback for a kind this build has not seen and for `transport`,
 * which never reaches the server at all. */
const REFUSAL_COPY = {
  no_context: "Nothing in the indexed corpus matches this question.",
  model_refused: "The retrieved passages do not answer this question.",
  generation_unavailable: "The answer service is unavailable.",
  not_grounded: "The draft answer could not be tied back to the passages.",
  transport: "The connection to the server failed.",
};

/* -- microphone -----------------------------------------------------------
 *
 * Tap to start, tap to stop — not hold-to-talk. Press-and-hold loses the
 * recording if the pointer leaves the button, and the demo needs a hand free.
 *
 * Recording goes to POST /stt as one blob rather than streaming over a socket
 * (ADR-029). The transcript lands in the text box before anything is asked, so
 * the user sees what was heard and can fix it — and boundary A's clock starts
 * at the transcript, not at the microphone.
 */

let recorder = null, chunks = [];
/* The <span> for the sentence currently streaming, until L4 judges it. */
let sentence = null;

function micState(recording, note) {
  mic.setAttribute("aria-pressed", String(recording));
  micLabel.textContent = recording ? "Stop" : "Tap to speak";
  if (note !== undefined) micNote.textContent = note;
}

function showHeard(t) {
  els.heard.hidden = false;
  els.heard.replaceChildren(t.text);
}

async function sendAudio(blob) {
  const body = new FormData();
  body.append("file", blob, "audio.webm");
  micState(false, "Transcribing…");
  try {
    const res = await fetch("/stt", { method: "POST", body });
    const t = await res.json();
    if (!t.ok) {
      // First rung of the degradation ladder: the text box is always there.
      micState(false, "Couldn't catch that. Type your question instead.");
      input.focus();
      return;
    }
    showHeard(t);
    input.value = t.text;
    micState(false, "Hindi, Bengali, Tamil or English. Or type below.");
    form.requestSubmit();
  } catch (err) {
    micState(false, `Speech service unreachable — type instead. (${err})`);
    input.focus();
  }
}

async function startRecording() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    micState(false, "Microphone blocked — type your question instead.");
    input.focus();
    return;
  }
  chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    sendAudio(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
    recorder = null;
  };
  recorder.start();
  micState(true, "Listening… tap again when you're done.");
}

mic.addEventListener("click", () => {
  if (recorder) recorder.stop();
  else startRecording();
});

if (!navigator.mediaDevices || !window.MediaRecorder) {
  mic.disabled = true;
  micNote.textContent = "This browser has no microphone API — type below.";
}

function reset() {
  els.stages.replaceChildren();
  els.list.replaceChildren();
  els.answer.textContent = "";
  sentence = null;
  els.boundary.hidden = els.refusal.hidden = els.sources.hidden = true;
  els.details.hidden = true;
  els.corrections.hidden = true;
}

/* Internal stage ids are the server's vocabulary, not the reader's. */
const STAGE_LABELS = {
  guardrail_l1: "safety check", guardrail_l2: "scope check",
  guardrail_l3: "confidence check", stage4_loose: "query cleanup",
  stage4_rewrite: "query cleanup", stage3_embed: "understand question",
  stage3_retrieve: "search corpus", stage3_fuse: "merge results",
  stage3_rescore: "rescore", stage3_signals: "confidence",
  stage5_expansion: "expand query", stage6_rerank: "rerank",
  stage7_context: "pick passages", generation: "write answer",
  harness: "overhead",
};

function renderStages(stages) {
  els.details.hidden = false;
  els.stages.replaceChildren(...stages.map((s) => {
    const li = document.createElement("li");
    li.className = "stage";
    li.dataset.status = s.status || (s.enabled === false ? "off" : "ok");
    // Status as a word, not only a colour, and the number next to the name it
    // belongs to — a bar of coloured boxes is not a measurement.
    li.append(STAGE_LABELS[s.stage] || s.stage.replace(/_/g, " "), " ");
    const b = document.createElement("b");
    b.textContent = li.dataset.status === "off"
      ? "off" : `${s.duration_ms.toFixed(2)} ms`;
    li.append(b);
    return li;
  }));
}

function renderBoundary(ev) {
  els.boundary.hidden = false;
  els.boundary.replaceChildren();
  const b = document.createElement("b");
  b.textContent = `${ev.boundary_a_ms.toFixed(2)} ms`;
  const small = document.createElement("small");
  // The boundary statement travels with the number. A latency figure without
  // its boundary is the thing README.md exists to stop.
  small.textContent = "to find and select the passages, before the answer is written";
  els.boundary.append("Searched in ", b, small);
}

function renderCorrections(ev) {
  if (!ev.corrections || !ev.corrections.length) return;
  els.corrections.hidden = false;
  els.corrections.replaceChildren("corrected: ");
  ev.corrections.forEach(([before, after], i) => {
    const del = document.createElement("del"), ins = document.createElement("ins");
    del.textContent = before; ins.textContent = after;
    if (i) els.corrections.append(", ");
    els.corrections.append(del, " → ", ins);
  });
}

function renderSources(chunks) {
  if (!chunks.length) return;
  els.sources.hidden = false;
  els.list.replaceChildren(...chunks.map((c, i) => {
    const wrap = document.createElement("div");
    wrap.className = "source";
    const n = document.createElement("span");
    n.className = "n"; n.textContent = i + 1;
    const body = document.createElement("div");
    const p = document.createElement("p");
    p.textContent = c.text;
    body.append(p);
    wrap.append(n, body);
    return wrap;
  }));
}

function refuse(kind, reason, copy) {
  els.refusal.hidden = false;
  // L4 replaces the answer rather than trimming it: a partially hallucinated
  // answer with the hallucinations removed is still a broken answer.
  if (kind === "not_grounded") els.answer.textContent = "";
  els.refusal.replaceChildren(copy || REFUSAL_COPY[kind] || "Refused.");
}

const HANDLERS = {
  query: renderCorrections,
  retrieval: (ev) => {
    renderStages(ev.stages);
    renderBoundary(ev);
    renderSources(ev.context.chunks);
  },
  // One span per sentence, so L4's verdict can mark the sentence it judged.
  // The verdict arrives immediately behind the token that closed the sentence,
  // so "the span still open" is the sentence being judged.
  token: (ev) => {
    if (!sentence) els.answer.append((sentence = document.createElement("span")));
    sentence.textContent += ev.text;
  },
  refusal: (ev) => refuse(ev.kind, ev.reason, ev.copy),
  // L4, per sentence, arriving behind the tokens it judges.
  grounding: (ev) => {
    if (!sentence) return;
    // Too short to carry n-grams — closed unmarked rather than marked "unknown".
    if (ev.label === "skipped") { sentence = null; return; }
    sentence.className = `g-${ev.label}`;
    // Never colour alone (DESIGN_SYSTEM.md): the label is in the title, and
    // anything not grounded carries a visible mark of its own.
    sentence.title = `${ev.label} · overlap ${ev.overlap.toFixed(2)}` +
                     (ev.chunk_id ? ` · ${ev.chunk_id}` : "");
    if (ev.label === "ungrounded") sentence.append(" ⚠");
    else if (ev.label === "ambiguous") sentence.append(" ?");
    sentence = null;
  },
  done: (ev) => {
    renderStages(ev.stages);
    const small = els.boundary.querySelector("small");
    if (!small) return;
    const bits = [`answered in ${(ev.wall_clock_ms / 1000).toFixed(1)} s`];
    if (ev.grounding && ev.grounding.judged) {
      const g = ev.grounding;
      bits.push(`${g.grounded} of ${g.judged} sentences matched to sources`);
    }
    small.textContent = bits.join(" · ");
  },
};

/* One SSE frame per blank line; `event:` names it, `data:` carries the JSON. */
function* frames(buffer) {
  for (const raw of buffer.split("\n\n")) {
    const line = raw.split("\n").find((l) => l.startsWith("data:"));
    if (line) yield JSON.parse(line.slice(5));
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q) return;
  reset();
  go.disabled = true;
  try {
    const res = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q }),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      // Keep the trailing partial frame in the buffer — splitting on it would
      // hand JSON.parse half an object.
      const cut = buf.lastIndexOf("\n\n");
      if (cut < 0) continue;
      for (const ev of frames(buf.slice(0, cut))) HANDLERS[ev.type]?.(ev);
      buf = buf.slice(cut + 2);
    }
  } catch (err) {
    refuse("transport", String(err));
  } finally {
    go.disabled = false;
  }
});
