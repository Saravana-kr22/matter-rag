"""Search module — semantic search over any BaseVectorStore backend."""

from __future__ import annotations

import logging
from typing import List

import numpy as np

# SearchResult is defined in base_store; re-exported here for backward compat.
from src.database.base_store import BaseVectorStore, SearchResult  # noqa: F401
from src.database.vector_store import VectorStore  # backward-compat alias
from src.embeddings.embeddings import EmbeddingsModule

logger = logging.getLogger(__name__)


class FAISSSearch:
    """Perform semantic search over any BaseVectorStore.

    Despite its name the class is now backend-agnostic; it delegates all
    vector similarity work to ``store.search_by_vector()``.

    Usage::

        searcher = FAISSSearch(store, embeddings_module)
        results  = searcher.search("OnOff cluster test", k=5, threshold=0.65)
    """

    def __init__(self, store: BaseVectorStore, embeddings: EmbeddingsModule) -> None:
        self.store = store
        self.embeddings = embeddings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        """Search for top-k documents most similar to a text query.

        Args:
            query: Natural language query string.
            k: Maximum number of results to return.
            threshold: Minimum cosine similarity score (0–1).

        Returns:
            List of SearchResult sorted by descending score.
        """
        if self.store.is_empty:
            logger.warning("Vector store is empty. Cannot search.")
            return []

        query_vec = self.embeddings.embed_query(query)
        return self.store.search_by_vector(query_vec, k=k, threshold=threshold)

    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        """Search using a precomputed embedding vector.

        Args:
            query_vector: 1-D np.ndarray of shape (embedding_dim,).
            k: Maximum number of results.
            threshold: Minimum similarity score.

        Returns:
            List of SearchResult sorted by descending score.
        """
        return self.store.search_by_vector(query_vector, k=k, threshold=threshold)

    def batch_search(
        self,
        queries: List[str],
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[List[SearchResult]]:
        """Search multiple queries in one batch.

        Args:
            queries: List of query strings.
            k: Top-k per query.
            threshold: Minimum score filter.

        Returns:
            List of result lists, one per query.
        """
        if not queries:
            return []

        query_vecs = self.embeddings.embed_texts(queries, is_query=True)
        return [
            self.store.search_by_vector(vec, k=k, threshold=threshold)
            for vec in query_vecs
        ]
