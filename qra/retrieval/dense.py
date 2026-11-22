"""Dense retrieval: an IVF index (inverted file) over int8 vectors, sharded.

Exact vector search compares the query with every vector: O(N). An IVF index first partitions the space into `nlist`
cells with a coarse quantiser (k-means centroids). A query is compared with the centroids (cheap) and then only with
the vectors in the `nprobe` nearest cells, so the work is roughly nprobe/nlist of the data and the choice of
nlist near sqrt(N) makes a query O(sqrt(N)) instead of O(N). Vectors are stored as int8 (4x smaller than float32), and
the full-precision query is compared with them directly, so the quantisation error is small.

The embedder is a protocol (`embed(texts) -> float32 matrix, rows L2-normalised`). `HashingEmbedder` below is a
deterministic, dependency-free stand-in (hashed word and character-trigram features) used for tests, the benchmark and
the help-centre demo: it needs no API and no model. A production deployment plugs in a learned embedding model here;
nothing else in the package changes.
"""
from __future__ import annotations

import hashlib
import heapq
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .text import tokens
from .types import Hit, shard_of


class HashingEmbedder:
    """Signed feature hashing of words and character trigrams, L2-normalised. Deterministic and stateless."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    @staticmethod
    def _h(feature: str) -> int:
        return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "little")

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in tokens(text):
                feats = [(word, 1.0)] + [(f"#{word[i:i + 3]}", 0.5) for i in range(max(0, len(word) - 2))]
                for feat, weight in feats:
                    h = self._h(feat)
                    out[row, h % self.dim] += weight if (h >> 40) & 1 else -weight
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


class IVFIndex:
    def __init__(self, dim: int, nlist: int = 64, seed: int = 0):
        self.dim, self.nlist, self._seed = dim, nlist, seed
        self.centroids: np.ndarray | None = None
        self._chunks: list[list[tuple[np.ndarray, np.ndarray]]] = [[] for _ in range(nlist)]
        self._ids: list[np.ndarray | None] = [None] * nlist
        self._vecs: list[np.ndarray | None] = [None] * nlist
        self._dirty = False
        self._lock = threading.Lock()
        self.size = 0

    # ---- training and writing -------------------------------------------------------------------------------
    def train(self, sample: np.ndarray, iterations: int = 8) -> None:
        """Spherical k-means on a sample (vectors are unit length, so similarity is a dot product)."""
        rng = np.random.default_rng(self._seed)
        n = len(sample)
        c = sample[rng.choice(n, size=min(self.nlist, n), replace=False)].copy()
        if len(c) < self.nlist:  # fewer sample points than cells: pad with random unit vectors
            pad = rng.standard_normal((self.nlist - len(c), self.dim)).astype(np.float32)
            c = np.vstack([c, pad / np.linalg.norm(pad, axis=1, keepdims=True)])
        for _ in range(iterations):
            assign = np.argmax(sample @ c.T, axis=1)
            for j in range(self.nlist):
                members = sample[assign == j]
                if len(members):
                    m = members.sum(axis=0)
                    c[j] = m / (np.linalg.norm(m) or 1.0)
        self.centroids = c.astype(np.float32)

    def add(self, ids: np.ndarray, vecs: np.ndarray) -> None:
        if self.centroids is None:
            raise RuntimeError("train the index before adding vectors")
        cells = np.argmax(vecs @ self.centroids.T, axis=1)
        quant = np.clip(np.rint(vecs * 127.0), -127, 127).astype(np.int8)
        order = np.argsort(cells, kind="stable")
        cells, ids, quant = cells[order], ids[order], quant[order]
        bounds = np.searchsorted(cells, np.arange(self.nlist + 1))
        with self._lock:
            for j in range(self.nlist):
                lo, hi = bounds[j], bounds[j + 1]
                if hi > lo:
                    self._chunks[j].append((ids[lo:hi], quant[lo:hi]))
            self.size += len(ids)
            self._dirty = True

    def seal(self) -> None:
        """Merge the appended chunks of every cell into contiguous arrays (called lazily before searching)."""
        with self._lock:
            if not self._dirty:
                return
            for j, chunks in enumerate(self._chunks):
                if chunks:
                    parts = ([(self._ids[j], self._vecs[j])] if self._ids[j] is not None else []) + chunks
                    self._ids[j] = np.concatenate([p[0] for p in parts])
                    self._vecs[j] = np.concatenate([p[1] for p in parts])
                    self._chunks[j] = []
            self._dirty = False

    # ---- searching ------------------------------------------------------------------------------------------
    def search(self, query: np.ndarray, k: int, nprobe: int = 8) -> list[Hit]:
        if self.centroids is None or not self.size:
            return []
        self.seal()
        nprobe = min(nprobe, self.nlist)
        cells = np.argpartition(-(self.centroids @ query), nprobe - 1)[:nprobe]
        ids = [self._ids[j] for j in cells if self._ids[j] is not None]
        if not ids:
            return []
        all_ids = np.concatenate(ids)
        scores = np.concatenate([self._vecs[j] for j in cells if self._ids[j] is not None]).astype(np.float32) @ query / 127.0
        top = np.argpartition(-scores, min(k, len(scores)) - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [Hit(int(all_ids[i]), float(scores[i]), "dense") for i in top]

    def exact(self, query: np.ndarray, k: int) -> list[Hit]:
        """Brute-force search over everything: the ground truth that recall is measured against."""
        self.seal()
        ids = np.concatenate([i for i in self._ids if i is not None])
        scores = np.concatenate([v for v in self._vecs if v is not None]).astype(np.float32) @ query / 127.0
        top = np.argpartition(-scores, min(k, len(scores)) - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [Hit(int(ids[i]), float(scores[i]), "dense") for i in top]


class ShardedDense:
    """One IVF index per shard; a query visits every shard and the per-shard top-k lists are merged."""

    def __init__(self, shards: int, dim: int, nlist: int, seed: int = 0):
        self.shards, self.dim = shards, dim
        self._indexes = [IVFIndex(dim, nlist, seed + i) for i in range(shards)]
        self._pool = ThreadPoolExecutor(max_workers=shards, thread_name_prefix="dense")

    def train(self, sample: np.ndarray) -> None:
        for idx in self._indexes:
            idx.train(sample)

    def add(self, ids: np.ndarray, vecs: np.ndarray) -> None:
        owner = np.array([shard_of(int(i), self.shards) for i in ids])
        for s in range(self.shards):
            mask = owner == s
            if mask.any():
                self._indexes[s].add(ids[mask], vecs[mask])

    @property
    def size(self) -> int:
        return sum(i.size for i in self._indexes)

    def search(self, query: np.ndarray, k: int, nprobe: int = 8) -> list[Hit]:
        parts = self._pool.map(lambda i: i.search(query, k, nprobe), self._indexes)
        return heapq.nlargest(k, (h for part in parts for h in part), key=lambda h: h.score)

    def exact(self, query: np.ndarray, k: int) -> list[Hit]:
        parts = self._pool.map(lambda i: i.exact(query, k), self._indexes)
        return heapq.nlargest(k, (h for part in parts for h in part), key=lambda h: h.score)
