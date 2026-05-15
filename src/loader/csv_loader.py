"""CSV loader — converts rows to prose Documents and merges via an injected chunker."""

from __future__ import annotations

import csv
import io
import logging
from typing import List

from src.chunker.base_chunker import BaseChunker, GenericChunker
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)


class CSVLoader(BaseDocumentLoader):
    """Load CSV files, converting each row to a prose Document chunk."""

    def __init__(self, chunker: BaseChunker | None = None) -> None:
        self._chunker: BaseChunker = chunker or GenericChunker()

    def supported_extensions(self) -> List[str]:
        return [".csv"]

    def load(self, fetched: FetchedDocument) -> List[Document]:
        """Parse CSV rows into prose, then merge small docs into chunks."""
        reader = csv.DictReader(io.StringIO(fetched.content))
        row_docs: List[Document] = []
        for row_num, row in enumerate(reader, start=1):
            text = "  |  ".join(f"{k}: {v}" for k, v in row.items() if v)
            row_docs.append(Document(
                page_content=text,
                metadata={**fetched.metadata, "row": row_num, "columns": list(row.keys())},
            ))
        return self._merge_rows(row_docs)

    def _merge_rows(self, docs: List[Document]) -> List[Document]:
        """Merge small row Documents into chunker-sized blocks."""
        if not docs:
            return []

        # Estimate chunk_size from the chunker if available
        chunk_size = getattr(self._chunker, "chunk_size", 1000)

        merged: List[Document] = []
        buffer: List[Document] = []
        buffer_text = ""

        for doc in docs:
            buffer.append(doc)
            buffer_text += "\n" + doc.page_content
            if len(buffer_text) >= chunk_size:
                merged.append(Document(
                    page_content=buffer_text.strip(),
                    metadata={
                        **buffer[0].metadata,
                        "chunk_rows": [d.metadata.get("row") for d in buffer],
                    },
                ))
                buffer = []
                buffer_text = ""

        if buffer:
            merged.append(Document(
                page_content=buffer_text.strip(),
                metadata={
                    **buffer[0].metadata,
                    "chunk_rows": [d.metadata.get("row") for d in buffer],
                },
            ))
        return merged
