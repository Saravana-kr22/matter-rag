"""HTML loader — heading-based section chunker for HTML documents."""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from src.chunker.base_chunker import BaseChunker, GenericChunker
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)

# Heading tags h1–h6 used for section splitting
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Tags whose entire subtree should be discarded (no text extracted)
_SKIP_TAGS = {"style", "script", "noscript", "head", "svg", "template", "link", "meta"}


class _SectionParser(HTMLParser):
    """Extract heading-delimited sections from HTML.

    Each section is a tuple of (heading_level, heading_text, body_text).
    Content before the first heading is stored as (0, "", preamble_text).

    Content inside ``<style>``, ``<script>``, ``<head>``, ``<svg>``,
    ``<noscript>``, and similar non-content tags is discarded.
    """

    def __init__(self) -> None:
        super().__init__()
        self._sections: List[Tuple[int, str, str]] = []
        self._in_heading: Optional[int] = None   # heading level when inside <hN>
        self._heading_buf: List[str] = []
        self._body_buf: List[str] = []
        self._current_heading_level: int = 0
        self._current_heading_text: str = ""
        # Buffer for the preamble (text before first heading)
        self._preamble_done: bool = False
        # Depth counter for skip-tag subtrees; >0 means discard character data
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag in _HEADING_TAGS:
            # Flush previous section
            self._flush_section()
            self._in_heading = int(tag[1])
            self._heading_buf = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return
        if tag in _HEADING_TAGS and self._in_heading is not None:
            self._current_heading_level = self._in_heading
            self._current_heading_text = "".join(self._heading_buf).strip()
            self._in_heading = None
            self._body_buf = []
            self._preamble_done = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_heading is not None:
            self._heading_buf.append(data)
        else:
            self._body_buf.append(data)

    def _flush_section(self) -> None:
        body = "".join(self._body_buf).strip()
        if body or self._current_heading_text:
            self._sections.append((
                self._current_heading_level,
                self._current_heading_text,
                body,
            ))
        self._body_buf = []

    def close(self) -> None:
        self._flush_section()
        super().close()

    @property
    def sections(self) -> List[Tuple[int, str, str]]:
        return self._sections


def _clean_text(text: str) -> str:
    """Collapse whitespace runs; strip leading/trailing blank lines."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


class HTMLLoader(BaseDocumentLoader):
    """Load HTML documents with heading-based section splitting.

    Each ``<h1>``–``<h6>`` section becomes one or more ``Document`` chunks.
    Content before the first heading is emitted as a 'preamble' chunk.

    Metadata keys added per chunk:
      - ``chunk_type``:     "preamble" | "section"
      - ``heading``:        heading text (empty string for preamble)
      - ``heading_level``:  1–6 (0 for preamble)
      - ``chunk_index``:    sequential index within this document
    """

    def __init__(self, chunker: BaseChunker | None = None) -> None:
        self._chunker: BaseChunker = chunker or GenericChunker()

    def supported_extensions(self) -> List[str]:
        return [".html", ".htm"]

    def load(self, fetched: FetchedDocument) -> List[Document]:
        parser = _SectionParser()
        try:
            parser.feed(fetched.content)
            parser.close()
        except Exception as exc:
            logger.warning("[HTMLLoader] Parser error on %s: %s — falling back to plain text",
                           fetched.path, exc)
            return self._chunker.chunk(fetched.content, fetched.metadata)

        sections = parser.sections
        if not sections:
            # No structure found — treat as plain text
            logger.debug("[HTMLLoader] No sections in %s — using generic chunker", fetched.path)
            return self._chunker.chunk(fetched.content, fetched.metadata)

        all_docs: List[Document] = []
        chunk_index = 0

        for level, heading, body in sections:
            body_clean = _clean_text(body)
            if not body_clean and not heading:
                continue

            chunk_type = "preamble" if level == 0 else "section"
            # Build section text for the chunker: prepend heading so it's part of embedding
            if heading:
                section_text = f"{heading}\n\n{body_clean}" if body_clean else heading
            else:
                section_text = body_clean

            base_meta = {
                **fetched.metadata,
                "chunk_type": chunk_type,
                "heading": heading,
                "heading_level": level,
            }

            # Delegate to chunker for further splitting if section is large
            chunks = self._chunker.chunk(section_text, base_meta)
            if not chunks:
                continue

            for doc in chunks:
                doc.metadata["chunk_index"] = chunk_index
                chunk_index += 1
                all_docs.append(doc)

        if not all_docs:
            # Fall back if sections produced nothing useful
            return self._chunker.chunk(fetched.content, fetched.metadata)

        logger.debug("[HTMLLoader] %s → %d chunks from %d sections",
                     fetched.path, len(all_docs), len(sections))
        return all_docs
