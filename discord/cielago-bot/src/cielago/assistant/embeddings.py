"""Local text embeddings for dedup and (later) KB search.

Two backends, one interface:

  * OnnxEmbedder — bge-small-en-v1.5 exported to ONNX, run on CPU via
    onnxruntime. Real semantic similarity. Activated when a model directory
    (tokenizer.json + model.onnx) is present and the optional `embeddings`
    extra is installed.
  * HashEmbedder — a dependency-free char-trigram hashing vector. Deterministic,
    instant, no model files. The safety net so the bot never crash-loops if the
    model isn't staged, and what the unit tests exercise.

Degrade, never fail (plan §1.5): load_embedder() falls back to HashEmbedder on
any import/load error. Everything downstream only sees `.embed()`, `.dim`,
`.backend`, and `.dup_threshold`, so a switch is invisible above this module.

Embeddings are L2-normalized, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import hashlib
import math
import struct
from array import array
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()


class Embedder(Protocol):
    backend: str
    dim: int
    dup_threshold: float

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --- vector helpers (pure, backend-agnostic) ---


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product. Assumes both vectors are already L2-normalized (they are, as
    produced by either backend). Returns 0.0 on a dim mismatch rather than raising."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def pack(vec: list[float]) -> bytes:
    """Float32 blob for sqlite storage."""
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


# --- hashing fallback (no deps) ---


class HashEmbedder:
    """Char-trigram + word hashing into a fixed-dim L2-normalized vector.

    Not semantic, but a solid lexical signal: near-duplicate phrasings land close
    together, which is enough for ticket dedup at community volume and for keeping
    CI free of a 130 MB model download.
    """

    backend = "hash"
    dup_threshold = 0.55

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        low = "".join(c if c.isalnum() else " " for c in text.lower())
        words = low.split()
        feats: list[str] = [f"w:{w}" for w in words]
        padded = f" {low} "
        feats += [f"t:{padded[i : i + 3]}" for i in range(len(padded) - 2)]
        return feats

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in self._features(text):
            h = int.from_bytes(hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0  # signed hashing limits collision bias
            vec[idx] += sign
        return _l2_normalize(vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


# --- ONNX bge-small backend ---


class OnnxEmbedder:
    """bge-small-en-v1.5 ONNX, CLS-pooled + L2-normalized. CPU only."""

    backend = "bge-small-onnx"
    dup_threshold = 0.84
    _MAX_TOKENS = 256

    def __init__(self, model_dir: str) -> None:
        import numpy as np  # noqa: F401  (validated present here so load fails early)
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = Path(model_dir)
        tok_path = d / "tokenizer.json"
        onnx_path = d / "model.onnx"
        if not tok_path.exists() or not onnx_path.exists():
            raise FileNotFoundError(f"missing tokenizer.json/model.onnx in {model_dir}")

        self._tok = Tokenizer.from_file(str(tok_path))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # be a polite tenant on the shared mail host
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(onnx_path), sess_options=opts,
                                          providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        # bge-small hidden size; corrected from the real output on first embed.
        self.dim = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        if not texts:
            return []
        encs = [self._tok.encode(t) for t in texts]
        maxlen = min(self._MAX_TOKENS, max((len(e.ids) for e in encs), default=1))

        def row(seq: list[int], pad: int) -> list[int]:
            seq = seq[:maxlen]
            return seq + [pad] * (maxlen - len(seq))

        ids = np.array([row(e.ids, 0) for e in encs], dtype=np.int64)
        mask = np.array([row(e.attention_mask, 0) for e in encs], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)

        out = self._sess.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        arr = np.asarray(out)
        pooled = arr[:, 0] if arr.ndim == 3 else arr  # CLS token, or already pooled
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = pooled / norms
        self.dim = int(normed.shape[1])
        return normed.astype("float32").tolist()


def load_embedder(model_dir: str | None, prefer_onnx: bool = True) -> Embedder:
    """Return the best available embedder. Falls back to HashEmbedder on any
    problem so the assistant always has working dedup."""
    if prefer_onnx and model_dir:
        try:
            emb = OnnxEmbedder(model_dir)
            log.info("assistant.embedder", backend=emb.backend, dim=emb.dim, dir=model_dir)
            return emb
        except Exception:
            log.warning("assistant.embedder_onnx_unavailable", dir=model_dir, exc_info=True)
    emb = HashEmbedder()
    log.info("assistant.embedder", backend=emb.backend, dim=emb.dim)
    return emb
