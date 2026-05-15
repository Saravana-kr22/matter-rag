"""Text loader — plain text and Markdown via an injected chunker."""

from __future__ import annotations

import logging
from typing import List

from src.chunker.base_chunker import BaseChunker, GenericChunker
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)


class TextLoader(BaseDocumentLoader):
    """Load plain text (.txt) and Markdown (.md) files."""

    def __init__(self, chunker: BaseChunker | None = None) -> None:
        self._chunker: BaseChunker = chunker or GenericChunker()

    def supported_extensions(self) -> List[str]:
        return [".txt", ".md"]

    def load(self, fetched: FetchedDocument) -> List[Document]:
        return self._chunker.chunk(fetched.content, fetched.metadata)
