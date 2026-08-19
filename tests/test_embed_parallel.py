"""embed_parallel's in-place fill.

The build was OOM-killed twice on this box (change log, 15 Aug). The second kill
took the *parent*, which was holding the shard list and its vstack copy at the
same time, so the assembly now writes into a preallocated array. That swap is
where a silent misalignment would live: a wrong offset produces an array of the
right shape full of the wrong rows, and nothing downstream notices — FAISS
happily indexes garbage. These checks pin row identity, not just shape.
"""
import numpy as np
import pytest

from dhvani.build.build_index import embed_parallel
from dhvani.embed import DEFAULT_MODEL, MODELS

WORKERS, THREADS = 2, 1


def test_empty_input_returns_empty_matrix():
    out = embed_parallel([], "passage: ", DEFAULT_MODEL, WORKERS, THREADS)
    assert out.shape == (0, MODELS[DEFAULT_MODEL].dims)


@pytest.mark.parametrize("n_texts, shard", [(7, 3), (9, 3), (1, 4096)])
def test_rows_land_in_input_order(n_texts, shard):
    """Multi-shard assembly keeps row i as the embedding of text i.

    Compared against a single-shard run, which needs no offset arithmetic at
    all. Not by equality: INT8 output shifts by ~1e-2 with the batch's padding,
    so the same text in a 3-row shard and a 9-row shard is close but not equal.
    Identity is asserted the way retrieval will use these vectors — every row's
    nearest neighbour in the reference run is itself.
    """
    texts = [f"शहर संख्या {i} का इतिहास" for i in range(n_texts)]
    prefix = MODELS[DEFAULT_MODEL].passage_prefix

    sharded = embed_parallel(texts, prefix, DEFAULT_MODEL, WORKERS, THREADS,
                             shard=shard)
    single = embed_parallel(texts, prefix, DEFAULT_MODEL, 1, THREADS,
                            shard=len(texts))

    assert sharded.shape == (n_texts, MODELS[DEFAULT_MODEL].dims)
    assert sharded.dtype == np.float32

    sim = _unit(sharded) @ _unit(single).T
    np.testing.assert_array_equal(sim.argmax(axis=1), np.arange(n_texts))


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=1, keepdims=True)
