# Search Module — Hooks

## When you change FAISSSearch constructor signature
- Update call sites in `src/engine/nodes.py::search_node`.
- Update `tests/test_search.py` fixtures.

## When you add a new search method
- Implement in `FAISSSearch` and delegate to `store.search_by_vector()` — never call store internals directly.
- Add test in `tests/test_search.py` covering: normal results, empty store, threshold filtering.
- Document in `CLAUDE.md` API table.

## When you change SearchResult fields
- `SearchResult` is defined in `src/database/base_store.py` — change it there.
- After changing, update `src/engine/nodes.py::_format_test_cases` to reflect new/renamed fields.
- Update `src/database/CLAUDE.md` SearchResult section.
- Run `pytest tests/test_search.py tests/test_database.py -v`.

## When you change threshold semantics
- Update the `threshold` docstring in both `FAISSSearch.search()` and `BaseVectorStore.search_by_vector()`.
- Update `pipeline.similarity_threshold` default in `models.py` and `config/config.yaml` if needed.
- Update the threshold description in `src/search/CLAUDE.md`.

## Downstream impact
Search results (`List[SearchResult]`) are consumed by `src/engine/nodes.py::analyze_node` via `_format_test_cases`. Changing `SearchResult` or its `metadata` contract requires updating the LLM prompt formatter.
