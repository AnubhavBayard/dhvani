"""Stage 4 — query rewriting, before anything touches the index.

Spec: `docs/RAG_PIPELINE.md` stage 4. Repairs what STT mangles — script variants,
Indic numerals, garbled proper nouns — because a query rewritten *after*
retrieval is repair work applied to a decision already made.

Two steps, no model:

1. **Normalization** — the same `normalize()` the build applies to every chunk.
   Imported, never reimplemented: a normalization mismatch between the index and
   the query is invisible and destroys lexical recall.
2. **Phonetic correction** — terms the corpus has never seen are matched against
   the phonetic vocabulary built at index time (`phonetic_vocab.json`). The
   bucket key is `soundex(term)[1:]`, because `libindic/soundex` passes the first
   character through verbatim and the full code is therefore script-tagged
   (`tests/test_phonetic_contract.py`). The tail key *selects* candidates;
   `Soundex.compare()` and a bounded edit distance *score* them.

Terms the corpus already knows are left alone. Dense retrieval handles ordinary
paraphrase, and an aggressive rewriter here destroys more than it fixes — so the
rewriter only ever touches a term it cannot find.

    from dhvani.retrieve.stage4 import QueryRewriter
    rw = QueryRewriter.load("index/full")
    query, trace = rw.rewrite("मुंबई मे कितने लोग")
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dhvani.build.chunk import normalize
from dhvani.harness.contracts import PipelineTrace, Query, stage

# Script of a query, from the first character that belongs to one. Used for the
# tier decision and for reporting, not for retrieval — the index is multilingual
# and a query is answered from every corpus.
_SCRIPT_RANGES = (
    ("Deva", 0x0900, 0x097F), ("Beng", 0x0980, 0x09FF),
    ("Taml", 0x0B80, 0x0BFF), ("Latn", 0x0041, 0x024F),
    # Out of subset (ADR-012), and named rather than lumped into "und": the T7
    # refusal copy is "that sounded like {lang}", which needs the language.
    ("Guru", 0x0A00, 0x0A7F), ("Gujr", 0x0A80, 0x0AFF),
    ("Orya", 0x0B00, 0x0B7F), ("Telu", 0x0C00, 0x0C7F),
    ("Knda", 0x0C80, 0x0CFF), ("Mlym", 0x0D00, 0x0D7F),
    ("Arab", 0x0600, 0x06FF),
)
_SCRIPT_LANG = {"Deva": "hin_Deva", "Beng": "ben_Beng",
                "Taml": "tam_Taml", "Latn": "eng_Latn",
                "Guru": "pan_Guru", "Gujr": "guj_Gujr", "Orya": "ory_Orya",
                "Telu": "tel_Telu", "Knda": "kan_Knda", "Mlym": "mal_Mlym",
                "Arab": "urd_Arab"}


@dataclass(frozen=True)
class Stage4Config:
    enabled: bool = True
    max_edit_distance: int = 2
    # 5, not 3, and the difference is measured. At 3 the rewriter touched 134 of
    # 500 clean queries and cost 0.0207 recall@10; at 5 it touches 75 and costs
    # 0.0103, while on the garbled set it is the only setting that pays at all
    # (+0.0035 recall@10, +0.0104 MRR@10). Short tokens are where a phonetic
    # code carries the least signal and a wrong correction the most damage.
    min_term_len: int = 5
    max_candidates: int = 32     # the vocabulary's own bucket cap
    # Minimum `Soundex.compare()` result to accept a correction: 1 is phonetic
    # equality within a language, 2 across languages, -1 no match. The tail code
    # only *blocks*; without a floor here a candidate that shares a bucket but
    # fails the phonetic comparison can still win on edit distance alone, which
    # is how a rewriter starts damaging correct rare terms.
    min_phonetic: int = 1
    llm_rewrite: bool = False    # escalation tier only; never the default path


def detect_script(text: str) -> tuple[str, str]:
    """(script, language) from the first character in a known range.

    Mixed-script queries are real — an English brand name inside a Hindi
    sentence — and the first Indic character wins, because that is the language
    the user is speaking.
    """
    latin = None
    for ch in text:
        code = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= code <= hi:
                if name == "Latn":
                    latin = latin or name
                    break
                return name, _SCRIPT_LANG[name]
    return (latin, _SCRIPT_LANG[latin]) if latin else ("Zyyy", "und")


def edit_distance_within(a: str, b: str, limit: int) -> int | None:
    """Levenshtein distance, or None once it is certain to exceed `limit`.

    Bounded on purpose: the caller only cares whether a candidate is within two
    edits, and the full matrix for a rejected candidate is work nobody reads.
    """
    if abs(len(a) - len(b)) > limit:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= limit else None


class QueryRewriter:
    """Query-time view of the phonetic vocabulary. Loaded once at boot."""

    def __init__(self, buckets: dict[str, list[str]], known: set[str]):
        self.buckets = buckets
        # Membership comes from the lexical index's vocabulary, not from the
        # phonetic buckets: those are capped at 32 terms per code, so a common
        # word can be absent from them while being everywhere in the corpus.
        # Read from the buckets, "what" was unknown and got "corrected" to
        # "that" (measured 18 Aug, ADR-023).
        self.known = known
        from libindic.soundex import Soundex
        self.soundex = Soundex()

    @classmethod
    def load(cls, index_dir: str | Path = "index") -> "QueryRewriter":
        d = Path(index_dir)
        buckets = json.loads((d / "phonetic_vocab.json").read_text())
        # `vocab.index.json` is exactly what BM25 indexed, tokenized by the
        # pattern both sides share — so "known to the corpus" here means the
        # same thing it means to the retriever.
        known = set(json.loads((d / "bm25" / "vocab.index.json").read_text()))
        return cls(buckets, known)

    def _correct(self, term: str, cfg: Stage4Config) -> str | None:
        """The best in-vocabulary term for one out-of-vocabulary term, or None.

        Scoring is `compare()` first, edit distance second. `compare()` returns 2
        for phonetic equality *across* languages and 1 within one, which is the
        signal the tail key cannot give on its own — Bengali and Tamil diverge
        from Devanagari on some words (RAG_PIPELINE.md stage 4).
        """
        try:
            code = self.soundex.soundex(term)[1:]
        except Exception:  # noqa: BLE001 — a term the library cannot encode
            return None
        candidates = self.buckets.get(code)
        if not candidates:
            # ponytail: exact tail-code blocking only. A dropped matra mid-word
            # changes the code itself (कंप्यटर → NMOIP00 vs कंप्यूटर → NMOCIP0),
            # so that class of garble is a miss, not a correction. The upgrade
            # is a deletion-neighbourhood index over the codes (SymSpell-style),
            # which costs ~2M extra keys — worth it only if the measured catch
            # rate says so.
            return None

        best, best_key = None, None
        for cand in candidates[:cfg.max_candidates]:
            dist = edit_distance_within(term, cand, cfg.max_edit_distance)
            if dist is None:
                continue
            try:
                phon = self.soundex.compare(term, cand)
            except Exception:  # noqa: BLE001
                phon = 0
            if phon < cfg.min_phonetic:
                continue
            # Prefer a phonetic match, then the fewest edits, then the shortest
            # candidate — deterministic, so replay reproduces the same rewrite.
            key = (-max(phon, 0), dist, len(cand), cand)
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return best

    def rewrite(self, raw: str, cfg: Stage4Config | None = None,
                trace: PipelineTrace | None = None) -> tuple[Query, PipelineTrace]:
        cfg = cfg or Stage4Config()
        trace = trace or PipelineTrace()

        with stage(trace, "stage4_rewrite", enabled=cfg.enabled,
                   max_edit_distance=cfg.max_edit_distance) as st:
            if not cfg.enabled:
                # The ablation arm still gets a row, and the raw transcript is
                # what stage 3 receives — not a half-normalized version of it.
                st.detail["reason"] = "disabled — ablation arm '− stage 4'"
                script, lang = detect_script(raw)
                return Query(raw=raw, lang=lang, script=script,
                             method="passthrough"), trace

            text = normalize(raw)
            script, lang = detect_script(text)
            corrections: list[tuple[str, str]] = []
            out = []
            for term in text.split():
                if len(term) < cfg.min_term_len or term in self.known:
                    out.append(term)
                    continue
                fixed = self._correct(term, cfg)
                out.append(fixed or term)
                if fixed and fixed != term:
                    corrections.append((term, fixed))

            rewritten = " ".join(out)
            st.detail.update(corrections=len(corrections), terms=len(out),
                             script=script, normalized=rewritten != raw)
            return Query(raw=raw,
                         # Only claim a rewrite when the text actually changed;
                         # `Query.text` falls back to `raw`, and an identical
                         # "rewritten" string makes the trace lie about what
                         # this stage did.
                         rewritten=rewritten if rewritten != raw else None,
                         lang=lang, script=script,
                         method="phonetic" if corrections else "passthrough",
                         corrections=corrections), trace
