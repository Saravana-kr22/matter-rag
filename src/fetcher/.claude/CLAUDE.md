# Fetcher Module

## Purpose
Retrieve raw document content from pluggable, configurable sources and return `FetchedDocument` objects for the processor and loader stages.
Sources are defined in `sources.json` (project root) — no code changes needed to add/remove sources.

## Architecture

```
sources.json
    │
    ▼
fetcher_registry.py  (load_sources + create_fetcher factory)
    │
    ├── sources/github_pr_fetcher.py       (type: "github_pr")
    ├── sources/github_repo_fetcher.py     (type: "github_repo")
    ├── sources/github_tag_diff_fetcher.py (type: "github_tag_diff")
    ├── sources/local_folder_fetcher.py    (type: "local_folder")
    ├── sources/url_fetcher.py             (type: "url")
    ├── sources/csv_fetcher.py             (type: "csv")
    └── sources/matter_xml_fetcher.py      (type: "matter_xml")

spec_diff_builder.py  (standalone — called by run_ghpr_analysis.py for --pr-url mode)
docker_base.py        (standalone — pull Docker image + extract pre-built data)
```

Legacy fallback: `document_fetcher.py::DocumentFetcher` (used when `sources.json` is absent)

## Files

| File | Class(es) | Role |
|---|---|---|
| `base_fetcher.py` | `FetchedDocument`, `BaseFetcher`, `resolve_config_vars()` | Core types + ABC |
| `fetcher_registry.py` | `REGISTRY`, `create_fetcher()`, `load_sources()` | Factory + registry |
| `sources/github_pr_fetcher.py` | `GitHubPRFetcher` | GitHub PR unified diff |
| `sources/github_repo_fetcher.py` | `GitHubRepoFetcher` | Download full files from a repo path; saves locally |
| `sources/github_tag_diff_fetcher.py` | `GitHubTagDiffFetcher` | GitHub compare API (PR vs tag) |
| `sources/local_folder_fetcher.py` | `LocalFolderFetcher` | Recursive local directory walk |
| `sources/url_fetcher.py` | `URLFetcher` | Generic HTTP/HTTPS URL with HTML stripping |
| `sources/csv_fetcher.py` | `CSVFetcher` | CSV rows → prose FetchedDocument |
| `sources/matter_xml_fetcher.py` | `MatterXMLFetcher` | Matter DM XML files → structured schema FetchedDocument |
| `document_fetcher.py` | `DocumentFetcher` | Legacy fallback; re-exports `FetchedDocument` |
| `spec_diff_builder.py` | `build_spec_diff()` | Generate diff HTML from a spec PR via Docker make; patches Makefile and uses background watcher |
| `docker_base.py` | `extract_docker_base()` | Pull Docker image, extract pre-built KG/FAISS/DM XMLs to local `data/` |

## Key Types

### FetchedDocument
```python
@dataclass
class FetchedDocument:
    path: str        # relative path or URL fragment
    content: str     # raw text content (or human-readable summary for XML/binary)
    metadata: dict   # source, absolute_path, pr_url, status, schema, _process_rules, …
    extension: str   # computed property: Path(path).suffix.lower()
```

`metadata["_process_rules"]` — list of per-source rule dicts; read by `process_documents_node`.
`metadata["schema"]` — structured dict for DM XML docs; used by `build_knowledge_graph_node`.

### BaseFetcher (ABC)
```python
class BaseFetcher(ABC):
    @classmethod @abstractmethod
    def source_type(cls) -> str: ...          # "github_pr", "matter_xml", etc.
    @abstractmethod
    def fetch(self) -> List[FetchedDocument]: ...
    @classmethod @abstractmethod
    def from_config(cls, source_cfg: dict, app_cfg: AppConfig) -> "BaseFetcher": ...
```

## Registry

```python
REGISTRY = {
    "github_pr":       GitHubPRFetcher,
    "github_repo":     GitHubRepoFetcher,
    "github_tag_diff": GitHubTagDiffFetcher,
    "local_folder":    LocalFolderFetcher,
    "url":             URLFetcher,
    "csv":             CSVFetcher,
    "matter_xml":      MatterXMLFetcher,
}
```

## Role Routing in `fetch_documents_node`

| `role` | State field | Used by |
|---|---|---|
| `"pr"` | `pr_documents` | PR diff analysis |
| `"test_plan"` (default) | `test_plan_fetched` | Test plan vector DB + KG |
| `"spec"` | `spec_fetched` | Spec REQUIREMENT/BEHAVIOR_RULE nodes in KG |
| `"data_model"` | `data_model_fetched` | DM XML schema ingest into KG |
| `"test_plans_adoc_folder"` | `test_plan_adoc_sources` | Raw adoc files used by `write_updated_testplan_node` to write back LLM-suggested TC changes |

