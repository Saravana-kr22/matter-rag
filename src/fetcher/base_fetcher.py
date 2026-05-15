"""Base fetcher — FetchedDocument dataclass + BaseFetcher ABC."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from src.config.config_loader import AppConfig


@dataclass
class FetchedDocument:
    """A raw document fetched from any source."""
    path: str                          # relative path or URL fragment
    content: str                       # raw text content
    metadata: dict = field(default_factory=dict)

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.lower()


class BaseFetcher(ABC):
    """Abstract base for all source fetchers.

    To add a new source type:
    1. Subclass BaseFetcher
    2. Implement source_type(), fetch(), from_config()
    3. Register in fetcher_registry.REGISTRY
    """

    @classmethod
    @abstractmethod
    def source_type(cls) -> str:
        """Return the string type key used in sources.json, e.g. 'github_pr'."""
        ...

    @abstractmethod
    def fetch(self) -> List[FetchedDocument]:
        """Fetch documents from this source and return them as FetchedDocument list."""
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "BaseFetcher":
        """Construct an instance from a sources.json entry + global AppConfig."""
        ...


def substitute_env_vars(value: str) -> str:
    """Replace ${VAR} tokens in a string with values from os.environ."""
    import os
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )


def resolve_config_vars(cfg: dict) -> dict:
    """Recursively substitute ${VAR} in all string values of a dict."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, str):
            out[k] = substitute_env_vars(v)
        elif isinstance(v, dict):
            out[k] = resolve_config_vars(v)
        elif isinstance(v, list):
            out[k] = [
                resolve_config_vars(i) if isinstance(i, dict)
                else (substitute_env_vars(i) if isinstance(i, str) else i)
                for i in v
            ]
        else:
            out[k] = v
    return out
