"""Tests for config loader and models."""
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from src.config.config_loader import load_config
from src.config.models import AppConfig, ChunkerConfig


MINIMAL_YAML = textwrap.dedent("""\
    llm:
      provider: local
      local_model: llama3.2
    embeddings:
      model: BAAI/bge-small-en-v1.5
    """)


def test_load_minimal_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_YAML)
    config = load_config(cfg_file)
    assert isinstance(config, AppConfig)
    assert config.llm.provider == "local"
    assert config.llm.local_model == "llama3.2"
    assert config.embeddings.model == "BAAI/bge-small-en-v1.5"


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-123")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("fetcher:\n  github_token: ${GITHUB_TOKEN}\n")
    config = load_config(cfg_file)
    assert config.fetcher.github_token == "test-token-123"


def test_defaults_applied(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}")
    config = load_config(cfg_file)
    assert config.llm.provider == "claude_cli"
    assert config.pipeline.search_top_k == 10
    assert config.loader.chunk_size == 1000


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_chunker_config_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}")
    config = load_config(cfg_file)
    assert isinstance(config.chunker, ChunkerConfig)
    assert config.chunker.chunker_type == "matter_tc"
    assert config.chunker.chunk_size == 1000
    assert config.chunker.chunk_overlap == 200


def test_chunker_config_from_yaml(tmp_path):
    yaml = textwrap.dedent("""\
        chunker:
          chunker_type: generic
          chunk_size: 500
          chunk_overlap: 50
        """)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml)
    config = load_config(cfg_file)
    assert config.chunker.chunker_type == "generic"
    assert config.chunker.chunk_size == 500
    assert config.chunker.chunk_overlap == 50


def test_overrides_dict(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_YAML)
    config = load_config(cfg_file, overrides={"llm": {"provider": "local", "temperature": 0.9}})
    assert config.llm.provider == "local"
    assert config.llm.temperature == 0.9


def test_overrides_dict_nested(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}")
    config = load_config(cfg_file, overrides={"pipeline": {"rebuild_index": True, "search_top_k": 5}})
    assert config.pipeline.rebuild_index is True
    assert config.pipeline.search_top_k == 5


def test_env_var_override(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}")
    monkeypatch.setenv("MATTER_RAG__LLM__PROVIDER", "local")
    monkeypatch.setenv("MATTER_RAG__PIPELINE__SEARCH_TOP_K", "7")
    config = load_config(cfg_file)
    assert config.llm.provider == "local"
    assert config.pipeline.search_top_k == 7


def test_env_var_bool_coercion(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("{}")
    monkeypatch.setenv("MATTER_RAG__PIPELINE__REBUILD_INDEX", "true")
    config = load_config(cfg_file)
    assert config.pipeline.rebuild_index is True


def test_override_precedence(tmp_path, monkeypatch):
    """overrides dict beats env var beats yaml."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  provider: claude_cli\n")
    monkeypatch.setenv("MATTER_RAG__LLM__PROVIDER", "local")
    config = load_config(cfg_file, overrides={"llm": {"provider": "claude_cli"}})
    assert config.llm.provider == "claude_cli"  # overrides dict wins over env var
    """models.py should be importable without triggering yaml/file I/O."""
    from src.config.models import (
        AppConfig, LLMConfig, EmbeddingsConfig, DatabaseConfig,
        FetcherConfig, LoaderConfig, ChunkerConfig, PipelineConfig,
        KnowledgeGraphConfig, LoggingConfig,
    )
    cfg = AppConfig()
    assert cfg.chunker.chunker_type == "matter_tc"
