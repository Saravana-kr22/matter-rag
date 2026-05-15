"""Backward-compatibility shim — MatterKGBuilder now lives in matter_kg_builder.py.

Any code that previously imported ``KnowledgeGraphBuilder`` from this module
continues to work without changes.
"""

from src.knowledge_graph.matter_kg_builder import (  # noqa: F401
    MatterKGBuilder,
    KnowledgeGraphBuilder,
    NodeType,
    EdgeType,
    GraphNode,
    GraphEdge,
)

__all__ = [
    "MatterKGBuilder",
    "KnowledgeGraphBuilder",
    "NodeType",
    "EdgeType",
    "GraphNode",
    "GraphEdge",
]
