"""Tests for document fetcher (local directory only — no network)."""
import textwrap
from pathlib import Path

import pytest

from src.config.config_loader import FetcherConfig
from src.fetcher.document_fetcher import DocumentFetcher, FetchedDocument


@pytest.fixture
def fetcher():
    return DocumentFetcher(FetcherConfig())


def test_fetch_local_finds_files(fetcher, tmp_path):
    (tmp_path / "plan.adoc").write_text("== Test Case\nContent here.")
    (tmp_path / "data.csv").write_text("id,name\n1,foo")
    (tmp_path / "skip.xyz").write_text("irrelevant")
    docs = fetcher.fetch_local(tmp_path)
    paths = [d.path for d in docs]
    assert any("plan.adoc" in p for p in paths)
    assert any("data.csv" in p for p in paths)
    assert not any("skip.xyz" in p for p in paths)


def test_fetch_local_recursive(fetcher, tmp_path):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "nested.md").write_text("# Nested doc")
    docs = fetcher.fetch_local(tmp_path)
    assert any("nested.md" in d.path for d in docs)


def test_fetch_local_missing_dir(fetcher):
    with pytest.raises(FileNotFoundError):
        fetcher.fetch_local("/no/such/directory")


def test_parse_pr_url():
    owner, repo, num = DocumentFetcher._parse_pr_url(
        "https://github.com/project-chip/connectedhomeip/pull/1234"
    )
    assert owner == "project-chip"
    assert repo == "connectedhomeip"
    assert num == "1234"


def test_parse_pr_url_invalid():
    with pytest.raises(ValueError):
        DocumentFetcher._parse_pr_url("https://github.com/owner/repo/issues/1")
