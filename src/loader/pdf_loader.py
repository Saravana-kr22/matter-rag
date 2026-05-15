"""PDF loader — extracts text from PDF files and chunks via an injected BaseChunker."""

from __future__ import annotations

import logging
from typing import List

from src.chunker.base_chunker import BaseChunker, GenericChunker
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)


class PDFLoader(BaseDocumentLoader):
    """Load PDF files using pypdf, then chunk via an injected BaseChunker."""

    def __init__(self, chunker: BaseChunker | None = None) -> None:
        self._chunker: BaseChunker = chunker or GenericChunker()

    def supported_extensions(self) -> List[str]:
        return [".pdf"]

    def load(self, fetched: FetchedDocument) -> List[Document]:
        """Extract text from a PDF and return chunked Documents.

        Uses ``fetched.metadata['absolute_path']`` when available to read
        binary PDF via pypdf; otherwise treats ``fetched.content`` as
        already-extracted text.
        """
        try:
            import pypdf  # type: ignore
        except ImportError:
            raise ImportError("Install pypdf: pip install pypdf")

        metadata = fetched.metadata
        abs_path = metadata.get("absolute_path")

        if not abs_path:
            return self._chunker.chunk(fetched.content, metadata)

        pages: List[Document] = []
        with open(abs_path, "rb") as fh:
            reader = pypdf.PdfReader(fh)
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                pages.append(Document(
                    page_content=text.strip(),
                    metadata={**metadata, "page_number": i + 1},
                ))

        combined = "\n\n".join(d.page_content for d in pages)
        return self._chunker.chunk(combined, metadata)
