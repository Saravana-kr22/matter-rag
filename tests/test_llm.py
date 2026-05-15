"""Tests for LLM provider factory."""
import pytest
from unittest.mock import MagicMock, patch

from src.config.config_loader import LLMConfig
from src.llm.llm_provider import get_llm, ClaudeProvider, OllamaProvider


def test_get_llm_claude():
    config = LLMConfig(provider="claude_cli")
    with patch("anthropic.Anthropic", return_value=MagicMock()):
        provider = get_llm(config)
    assert isinstance(provider, ClaudeProvider)


def test_get_llm_local():
    config = LLMConfig(provider="local")
    with patch.dict("sys.modules", {"ollama": MagicMock()}):
        provider = get_llm(config)
    assert isinstance(provider, OllamaProvider)


def test_get_llm_invalid():
    config = LLMConfig(provider="unknown_provider")
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_llm(config)
