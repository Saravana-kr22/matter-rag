"""Chunker module — text splitting strategies for Matter RAG documents."""

from src.chunker.base_chunker import BaseChunker, GenericChunker
from src.chunker.matter_tc_chunker import (
    IgnoreRule,
    MatterTCChunker,
    TCRecord,
    apply_ignore_rules,
)

__all__ = [
    "BaseChunker",
    "GenericChunker",
    "IgnoreRule",
    "MatterTCChunker",
    "TCRecord",
    "apply_ignore_rules",
]
