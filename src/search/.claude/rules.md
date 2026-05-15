# Search Module — Rules

## SearchResult rules
- `SearchResult` is defined in `src.database.base_store` — do not redefine it here.
- `score` is cosine similarity in range `[0.0, 1.0]` — never raw distance or negative values.
- `rank` is the 0-based position in the result list, not the FAISS internal index.
- `metadata` is passed through as-is from the vector store — never modify it in the search module.

## FAISSSearch rules
- `FAISSSearch` must accept any `BaseVectorStore` — never type-hint the constructor parameter as `FAISSStore` or `VectorStore`.
- `search()` must check `store.is_empty` and return `[]` before calling `embed_query()` — avoid loading the model unnecessarily.
- `search_by_vector()` delegates to `store.search_by_vector()` — do not duplicate vector normalisation logic here.
- `batch_search()` must call `embed_texts(..., is_query=True)` — not `embed_documents()`.

## Threshold rules
- Threshold filtering is the responsibility of the **store** (`search_by_vector` contract), not `FAISSSearch`.
- `FAISSSearch` passes `threshold` directly to `store.search_by_vector()` — do not re-filter.

## Import rules
- Do not import `faiss` directly in this module — all FAISS operations live in `src.database.faiss_store`.
- `SearchResult` re-export from `faiss_search.py` (`from src.database.base_store import SearchResult`) is the only cross-module symbol here.
