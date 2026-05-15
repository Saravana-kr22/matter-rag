# Fetcher Module — Rules

## FetchedDocument rules
- `content` must always be a `str` — never `bytes`. Decode binary content before constructing.
- `extension` is a computed property derived from `path` — never set it manually.
- `metadata` must include at least `"source"` (e.g. `"github_pr"` or `"local"`).
- For local files, `metadata["absolute_path"]` must be the absolute filesystem path so `PDFLoader` can re-read the binary file.
- `metadata["_process_rules"]` must be a list of rule dicts (may be empty) — `process_node` expects this key to always be present or absent (never `None`).

## BaseFetcher rules
- Every concrete fetcher must implement `source_type()`, `fetch()`, and `from_config()`.
- `source_type()` must return a unique lowercase string matching the `"type"` value in `sources.json`.
- `from_config()` must accept `(source_cfg: dict, app_cfg: AppConfig)` — `app_cfg` may be used for defaults (e.g. timeout).
- `fetch()` must not raise for individual file/item errors — log a warning and skip.
- `fetch()` must not raise `FileNotFoundError` for an empty directory — return `[]`.

## Registry rules
- `REGISTRY` in `fetcher_registry.py` is the single source of truth for type→class mapping.
- Adding a new source type = add class + add to `REGISTRY`. No other changes required.
- `load_sources()` returns `[]` (not raises) when `sources.json` is absent — this triggers the legacy fallback in `fetch_node`.

## GitHub API rules
- Always paginate `/pulls/{n}/files` with `?per_page=100` — PRs can have more than 30 files.
- Use the head commit SHA from PR metadata to construct raw content URLs — never use `main`/`master`.
- Fall back to the diff `patch` field when the raw download fails (binary files, rate limit).
- Never store or log the GitHub token value.
- Respect `github_timeout` and `github_max_retries` from config; use `urllib3.Retry` with backoff.

## Extension filtering rules
- Extension filter is applied in each fetcher's `fetch()` — only return files whose suffix is in the configured `extensions` list.
- Extensions must include the leading dot (e.g. `.adoc`, not `adoc`).

## ${VAR} substitution rules
- `resolve_config_vars(cfg)` in `base_fetcher.py` handles `${VAR}` → `os.environ[VAR]` substitution.
- Every fetcher's `from_config()` must call `resolve_config_vars()` on its config dict before reading values.
- Missing environment variables cause a `KeyError` at fetch time — surface this clearly to users.

## No side effects
- Fetchers must not create files or directories.
- Fetchers must not modify `app_cfg` or global state.
