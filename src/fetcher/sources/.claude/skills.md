# Fetcher Sources — Skills

## Fetch PR diff files
```python
from src.fetcher.sources.github_pr_fetcher import GitHubPRFetcher
from src.config.config_loader import load_config

cfg = load_config()
fetcher = GitHubPRFetcher.from_config({
    "pr_url": "https://github.com/project-chip/connectedhomeip/pull/1234",
    "token": "ghp_...",
    "extensions": [".adoc", ".md"],
}, cfg)
docs = fetcher.fetch()
for d in docs:
    print(d.path, d.metadata["status"])  # "added" | "modified" | "removed"
```

## Download test plan files from GitHub repo
```python
from src.fetcher.sources.github_repo_fetcher import GitHubRepoFetcher

fetcher = GitHubRepoFetcher.from_config({
    "repo": "project-chip/connectedhomeip",
    "path": "src/app/tests/suites/certification",
    "ref": "master",
    "token": "ghp_...",
    "extensions": [".adoc"],
    "local_save_dir": "data/raw/test_plans",
}, cfg)
docs = fetcher.fetch()
# files saved to data/raw/test_plans/; metadata["absolute_path"] set
```

## Fetch local folder
```python
from src.fetcher.sources.local_folder_fetcher import LocalFolderFetcher

fetcher = LocalFolderFetcher.from_config({"path": "data/raw/test_plans"}, cfg)
docs = fetcher.fetch()
# metadata["absolute_path"] is set — required for PDFLoader
```

## Fetch a URL (HTML auto-stripped)
```python
from src.fetcher.sources.url_fetcher import URLFetcher

fetcher = URLFetcher.from_config({"url": "https://example.com/spec.html"}, cfg)
docs = fetcher.fetch()
print(docs[0].content[:200])  # plain text, tags removed
```

## Fetch CSV rows as prose documents
```python
from src.fetcher.sources.csv_fetcher import CSVFetcher

fetcher = CSVFetcher.from_config({
    "path": "data/raw/requirements.csv",
    "columns": ["id", "description", "priority"],
    "row_delimiter": " | ",
}, cfg)
docs = fetcher.fetch()
# each row: "id: REQ-001 | description: The system shall ... | priority: P1"
```

## Fetch compare diff (PR vs tag)
```python
from src.fetcher.sources.github_tag_diff_fetcher import GitHubTagDiffFetcher

fetcher = GitHubTagDiffFetcher.from_config({
    "repo": "project-chip/connectedhomeip",
    "pr_number": "1234",
    "base_tag": "v1.3.0",
    "token": "ghp_...",
}, cfg)
docs = fetcher.fetch()
```

## Add a new source type
1. Create `src/fetcher/sources/<name>_fetcher.py` subclassing `BaseFetcher`
2. Implement `source_type()`, `fetch()`, `from_config()`
3. Call `resolve_config_vars(source_cfg)` in `from_config()` before reading values
4. Register in `REGISTRY` in `src/fetcher/fetcher_registry.py`
5. Add entry to `sources.json` with `"type": "<name>"`
