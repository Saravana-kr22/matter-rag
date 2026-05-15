"""Document fetcher — legacy fetcher kept for backward compatibility.

New code should use the pluggable fetchers in src/fetcher/sources/ via
src/fetcher/fetcher_registry.create_fetcher().

FetchedDocument is now defined in src/fetcher/base_fetcher and re-exported here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.config_loader import FetcherConfig
from src.fetcher.base_fetcher import FetchedDocument  # re-export for backward compat  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class DocumentFetcher:
    """Fetch documents from GitHub PR diffs or a local directory."""

    def __init__(self, config: FetcherConfig) -> None:
        self.config = config
        self._session = self._build_session(config)

    def _build_session(self, config: FetcherConfig) -> requests.Session:
        session = requests.Session()
        if config.github_token:
            session.headers.update(
                {"Authorization": f"Bearer {config.github_token}",
                 "Accept": "application/vnd.github.v3+json"}
            )
        # Automatic retry on connection errors and 5xx responses
        retry = Retry(
            total=config.github_max_retries,
            backoff_factor=2,           # wait 2, 4, 8 … seconds between retries
            status_forcelist={500, 502, 503, 504},
            allowed_methods={"GET"},
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_pr(self, pr_url: str) -> List[FetchedDocument]:
        """Fetch all changed files from a GitHub PR URL.

        Args:
            pr_url: Full GitHub PR URL, e.g.
                    https://github.com/owner/repo/pull/123

        Returns:
            List of FetchedDocument objects, one per changed file.
        """
        owner, repo, pr_number = self._parse_pr_url(pr_url)
        logger.info("Fetching PR #%s from %s/%s", pr_number, owner, repo)

        # Fetch PR metadata
        pr_meta = self._get_json(
            f"{self.config.github_api_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        )

        # Fetch list of changed files
        files = self._get_paginated(
            f"{self.config.github_api_url}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        )

        docs: List[FetchedDocument] = []
        for file_info in files:
            filename = file_info.get("filename", "")
            patch = file_info.get("patch", "")
            status = file_info.get("status", "modified")

            if not self._is_relevant_extension(filename):
                continue

            docs.append(FetchedDocument(
                path=filename,
                # Use the unified diff (patch) as content so the LLM sees exactly
                # what changed, not thousands of unchanged lines.  Fall back to the
                # full raw file only when the PR has no patch (e.g. binary files).
                content=patch or self._fetch_raw_file(owner, repo, filename, pr_meta) or "",
                metadata={
                    "source": "github_pr",
                    "pr_url": pr_url,
                    "pr_number": pr_number,
                    "repo": f"{owner}/{repo}",
                    "status": status,
                    "additions": file_info.get("additions", 0),
                    "deletions": file_info.get("deletions", 0),
                    "patch": patch,
                },
            ))

        logger.info("Fetched %d relevant files from PR #%s", len(docs), pr_number)
        return docs

    def fetch_local(self, directory: str | Path) -> List[FetchedDocument]:
        """Recursively load all documents from a local directory.

        Args:
            directory: Path to the folder containing test plan documents.

        Returns:
            List of FetchedDocument objects.
        """
        root = Path(directory)
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {root.resolve()}")

        docs: List[FetchedDocument] = []
        for file_path in root.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in self.config.local_extensions:
                continue

            try:
                content = self._read_file(file_path)
                docs.append(FetchedDocument(
                    path=str(file_path.relative_to(root)),
                    content=content,
                    metadata={
                        "source": "local",
                        "absolute_path": str(file_path.resolve()),
                        "file_size": file_path.stat().st_size,
                    },
                ))
                logger.debug("Loaded local file: %s", file_path)
            except Exception as exc:
                logger.warning("Could not read %s: %s", file_path, exc)

        logger.info("Loaded %d files from %s", len(docs), root)
        return docs

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pr_url(url: str) -> tuple[str, str, str]:
        """Extract (owner, repo, pr_number) from a GitHub PR URL."""
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        # Expected: ['owner', 'repo', 'pull', 'number']
        if len(parts) < 4 or parts[2] != "pull":
            raise ValueError(
                f"Invalid GitHub PR URL: {url}. "
                "Expected format: https://github.com/owner/repo/pull/NNN"
            )
        return parts[0], parts[1], parts[3]

    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=self.config.github_timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(self, url: str) -> list:
        """Fetch all pages of a GitHub list endpoint."""
        results = []
        page = 1
        while True:
            resp = self._session.get(
                url,
                params={"per_page": 100, "page": page},
                timeout=self.config.github_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    def _fetch_raw_file(
        self, owner: str, repo: str, filename: str, pr_meta: dict
    ) -> Optional[str]:
        """Try to fetch the full raw file content from the PR head branch."""
        try:
            head_sha = pr_meta.get("head", {}).get("sha", "")
            if not head_sha:
                return None
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo}"
                f"/{head_sha}/{filename}"
            )
            resp = self._session.get(url, timeout=self.config.github_timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception as exc:
            logger.debug("Could not fetch raw file %s: %s", filename, exc)
        return None

    def _is_relevant_extension(self, filename: str) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in self.config.local_extensions or ext in {".adoc", ".md", ".txt"}

    @staticmethod
    def _read_file(path: Path) -> str:
        """Read a file as text, falling back to latin-1 if UTF-8 fails."""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1")
