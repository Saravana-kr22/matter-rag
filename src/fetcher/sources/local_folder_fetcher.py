"""Local folder fetcher — recursively loads files from a local directory."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import List, Optional

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)

_DEFAULT_EXTENSIONS = {".adoc", ".pdf", ".csv", ".md", ".txt", ".html", ".htm"}

# Sentinel value stored when sources.json uses extensions: ["*"]
_WILDCARD = "*"


class LocalFolderFetcher(BaseFetcher):
    """Recursively load all documents from a local directory.

    ``extensions`` in sources.json controls which file types are loaded:

    - Omitted or ``[]``          → default set: .adoc .pdf .csv .md .txt .html .htm
    - ``["*"]``                  → every file regardless of extension
    - ``[".html", ".adoc"]``     → only the listed extensions

    ``exclude`` in sources.json accepts a list of filename patterns to skip.
    Patterns are matched against the filename (not the full path) using
    fnmatch-style globbing (e.g. ``"*interop*"``, ``"interop_booklet.html"``).
    """

    def __init__(
        self,
        path: str,
        extensions: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        process_rules: Optional[List[dict]] = None,
        extra_metadata: Optional[dict] = None,
    ) -> None:
        self._path = Path(path)
        if extensions and _WILDCARD in extensions:
            self._extensions = None   # None → accept all
        else:
            self._extensions = set(extensions or _DEFAULT_EXTENSIONS)
        self._exclude = exclude or []
        self._process_rules = process_rules or []
        self._extra_metadata = extra_metadata or {}

    @classmethod
    def source_type(cls) -> str:
        return "local_folder"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "LocalFolderFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            path=cfg.get("path", ""),
            extensions=cfg.get("extensions"),
            exclude=cfg.get("exclude", []),
            process_rules=cfg.get("process_rules", []),
            extra_metadata=cfg.get("metadata", {}),
        )

    def _is_excluded(self, file_path: Path) -> bool:
        """Return True if this file should be skipped based on exclude patterns."""
        name = file_path.name
        for pattern in self._exclude:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def fetch(self) -> List[FetchedDocument]:
        if not self._path.exists():
            raise FileNotFoundError(f"Directory not found: {self._path.resolve()}")

        docs: List[FetchedDocument] = []
        skipped = 0
        for file_path in self._path.rglob("*"):
            if not file_path.is_file():
                continue
            if self._extensions is not None and file_path.suffix.lower() not in self._extensions:
                continue
            if self._is_excluded(file_path):
                logger.debug("[LocalFolderFetcher] Excluding %s (matches exclude pattern)", file_path.name)
                skipped += 1
                continue
            try:
                content = self._read_file(file_path)
                docs.append(FetchedDocument(
                    path=str(file_path.relative_to(self._path)),
                    content=content,
                    metadata={
                        "source": "local",
                        "source_id": "local_folder",
                        "absolute_path": str(file_path.resolve()),
                        "file_size": file_path.stat().st_size,
                        "_process_rules": self._process_rules,
                        **self._extra_metadata,
                    },
                ))
            except Exception as exc:
                logger.warning("[LocalFolderFetcher] Could not read %s: %s", file_path, exc)

        ext_desc = "*" if self._extensions is None else ",".join(sorted(self._extensions))
        if skipped:
            logger.info("[LocalFolderFetcher] Loaded %d files from %s (extensions: %s, excluded: %d)",
                        len(docs), self._path, ext_desc, skipped)
        else:
            logger.info("[LocalFolderFetcher] Loaded %d files from %s (extensions: %s)",
                        len(docs), self._path, ext_desc)
        return docs

    @staticmethod
    def _read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1")
