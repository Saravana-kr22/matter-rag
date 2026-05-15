"""Document loader — thin orchestrator kept for backward compatibility with nodes.py.

New code should use DocumentLoaderFactory directly:
    from src.loader.loader_factory import DocumentLoaderFactory
"""

from __future__ import annotations

import logging
from typing import List, Optional

from src.config.config_loader import LoaderConfig
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import Document
from src.loader.loader_factory import DocumentLoaderFactory

logger = logging.getLogger(__name__)

# Re-export Document so existing `from src.loader.document_loader import Document` still works
__all__ = ["Document", "DocumentLoader"]


class DocumentLoader:
    """Parse fetched documents into chunked Document objects.

    Thin wrapper over DocumentLoaderFactory that preserves the existing
    ``load()`` / ``load_all()`` interface expected by nodes.py.
    """

    def __init__(self, loader_config: LoaderConfig, chunker_config=None) -> None:
        self._factory = DocumentLoaderFactory(loader_config, chunker_config)

    def load(self, fetched: FetchedDocument) -> List[Document]:
        """Parse a FetchedDocument into a list of Document chunks."""
        return self._factory.load_one(fetched)

    def load_all(self, fetched_docs: List[FetchedDocument]) -> List[Document]:
        """Load and chunk all fetched documents."""
        return self._factory.load_all(fetched_docs)
