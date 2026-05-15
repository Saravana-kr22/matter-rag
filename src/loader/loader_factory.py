"""DocumentLoaderFactory — registry of per-extension loaders with injected chunkers."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.config.config_loader import LoaderConfig
from src.fetcher.document_fetcher import FetchedDocument
from src.loader.base_loader import BaseDocumentLoader, Document

logger = logging.getLogger(__name__)


class DocumentLoaderFactory:
    """Build and dispatch per-extension document loaders.

    Usage::

        factory = DocumentLoaderFactory(loader_config)
        docs = factory.load_all(fetched_documents)
    """

    def __init__(
        self,
        loader_config: LoaderConfig,
        chunker_config=None,
        chunker_map: Optional[Dict[str, object]] = None,
    ) -> None:
        """Initialise the factory.

        Args:
            loader_config: Loader settings (chunk_size, overlap, adoc_section_split).
            chunker_config: Optional ChunkerConfig. When provided, its chunk_size /
                chunk_overlap / chunker_type take precedence over loader_config values.
            chunker_map: Optional dict mapping file extension → BaseChunker instance,
                overriding the defaults derived from *chunker_config*.
        """
        from src.chunker.base_chunker import GenericChunker
        from src.chunker.matter_tc_chunker import MatterTCChunker
        from src.loader.adoc_loader import AdocLoader
        from src.loader.csv_loader import CSVLoader
        from src.loader.html_loader import HTMLLoader
        from src.loader.pdf_loader import PDFLoader
        from src.loader.text_loader import TextLoader

        # Determine effective chunk parameters
        size = loader_config.chunk_size
        overlap = loader_config.chunk_overlap
        chunker_type = "matter_tc"
        if chunker_config is not None:
            size = chunker_config.chunk_size
            overlap = chunker_config.chunk_overlap
            chunker_type = getattr(chunker_config, "chunker_type", "matter_tc")

        generic = GenericChunker(size, overlap)
        ignore_rules = getattr(chunker_config, "ignore_rules", []) if chunker_config else []
        matter_tc = MatterTCChunker(size, overlap, ignore_rules=ignore_rules)

        # Build default chunker per extension
        _defaults: Dict[str, object] = {
            ".pdf": generic,
            ".adoc": matter_tc if chunker_type == "matter_tc" else generic,
            ".csv": generic,
            ".md": generic,
            ".txt": generic,
            ".html": generic,
            ".htm": generic,
        }

        # Apply any user overrides
        if chunker_map:
            _defaults.update(chunker_map)

        # Build loader registry
        self._registry: Dict[str, BaseDocumentLoader] = {
            ".pdf": PDFLoader(_defaults[".pdf"]),
            ".adoc": AdocLoader(_defaults[".adoc"], loader_config.adoc_section_split),
            ".csv": CSVLoader(_defaults[".csv"]),
            ".md": TextLoader(_defaults[".md"]),
            ".txt": TextLoader(_defaults[".txt"]),
            ".html": HTMLLoader(_defaults[".html"]),
            ".htm": HTMLLoader(_defaults[".htm"]),
        }

        # Keep a fallback text loader for unknown extensions
        self._fallback: BaseDocumentLoader = TextLoader(generic)

    def get_loader(self, extension: str) -> BaseDocumentLoader:
        """Return the loader registered for *extension*, or the fallback TextLoader."""
        return self._registry.get(extension.lower(), self._fallback)

    def load_one(self, fetched: FetchedDocument) -> List[Document]:
        """Parse a single FetchedDocument into Document chunks."""
        ext = fetched.extension
        loader = self.get_loader(ext)
        logger.debug("Loading %s with %s", fetched.path, type(loader).__name__)
        docs = loader.load(fetched)
        # Ensure every chunk carries the source file path so LLM prompts and
        # reports display it correctly (loaders only spread fetched.metadata,
        # which does not include the path attribute).
        for doc in docs:
            doc.metadata.setdefault("path", fetched.path)
        logger.debug("Produced %d chunks from %s", len(docs), fetched.path)
        return docs

    def load_all(self, fetched_docs: List[FetchedDocument]) -> List[Document]:
        """Load and chunk all fetched documents, logging errors per file."""
        all_docs: List[Document] = []
        for fetched in fetched_docs:
            try:
                all_docs.extend(self.load_one(fetched))
            except Exception as exc:
                logger.warning("Failed to load %s: %s", fetched.path, exc)
        logger.info(
            "Total chunks loaded: %d from %d files", len(all_docs), len(fetched_docs)
        )
        return all_docs
