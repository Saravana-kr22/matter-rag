"""CSV fetcher — reads a CSV file and converts each row to a prose FetchedDocument."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import List, Optional

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)


class CSVFetcher(BaseFetcher):
    """Read a CSV file and convert each row into a FetchedDocument.

    Each row becomes a single document whose content is:
        "col1: val1 | col2: val2 | col3: val3"

    This makes CSV row data searchable as natural-language text by the embeddings module.
    """

    def __init__(
        self,
        path: str,
        columns: Optional[List[str]] = None,
        row_delimiter: str = " | ",
        process_rules: Optional[List[dict]] = None,
    ) -> None:
        self._path = Path(path)
        self._columns = columns        # None = use all columns
        self._row_delimiter = row_delimiter
        self._process_rules = process_rules or []

    @classmethod
    def source_type(cls) -> str:
        return "csv"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "CSVFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            path=cfg.get("path", ""),
            columns=cfg.get("columns"),
            row_delimiter=cfg.get("row_delimiter", " | "),
            process_rules=cfg.get("process_rules", []),
        )

    def fetch(self) -> List[FetchedDocument]:
        if not self._path.exists():
            raise FileNotFoundError(f"CSV file not found: {self._path.resolve()}")

        docs: List[FetchedDocument] = []
        with self._path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader):
                cols = self._columns or list(row.keys())
                parts = [f"{col}: {row.get(col, '').strip()}"
                         for col in cols if row.get(col, "").strip()]
                if not parts:
                    continue
                content = self._row_delimiter.join(parts)
                docs.append(FetchedDocument(
                    path=f"{self._path.name}::row_{row_num}",
                    content=content,
                    metadata={
                        "source": "csv",
                        "source_id": "csv",
                        "csv_path": str(self._path.resolve()),
                        "row_index": row_num,
                        "raw_row": dict(row),
                        "_process_rules": self._process_rules,
                    },
                ))

        logger.info("[CSVFetcher] Loaded %d rows from %s", len(docs), self._path)
        return docs
