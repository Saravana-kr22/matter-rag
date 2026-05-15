# Embeddings Module — Hooks

## When you change the default model
- Update `config/config.yaml` `embeddings.model`.
- Update `src/config/models.py` `EmbeddingsConfig.model` default.
- **Delete or rebuild** any existing FAISS index (`data/faiss_index/`) — dimension mismatch will cause a silent error.
- Update the model table in `src/embeddings/CLAUDE.md`.
- Run `pytest tests/test_embeddings.py -v`.

## When you change the BGE query prefix
- Rebuilding the corpus index is required for consistent similarity scores.
- Update `CLAUDE.md` asymmetric retrieval section.

## When you change normalisation behaviour
- Ensure `FAISSStore` L2-normalisation is consistent: vectors in the index and at query time must both be normalised (or both unnormalised).
- Check `ChromaStore` — it uses cosine space natively so no explicit normalisation needed there.

## When you add a new `embed_*` method
- Add corresponding test in `tests/test_embeddings.py`.
- Document the method signature in `CLAUDE.md` API table.

## Downstream impact
- Output of `embed_documents()` feeds directly into `src/database/*.add_documents()`.
- Output of `embed_query()` feeds into `src/search/faiss_search.py` → `store.search_by_vector()`.
- Changing vector dtype or shape will break both.
