# Database Module — Hooks

## When you add a new backend
1. Create `src/database/<name>_store.py` subclassing `BaseVectorStore`.
2. Add a branch to `create_vector_store()` in `vector_store.py`.
3. Add backend name to `DatabaseConfig.backend` docstring in `models.py`.
4. Add backend fields to `DatabaseConfig` (e.g. connection strings, collection names).
5. Add YAML block to `config/config.yaml` under `database:`.
6. Update `src/database/CLAUDE.md` backends table.
7. Add `pytest tests/test_database.py -k <name>` tests covering: add + search + threshold filtering.

## When you change FAISSStore index format
- Increment a version marker in the metadata JSON sidecar so stale indexes are detected on `load()`.
- Run `pytest tests/test_database.py` to verify round-trip (save → load → search).

## When you change metadata schema (add/rename keys)
- All three backends must handle the change consistently.
- ChromaStore: update `_encode_metadata` / `_decode_metadata` if a list field is added.
- PostgresStore: new keys are transparently stored in JSONB — no migration needed.
- FAISSStore: JSON sidecar stores raw dict — no migration needed.
- Update the metadata table in `src/database/CLAUDE.md` and `src/chunker/CLAUDE.md`.

## When you change search_by_vector signature
- Update `FAISSSearch` in `src/search/faiss_search.py` to match.
- Run `pytest tests/test_search.py -v`.

## Downstream impact
`search_by_vector` results feed directly into `src/engine/nodes.py::_format_test_cases()`.
Any change to `SearchResult` fields requires updating the LLM prompt formatting in `nodes.py`.
