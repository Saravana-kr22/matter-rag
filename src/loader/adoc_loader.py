"""AsciiDoc loader — splits on section headings and delegates to an injected chunker."""

from __future__ import annotations

import logging
import re
from typing import List

from src.chunker.base_chunker import BaseChunker
from src.chunker.matter_tc_chunker import MatterTCChunker
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)

# Match AsciiDoc headings: == Title, === Sub, ==== Sub-sub …
_SECTION_RE = re.compile(r"^(={1,6})\s+(.+)$", re.MULTILINE)


class AdocLoader(BaseDocumentLoader):
    """Load AsciiDoc files, splitting on section headings.

    Each section body is passed to the injected ``BaseChunker``.
    Default chunker is ``MatterTCChunker`` for TC-aware splitting.
    """

    def __init__(
        self,
        chunker: BaseChunker | None = None,
        section_split: bool = True,
    ) -> None:
        self._chunker: BaseChunker = chunker or MatterTCChunker()
        self._section_split = section_split

    def supported_extensions(self) -> List[str]:
        return [".adoc"]

    def load(self, fetched: FetchedDocument) -> List[Document]:
        if not self._section_split:
            return self._chunker.chunk(fetched.content, fetched.metadata)

        content = fetched.content
        metadata = fetched.metadata
        splits = _SECTION_RE.split(content)

        docs: List[Document] = []

        # Content before the first heading
        if splits:
            preamble = splits[0].strip()
            if preamble:
                docs.extend(self._chunker.chunk(
                    preamble,
                    {**metadata, "section": "preamble"},
                ))

        # Group (level_marks, title, body) triples
        i = 1
        while i + 2 <= len(splits):
            level_marks = splits[i]
            title = splits[i + 1].strip()
            body = splits[i + 2].strip()
            i += 3

            section_level = len(level_marks)
            section_text = f"{'=' * section_level} {title}\n\n{body}"
            docs.extend(self._chunker.chunk(
                section_text,
                {**metadata, "section": title, "section_level": section_level},
            ))

        return docs or self._chunker.chunk(content, metadata)
