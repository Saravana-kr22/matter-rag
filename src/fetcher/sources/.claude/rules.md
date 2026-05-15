# Fetcher Sources — Rules

## BaseFetcher contract
- `source_type()` must return a unique lowercase string matching the `"type"` value in `sources.json`.
- `from_config()` must call `resolve_config_vars(source_cfg)` before reading any value — handles `${VAR}` substitution.
- `fetch()` must not raise for individual file/item failures — log a warning and skip.
- `fetch()` must return `[]` for empty directories or missing resources (never `FileNotFoundError`).
- `fetch()` must filter files by `extensions` — only return files whose suffix is in the configured list.

## GitHub fetcher rules
- Always paginate with `?per_page=100` — PRs can have more than 30 files.
- Use the head commit SHA from PR metadata for raw content URLs — never use `main`/`master` as ref.
- Fall back to `patch` field when raw download fails (binary files, rate limit).
- Never log or print the token value.
- Respect `github_timeout` and `github_max_retries`; use `urllib3.Retry` with backoff.

## GitHubRepoFetcher rules
- Set `metadata["absolute_path"]` to the locally-saved file path — required by `PDFLoader`.
- Create `local_save_dir` if missing — never raise `FileNotFoundError`.
- Do not re-download files that already exist locally unless forced.

## LocalFolderFetcher rules
- `metadata["absolute_path"]` must be the absolute filesystem path — `str(Path(path).resolve())`.
- Read files as UTF-8; fall back to latin-1 on `UnicodeDecodeError` — never skip binary files silently.

## URLFetcher rules
- HTML stripping must use stdlib `html.parser` — no BeautifulSoup dependency.
- If `format` is `auto`, detect HTML from `Content-Type` response header, not file extension.

## CSVFetcher rules
- Empty rows (all fields blank) must be skipped.
- `row_delimiter` is configurable — never hardcode `" | "` in the prose string builder.
- If `columns` is specified, only include those columns in the prose output.

## Extension filter rules
- Extensions must include the leading dot (`.adoc`, not `adoc`).
- Extension comparison must be case-insensitive (`path.lower().endswith(ext)`).
