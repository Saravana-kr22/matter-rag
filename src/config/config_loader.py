"""Config loader — reads config.yaml and returns a typed AppConfig object.

Override precedence (highest wins):
  1. ``overrides`` dict passed directly to ``load_config()``
  2. Environment variables prefixed with ``MATTER_RAG__``
     Format: ``MATTER_RAG__<SECTION>__<KEY>=value``
     Example: ``MATTER_RAG__LLM__PROVIDER=local``
             ``MATTER_RAG__PIPELINE__REBUILD_INDEX=true``
  3. Values in the YAML file
  4. Dataclass defaults (src/config/models.py)

.env file:
  A ``.env`` file in the project root (or any parent directory) is loaded
  automatically at the start of ``load_config()``.  Copy ``.env.example``
  to ``.env`` and fill in your secrets.  Variables already set in the shell
  environment take precedence over values in ``.env``.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# Load .env file before anything else reads os.environ.
# override=False means shell env vars always win over .env values.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:  # python-dotenv not installed — silently skip
    pass

from src.config.models import (
    AnalysisConfig,
    AppConfig,
    ChunkerConfig,
    DatabaseConfig,
    EmbeddingsConfig,
    FetcherConfig,
    KnowledgeGraphConfig,
    LLMConfig,
    LoaderConfig,
    LoggingConfig,
    PipelineConfig,
    RerankerConfig,
    SpecRepoConfig,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Env-var prefix:  MATTER_RAG__LLM__PROVIDER  →  {"llm": {"provider": "..."}}
_ENV_PREFIX = "MATTER_RAG__"
_ENV_SEP = "__"

# Section key → dataclass mapping (must match AppConfig field names)
_SECTION_MAP = {
    "llm": LLMConfig,
    "embeddings": EmbeddingsConfig,
    "database": DatabaseConfig,
    "fetcher": FetcherConfig,
    "loader": LoaderConfig,
    "chunker": ChunkerConfig,
    "pipeline": PipelineConfig,
    "knowledge_graph": KnowledgeGraphConfig,
    "reranker": RerankerConfig,
    "logging": LoggingConfig,
    "analysis": AnalysisConfig,
}

# ---------------------------------------------------------------------------
# ${VAR} substitution
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env_vars(value: str) -> str:
    """Replace ``${VAR}`` tokens with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        result = os.environ.get(var_name, "")
        if not result:
            import warnings
            warnings.warn(f"Environment variable '{var_name}' is not set.", stacklevel=3)
        return result

    return _ENV_VAR_RE.sub(replacer, value)


def _resolve_strings(obj: Any) -> Any:
    """Recursively resolve ``${VAR}`` tokens in dicts/lists/strings."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_strings(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict with *override* values merged on top of *base*.

    Nested dicts are merged recursively; all other values are replaced.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Env-var overrides
# ---------------------------------------------------------------------------

def _coerce(value: str) -> Any:
    """Coerce an env-var string to a Python value via YAML parsing.

    This handles the common cases automatically:
    - ``"7"``     → ``7``       (int)
    - ``"3.14"``  → ``3.14``    (float)
    - ``"true"``  → ``True``    (bool)
    - ``"false"`` → ``False``   (bool)
    - ``"local"`` → ``"local"`` (str)
    - ``"[.adoc, .pdf]"`` → ``['.adoc', '.pdf']`` (list)
    """
    try:
        return yaml.safe_load(value)
    except Exception:
        return value


def _collect_env_overrides() -> Dict[str, Dict[str, Any]]:
    """Scan environment variables for ``MATTER_RAG__SECTION__KEY`` overrides.

    Returns a nested dict of the same shape as the raw YAML dict, so it can
    be merged with ``_deep_merge``.
    """
    overrides: Dict[str, Dict[str, Any]] = {}
    prefix_len = len(_ENV_PREFIX)

    for env_key, env_val in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        rest = env_key[prefix_len:]
        parts = rest.split(_ENV_SEP, maxsplit=1)
        if len(parts) != 2:
            continue
        section, field = parts[0].lower(), parts[1].lower()

        if section not in _SECTION_MAP:
            continue

        overrides.setdefault(section, {})[field] = _coerce(env_val)

    return overrides


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------

def _build(cls: type, data: dict) -> Any:
    """Populate a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Relative path resolution for overlay configs
# ---------------------------------------------------------------------------

_PATH_FIELD_HINTS = frozenset({
    "dir", "path", "file", "url",
})


