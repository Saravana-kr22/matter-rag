"""Knowledge graph factory — returns the correct backend based on config.

Usage::

    from src.knowledge_graph.graph_factory import create_knowledge_graph
    kg = create_knowledge_graph(config.knowledge_graph)
    kg.add_test_plan_documents(chunks)
"""

from __future__ import annotations

import logging

from src.knowledge_graph.base_graph import BaseKnowledgeGraph

logger = logging.getLogger(__name__)


def create_knowledge_graph(config) -> BaseKnowledgeGraph:
    """Instantiate the knowledge graph backend specified by ``config.backend``.

    Args:
        config: A ``KnowledgeGraphConfig`` instance.

    Returns:
        - ``"local"`` (default) → ``KnowledgeGraphBuilder`` (NetworkX, in-process)
        - ``"docker"``          → ``DockerKnowledgeGraph`` (HTTP client, remote service)

    The Docker client calls ``load()`` immediately to health-check the service;
    a ``ConnectionError`` is raised if the service is unreachable.
    """
    backend = getattr(config, "backend", "local")

    if backend == "docker":
        from src.knowledge_graph.docker_graph import DockerKnowledgeGraph  # noqa: PLC0415

        url = getattr(config, "docker_url", "http://localhost:8002")
        timeout = getattr(config, "docker_timeout", 30)
        logger.info("KG backend: docker  url=%s", url)
        kg = DockerKnowledgeGraph(url, timeout)
        kg.load()   # health-check / connection test
        return kg

    # Default: local NetworkX graph
    from src.knowledge_graph.matter_kg_builder import MatterKGBuilder  # noqa: PLC0415

    logger.info("KG backend: local (NetworkX)")
    return MatterKGBuilder(config)
