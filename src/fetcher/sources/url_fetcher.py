"""URL fetcher — fetches a document from an HTTP/HTTPS URL."""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import List, Optional

import requests

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)


class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keeping text content."""
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


class URLFetcher(BaseFetcher):
    """Fetch a single document from an HTTP/HTTPS URL.

    Supports Quip docs, internal wikis, and any plain-text or HTML URL.

    ``format`` values:
      - ``"auto"``         — strip HTML tags when ``Content-Type: text/html``, else plain text
      - ``"html"``         — always strip HTML tags; content becomes plain text
      - ``"raw_html"``     — preserve raw HTML structure; ``HTMLLoader`` in the loader stage
                             will parse headings into structured chunks
      - ``"matter_diff"``  — preserve raw HTML and mark document as a Matter spec diff
                             (appclusters_diff.html); ``process_documents_node`` expands it
                             into one ``FetchedDocument`` per diff section via
                             ``ProcessMatterHtmlDoc``
      - ``"text"``         — always treat response as plain text (no tag stripping)
    """

    def __init__(
        self,
        url: str,
        fmt: str = "auto",             # "auto" | "html" | "raw_html" | "matter_diff" | "text"
        timeout: int = 60,
        headers: Optional[dict] = None,
        process_rules: Optional[List[dict]] = None,
        extra_metadata: Optional[dict] = None,
    ) -> None:
        self._url = url
        self._fmt = fmt
        self._timeout = timeout
        self._headers = headers or {}
        self._process_rules = process_rules or []
        self._extra_metadata = extra_metadata or {}

    @classmethod
    def source_type(cls) -> str:
        return "url"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "URLFetcher":
        cfg = resolve_config_vars(source_cfg)
        # Collect any extra metadata keys the source config provides
        known_keys = {"id", "type", "role", "url", "format", "timeout", "headers",
                      "process_rules", "matter_diff_cluster", "matter_diff_section"}
        extra: dict = {}
        for k in ("matter_diff_cluster", "matter_diff_section"):
            if k in cfg:
                extra[k] = cfg[k]
        return cls(
            url=cfg.get("url", ""),
            fmt=cfg.get("format", "auto"),
            timeout=int(cfg.get("timeout", 60)),
            headers=cfg.get("headers"),
            process_rules=cfg.get("process_rules", []),
            extra_metadata=extra,
        )

    def fetch(self) -> List[FetchedDocument]:
        logger.info("[URLFetcher] Fetching %s", self._url)
        resp = requests.get(self._url, headers=self._headers, timeout=self._timeout)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        raw_text = resp.text

        fmt = self._fmt
        if fmt == "auto":
            fmt = "html" if "text/html" in content_type else "text"

        matter_diff_flag = False
        if fmt == "matter_diff":
            # Preserve HTML; mark for ProcessMatterHtmlDoc expansion in process stage
            content = raw_text
            is_html = True
            matter_diff_flag = True
        elif fmt == "raw_html":
            # Preserve HTML structure — HTMLLoader will split on headings
            content = raw_text
            is_html = True
        elif fmt == "html":
            stripper = _HTMLStripper()
            stripper.feed(raw_text)
            content = stripper.get_text()
            is_html = True
        else:
            content = raw_text
            is_html = False

        # Derive a filename from the URL for path/extension purposes
        from urllib.parse import urlparse
        parsed = urlparse(self._url)
        path = parsed.path.rstrip("/") or "document"
        if not path.endswith((".html", ".htm", ".txt", ".md")):
            path += ".html" if is_html else ".txt"

        meta: dict = {
            "source": "url",
            "source_id": "url",
            "url": self._url,
            "content_type": content_type,
            "_process_rules": self._process_rules,
            **self._extra_metadata,
        }
        if matter_diff_flag:
            meta["matter_diff"] = True

        doc = FetchedDocument(path=path, content=content, metadata=meta)
        logger.info("[URLFetcher] Fetched %d chars from %s (format=%s)",
                    len(content), self._url, fmt)
        return [doc]
