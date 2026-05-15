# Document Updater Module — Rules

## BaseDocumentUpdater rules
- Every concrete updater must implement `supported_extension()` and `write_updates()`.
- `supported_extension()` must return a lowercase extension with a leading dot (e.g. `".adoc"`).
- `write_updates()` must never modify source files in-place — always write to `output_dir`.
- `write_updates()` must return a list of all file paths written (may be empty if nothing changed).

## AdocUpdater rules
- Output files must use the `_matter_ai_rag_update.adoc` suffix so they are clearly distinguishable from source files.
- `_TC_HEADING_RE` must remain anchored with `re.MULTILINE` and match exactly `^(={1,6})\s+(TC-...)`.
- Never overwrite an existing output file without logging a warning.
- If the source `.adoc` file for a TC is not found on disk, skip (log warning) — do not raise.

## Registry rules
- `REGISTRY` in `updater_registry.py` is the single source of truth for extension → updater class.
- `create_updater(extension)` returns `None` (not raises) when the extension is not registered — callers must handle `None`.
- Do not import updater implementations at module level in `updater_registry.py` if they have heavy dependencies — use lazy imports.

## Independence rules
- This module must not import from `src.knowledge_graph`, `src.database`, or `src.embeddings`.
- It may import `SearchResult` from `src.database.base_store` for type hints only.
- It reads `analysis_results` (plain dicts) and `search_results` (SearchResult objects) — both produced by engine nodes.

## No side effects
- `write_updates()` must not modify `analysis_results` or `search_results` in-place.
- Output directory must be created if missing (`Path(output_dir).mkdir(parents=True, exist_ok=True)`).
