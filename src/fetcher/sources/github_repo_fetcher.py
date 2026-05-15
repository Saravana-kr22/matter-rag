"""GitHub repository content fetcher — downloads raw files from a repo path and saves locally.

Downloads files from a GitHub repository directory using the Trees API, saves them flat
into a local directory, and returns FetchedDocument objects with ``absolute_path`` metadata
pointing to the saved local files.  This allows downstream stages to write back updated
``_matter_ai_rag_update.adoc`` files alongside the originals.
"""

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

_DEFAULT_EXTENSIONS = {".adoc"}


class GitHubRepoFetcher(BaseFetcher):
    """Download raw files from a GitHub repository path and save them locally.

    Uses the GitHub Trees API to enumerate files recursively, then downloads
    each matching file via ``raw.githubusercontent.com`` (falling back to the
    Git Blobs API for large files).  Files are saved flat into ``local_save_dir``
    using only their basename so ``LocalFolderFetcher`` can also read them.

    Returns ``FetchedDocument`` objects with:
    - ``path``  — repo-relative path (e.g. ``src/app/tests/suites/TC_OO.adoc``)
    - ``metadata["absolute_path"]`` — resolved path to the saved local copy
    - ``metadata["repo"]``, ``metadata["ref"]``, ``metadata["repo_path"]``

    Config keys (``sources.json``):

    .. code-block:: json

        {
          "id": "test_plans_github",
          "type": "github_repo",
          "role": "test_plan",
          "repo": "project-chip/connectedhomeip",
          "path": "src/app/tests/suites/certification",
          "ref": "master",
          "token": "${GITHUB_TOKEN}",
          "extensions": [".adoc"],
          "local_save_dir": "data/raw/test_plans"
        }
    """

    def __init__(
        self,
        repo: str,
        path: str = "",
        ref: str = "master",
        token: str = "",
        local_save_dir: str = "data/raw/test_plans",
        extensions: Optional[List[str]] = None,
        api_url: str = "https://api.github.com",
        timeout: int = 60,
        max_retries: int = 3,
        process_rules: Optional[List[dict]] = None,
    ) -> None:
        self._repo = repo
        self._path = path.strip("/")
        self._ref = ref
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._extensions = set(extensions or _DEFAULT_EXTENSIONS)
        self._local_save_dir = Path(local_save_dir)
        self._process_rules = process_rules or []
        self._session = _build_session(token, max_retries)

    @classmethod
    def source_type(cls) -> str:
        return "github_repo"

    @classmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "GitHubRepoFetcher":
        cfg = resolve_config_vars(source_cfg)
        return cls(
            repo=cfg.get("repo", app_cfg.fetcher.default_repo),
            path=cfg.get("path", ""),
            ref=cfg.get("ref", "master"),
            token=cfg.get("token", app_cfg.fetcher.github_token),
            local_save_dir=cfg.get("local_save_dir", "data/raw/test_plans"),
            extensions=cfg.get("extensions"),
            api_url=cfg.get("api_url", app_cfg.fetcher.github_api_url),
            timeout=int(cfg.get("timeout", app_cfg.fetcher.github_timeout)),
            max_retries=int(cfg.get("max_retries", app_cfg.fetcher.github_max_retries)),
            process_rules=cfg.get("process_rules", []),
        )

    def fetch(self) -> List[FetchedDocument]:
        self._local_save_dir.mkdir(parents=True, exist_ok=True)

        tree_sha = self._resolve_tree_sha()
        if not tree_sha:
            raise RuntimeError(
                f"[GitHubRepoFetcher] Could not resolve ref '{self._ref}' for {self._repo}"
            )

        tree_url = (
            f"{self._api_url}/repos/{self._repo}/git/trees/{tree_sha}?recursive=1"
        )
        tree_data = self._get_json(tree_url)
        tree_items = tree_data.get("tree", [])

        docs: List[FetchedDocument] = []
        for item in tree_items:
            if item.get("type") != "blob":
                continue
            item_path = item.get("path", "")
            if self._path and not item_path.startswith(self._path):
                continue
            if Path(item_path).suffix.lower() not in self._extensions:
                continue

            content = self._download_file(item_path, item.get("sha", ""))
            if content is None:
                logger.warning("[GitHubRepoFetcher] Skipping %s — download failed", item_path)
                continue

            local_file = self._save_locally(item_path, content)
            docs.append(FetchedDocument(
                path=item_path,
                content=content,
                metadata={
                    "source": "github_repo",
                    "source_id": "github_repo",
                    "repo": self._repo,
                    "ref": self._ref,
                    "repo_path": item_path,
                    "absolute_path": str(local_file.resolve()),
                    "file_size": len(content.encode("utf-8")),
                    "_process_rules": self._process_rules,
                },
            ))

        logger.info(
            "[GitHubRepoFetcher] Fetched %d file(s) from %s/%s@%s → saved to %s",
            len(docs), self._repo, self._path or "<root>", self._ref, self._local_save_dir,
        )
        return docs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_tree_sha(self) -> Optional[str]:
        """Resolve the configured ref to a git tree SHA via the commits API."""
        try:
            url = f"{self._api_url}/repos/{self._repo}/commits/{self._ref}"
            data = self._get_json(url)
            return data.get("commit", {}).get("tree", {}).get("sha")
        except Exception as exc:
            logger.debug("[GitHubRepoFetcher] ref resolution failed: %s", exc)
            return None

    def _download_file(self, repo_path: str, sha: str) -> Optional[str]:
        """Download file content — raw URL first, Git Blobs API as fallback."""
        owner, repo_name = self._repo.split("/", 1)
        try:
            raw_url = (
                f"https://raw.githubusercontent.com/{owner}/{repo_name}/{self._ref}/{repo_path}"
            )
            resp = self._session.get(raw_url, timeout=self._timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception as exc:
            logger.debug("[GitHubRepoFetcher] raw download failed for %s: %s", repo_path, exc)

        # Fallback: Git Blobs API returns base64-encoded content
        try:
            import base64
            blob_url = f"{self._api_url}/repos/{self._repo}/git/blobs/{sha}"
            blob = self._get_json(blob_url)
            if blob.get("encoding") == "base64":
                return base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("[GitHubRepoFetcher] blob fallback failed for %s: %s", repo_path, exc)

        return None

    def _save_locally(self, repo_path: str, content: str) -> Path:
        """Save content using only the basename so the directory stays flat."""
        local_file = self._local_save_dir / Path(repo_path).name
        local_file.write_text(content, encoding="utf-8")
        return local_file

    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()


def _build_session(token: str, max_retries: int) -> requests.Session:
    session = requests.Session()
    if token:
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        })
    retry = Retry(
        total=max_retries,
        backoff_factor=2,
        status_forcelist={500, 502, 503, 504},
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
