# Fetcher Sources — Hooks

## When you add a new source fetcher
1. Create `src/fetcher/sources/<name>_fetcher.py` subclassing `BaseFetcher`.
2. Implement `source_type()`, `fetch()`, and `from_config()`.
3. Call `resolve_config_vars(source_cfg)` at the start of `from_config()` for `${VAR}` substitution.
4. Register in `REGISTRY` in `src/fetcher/fetcher_registry.py`.
5. Add a `sources.json` example in `src/fetcher/.claude/CLAUDE.md`.
6. Add test in `tests/test_fetcher.py` with a mock or tmp fixture.
7. Update the table in this `CLAUDE.md`.

## When you change GitHub API version or endpoint
- Update the URL pattern in the relevant fetcher.
- Check pagination: all list endpoints must use `?per_page=100` with link-header following.
- Run `pytest tests/test_fetcher.py -k github -v`.

## When you change GitHubRepoFetcher local save behaviour
- Verify `metadata["absolute_path"]` is still set — PDFLoader requires it.
- Verify the save directory is created if missing.
- Run `pytest tests/test_fetcher.py -k repo -v`.

## When you change FetchedDocument metadata keys
- Update `src/processor/document_processor.py` if it reads new keys.
- Update loaders that read `metadata["absolute_path"]` (`PDFLoader`).
- Update `fetch_documents_node` in `nodes.py` if it reads new routing keys.

## When you add a new role value to sources.json
- Update `fetch_documents_node` in `src/engine/nodes.py` to route the new role to a new state key.
- Add the new state key to `PipelineState` TypedDict.
- Document in `src/fetcher/.claude/CLAUDE.md` sources format section.
