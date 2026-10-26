"""Embedding providers: Ollama/OpenAI-compatible (nomic-embed-text) and a deterministic fake.

nomic-embed-text expects task prefixes ("search_document: " for indexed text,
"search_query: " for queries); `OpenAIEmbedder` adds them. `HashingEmbedder` is a
deterministic, dependency-free stand-in for tests and CI: feature-hashed bag of stemmed
tokens and character trigrams, L2-normalised. It is not a semantic model, so it is never
used for reported retrieval numbers.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from data_agent.llm.client import LLMError, OpenAICompatibleClient
from data_agent.retrieval.text import tokenize


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _normalise(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class HashingEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        grams = [t[i : i + 3] for t in tokens for i in range(max(1, len(t) - 2))]
        for feature in tokens + grams:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return vec

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalise(np.stack([self._vec(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(self._vec(text)[None, :])[0]


class OpenAIEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str = "nomic-embed-text",
        api_key: str = "EMPTY",
        batch_size: int = 32,
        timeout_s: float = 60.0,
        doc_prefix: str = "search_document: ",
        query_prefix: str = "search_query: ",
    ):
        self.client = OpenAICompatibleClient(
            base_url, model=model, api_key=api_key, timeout_s=timeout_s, max_retries=2
        )
        self.name = model
        self.batch_size = batch_size
        self.doc_prefix = doc_prefix if "nomic" in model else ""
        self.query_prefix = query_prefix if "nomic" in model else ""
        self.dim = 0

    def _embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self.client.embed(texts[i : i + self.batch_size]))
        mat = np.asarray(out, dtype=np.float32)
        self.dim = mat.shape[1] if mat.size else self.dim
        return _normalise(mat) if mat.size else mat

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self.query_prefix + text])[0]

    def available(self) -> bool:
        try:
            self.client.embed(["ping"])
            return True
        except LLMError:
            return False
