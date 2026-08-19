"""ONNX sentence embedder — the one place a model is loaded and pooled.

Shared deliberately. `build/` embeds the corpus and `retrieve/` embeds the query,
and they must agree bit for bit: a different prefix, a different pooling, or a
different normalization between build and query silently destroys recall while
every test still passes. One implementation, two callers.

Model choice: ADR-011 and ADR-014. `multilingual-e5-small` INT8 is the default
because it is the only candidate publishing a pre-quantized INT8 ONNX build that
matches this runtime exactly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


@dataclass(frozen=True)
class ModelSpec:
    name: str
    onnx: str
    tokenizer: str
    pooling: str                      # "mean" | "cls"
    dims: int
    query_prefix: str = ""
    passage_prefix: str = ""
    dense: str | None = None          # LaBSE's post-pooling Dense head
    notes: str = ""

    @property
    def size_mb(self) -> float:
        p = Path(self.onnx)
        if not p.exists():
            return 0.0
        extra = sum(f.stat().st_size for f in p.parent.glob("*.onnx_data"))
        return round((p.stat().st_size + extra) / 1e6, 1)


MODELS: dict[str, ModelSpec] = {
    "multilingual-e5-small": ModelSpec(
        name="intfloat/multilingual-e5-small (INT8 ONNX)",
        onnx="models/multilingual-e5-small/onnx/model_qint8_avx512_vnni.onnx",
        tokenizer="models/multilingual-e5-small/tokenizer.json",
        pooling="mean",
        dims=384,
        # e5 is trained with these prefixes. Dropping them measurably hurts it,
        # so omitting them would benchmark a misuse of the model — and mixing
        # them between build and query would be worse.
        query_prefix="query: ",
        passage_prefix="passage: ",
        notes="pre-quantized INT8, matches the deployed runtime exactly",
    ),
    "bge-m3": ModelSpec(
        name="BAAI/bge-m3 (fp32 ONNX)",
        onnx="models/bge-m3/onnx/model.onnx",
        tokenizer="models/bge-m3/tokenizer.json",
        pooling="cls",
        dims=1024,
        notes="dense head only; sparse and ColBERT heads not exercised",
    ),
    "LaBSE": ModelSpec(
        name="sentence-transformers/LaBSE (fp32 ONNX)",
        onnx="models/LaBSE/onnx/model.onnx",
        tokenizer="models/LaBSE/tokenizer.json",
        pooling="cls",
        dims=768,
        dense="models/LaBSE/2_Dense/model.safetensors",
        notes="ONNX export is the transformer only; the sentence-transformers "
              "Dense(768->768)+tanh head is applied separately",
    ),
}

DEFAULT_MODEL = "multilingual-e5-small"


class Embedder:
    """Batched ONNX encoder returning L2-normalized float32 vectors.

    `infer_seconds` accumulates pure session-run time, separately from tokenizer
    and pooling cost, so a slow stage can be attributed rather than guessed at.
    """

    def __init__(self, spec: ModelSpec | str = DEFAULT_MODEL, threads: int = 2,
                 max_len: int = 512, cpu_mem_arena: bool = True):
        self.spec = MODELS[spec] if isinstance(spec, str) else spec
        self.max_len = max_len
        self.threads = threads
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # The arena caches every allocation it has ever made and never returns it
        # to the OS, so a worker's footprint ratchets up to its largest batch and
        # stays there. Harmless for one query-time session; it is what made eight
        # build workers cost 2.33 GB each (`dhvani/bench/embed_memory.py`).
        opts.enable_cpu_mem_arena = cpu_mem_arena
        self.sess = ort.InferenceSession(self.spec.onnx, opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.tok = Tokenizer.from_file(self.spec.tokenizer)
        self.tok.enable_truncation(max_length=max_len)
        self.tok.enable_padding()  # to longest in batch, not to max_len
        self.dense_w, self.dense_b = self._load_dense(self.spec.dense)
        self.infer_seconds = 0.0

    @staticmethod
    def _load_dense(path: str | None):
        if not path:
            return None, None
        from safetensors.numpy import load_file
        w = load_file(path)
        key_w = next(k for k in w if k.endswith("weight"))
        key_b = next((k for k in w if k.endswith("bias")), None)
        return (w[key_w].astype(np.float32),
                w[key_b].astype(np.float32) if key_b else None)

    def _forward(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {k: v for k, v in feed.items() if k in self.inputs}

        t0 = time.perf_counter()
        hidden = self.sess.run(None, feed)[0]
        self.infer_seconds += time.perf_counter() - t0

        if self.spec.pooling == "cls":
            pooled = hidden[:, 0]
        else:
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        if self.dense_w is not None:
            pooled = pooled @ self.dense_w.T
            if self.dense_b is not None:
                pooled = pooled + self.dense_b
            pooled = np.tanh(pooled)
        return pooled.astype(np.float32)

    def encode(self, texts: list[str], batch_size: int = 32,
               prefix: str = "", sort: bool = True) -> np.ndarray:
        """Encode `texts`, returning one row per input **in input order**.

        Batches are padded to their longest member, and the corpus is p50 91
        chars against a 2,000-char cap — so an unsorted batch pads thirty short
        chunks up to one long one. Grouping similar lengths together cost one
        argsort and measured 2.30 -> 1.69 GB peak and 45.7 -> 140.9 chunks/s
        (`docs/results/2026-08-15-embed-shard-memory.json`).

        The permutation is inverted before returning. It has to be: the caller
        joins these rows positionally against the chunk table, and vectors that
        are silently one permutation away from their text are an index that
        builds cleanly and retrieves nonsense.
        """
        if not texts:
            return np.zeros((0, self.spec.dims), dtype=np.float32)
        if prefix:
            texts = [prefix + t for t in texts]

        order = (np.argsort([len(t) for t in texts], kind="stable")
                 if sort and len(texts) > batch_size else np.arange(len(texts)))
        ordered = [texts[i] for i in order]
        out = np.vstack([self._forward(ordered[i:i + batch_size])
                         for i in range(0, len(ordered), batch_size)])
        out /= np.clip(np.linalg.norm(out, axis=1, keepdims=True), 1e-9, None)

        inverse = np.empty_like(order)
        inverse[order] = np.arange(len(order))
        return out[inverse]

    def encode_passages(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        return self.encode(texts, batch_size, self.spec.passage_prefix)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text], 1, self.spec.query_prefix)[0]
