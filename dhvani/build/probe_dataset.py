"""Dataset reconnaissance for ai4bharat/MSMARCO-XI.

Reads parquet footers over HTTP range requests (no full download) to get exact
per-file row counts, then pulls a bounded sample of row groups to measure
passage counts, passage lengths, and field distributions.

Writes evidence JSON to docs/results/. Re-runnable; the numbers in DATASET.md
must come from this, never from the dataset card.

    python -m dhvani.build.probe_dataset --out docs/results/<date>-dataset-probe.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem

REPO = "datasets/ai4bharat/MSMARCO-XI"

# 3-letter prefixes as they appear in the filenames, mapped to the FLORES-style
# code the rows themselves carry in `target_lang`.
LANGS = {
    "asm": "asm_Beng", "ben": "ben_Beng", "guj": "guj_Gujr", "hin": "hin_Deva",
    "kan": "kan_Knda", "mal": "mal_Mlym", "mar": "mar_Deva", "nep": "npi_Deva",
    "ori": "ory_Orya", "pan": "pan_Guru", "san": "san_Deva", "tam": "tam_Taml",
    "tel": "tel_Telu", "urd": "urd_Arab",
}


def footers(fs: HfFileSystem) -> dict:
    """Exact row counts and row-group layout for every parquet file, footer only."""
    out = {}
    for split in ("train", "validation"):
        for path in sorted(fs.ls(f"{REPO}/{split}", detail=False)):
            name = path.rsplit("/", 1)[-1]
            key = f"{split}/{name}"
            for _ in range(3):  # the hub read times out often enough to matter
                try:
                    t0 = time.perf_counter()
                    with fs.open(path, "rb") as fh:
                        md = pq.ParquetFile(fh).metadata
                    out[key] = {
                        "num_rows": md.num_rows,
                        "num_row_groups": md.num_row_groups,
                        "num_columns": md.num_columns,
                        "serialized_size_bytes": md.serialized_size,
                        "footer_read_seconds": round(time.perf_counter() - t0, 2),
                    }
                    print(f"  {key}: {md.num_rows:,} rows, "
                          f"{md.num_row_groups} row group(s)", file=sys.stderr, flush=True)
                    break
                except Exception as exc:  # noqa: BLE001 - retry anything transient
                    print(f"  {key}: retry after {type(exc).__name__}",
                          file=sys.stderr, flush=True)
            else:
                out[key] = {"error": "footer read failed after 3 attempts"}
    return out


# Artifact patterns. The corpus is LLM output (see `meta.model_name`), so it
# carries the translator's refusals and its untranslated passes. Counted here
# because CHUNKING.md's build-time filter and GUARDRAILS.md's T5 section both
# quote these numbers, and a number quoted from a script nobody can re-run is
# the same as no number.
ARTIFACTS = {
    "llm_refusal": re.compile(
        r"(?i)i can'?t (fulfill|help|assist|provide)"
        r"|i cannot (fulfill|help|assist|provide)|as an ai"),
    "leading_apology": re.compile(r"(?i)^\s*(i'?m sorry|sorry,)"),
    "ascii_only": re.compile(r"^[\x00-\x7F\s]+$"),
    # T5: does corpus-borne prompt injection occur naturally in this dataset?
    "injection_phrasing": re.compile(
        r"(?i)ignore (the )?(previous|above) instructions|system prompt|you are an ai"),
}


def _artifacts(strings) -> dict:
    return {name: sum(1 for s in strings if rx.search(s))
            for name, rx in ARTIFACTS.items()}


def _lengths(strings) -> dict:
    """Char-length distribution. Percentiles, not just a mean — the mean of a
    passage-length distribution hides the tail that decides the chunker."""
    lens = sorted(len(s) for s in strings)
    if not lens:
        return {}
    n = len(lens)
    pct = lambda p: lens[min(n - 1, int(p / 100 * n))]  # noqa: E731
    return {
        "n": n, "min": lens[0], "p25": pct(25), "p50": pct(50), "p75": pct(75),
        "p90": pct(90), "p95": pct(95), "p99": pct(99), "max": lens[-1],
        "mean": round(statistics.fmean(lens), 1),
    }


def sample_stats(path: Path, limit: int | None = None) -> dict:
    """Measure what the chunker and the index sizing actually depend on.

    Local file only. Every file in this dataset is a single parquet row group,
    so there is no cheap partial read over HTTP — a row-group read is a
    full-file download. Download once, measure locally.
    """
    t0 = time.perf_counter()
    tbl = pq.read_table(path, columns=[
        "target_lang", "source_lang", "query_type", "passages",
        "query", "Answer", "Eng_Query",
    ])
    if limit:
        tbl = tbl.slice(0, limit)
    read_s = time.perf_counter() - t0

    rows = tbl.num_rows
    passages = tbl.column("passages").to_pylist()
    eng, tgt, selected, per_row = [], [], [], []
    for p in passages:
        e = p.get("English_passages") or []
        t = p.get("Translated_passages") or []
        s = p.get("is_selected") or []
        eng.extend(e)
        tgt.extend(t)
        selected.append(sum(s))
        per_row.append(len(t))

    answers = tbl.column("Answer").to_pylist()
    # Indic scripts are 3 bytes per character in UTF-8. Chunk text is the largest
    # line item in index memory, so bytes — not characters — size the index.
    tgt_bytes = [len(s.encode()) for s in tgt]
    eng_bytes = [len(s.encode()) for s in eng]

    def counts(col):
        c = {}
        for v in tbl.column(col).to_pylist():
            c[v] = c.get(v, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    return {
        "file": str(path),
        "rows_sampled": rows,
        "read_seconds": round(read_s, 2),
        "target_lang_values": counts("target_lang"),
        "source_lang_values": counts("source_lang"),
        "query_type_values": counts("query_type"),
        "passages_per_row": {
            "mean": round(statistics.fmean(per_row), 2),
            "min": min(per_row), "max": max(per_row),
            "total_translated": len(tgt), "total_english": len(eng),
        },
        "is_selected_per_row": {
            "mean": round(statistics.fmean(selected), 3),
            "rows_with_zero_selected": sum(1 for s in selected if s == 0),
            "rows_with_multiple_selected": sum(1 for s in selected if s > 1),
        },
        "translated_passage_chars": _lengths(tgt),
        "english_passage_chars": _lengths(eng),
        "query_chars": _lengths(tbl.column("query").to_pylist()),
        "answer_chars": _lengths(answers),
        "empty_translated_passages": sum(1 for s in tgt if not s.strip()),
        "empty_answers": sum(1 for s in answers if not s.strip()),
        "utf8_bytes_per_translated_passage": {
            "mean": round(statistics.fmean(tgt_bytes), 1),
            "p95": sorted(tgt_bytes)[int(0.95 * len(tgt_bytes))],
            "total_mb": round(sum(tgt_bytes) / 1e6, 1),
        },
        "utf8_bytes_per_english_passage": {
            "mean": round(statistics.fmean(eng_bytes), 1),
            "total_mb": round(sum(eng_bytes) / 1e6, 1),
        },
        "artifacts_in_translated_passages": _artifacts(tgt),
        "artifacts_in_answers": _artifacts(answers),
        "most_common_answers": [
            {"count": n, "text": v[:80]}
            for v, n in collections.Counter(answers).most_common(4)
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"docs/results/{date.today()}-dataset-probe.json")
    ap.add_argument("--langs", nargs="*", default=[],
                    help="3-letter file prefixes to sample from --local-dir")
    ap.add_argument("--split", default="validation", choices=("validation", "train"))
    ap.add_argument("--local-dir", default="data/raw",
                    help="where the downloaded parquet lives")
    ap.add_argument("--limit", type=int, default=None, help="cap rows per file")
    ap.add_argument("--skip-footers", action="store_true")
    args = ap.parse_args()

    fs = HfFileSystem()
    result = {
        "dataset": "ai4bharat/MSMARCO-XI",
        "probed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "parquet footer via HTTP range request; row-group sample for stats",
    }

    if not args.skip_footers:
        print("footers:", file=sys.stderr)
        result["files"] = footers(fs)
        result["totals"] = {
            "num_rows": sum(v["num_rows"] for v in result["files"].values()),
            "num_files": len(result["files"]),
        }

    result["samples"] = {}
    suffix = "val" if args.split == "validation" else "train"
    for lang in args.langs:
        path = Path(args.local_dir) / args.split / f"{lang}{suffix}.parquet"
        if not path.exists():
            print(f"skip {lang}: {path} not downloaded", file=sys.stderr)
            continue
        print(f"sampling {path} ...", file=sys.stderr, flush=True)
        result["samples"][lang] = sample_stats(path, args.limit)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.skip_footers and out.exists():
        # Footer reads are slow and network-bound; a stats-only re-run keeps the
        # inventory it already has rather than dropping it from the evidence.
        previous = json.loads(out.read_text())
        for key in ("files", "totals"):
            if key in previous:
                result.setdefault(key, previous[key])
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
