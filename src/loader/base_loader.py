"""Base loader — Document dataclass and BaseDocumentLoader ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.fetcher.document_fetcher import FetchedDocument


@dataclass
class Document:
    """A chunk of parsed document content ready for embedding."""
    page_content: str
    metadata: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.page_content)


class BaseDocumentLoader(ABC):
    """Abstract base class for all document loaders."""

    @abstractmethod
    def load(self, fetched: "FetchedDocument") -> List[Document]:
        """Parse a FetchedDocument into a list of Document chunks.

        Args:
            fetched: A FetchedDocument from the fetcher module.

        Returns:
            List of Document objects.
        """
        ...

    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """Return the list of file extensions this loader handles."""
        ...
