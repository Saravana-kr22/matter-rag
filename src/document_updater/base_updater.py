"""Base class for document updaters.

Each updater handles a specific file format (.adoc, .csv, .txt, etc.)
and applies LLM-suggested test case changes to produce updated output files.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List


class BaseDocumentUpdater(ABC):
    """ABC for writing LLM-suggested TC changes back to source documents.

    Subclass this to support a new output format.  Register the subclass
    in ``updater_registry.REGISTRY`` — no other files need to change.

    Example::

        class MyFormatUpdater(BaseDocumentUpdater):
            @classmethod
            def supported_extension(cls) -> str:
                return ".myext"

            def write_updates(self, analysis_results, search_results, output_dir):
                ...
                return ["/path/to/output.myext"]
    """

    @classmethod
    @abstractmethod
    def supported_extension(cls) -> str:
        """Return the file extension this updater handles, e.g. ``".adoc"``."""

    @abstractmethod
    def write_updates(
        self,
        analysis_results: List[dict],
        search_results: Dict[str, List],
        output_dir: str,
    ) -> List[str]:
        """Apply LLM analysis results to source documents and write output files.

        Args:
            analysis_results: Per-PR-chunk dicts from ``analyze_node``.
                              Each dict may contain ``"llm_json"`` with
                              ``"missing_tests"`` and ``"update_candidates"``
                              keyed by the LLM structured output.
            search_results:   FAISS search results (pr_chunk_key →
                              List[SearchResult]) providing the
                              ``tc_id → absolute_path`` mapping.
            output_dir:       Directory where output files should be written.
                              The caller creates a timestamped sub-folder so
                              implementors can write directly into this path.

        Returns:
            List of absolute paths of files that were written.
        """
