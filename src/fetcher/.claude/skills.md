# Fetcher Module — Skills

## Fetch using sources.json (recommended)
```python
from src.fetcher.fetcher_registry import load_sources, create_fetcher
from src.config.config_loader import load_config

cfg = load_config()
sources = load_sources("sources.json")  # returns [] if file absent
for src in sources:
    fetcher = create_fetcher(src, cfg)
    docs = fetcher.fetch()
    print(src["id"], len(docs))
```

## Fetch a GitHub PR directly
```python
from src.fetcher.sources.github_pr_fetcher import GitHubPRFetcher

fetcher = GitHubPRFetcher.from_config({
    "pr_url": "https://github.com/project-chip/connectedhomeip/pull/1234",
    "token": "ghp_...",
    "extensions": [".adoc", ".md", ".txt"],
}, cfg)
docs = fetcher.fetch()
for d in docs:
    print(d.path, d.extension, len(d.content))
```

## Fetch a local directory
```python
from src.fetcher.sources.local_folder_fetcher import LocalFolderFetcher

fetcher = LocalFolderFetcher.from_config({"path": "data/raw/test_plans"}, cfg)
docs = fetcher.fetch()
# metadata["absolute_path"] is set for each file (needed by PDFLoader)
```

## Fetch a URL (Quip page, web doc)
```python
from src.fetcher.sources.url_fetcher import URLFetcher

fetcher = URLFetcher.from_config({"url": "https://example.com/spec.html"}, cfg)
docs = fetcher.fetch()  # HTML tags auto-stripped
```

## Fetch a CSV file
```python
from src.fetcher.sources.csv_fetcher import CSVFetcher

fetcher = CSVFetcher.from_config({"path": "data/raw/requirements.csv"}, cfg)
docs = fetcher.fetch()  # each row → one FetchedDocument with prose content
```

## Create a FetchedDocument manually (tests / scripts)
```python
from src.fetcher.base_fetcher import FetchedDocument

doc = FetchedDocument(
    path="spec.adoc",
    content="== TC-OO-2.1\n\n=== Purpose\nVerify OnOff.",
    metadata={"source": "local"},
)
print(doc.extension)  # ".adoc"
```

## Check ${VAR} substitution
```python
from src.fetcher.fetcher_registry import load_sources
import os
os.environ["PR_URL"] = "https://github.com/project-chip/connectedhomeip/pull/1234"
sources = load_sources("sources.json")
print(sources[0]["pr_url"])  # full URL, not ${PR_URL}
```

## Legacy fallback (no sources.json)
```python
from src.fetcher.document_fetcher import DocumentFetcher
from src.config.config_loader import load_config

cfg = load_config()
fetcher = DocumentFetcher(cfg.fetcher)
docs = fetcher.fetch_pr("https://github.com/.../pull/1234")
local_docs = fetcher.fetch_local("data/raw/test_plans/")
```

## Filter by extension
Per-source extensions come from the `extensions` key in `sources.json`.
To add `.xml` support for a source, update its entry:
```json
{ "id": "pr_changes", "type": "github_pr", "extensions": [".adoc", ".pdf", ".xml"] }
```
