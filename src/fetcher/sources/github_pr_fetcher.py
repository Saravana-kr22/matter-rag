"""GitHub PR fetcher — fetches changed files from a GitHub pull request."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)

_DEFAULT_EXTENSIONS = {".adoc", ".md", ".txt", ".csv", ".pdf"}


class GitHubPRFetcher(BaseFetcher):
    """Fetch all changed files from a GitHub pull request (unified diff)."""

    def __init__(
        self,
        pr_url: str,
        token: str = "",
        api_url: str = "https://api.github.com",
        timeout: int = 60,
        max_retries: int = 3,
        extensions: Optional[List[str]] = None,
        process_rules: Optional[List[dict]] = None,
    ) -> None:
        self._pr_url = pr_url
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._extensions = set(extensions or _DEFAULT_EXTENSIONS)
        self._process_rules = process_rules or []
        self._session = _build_session(token, max_retries)

    @classmethod
    def source_type(cls) -> str:
        return "github_pr"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "GitHubPRFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            pr_url=cfg.get("pr_url", ""),
            token=cfg.get("token", app_cfg.fetcher.github_token),
            api_url=cfg.get("api_url", app_cfg.fetcher.github_api_url),
            timeout=int(cfg.get("timeout", app_cfg.fetcher.github_timeout)),
            max_retries=int(cfg.get("max_retries", app_cfg.fetcher.github_max_retries)),
            extensions=cfg.get("extensions"),
            process_rules=cfg.get("process_rules", []),
        )

    def fetch(self) -> List[FetchedDocument]:
        from urllib.parse import urlparse
        if not self._pr_url or self._pr_url.strip().lower() in ("", "none"):
            logger.debug("[GitHubPRFetcher] pr_url is empty — skipping")
            return []
        parsed = urlparse(self._pr_url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 4 or parts[2] != "pull":
            raise ValueError(f"Invalid GitHub PR URL: {self._pr_url}")
        owner, repo, pr_number = parts[0], parts[1], parts[3]

        logger.info("[GitHubPRFetcher] Fetching PR #%s from %s/%s", pr_number, owner, repo)

        pr_meta = self._get_json(f"{self._api_url}/repos/{owner}/{repo}/pulls/{pr_number}")
        files = self._get_paginated(f"{self._api_url}/repos/{owner}/{repo}/pulls/{pr_number}/files")

        docs: List[FetchedDocument] = []
        for file_info in files:
            filename = file_info.get("filename", "")
            if Path(filename).suffix.lower() not in self._extensions:
                continue
            patch = file_info.get("patch", "")
            status = file_info.get("status", "modified")
            content = patch or self._fetch_raw(owner, repo, filename, pr_meta) or ""
            docs.append(FetchedDocument(
                path=filename,
                content=content,
                metadata={
                    "source": "github_pr",
                    "source_id": "github_pr",
                    "pr_url": self._pr_url,
                    "pr_number": pr_number,
                    "repo": f"{owner}/{repo}",
                    "status": status,
                    "additions": file_info.get("additions", 0),
                    "deletions": file_info.get("deletions", 0),
                    "_process_rules": self._process_rules,
                },
            ))

        logger.info("[GitHubPRFetcher] Fetched %d relevant files from PR #%s", len(docs), pr_number)
        return docs

    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(self, url: str) -> list:
        results, page = [], 1
        while True:
            resp = self._session.get(url, params={"per_page": 100, "page": page}, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    def _fetch_raw(self, owner: str, repo: str, filename: str, pr_meta: dict) -> Optional[str]:
        try:
            head_sha = pr_meta.get("head", {}).get("sha", "")
            if not head_sha:
                return None
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{head_sha}/{filename}"
            resp = self._session.get(url, timeout=self._timeout)
            return resp.text if resp.status_code == 200 else None
        except Exception as exc:
            logger.debug("[GitHubPRFetcher] raw fetch failed for %s: %s", filename, exc)
            return None


def _build_session(token: str, max_retries: int) -> requests.Session:
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"Bearer {token}",
                                 "Accept": "application/vnd.github.v3+json"})
    retry = Retry(total=max_retries, backoff_factor=2,
                  status_forcelist={500, 502, 503, 504},
                  allowed_methods={"GET"}, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
