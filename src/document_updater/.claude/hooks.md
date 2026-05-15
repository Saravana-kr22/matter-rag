# Document Updater Module — Hooks

## When you add a new file format updater
1. Create `src/document_updater/<fmt>_updater.py` subclassing `BaseDocumentUpdater`.
2. Implement `supported_extension()` and `write_updates()`.
3. Register in `REGISTRY` in `updater_registry.py`.
4. Add the extension to `FetcherConfig.local_extensions` if test plans in that format should be fetched.
5. Add test in `tests/test_document_updater.py`.
6. Update the `CLAUDE.md` Registry table.

## When you change AdocUpdater output filename pattern
- Update the `_matter_ai_rag_update` suffix or path logic in `adoc_updater.py`.
- Update `write_adoc_updates_node` in `nodes.py` if it reads `adoc_output_paths` by suffix.
- Update documentation in `CLAUDE.md`.

## When you change the TC heading regex (`_TC_HEADING_RE`)
- Verify it still matches `TC-OO-2.1`, `TC-MCORE-1.1`, `TC-LV-3.10`.
- Verify it does NOT match sub-headings (`=== TC-OO-2.1`) when the regex is anchored to `^`.
- Run `pytest tests/test_document_updater.py -v`.

## When you change `write_updates` signature
- Update the call site in `src/engine/nodes.py::write_adoc_updates_node`.
- Update `BaseDocumentUpdater` ABC signature.
- Update all concrete updater implementations.

## Downstream impact
- `write_adoc_updates_node` collects returned file paths into `state["adoc_output_paths"]`.
- `generate_report_node` references `adoc_output_paths` in the final report.
