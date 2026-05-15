"""Docker-hosted knowledge graph HTTP client.

When ``knowledge_graph.backend = "docker"`` in config, the pipeline uses this
client to communicate with a pre-built KG service instead of building a local
NetworkX graph.  The Docker image is pulled from a registry; only HTTP client
code lives here — no server code.

REST API the Docker image must expose (default port 8002):

    GET  /health                     → {"status": "ok", "num_nodes": N, "num_edges": N}
    POST /add_test_plan_documents    → body: {"documents": [...]}  → {"added": N}
    POST /add_pr_documents           → body: {"documents": [...]}  → {"added": N}
    POST /extract_matter_entities    → body: {"documents": [...]}  → {"extracted": N}
    POST /search_by_entities         → body: {"text": str, "max_results": int}
                                       → {"results": [{node_id, node_type, label, properties}]}
    GET  /coverage_gaps              → {"gaps": [{node_id, node_type, label, properties}]}
    GET  /stats                      → {"num_nodes": N, "num_edges": N}

Documents are serialised as {"page_content": str, "metadata": dict}.
"""

from __future__ import annotations

import logging
from typing import List

from src.knowledge_graph.base_graph import BaseKnowledgeGraph, GraphNode, NodeType
from src.loader.base_loader import Document

logger = logging.getLogger(__name__)


class DockerKnowledgeGraph(BaseKnowledgeGraph):
    """HTTP client for a Docker-hosted Matter knowledge graph service."""

    def __init__(self, url: str, timeout: int = 30) -> None:
        self._base_url = url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Ping the Docker service health endpoint.

        Raises:
            ConnectionError: if the service is unreachable or returns non-200.
        """
        import requests  # type: ignore

        url = f"{self._base_url}/health"
        try:
            resp = requests.get(url, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerKnowledgeGraph: cannot reach {url} — is the Docker service running? ({exc})"
            ) from exc

        if resp.status_code != 200:
            raise ConnectionError(
                f"DockerKnowledgeGraph: {url} returned HTTP {resp.status_code}"
            )

        data = resp.json()
        logger.info(
            "DockerKnowledgeGraph connected: %s — nodes=%s edges=%s",
            self._base_url,
            data.get("num_nodes", "?"),
            data.get("num_edges", "?"),
        )

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def add_data_model_documents(self, documents: List[Document]) -> None:
        """Send data-model XML documents to the Docker KG service."""
        added = self._post_documents("/add_data_model_documents", documents)
        logger.info("DockerKnowledgeGraph.add_data_model_documents: %d added", added)

    def add_test_plan_documents(self, documents: List[Document]) -> None:
        """Send test plan documents to the Docker KG service."""
        added = self._post_documents("/add_test_plan_documents", documents)
        logger.info("DockerKnowledgeGraph.add_test_plan_documents: %d added", added)

    def add_pr_documents(self, documents: List[Document]) -> None:
        """Send PR change documents to the Docker KG service."""
        added = self._post_documents("/add_pr_documents", documents)
        logger.info("DockerKnowledgeGraph.add_pr_documents: %d added", added)

    def add_spec_documents(self, documents: List[Document]) -> None:
        """Send Matter specification documents to the Docker KG service."""
        added = self._post_documents("/add_spec_documents", documents)
        logger.info("DockerKnowledgeGraph.add_spec_documents: %d added", added)

    def load_from_json(self, path: str) -> None:
        """No-op: Docker service manages its own persistent state."""
        logger.debug("DockerKnowledgeGraph.load_from_json: no-op (Docker service is pre-loaded)")

    def extract_matter_entities(self, documents: List[Document]) -> None:
        """Ask the Docker KG service to extract Matter entities."""
        extracted = self._post_documents("/extract_matter_entities", documents)
        logger.info("DockerKnowledgeGraph.extract_matter_entities: %d extracted", extracted)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def search_by_entities(self, text: str, max_results: int = 10) -> List[GraphNode]:
        """Search the Docker KG by entity extraction."""
        import requests  # type: ignore

        url = f"{self._base_url}/search_by_entities"
        resp = self._checked_post(url, {"text": text, "max_results": max_results})
        return self._deserialize_nodes(resp.json().get("results", []))

    def get_coverage_gaps(self) -> List[GraphNode]:
        """Fetch PR_CHANGE nodes with no linked test cases from the Docker service."""
        import requests  # type: ignore

        url = f"{self._base_url}/coverage_gaps"
        try:
            resp = requests.get(url, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerKnowledgeGraph: cannot reach {url} ({exc})"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"DockerKnowledgeGraph: {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        return self._deserialize_nodes(resp.json().get("gaps", []))

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def num_nodes(self) -> int:
        return self._get_stats().get("num_nodes", 0)

    @property
    def num_edges(self) -> int:
        return self._get_stats().get("num_edges", 0)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _post_documents(self, path: str, documents: List[Document]) -> int:
        """POST a list of Documents to *path* and return the count from the response."""
        url = f"{self._base_url}{path}"
        payload = {"documents": self._serialize_documents(documents)}
        resp = self._checked_post(url, payload)
        data = resp.json()
        return data.get("added", data.get("extracted", 0))

    def _checked_post(self, url: str, payload: dict):
        """POST *payload* as JSON, raise on connection / non-2xx errors."""
        import requests  # type: ignore

        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                f"DockerKnowledgeGraph: cannot reach {url} ({exc})"
            ) from exc

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"DockerKnowledgeGraph: {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    def _get_stats(self) -> dict:
        import requests  # type: ignore

        url = f"{self._base_url}/stats"
        try:
            resp = requests.get(url, timeout=self._timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"num_nodes": 0, "num_edges": 0}

    @staticmethod
    def _serialize_documents(documents: List[Document]) -> list:
        return [
            {"page_content": doc.page_content, "metadata": doc.metadata}
            for doc in documents
        ]

    @staticmethod
    def _deserialize_nodes(raw: list) -> List[GraphNode]:
        nodes = []
        for item in raw:
            try:
                node_type = NodeType(item.get("node_type", "Section"))
            except ValueError:
                node_type = NodeType.SECTION
            nodes.append(GraphNode(
                node_id=item.get("node_id", ""),
                node_type=node_type,
                label=item.get("label", ""),
                properties=item.get("properties", {}),
            ))
        return nodes
