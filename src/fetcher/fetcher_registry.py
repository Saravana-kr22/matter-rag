"""Fetcher registry — maps source type strings to BaseFetcher subclasses."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List

from src.config.config_loader import AppConfig
from src.fetcher.base_fetcher import BaseFetcher, FetchedDocument  # noqa: F401
from src.fetcher.sources.csv_fetcher import CSVFetcher
from src.fetcher.sources.github_pr_fetcher import GitHubPRFetcher
from src.fetcher.sources.github_repo_fetcher import GitHubRepoFetcher
from src.fetcher.sources.github_tag_diff_fetcher import GitHubTagDiffFetcher
from src.fetcher.sources.local_folder_fetcher import LocalFolderFetcher
from src.fetcher.sources.matter_xml_fetcher import MatterXMLFetcher
from src.fetcher.sources.url_fetcher import URLFetcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry — add new fetcher classes here, no other file needs to change
# ---------------------------------------------------------------------------

REGISTRY: dict[str, type[BaseFetcher]] = {
    GitHubPRFetcher.source_type():       GitHubPRFetcher,
    GitHubRepoFetcher.source_type():     GitHubRepoFetcher,
    GitHubTagDiffFetcher.source_type():  GitHubTagDiffFetcher,
    LocalFolderFetcher.source_type():    LocalFolderFetcher,
    MatterXMLFetcher.source_type():      MatterXMLFetcher,
    URLFetcher.source_type():            URLFetcher,
    CSVFetcher.source_type():            CSVFetcher,
}


def create_fetcher(source_cfg: dict, app_cfg: AppConfig) -> BaseFetcher:
    """Instantiate a BaseFetcher from a sources.json entry.

    Args:
        source_cfg: One entry from the ``sources`` list in sources.json.
                    Must have a ``type`` field matching a key in REGISTRY.
        app_cfg:    Global AppConfig for fallback values (tokens, timeouts, etc.)

    Raises:
        ValueError: If ``type`` is unknown.
    """
    t = source_cfg.get("type", "")
    cls = REGISTRY.get(t)
    if cls is None:
        raise ValueError(
            f"Unknown source type '{t}'. "
            f"Registered types: {sorted(REGISTRY)}"
        )
    logger.debug("Creating fetcher: type=%s id=%s", t, source_cfg.get("id", "?"))
    return cls.from_config(source_cfg, app_cfg)


def load_sources(path: str = "sources.json") -> List[dict]:
    """Load the sources list from a sources.json file.

    Returns [] (empty list) if the file does not exist — callers treat this
    as a signal to fall back to legacy CLI-arg behaviour.

    ${VAR} substitution in string values is handled inside each fetcher's
    ``from_config()`` via ``resolve_config_vars()``.
    """
    p = Path(path)
    if not p.exists():
        logger.debug("sources.json not found at %s — using legacy fetch mode", p.resolve())
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sources = data.get("sources", [])
        logger.info("Loaded %d source(s) from %s", len(sources), p)
        return sources
    except Exception as exc:
        logger.warning("Failed to load sources.json: %s", exc)
        return []
