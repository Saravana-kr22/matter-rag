"""Document processor — applies configurable text-cleaning rules to FetchedDocuments."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List

from src.fetcher.base_fetcher import FetchedDocument

logger = logging.getLogger(__name__)


class DocumentProcessor:
    """Apply a chain of text-cleaning rules to FetchedDocument content.

    Rules are loaded from:
      1. A global ``.ignore_rules.json`` file (shared across all sources)
      2. Per-source ``process_rules`` list embedded in each document's metadata
         under the ``_process_rules`` key (set by each BaseFetcher)

    Global rules run first; per-source rules run after.

    Rule format (one dict per rule)::

        {
            "type":         "strip_regex",          # required — see table below
            "comment":      "optional description",
            "apply_to":     [".adoc", ".md"],       # optional extension filter
            ...type-specific keys...
        }

    Supported rule types:

    +-----------------------+-------------------------------------------------------+
    | type                  | keys                                                  |
    +-----------------------+-------------------------------------------------------+
    | strip_regex           | pattern (re), scope (line|block, default line)        |
    | strip_block_between   | start_pattern, end_pattern (inclusive)                |
    | strip_first_lines     | count (int)                                           |
    | strip_last_lines      | count (int)                                           |
    | normalize_whitespace  | (none) — collapses 3+ blank lines to 1               |
    | replace_regex         | pattern (re), replacement (str)                       |
    +-----------------------+-------------------------------------------------------+
    """

    def __init__(self, global_rules_path: str = ".ignore_rules.json") -> None:
        self._global_rules = self._load_rules(global_rules_path)
        logger.debug(
            "[DocumentProcessor] Loaded %d global rule(s) from %s",
            len(self._global_rules), global_rules_path,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        doc: FetchedDocument,
        source_rules: List[dict] | tuple = (),
    ) -> FetchedDocument:
        """Apply global rules then source-level rules to doc.content.

        Returns a new FetchedDocument; original is not modified.
        """
        rules = self._global_rules + list(source_rules)
        content = doc.content
        rules_applied = 0

        for rule in rules:
            apply_to = rule.get("apply_to")
            if apply_to and doc.extension not in apply_to:
                continue
            try:
                content = self._apply(content, rule)
                rules_applied += 1
            except Exception as exc:
                logger.warning(
                    "[DocumentProcessor] Rule '%s' failed on %s: %s",
                    rule.get("type"), doc.path, exc,
                )

        meta = {
            **doc.metadata,
            "_processed": True,
            "_rules_applied": rules_applied,
        }
        # Remove internal key so downstream modules don't see it
        meta.pop("_process_rules", None)
        return FetchedDocument(path=doc.path, content=content, metadata=meta)

    # ------------------------------------------------------------------
    # Rule dispatch
    # ------------------------------------------------------------------

    def _apply(self, text: str, rule: dict) -> str:
        t = rule.get("type", "")
        if t == "strip_regex":
            return self._strip_regex(text, rule)
        if t == "strip_block_between":
            return self._strip_block_between(text, rule)
        if t == "strip_first_lines":
            return self._strip_first_lines(text, rule)
        if t == "strip_last_lines":
            return self._strip_last_lines(text, rule)
        if t == "normalize_whitespace":
            return self._normalize_whitespace(text)
        if t == "replace_regex":
            return self._replace_regex(text, rule)
        logger.warning("[DocumentProcessor] Unknown rule type '%s' — skipping", t)
        return text

    # ------------------------------------------------------------------
    # Rule implementations
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_regex(text: str, rule: dict) -> str:
        """Remove lines (or block matches) whose content matches `pattern`."""
        pattern = rule.get("pattern", "")
        scope = rule.get("scope", "line")
        if scope == "line":
            lines = text.splitlines()
            filtered = [l for l in lines if not re.search(pattern, l)]
            return "\n".join(filtered)
        # scope == "block": remove all matches in the full text
        return re.sub(pattern, "", text, flags=re.MULTILINE)

    @staticmethod
    def _strip_block_between(text: str, rule: dict) -> str:
        """Remove blocks of text between start_pattern and end_pattern (inclusive)."""
        start = rule.get("start_pattern", "")
        end = rule.get("end_pattern", "")
        if not start or not end:
            return text
        lines = text.splitlines()
        result, inside = [], False
        for line in lines:
            if re.search(start, line):
                inside = True
                continue
            if inside and re.search(end, line):
                inside = False
                continue
            if not inside:
                result.append(line)
        return "\n".join(result)

    @staticmethod
    def _strip_first_lines(text: str, rule: dict) -> str:
        count = int(rule.get("count", 0))
        if count <= 0:
            return text
        return "\n".join(text.splitlines()[count:])

    @staticmethod
    def _strip_last_lines(text: str, rule: dict) -> str:
        count = int(rule.get("count", 0))
        if count <= 0:
            return text
        lines = text.splitlines()
        return "\n".join(lines[:-count] if count < len(lines) else [])

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """Collapse 3 or more consecutive blank lines down to a single blank line."""
        return re.sub(r"\n{3,}", "\n\n", text)

    @staticmethod
    def _replace_regex(text: str, rule: dict) -> str:
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        return re.sub(pattern, replacement, text, flags=re.MULTILINE)

    # ------------------------------------------------------------------
    # Loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_rules(path: str) -> List[dict]:
        p = Path(path)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("rules", [])
        except Exception as exc:
            logger.warning("[DocumentProcessor] Failed to load rules from %s: %s", path, exc)
            return []
