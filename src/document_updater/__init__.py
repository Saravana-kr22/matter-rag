"""document_updater — pluggable document update module for Matter RAG pipeline.

Re-exports the public API:
  BaseDocumentUpdater  — ABC for format-specific updaters
  AdocUpdater          — AsciiDoc updater (replaces / appends TC sections)
  create_updater()     — factory: extension string → BaseDocumentUpdater instance

Add support for a new format by:
  1. Subclassing BaseDocumentUpdater in a new file.
  2. Adding the class to updater_registry.REGISTRY.
"""

from src.document_updater.adoc_updater import AdocUpdater
from src.document_updater.base_updater import BaseDocumentUpdater
from src.document_updater.updater_registry import REGISTRY, create_updater

__all__ = [
    "BaseDocumentUpdater",
    "AdocUpdater",
    "REGISTRY",
    "create_updater",
]
