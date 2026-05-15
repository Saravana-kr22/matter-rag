"""GitHub tag-diff fetcher — diffs a PR head against a base tag using the compare API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument, resolve_config_vars

logger = logging.getLogger(__name__)

_DEFAULT_EXTENSIONS = {".adoc", ".md", ".txt", ".csv", ".pdf"}


class GitHubTagDiffFetcher(BaseFetcher):
    """Fetch files changed between a base tag and the head of a PR.

    Uses the GitHub compare API:
        GET /repos/{owner}/{repo}/compare/{base_tag}...{head_sha}

    This shows ALL changes since the tag, not just the immediate PR diff — equivalent
    to ``git diff v1.3.0...HEAD``.
    """

    def __init__(
        self,
        repo: str,
        pr_number: str,
        base_tag: str,
        token: str = "",
        api_url: str = "https://api.github.com",
        timeout: int = 60,
        max_retries: int = 3,
        extensions: Optional[List[str]] = None,
        process_rules: Optional[List[dict]] = None,
    ) -> None:
        self._repo = repo          # "owner/repo"
        self._pr_number = pr_number
        self._base_tag = base_tag  # tag name or commit SHA
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._extensions = set(extensions or _DEFAULT_EXTENSIONS)
        self._process_rules = process_rules or []
        self._session = _build_session(token, max_retries)

    @classmethod
    def source_type(cls) -> str:
        return "github_tag_diff"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "GitHubTagDiffFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            repo=cfg.get("repo", app_cfg.fetcher.default_repo),
            pr_number=str(cfg.get("pr_number", "")),
            base_tag=cfg.get("base_tag", ""),
            token=cfg.get("token", app_cfg.fetcher.github_token),
            api_url=cfg.get("api_url", app_cfg.fetcher.github_api_url),
            timeout=int(cfg.get("timeout", app_cfg.fetcher.github_timeout)),
            max_retries=int(cfg.get("max_retries", app_cfg.fetcher.github_max_retries)),
            extensions=cfg.get("extensions"),
            process_rules=cfg.get("process_rules", []),
        )

    def fetch(self) -> List[FetchedDocument]:
        owner, repo = self._repo.split("/", 1)

        # Resolve head SHA from PR number
        pr_meta = self._get_json(f"{self._api_url}/repos/{owner}/{repo}/pulls/{self._pr_number}")
        head_sha = pr_meta.get("head", {}).get("sha", "")
        if not head_sha:
            raise ValueError(f"Could not resolve head SHA for PR #{self._pr_number}")

        logger.info(
            "[GitHubTagDiffFetcher] Comparing %s...%s in %s",
            self._base_tag, head_sha[:8], self._repo,
        )

        compare_url = f"{self._api_url}/repos/{owner}/{repo}/compare/{self._base_tag}...{head_sha}"
        # compare API is paginated via 'page' param
        files = self._get_compare_files(compare_url)

        docs: List[FetchedDocument] = []
        for file_info in files:
            filename = file_info.get("filename", "")
            if Path(filename).suffix.lower() not in self._extensions:
                continue
            patch = file_info.get("patch", "")
            content = patch or self._fetch_raw(owner, repo, filename, head_sha) or ""
            docs.append(FetchedDocument(
                path=filename,
                content=content,
                metadata={
                    "source": "github_tag_diff",
                    "source_id": "github_tag_diff",
                    "repo": self._repo,
                    "pr_number": self._pr_number,
                    "base_tag": self._base_tag,
                    "head_sha": head_sha,
                    "status": file_info.get("status", "modified"),
                    "additions": file_info.get("additions", 0),
                    "deletions": file_info.get("deletions", 0),
                    "_process_rules": self._process_rules,
                },
            ))

        logger.info(
            "[GitHubTagDiffFetcher] %d relevant files changed since %s",
            len(docs), self._base_tag,
        )
        return docs

    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_compare_files(self, base_url: str) -> list:
        """Paginate through all files in a compare response."""
        results, page = [], 1
        while True:
            resp = self._session.get(base_url, params={"page": page, "per_page": 100},
                                     timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("files", [])
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def _fetch_raw(self, owner: str, repo: str, filename: str, sha: str) -> Optional[str]:
        try:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{filename}"
            resp = self._session.get(url, timeout=self._timeout)
            return resp.text if resp.status_code == 200 else None
        except Exception as exc:
            logger.debug("[GitHubTagDiffFetcher] raw fetch failed for %s: %s", filename, exc)
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
