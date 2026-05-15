# Embeddings Module — Rules

## Asymmetric retrieval — critical
- **Always** use `embed_query()` for user/search queries and `embed_documents()` / `embed_texts(..., is_query=False)` for corpus documents. Mixing these will silently degrade retrieval quality.
- Never pass a query string to `embed_documents()`.

## Normalisation
- The `normalize` config flag must be `true` when using `IndexFlatIP` (FAISS) or cosine distance (Chroma/Postgres). Inner product on un-normalised vectors does NOT equal cosine similarity.
- `normalize` is applied inside `EmbeddingsModule` — do not re-normalise outside unless you explicitly need to.

## Lazy loading
- The sentence-transformer model must remain lazily loaded — do not move the `SentenceTransformer(...)` call to `__init__`. This keeps startup fast when the module is imported but not yet used.

## Batching
- Always respect `EmbeddingsConfig.batch_size` when encoding large document sets — sentence-transformers handles batching internally but needs the hint.
- Do not call `embed_documents()` in a loop per document — pass the whole list.

## Model compatibility
- If you change the default model, ensure the new embedding dimension is reflected in all stored indexes. Mixing dimensions between the FAISS index and a new model will cause a silent dimension mismatch error.
- Document the new model's dimension in `CLAUDE.md`.

## Return types
- `embed_documents` and `embed_texts` always return `np.ndarray` with `dtype=float32`.
- `embed_query` returns a 1-D `np.ndarray (dim,)` — not `(1, dim)`.
