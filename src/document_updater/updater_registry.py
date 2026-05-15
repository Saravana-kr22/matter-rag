"""Document updater registry — maps file extensions to BaseDocumentUpdater subclasses.

Add a new updater by:
1. Subclassing ``BaseDocumentUpdater`` and implementing ``supported_extension()``
   and ``write_updates()``.
2. Adding an entry to ``REGISTRY`` below — no other files need to change.
"""

from __future__ import annotations

import logging
from typing import Dict, Type

from src.document_updater.adoc_updater import AdocUpdater
from src.document_updater.base_updater import BaseDocumentUpdater

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry — add new updater classes here
# ---------------------------------------------------------------------------

REGISTRY: Dict[str, Type[BaseDocumentUpdater]] = {
    AdocUpdater.supported_extension(): AdocUpdater,
    # ".csv": CsvUpdater,   # future
    # ".txt": TxtUpdater,   # future
}


def create_updater(extension: str) -> BaseDocumentUpdater:
    """Return an instantiated updater for the given file extension.

    Args:
        extension: File extension string, e.g. ``".adoc"``.

    Raises:
        ValueError: If no updater is registered for *extension*.
    """
    cls = REGISTRY.get(extension.lower())
    if cls is None:
        raise ValueError(
            f"No document updater registered for extension '{extension}'. "
            f"Registered: {sorted(REGISTRY)}"
        )
    logger.debug("Creating updater for extension: %s", extension)
    return cls()
