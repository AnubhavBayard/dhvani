"""The part checkpoint, and the merge that reassembles it.

The build is split across processes so no single process has to hold the whole
thing (change log 15 Aug: OOM-killed twice, both times on accumulated state).
That split is only safe if it is invisible in the artifacts, and the thing that
would break silently is row order: chunks row i is FAISS id i is BM25 doc i
(`retrieve/stage3.py`), so a merge that orders parts by whichever finished first
builds a clean index that retrieves the wrong passage.

These checks use synthetic chunks rather than the dataset — the property under
test is the checkpoint and the merge, not chunking.
"""

from __future__ import annotations

import numpy as np
import pyarrow.parquet as pq
import pytest

from dhvani.build.build_index import (merge_parts, part_complete, part_paths,
                                      write_part)
from dhvani.embed import DEFAULT_MODEL, MODELS
from dhvani.harness.contracts import Chunk

WORKERS, THREADS = 2, 1


def _chunks(prefix: str, n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"{prefix}-{i}", text=f"{prefix} passage number {i}",
                  doc_id=str(i), passage_idx=0, strategy="s1_passage", ordinal=0,
                  lang="hin_Deva", script="Devanagari", is_selected=False,
                  query_type="description", split="validation",
                  token_count=5, char_span=(0, 10), parent_text=None,
                  overlap_with=[])
            for i in range(n)]


def _write(out, lang, chunks, state):
    write_part(out, lang, chunks, DEFAULT_MODEL, WORKERS, THREADS, 8, False)
    state[lang] = {"chunks": len(chunks)}
    return state


@pytest.fixture(scope="module")
def parts(tmp_path_factory):
    out = tmp_path_factory.mktemp("parts")
    state: dict = {}
    _write(out, "hin", _chunks("hin", 40), state)
    _write(out, "ben", _chunks("ben", 25), state)
    return out, state


def test_part_is_published_atomically(parts):
    """A part exists only once both its files are complete — an interrupted run
    must not leave something the next run counts as done."""
    out, state = parts
    for lang in ("hin", "ben"):
        vecs, table = part_paths(out, lang)
        assert vecs.exists() and table.exists()
        assert part_complete(out, lang, state)
        assert not list(out.glob("parts/*.tmp.*")), "temp files survived"
    assert not part_complete(out, "tam", state)


def test_part_vectors_match_their_chunks(parts):
    """The vectors are on disk in the same order as the parquet rows. Checked the
    way retrieval uses them — every row's nearest neighbour is itself — because
    INT8 output shifts ~1e-2 with a batch's padding, so equality is the wrong
    test (see `tests/test_embed_sort.py`)."""
    out, _ = parts
    vecs, table = part_paths(out, "hin")
    v = np.load(vecs, mmap_mode="r")
    texts = pq.read_table(table, columns=["text"]).column("text").to_pylist()
    assert v.shape == (len(texts), MODELS[DEFAULT_MODEL].dims)
    assert np.argmax(np.asarray(v) @ np.asarray(v).T, axis=1).tolist() == \
        list(range(len(texts)))


def test_merge_order_is_the_lang_order_not_the_build_order(parts):
    """Building hin then ben must produce the same index as ben then hin: the
    merge follows `lang_order`, and row order is the join key."""
    out, state = parts
    a = merge_parts(out, ["hin", "ben"], state, m=8, ef_construction=32,
                    min_term_freq=1, train_sample=64, block=16)
    order_a = pq.read_table(out / "chunks.parquet",
                            columns=["chunk_id"]).column("chunk_id").to_pylist()

    b = merge_parts(out, ["ben", "hin"], state, m=8, ef_construction=32,
                    min_term_freq=1, train_sample=64, block=16)
    order_b = pq.read_table(out / "chunks.parquet",
                            columns=["chunk_id"]).column("chunk_id").to_pylist()

    assert a["merged_langs"] == ["hin", "ben"]
    assert b["merged_langs"] == ["ben", "hin"]
    assert order_a[:40] == [f"hin-{i}" for i in range(40)]
    assert order_b[:25] == [f"ben-{i}" for i in range(25)]
    assert a["faiss"]["vectors"] == b["faiss"]["vectors"] == 65


def test_merge_joins_faiss_to_the_chunk_store(parts):
    """The assert that catches a dropped or duplicated part: FAISS row count and
    chunk store row count are the same number or the index is unusable."""
    out, state = parts
    r = merge_parts(out, ["hin", "ben"], state, m=8, ef_construction=32,
                    min_term_freq=1, train_sample=64, block=16)
    n = pq.read_table(out / "chunks.parquet").num_rows
    assert r["faiss"]["vectors"] == n == 65
    assert r["bm25"]["documents"] == n


def test_merge_skips_a_lang_with_no_part(parts):
    """A lang in the order but not on disk is skipped, not merged as zero rows —
    this is what makes `--no-merge` runs resumable."""
    out, state = parts
    r = merge_parts(out, ["hin", "tam", "ben"], state, m=8, ef_construction=32,
                    min_term_freq=1, train_sample=64, block=16)
    assert r["merged_langs"] == ["hin", "ben"]
    assert r["faiss"]["vectors"] == 65


def test_built_index_has_no_duplicate_chunk_ids():
    """The invariant the 18 Aug build broke: an id must address exactly one row.

    Stage 3 returns `chunk_id`s, and stage 7's dedupe and the UI's citations key
    on them, so a duplicated id is a citation pointing at four passages in four
    languages (ADR-020). Runs against the real index when one is built.
    """
    from pathlib import Path

    index = Path(__file__).resolve().parents[1] / "index" / "full" / "chunks.parquet"
    if not index.exists():
        pytest.skip("full index not built")
    ids = pq.read_table(index, columns=["chunk_id"]).column("chunk_id").to_pylist()
    assert len(ids) == len(set(ids)), f"{len(ids) - len(set(ids)):,} duplicate chunk_ids"