## `sources.json` Format

```json
{
  "sources": [
    {"id": "pr_changes",        "type": "github_pr",    "role": "pr",
     "pr_url": "${PR_URL}", "token": "${GITHUB_TOKEN}", "extensions": [".adoc", ".md"]},
    {"id": "test_plans_local",  "type": "local_folder", "role": "test_plan",
     "path": "data/raw/test_plans"},
    {"id": "matter_spec",       "type": "local_folder", "role": "spec",
     "path": "data/matter_spec", "extensions": [".html", ".htm", ".adoc"]},
    {"id": "matter_data_model", "type": "matter_xml",   "role": "data_model",
     "path": "data/data_model"}
  ]
}
```

## Source Type Details

### github_pr
Config: `pr_url`, `token`, `api_url`, `timeout`, `max_retries`, `extensions`.
Content = unified diff patch; falls back to raw file on `raw.githubusercontent.com`.

### github_repo
Config: `repo` (owner/repo), `path` (sub-path), `ref`, `token`, `extensions`, `local_save_dir`.
Downloads full files from a repo tree. Sets `metadata["absolute_path"]` for each saved file.

### github_tag_diff
Config: `repo`, `pr_number`, `base_tag`, `token`, `api_url`, `timeout`.
Calls `GET /repos/{owner}/{repo}/compare/{base_tag}...{head_sha}`.

### local_folder
Config: `path`, `extensions`.
Recursively walks with `Path.rglob("*")`; reads UTF-8 (latin-1 fallback).
Sets `metadata["absolute_path"]` for `PDFLoader`.

### url
Config: `url`, `format` (`html`|`raw_html`|`matter_diff`|`text`|auto), `timeout`, `headers`.
`raw_html` preserves HTML markup; `matter_diff` triggers `ProcessMatterHtmlDoc` expansion.

### csv
Config: `path`, `columns` (optional), `row_delimiter` (default `" | "`).
Each row → one `FetchedDocument` with prose `"col: val | col2: val2 …"`.

### matter_xml
Config: `path` (directory of DM XML files), `metadata` (optional extra metadata).
Parses CSA DevX XML format. Each cluster → one `FetchedDocument` with:
- `content` = human-readable text summary (for embeddings)
- `metadata["schema"]` = `{"cluster_name", "cluster_id", "revision", "attributes", "commands", "events", "features"}`

Handles: single-cluster files, multi-cluster files, namespace-prefixed tags.
Set `role: "data_model"` in sources.json; rebuild with `--build-data-model`.

## spec_diff_builder.py Implementation Details

`build_spec_diff()` generates diff HTML from a spec PR via Docker make. Key behaviors:

- **Makefile patching**: Downgrades `--failure-level=INFO` to `--failure-level=WARNING` in the
  top-level Makefile so that unrelated broken cross-references (asciidoctor INFO messages) do
  not abort the build.
- **Background watcher**: The diff target creates `build/base/` with its own Makefile from the
  base commit. A wrapper shell script runs a background watcher process that polls for
  `build/base/Makefile` to appear and patches it in-place (same INFO to WARNING downgrade).
  The watcher polls every 0.5s for up to 60s, then exits.
- **Timestamped console logging**: Adds a `StreamHandler` with `%(asctime)s` format to the
  module logger so users see timestamps during long-running build steps (15-30 minutes).
- **Cleanup**: The original Makefile is restored after the build, and the wrapper script is
  deleted regardless of success or failure.

## docker_base.py Implementation Details

`extract_docker_base()` pulls a Docker image and extracts pre-built pipeline data
(KG, FAISS index, DM XMLs, spec HTML, manifest) into a local `data/` directory.

- **Container paths**: Extracts from `/data/` inside the container (e.g. `/data/knowledge_graph`,
  `/data/faiss_index`, `/data/data_model`, `/data/matter_spec`, `/data/test_plans`,
  `/data/manifest.json`).
- **Nesting prevention**: Before `docker cp`, removes existing destination directories
  (`shutil.rmtree`) or files (`unlink`) to prevent Docker from copying into an existing
  directory (which would create `dir/dir/` nesting).
- **Cleanup**: The temporary container is always removed (`docker rm`) in a `finally` block.

## Authentication
Set `GITHUB_TOKEN` env var. Referenced as `${GITHUB_TOKEN}` in `sources.json`.
Without a token, GitHub API rate-limit is 60 req/hour (unauthenticated).
