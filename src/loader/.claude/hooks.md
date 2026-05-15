# Loader Module — Hooks

## When you add a new loader
1. Create `src/loader/<fmt>_loader.py` with a `BaseDocumentLoader` subclass.
2. Register the extension in `DocumentLoaderFactory.__init__()`.
3. Add the extension to `FetcherConfig.local_extensions` default in `models.py`.
4. Add the extension to `fetcher.local_extensions` in `config/config.yaml`.
5. Write tests in `tests/test_loader.py` covering: empty input, normal input, metadata keys.
6. Update the loader table in `src/loader/CLAUDE.md`.

## When you change the Document dataclass
- Grep all usages: `grep -r "Document\|page_content\|metadata" src/`
- Update embeddings module — `EmbeddingsModule.embed_documents()` accesses `doc.page_content`.
- Update all three database backends — metadata is stored as-is.
- Update `src/engine/nodes.py::_format_test_cases` if new metadata keys should appear in the LLM prompt.

## When you change AdocLoader section splitting
- Run `pytest tests/test_loader.py -k adoc -v`.
- Verify TC headings (`== TC-OO-2.1`) are preserved as the leading line of each split section.

## When you change DocumentLoaderFactory defaults
- Run `pytest tests/test_loader.py -v`.
- Check that `DocumentLoader` (thin wrapper) still behaves identically.

## Downstream impact
- `Document.page_content` is the direct input to `EmbeddingsModule.embed_documents()`.
- `Document.metadata` is stored verbatim in all three vector store backends.
- TC metadata keys set here (`tc_id`, `cluster_name`, `pics_codes`, `section_type`) are read by `src/engine/nodes.py::_format_test_cases`.
