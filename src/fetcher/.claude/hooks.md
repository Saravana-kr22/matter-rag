# Fetcher Module — Hooks

## When you add a new source type (e.g. Jira, Confluence, S3)
1. Create `src/fetcher/sources/<name>_fetcher.py` subclassing `BaseFetcher`.
2. Implement `source_type()`, `fetch()`, and `from_config()`.
3. Register in `REGISTRY` in `src/fetcher/fetcher_registry.py`.
4. Add a source entry in `sources.json` with the new `"type"` value.
5. Add corresponding config fields to `FetcherConfig` in `models.py` if needed for defaults.
6. Add test in `tests/test_fetcher.py` using a mock HTTP response or tmp file.
7. Update `src/fetcher/.claude/CLAUDE.md` source type table.

## When you add a new file extension
- Add the extension (with leading dot) to the `extensions` list in the relevant `sources.json` entry.
- Register a corresponding loader in `src/loader/document_loader.py`.
- Update the supported extensions list in `CLAUDE.md`.

## When you change the GitHub API endpoint or pagination logic
- Run `pytest tests/test_fetcher.py -v`.
- Test against a real PR with >100 files to verify pagination still works.

## When you change FetchedDocument fields
- Update `src/loader/base_loader.py` — `BaseDocumentLoader.load()` receives `FetchedDocument`.
- Update all per-format loaders (`pdf_loader.py`, `adoc_loader.py`, etc.) if they read `metadata` keys.
- Update `src/processor/document_processor.py` if it reads new/renamed metadata keys.
- Update test fixtures in `tests/test_fetcher.py` and `tests/test_loader.py`.

## When you change sources.json format
- Update `load_sources()` in `fetcher_registry.py` if the top-level structure changes.
- Update `resolve_config_vars()` in `base_fetcher.py` if the substitution logic changes.
- Update the format docs in `CLAUDE.md`.

## Downstream impact
`FetchedDocument` is the contract between fetcher and processor/loader. Changing it requires updating:
- `src/processor/document_processor.py` (reads `doc.content`, `doc.extension`, `doc.metadata["_process_rules"]`)
- `src/loader/` loaders (receive processed `FetchedDocument`)
