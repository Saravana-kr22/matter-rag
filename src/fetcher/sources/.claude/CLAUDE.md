# Fetcher Sources Sub-package

## Purpose
Concrete `BaseFetcher` implementations — one file per source type.
Each class is registered in `src/fetcher/fetcher_registry.py::REGISTRY`.

## Files

| File | Class | `type` key | Description |
|---|---|---|---|
| `github_pr_fetcher.py` | `GitHubPRFetcher` | `"github_pr"` | GitHub PR unified diff |
| `github_repo_fetcher.py` | `GitHubRepoFetcher` | `"github_repo"` | Download full files from a repo path; saves to disk |
| `github_tag_diff_fetcher.py` | `GitHubTagDiffFetcher` | `"github_tag_diff"` | GitHub compare API (base tag → PR head) |
| `local_folder_fetcher.py` | `LocalFolderFetcher` | `"local_folder"` | Recursive local directory walk |
| `url_fetcher.py` | `URLFetcher` | `"url"` | HTTP/HTTPS URL with optional HTML stripping |
| `csv_fetcher.py` | `CSVFetcher` | `"csv"` | CSV rows → one FetchedDocument per row |
| `matter_xml_fetcher.py` | `MatterXMLFetcher` | `"matter_xml"` | Matter DM XML → structured schema FetchedDocument per cluster |
| `zap_xml_adapter.py` | `ZapXMLAdapter` | — | Converts ZAP-format XML to standard DM XML |

## All fetchers implement BaseFetcher
```python
class BaseFetcher(ABC):
    @classmethod def source_type(cls) -> str: ...        # must match sources.json "type"
    def fetch(self) -> List[FetchedDocument]: ...        # main entry point
    @classmethod def from_config(cls, src_cfg, app_cfg): ...  # factory method
```

## GitHubPRFetcher
- Uses GitHub REST API: `GET /repos/{owner}/{repo}/pulls/{n}/files` (paginated, 100/page)
- Content: unified diff patch (or raw file if patch is None / binary)
- Metadata keys: `pr_url`, `status` (added/modified/removed), `source`
- Auth: `token` → `Authorization: Bearer` header
- Retry: `urllib3.Retry` with exponential backoff (`max_retries`, `timeout` from config)

## GitHubRepoFetcher
- Downloads full file content (not diffs) from a specific path in a GitHub repo
- Uses Trees API (recursive) to enumerate files; falls back to Blobs API for large files
- Saves files to `local_save_dir`; sets `metadata["absolute_path"]` for each
- Use case: bulk-downloading test plan `.adoc` files from `connectedhomeip` repo

## GitHubTagDiffFetcher
- Compares base tag to PR head: `GET /repos/{owner}/{repo}/compare/{base_tag}...{head_sha}`
- Resolves `head_sha` from PR metadata first (`GET /repos/{owner}/{repo}/pulls/{pr_number}`)
- Config keys: `repo`, `pr_number`, `base_tag`, `token`, `api_url`, `timeout`

## LocalFolderFetcher
- `Path(path).rglob("*")` for all files matching `extensions`
- UTF-8 decode with latin-1 fallback
- Sets `metadata["absolute_path"]` (required by `PDFLoader`)
- Returns `[]` for missing/empty directory (never raises)

## URLFetcher
- Downloads URL via `requests`; auto-detects HTML (`Content-Type: text/html`)
- `format="raw_html"` preserves HTML markup for `HTMLLoader`
- `format="matter_diff"` sets `metadata["matter_diff"]=True` to trigger `ProcessMatterHtmlDoc` expansion
- Config: `url`, `format` (`html`|`raw_html`|`matter_diff`|`text`|auto), `timeout`, `headers`

## CSVFetcher
- Reads CSV via stdlib `csv`; converts each row to prose: `"col: val | col2: val2 …"`
- Config: `path`, `columns` (optional column subset), `row_delimiter` (default `" | "`)
- One `FetchedDocument` per row; empty rows skipped

## MatterXMLFetcher
- Reads all `.xml` files from a directory (CSA DevX format)
- Handles: single-cluster files, multi-cluster files (`<clusters>`, `<configurator>`, `<matter>`, `<zigbee>` roots), namespace-prefixed tags
- Each cluster → one `FetchedDocument`:
  - `content` = human-readable text summary (cluster name, revision, entity counts + name lists)
  - `metadata["schema"]` = structured dict:
    ```python
    {
      "cluster_name": "On/Off",
      "cluster_id": "0x0006",
      "revision": "6",
      "attributes": [{"id": "0x0000", "name": "OnOff", "type": "boolean", ...}],
      "commands":   [{"id": "0x00",   "name": "Off",   "direction": "...", ...}],
      "events":     [...],
      "features":   [...]
    }
    ```
- Used by `build_knowledge_graph_node` via `kg.add_data_model_documents(docs)`
- Configure with `role: "data_model"` in sources.json; rebuild KG with `--build-data-model`

## ZapXMLAdapter
- `convert_zap_to_dm_xml(zap_xml_path, cluster_name_lookup, pics_code_overrides)` — converts ZAP format to standard DM XML
- `is_zap_format(xml_path)` — detects `<configurator>` root element
- `is_device_type_xml(xml_path)` — detects `<deviceType>` children (skipped during conversion)
- Handles: `<clusterExtension>` (adds to existing cluster), standalone `<cluster>` (new cluster), type-only files (enums/bitmaps)
- Auto-derives PICS code from `<define>` element; supports `pics_code_overrides` dict for manual mapping
- Used by `scripts/helper_scripts/convert_zap_xmls.py`

## Session / retry pattern (GitHub fetchers)
All GitHub fetchers share the same session-building pattern:
```python
session = requests.Session()
retry = urllib3.Retry(total=max_retries, backoff_factor=2, status_forcelist=[429, 500, 502, 503])
session.mount("https://", HTTPAdapter(max_retries=retry))
session.headers["Authorization"] = f"Bearer {token}"
```
