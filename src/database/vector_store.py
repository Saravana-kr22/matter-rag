"""Vector store — factory function + backward-compatible VectorStore alias.

Use ``create_vector_store(config)`` to get a backend-specific store.
``VectorStore`` is kept as an alias for ``FAISSStore`` for backward compatibility.
"""

from __future__ import annotations

from src.database.base_store import BaseVectorStore, SearchResult
from src.database.faiss_store import FAISSStore, StoredEntry

# Backward-compat: existing code that does `VectorStore(config.database)` still works.
VectorStore = FAISSStore

__all__ = [
    "BaseVectorStore",
    "FAISSStore",
    "SearchResult",
    "StoredEntry",
    "VectorStore",
    "create_vector_store",
]


def create_vector_store(config) -> BaseVectorStore:
    """Instantiate the vector store backend specified by ``config.backend``.

    Args:
        config: A ``DatabaseConfig`` instance.

    Returns:
        A ``BaseVectorStore`` implementation:
        - ``"faiss"``    → ``FAISSStore``   (default, file-based)
        - ``"chroma"``   → ``ChromaStore``  (ChromaDB persistent client)
        - ``"postgres"`` → ``PostgresStore`` (PostgreSQL + pgvector)

    Example::

        from src.database.vector_store import create_vector_store
        store = create_vector_store(config.database)
        store.add_documents(chunks, embeddings)
        results = store.search_by_vector(query_vec, k=10, threshold=0.65)
    """
    backend = getattr(config, "backend", "faiss")

    if backend == "chroma":
        from src.database.chroma_store import ChromaStore  # noqa: PLC0415
        return ChromaStore(config)

    if backend == "postgres":
        from src.database.postgres_store import PostgresStore  # noqa: PLC0415
        return PostgresStore(config)

    if backend == "docker":
        from src.database.docker_store import DockerVectorStore  # noqa: PLC0415

        url = getattr(config, "docker_vector_store_url", "http://localhost:8001")
        timeout = getattr(config, "docker_timeout", 30)
        store = DockerVectorStore(url, timeout)
        store.load()   # health-check / connection test
        return store

    # Default: FAISS
    return FAISSStore(config)
