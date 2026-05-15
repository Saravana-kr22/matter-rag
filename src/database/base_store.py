"""BaseVectorStore — abstract interface for all vector store backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

import numpy as np


@dataclass
class SearchResult:
    """A single semantic search result."""
    score: float
    doc_id: str
    page_content: str
    metadata: dict
    rank: int = 0


class BaseVectorStore(ABC):
    """Abstract base class for vector store backends.

    All backends must support:
      - add_documents(docs, embeddings) → list of doc_ids
      - search_by_vector(vec, k, threshold) → List[SearchResult]
      - size property
      - is_empty property

    save() / load() are optional (no-op for DB-backed stores).
    """

    @abstractmethod
    def add_documents(self, documents, embeddings: np.ndarray) -> List[str]:
        """Embed and store documents. Returns list of assigned doc_ids."""

    @abstractmethod
    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        """Return top-k documents most similar to query_vector.

        query_vector should be a 1-D float32 array.
        Results are sorted by descending score; entries below threshold are excluded.
        """

    def save(self) -> None:
        """Persist state to disk (no-op for DB-backed stores)."""

    def load(self) -> None:
        """Restore state from disk (no-op for DB-backed stores)."""

    @property
    @abstractmethod
    def size(self) -> int:
        """Total number of stored documents."""

    @property
    def is_empty(self) -> bool:
        return self.size == 0
