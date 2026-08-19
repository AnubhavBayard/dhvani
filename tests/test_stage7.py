"""Stage 7 — context selection.

The store is built in memory rather than read off `index/`, so these run without
a built index and fail for exactly one reason: the selection logic changed.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from dhvani.harness.contracts import Query, RetrievalResult, ScoredChunk
from dhvani.retrieve.stage7 import (ChunkStore, Stage7Config, TokenCounter,
                                    jaccard, select_context, shingles)


class FakeCounter:
    name = "fake (1 token per word)"

    @staticmethod
    def count(text: str) -> int:
        return len(text.split())


def store_of(rows: list[dict]) -> ChunkStore:
    cols = {c: [r.get(c) for r in rows] for c in ChunkStore.COLUMNS}
    return ChunkStore(pa.table(cols, schema=pa.schema([
        ("chunk_id", pa.string()), ("text", pa.string()),
        ("parent_text", pa.string()), ("doc_id", pa.string()),
        ("lang", pa.string()), ("strategy", pa.string()),
        ("overlap_with", pa.list_(pa.string()))])))


def row(i: int, text: str, *, overlap=None, lang="hin_Deva", parent=None) -> dict:
    return {"chunk_id": f"c{i}", "text": text, "parent_text": parent,
            "doc_id": "42", "lang": lang, "strategy": "s1_passage",
            "overlap_with": overlap or []}


def result_of(n: int) -> RetrievalResult:
    return RetrievalResult(query=Query(raw="q"), chunks=[
        ScoredChunk(chunk_id=f"c{i}", row=i, score=1.0 - i / 100, rank=i)
        for i in range(n)])


def run(rows, cfg=None, n=None):
    return select_context(result_of(n if n is not None else len(rows)),
                          store_of(rows), FakeCounter(), cfg or Stage7Config())


def test_keeps_order_and_counts_tokens():
    ctx, trace = run([row(0, "alpha beta"), row(1, "gamma delta epsilon")])
    assert [c.chunk_id for c in ctx.chunks] == ["c0", "c1"]
    assert [c.rank for c in ctx.chunks] == [0, 1]
    assert ctx.tokens == 5 and [c.tokens for c in ctx.chunks] == [2, 3]
    assert trace.get("stage7_context").status == "ok"


def test_parent_text_wins_when_present():
    """S2 embeds narrow and returns wide — the window gets the parent."""
    ctx, _ = run([row(0, "narrow", parent="the wide parent span")])
    assert ctx.chunks[0].text == "the wide parent span"


def test_overlap_metadata_dedupes():
    ctx, _ = run([row(0, "one two three"), row(1, "four five", overlap=["c0"])])
    assert [c.chunk_id for c in ctx.chunks] == ["c0"]
    assert ctx.dropped_overlap == 1 and ctx.dropped == 1


def test_same_id_in_another_language_is_not_a_duplicate():
    """chunk_id is unique per corpus, not globally — an id-only dedupe would
    silently drop the cross-lingual hit this system claims. Same id, translated
    text, which is exactly what the four parallel corpora hold."""
    hin, ben = row(0, "गोवा में मौसम"), row(1, "গোয়ায় আবহাওয়া", lang="ben_Beng")
    ben["chunk_id"] = hin["chunk_id"]
    ctx, _ = run([hin, ben])
    assert [c.lang for c in ctx.chunks] == ["hin_Deva", "ben_Beng"]


def test_jaccard_catches_near_duplicates_with_no_shared_id():
    a = "the quick brown fox jumps over the lazy dog"
    ctx, _ = run([row(0, a), row(1, a + " today")])
    assert len(ctx.chunks) == 1 and ctx.dropped_jaccard == 1


def test_dedupe_off_keeps_both_arms():
    a = "the quick brown fox jumps over the lazy dog"
    ctx, _ = run([row(0, a), row(1, a, overlap=["c0"])],
                 Stage7Config(dedupe=False))
    assert len(ctx.chunks) == 2 and ctx.dropped == 0


def test_budget_skips_the_long_chunk_and_keeps_going():
    """A long chunk at rank 1 must not evict the short ones behind it."""
    rows = [row(0, "a b"), row(1, " ".join("x" * 20)), row(2, "c d")]
    ctx, _ = run(rows, Stage7Config(token_budget=5))
    assert [c.chunk_id for c in ctx.chunks] == ["c0", "c2"]
    assert ctx.dropped_budget == 1 and ctx.dropped_capped == 0 and ctx.tokens == 4


def test_max_chunks_caps_the_window():
    ctx, _ = run([row(i, f"w{i}") for i in range(10)], Stage7Config(max_chunks=3))
    assert len(ctx.chunks) == 3 and ctx.dropped_capped == 7 and ctx.dropped_budget == 0


def test_empty_result_is_a_degraded_row_not_an_exception():
    ctx, trace = run([], n=0)
    st = trace.get("stage7_context")
    assert ctx.empty and st.degraded and st.ok
    assert "refusal" in st.detail["reason"]


def test_disabled_stage_still_emits_its_row():
    ctx, trace = run([row(0, "a b")], Stage7Config(enabled=False))
    st = trace.get("stage7_context")
    assert st.status == "off" and not ctx.chunks and st.duration_ms == 0.0


def test_shingle_and_jaccard_edges():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"ab", "bc"}, {"ab", "bc"}) == 1.0
    assert shingles("ab", 4) == {"ab"}          # shorter than the shingle
    assert shingles("a  b", 2) == {"a ", " b"}  # whitespace collapsed first


@pytest.mark.parametrize("text", ["गोवा में मौसम", "hello world"])
def test_real_tokenizer_counts_both_scripts(text):
    """The proxy tokenizer must at least be loadable and non-degenerate — a
    counter that returns 0 turns the token budget off without saying so."""
    pytest.importorskip("tokenizers")
    try:
        c = TokenCounter()
    except Exception as exc:  # model not downloaded on this box
        pytest.skip(f"tokenizer unavailable: {exc}")
    assert c.count(text) > 0 and "proxy" in c.name


# -- the store's format is the footprint (ADR-033) --------------------------

def test_arrow_store_round_trips_every_column(tmp_path):
    """The serving copy must be the same table, or every citation is wrong."""
    import pyarrow.parquet as pq

    from dhvani.build.arrow_store import ARROW_NAME, write_arrow_store

    rows = [{"chunk_id": f"c{i}", "text": f"text {i}", "parent_text": None,
             "doc_id": str(i), "lang": "hin_Deva", "strategy": "s1_passage",
             "overlap_with": []} for i in range(5)]
    rows[2]["parent_text"] = "the wider window"
    table = store_of(rows).t
    pq.write_table(table, tmp_path / "chunks.parquet", compression="zstd")
    report = write_arrow_store(tmp_path / "chunks.parquet", tmp_path / ARROW_NAME)

    assert report["rows"] == 5
    store = ChunkStore.load(tmp_path)
    assert [store.get(i) for i in range(5)] == [store_of(rows).get(i) for i in range(5)]
    # S2 returns the wider unit: the parent replaces the text it was scored on.
    assert store.get(2)["text"] == "the wider window"


def test_the_store_still_loads_from_parquet_alone(tmp_path):
    """An index built before `chunks.arrow` existed must still serve."""
    import pyarrow.parquet as pq

    rows = [{"chunk_id": "c0", "text": "only parquet here", "parent_text": None,
             "doc_id": "0", "lang": "eng_Latn", "strategy": "s1_passage",
             "overlap_with": []}]
    pq.write_table(store_of(rows).t, tmp_path / "chunks.parquet")
    assert ChunkStore.load(tmp_path).get(0)["text"] == "only parquet here"
