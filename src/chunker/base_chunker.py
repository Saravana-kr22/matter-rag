"""Base chunker ABC and generic sliding-window implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


class BaseChunker(ABC):
    """Abstract base class for all text chunkers."""

    @abstractmethod
    def chunk(self, text: str, metadata: dict) -> list:
        """Split text into a list of Document objects.

        Args:
            text: Raw text to split.
            metadata: Base metadata dict to attach (or augment) to every chunk.

        Returns:
            List of Document objects.
        """
        ...


class GenericChunker(BaseChunker):
    """Sliding-window character-level chunker.

    Produces overlapping chunks of fixed character length.
    """

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk(self, text: str, metadata: dict) -> list:
        """Split *text* into overlapping character-level chunks.

        Args:
            text: Input text.
            metadata: Base metadata; each chunk adds ``chunk_index``.

        Returns:
            List of Document objects.
        """
        from src.loader.base_loader import Document

        text = text.strip()
        if not text:
            return []

        size = self.chunk_size
        overlap = self.chunk_overlap
        chunks: list = []
        start = 0
        chunk_index = 0
        while start < len(text):
            end = start + size
            chunk = text[start:end]
            chunks.append(Document(
                page_content=chunk,
                metadata={**metadata, "chunk_index": chunk_index},
            ))
            chunk_index += 1
            start += size - overlap
            if start >= len(text):
                break

        return chunks
