"""Docker-hosted vector store HTTP client.

When ``database.backend = "docker"`` in config, the pipeline uses this client
to communicate with a pre-built vector store service instead of maintaining a
local FAISS index.  The Docker image is pulled from a registry; only HTTP
client code lives here.

REST API the Docker image must expose (default port 8001):

    GET  /health         → {"status": "ok", "size": N}
    POST /add_documents  → body: {"documents": [...], "embeddings": [[float]]}
                           → {"added": N}
    POST /search         → body: {"query_vector": [float], "k": int, "threshold": float}
                           → {"results": [{score, doc_id, page_content, metadata, rank}]}
    GET  /size           → {"size": N}

Documents are serialised as {"page_content": str, "metadata": dict}.
"""

from __future__ import annotations

import logging
from typing import List

import numpy as np

from src.database.base_store import BaseVectorStore, SearchResult
from src.loader.base_loader import Document

logger = logging.getLogger(__name__)


class DockerVectorStore(BaseVectorStore):
    """HTTP client for a Docker-hosted vector store service."""

    def __init__(self, url: str, timeout: int = 30) -> None:
        self._base_url = url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Ping the Docker service health endpoint.

        The Docker image is pre-loaded with embeddings, so ``load()`` is a
        health-check rather than a restore operation.

        Raises:
            ConnectionError: if the service is unreachable or returns non-200.
        """
        import requests  # type: ignore

        url = f"{self._base_url}/health"
        try:
            resp = requests.get(url, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerVectorStore: cannot reach {url} — is the Docker service running? ({exc})"
            ) from exc

        if resp.status_code != 200:
            raise ConnectionError(
                f"DockerVectorStore: {url} returned HTTP {resp.status_code}"
            )

        data = resp.json()
        logger.info(
            "DockerVectorStore connected: %s — size=%s",
            self._base_url,
            data.get("size", "?"),
        )

    def save(self) -> None:
        """No-op: Docker image manages its own persistence."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_documents(self, documents: List[Document], embeddings: np.ndarray) -> List[str]:
        """Send documents + embeddings to the Docker vector store service."""
        import requests  # type: ignore

        url = f"{self._base_url}/add_documents"
        payload = {
            "documents": [
                {"page_content": doc.page_content, "metadata": doc.metadata}
                for doc in documents
            ],
            "embeddings": embeddings.tolist(),
        }

        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerVectorStore: cannot reach {url} ({exc})"
            ) from exc

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"DockerVectorStore: {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        added = data.get("added", 0)
        logger.info("DockerVectorStore.add_documents: %d added", added)
        return [str(i) for i in range(added)]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        """Query the Docker service for the k most similar documents."""
        import requests  # type: ignore

        url = f"{self._base_url}/search"
        payload = {
            "query_vector": query_vector.tolist(),
            "k": k,
            "threshold": threshold,
        }

        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerVectorStore: cannot reach {url} ({exc})"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"DockerVectorStore: {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        results = []
        for item in resp.json().get("results", []):
            results.append(SearchResult(
                score=float(item.get("score", 0.0)),
                doc_id=str(item.get("doc_id", "")),
                page_content=item.get("page_content", ""),
                metadata=item.get("metadata", {}),
                rank=int(item.get("rank", 0)),
            ))
        return results

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        import requests  # type: ignore

        url = f"{self._base_url}/size"
        try:
            resp = requests.get(url, timeout=self._timeout)
            if resp.status_code == 200:
                return int(resp.json().get("size", 0))
        except Exception:
            pass
        return 0
