"""Embedders return L2-normalised float32 vectors, so cosine similarity is a dot product.

FastEmbedEmbedder runs bge-small locally on CPU, so memory and cache text never leaves the
container. HashingEmbedder is a dependency-free fallback (and the test default). It only
matches near-identical wording, so it behaves like a fuzzy exact match, not real semantics.
"""

import asyncio
import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class Embedder(Protocol):
    model_name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class HashingEmbedder:
    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_name = f"hashing-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        words = re.findall(r"[a-z0-9]+", text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        v = np.zeros(self.dim, dtype=np.float32)
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        return _normalise(np.stack([self._vec(t) for t in texts]))


class FastEmbedEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding  # optional dependency

        self.model_name = model_name
        self._model = TextEmbedding(model_name)
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        vecs = await asyncio.to_thread(lambda: list(self._model.embed(list(texts))))
        return _normalise(np.stack(vecs))


def default_embedder(model_name: str) -> Embedder:
    try:
        return FastEmbedEmbedder(model_name)
    except ImportError:
        log.warning("fastembed not installed; using HashingEmbedder (wording-level similarity only)")
        return HashingEmbedder()


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