def _looks_like_path(key: str, value: str) -> bool:
    """Heuristic: is this config value likely a filesystem path?"""
    if not isinstance(value, str) or not value:
        return False
    # HTTP/HTTPS URLs are never filesystem paths
    if value.startswith("http://") or value.startswith("https://"):
        return False
    key_lower = key.lower()
    if any(hint in key_lower for hint in _PATH_FIELD_HINTS):
        return True
    if value.startswith("./") or value.startswith("../"):
        return True
    return False


def _resolve_relative_paths(obj: Any, base_dir: Path) -> Any:
    """Resolve relative paths in overlay config values relative to base_dir.

    Only resolves string values in keys that look like file/dir paths
    (contain 'dir', 'path', 'file' in the key name, or start with './' or '../').
    Absolute paths and non-path values are left unchanged.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(v, str) and _looks_like_path(k, v):
                p = Path(v)
                if not p.is_absolute():
                    result[k] = str(base_dir / v)
                else:
                    result[k] = v
            elif isinstance(v, list):
                result[k] = [
                    str(base_dir / item) if isinstance(item, str) and not Path(item).is_absolute() and _looks_like_path(k, item)
                    else _resolve_relative_paths(item, base_dir) if isinstance(item, dict) else item
                    for item in v
                ]
            elif isinstance(v, dict):
                result[k] = _resolve_relative_paths(v, base_dir)
            else:
                result[k] = v
        return result
    return obj


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(
    path: str | Path = "config/config.yaml",
    overrides: Optional[Dict[str, Any]] = None,
    additional_config: Optional[str | Path] = None,
) -> AppConfig:
    """Load config.yaml and apply layered overrides.

    Args:
        path: Path to the base YAML config file.
        overrides: Optional nested dict of values that take highest priority,
            e.g. ``{"llm": {"provider": "local"}, "pipeline": {"rebuild_index": True}}``.
        additional_config: Optional path to an overlay YAML config file. Values in
            the overlay are deep-merged on top of the base config. Use this for
            extending the pipeline with additional data sources (DM XMLs, test plans,
            spec sections) without modifying the base config.

    Returns:
        Fully-populated ``AppConfig`` dataclass instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If the YAML is malformed.

    Override precedence (highest wins):
        1. *overrides* argument
        2. ``MATTER_RAG__<SECTION>__<KEY>`` env vars
        3. *additional_config* overlay file
        4. config.yaml values
        5. Dataclass defaults
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")

    with config_path.open("r") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    # Layer 4: ${VAR} substitution in YAML strings
    raw = _resolve_strings(raw)

    # Layer 3: additional config overlay (deep-merged on top of base)
    if additional_config:
        overlay_path = Path(additional_config)
        if overlay_path.exists():
            with overlay_path.open("r") as fh:
                overlay_raw: dict = yaml.safe_load(fh) or {}
            overlay_raw = _resolve_strings(overlay_raw)
            # Resolve relative paths in overlay relative to the overlay file's directory
            overlay_dir = overlay_path.resolve().parent
            overlay_raw = _resolve_relative_paths(overlay_raw, overlay_dir)
            raw = _deep_merge(raw, overlay_raw)

    # Layer 2: env-var overrides
    env_overrides = _collect_env_overrides()
    if env_overrides:
        raw = _deep_merge(raw, env_overrides)

    # Layer 1: explicit overrides argument
    if overrides:
        raw = _deep_merge(raw, overrides)

    return AppConfig(
        llm=_build(LLMConfig, raw.get("llm", {})),
        embeddings=_build(EmbeddingsConfig, raw.get("embeddings", {})),
        database=_build(DatabaseConfig, raw.get("database", {})),
        fetcher=_build(FetcherConfig, raw.get("fetcher", {})),
        loader=_build(LoaderConfig, raw.get("loader", {})),
        chunker=_build(ChunkerConfig, raw.get("chunker", {})),
        pipeline=_build(PipelineConfig, raw.get("pipeline", {})),
        knowledge_graph=_build(KnowledgeGraphConfig, raw.get("knowledge_graph", {})),
        reranker=_build(RerankerConfig, raw.get("reranker", {})),
        logging=_build(LoggingConfig, raw.get("logging", {})),
        analysis=_build(AnalysisConfig, raw.get("analysis", {})),
        spec_repo=_build(SpecRepoConfig, raw.get("spec_repo", {})),
    )
