"""Length-sorted batching must not reorder the output.

`Embedder.encode` sorts its input by length so a batch is not padded to one long
outlier — measured 2.30 -> 1.69 GB peak and 45.7 -> 140.9 chunks/s per worker
(`docs/results/2026-08-15-embed-shard-memory.json`). The optimization is only
safe if the permutation is inverted before returning: the build joins these rows
positionally against the chunk table, so a surviving permutation is an index that
builds cleanly, passes every shape assertion, and retrieves the wrong passage.
"""
import numpy as np

from dhvani.embed import DEFAULT_MODEL, MODELS, Embedder

PREFIX = MODELS[DEFAULT_MODEL].passage_prefix
BATCH = 4


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _varied(n: int) -> list[str]:
    """Lengths that actually permute under argsort, longest first so an
    unsorted-equals-sorted bug cannot pass by accident."""
    return [f"शहर {i} " + "इतिहास " * ((n - i) * 3 + 1) for i in range(n)]


def test_sorted_rows_match_unsorted_rows_one_for_one():
    texts = _varied(13)
    emb = Embedder(threads=1)

    sorted_out = emb.encode(texts, BATCH, PREFIX, sort=True)
    reference = emb.encode(texts, BATCH, PREFIX, sort=False)

    assert sorted_out.shape == (len(texts), MODELS[DEFAULT_MODEL].dims)
    # Not equality: INT8 output shifts ~1e-2 with the batch's padding, which is
    # the whole reason sorting is cheaper. Identity is asserted the way retrieval
    # uses these vectors — every row's nearest neighbour is its own reference row.
    sim = _unit(sorted_out) @ _unit(reference).T
    np.testing.assert_array_equal(sim.argmax(axis=1), np.arange(len(texts)))


def test_permutation_is_exactly_inverted_when_lengths_are_equal():
    """Equal lengths make the stable argsort the identity, so the round trip is
    exact — this pins the inverse-permutation arithmetic itself, with no INT8
    padding noise in the way."""
    texts = [f"शहर संख्या {i:03d} इतिहास" for i in range(9)]
    assert len({len(t) for t in texts}) == 1
    emb = Embedder(threads=1)

    np.testing.assert_array_equal(emb.encode(texts, BATCH, PREFIX, sort=True),
                                  emb.encode(texts, BATCH, PREFIX, sort=False))


def test_single_batch_and_empty_input_are_unchanged():
    emb = Embedder(threads=1)
    assert emb.encode([], BATCH, PREFIX).shape == (0, MODELS[DEFAULT_MODEL].dims)
    # At or below one batch there is nothing to gain and nothing to permute.
    short = _varied(BATCH)
    np.testing.assert_array_equal(emb.encode(short, BATCH, PREFIX, sort=True),
                                  emb.encode(short, BATCH, PREFIX, sort=False))
