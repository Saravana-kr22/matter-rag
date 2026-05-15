# Database Module — Rules

## BaseVectorStore contract
- Every backend must implement `add_documents`, `search_by_vector`, and `size`.
- `save()` and `load()` are optional — the base class provides no-op defaults.
- `search_by_vector` must return results **sorted by descending score** and exclude anything below `threshold`.
- `is_empty` is derived from `size` — never override it independently.

## FAISSStore rules
- L2-normalise vectors before insertion when `index_type == "IndexFlatIP"` so inner product equals cosine similarity.
- `_id_counter` must be monotonically increasing — never reuse or reassign doc IDs within a session.
- `StoredEntry.metadata` must be a plain Python dict (JSON-serialisable) — no numpy types, no nested objects with custom classes.
- `save()` must create parent directories automatically.

## ChromaStore rules
- ChromaDB metadata values must be `str | int | float | bool`. Always encode list fields (e.g. `pics_codes`) as JSON strings via `_encode_metadata`, and decode them on read via `_decode_metadata`.
- ChromaDB distances are cosine *distances* (0 = identical). Convert to similarity with `score = 1.0 - dist / 2.0`.
- The collection is created with `hnsw:space: cosine` — never change this without migrating the existing collection.

## PostgresStore rules
- Use `JSONB` for metadata — not `TEXT` — so it can be queried with `->` / `->>` operators.
- Always use parameterised queries (`%s`) — never f-string SQL with user data.
- Call `_ensure_table(dim)` before the first insert; this is idempotent.
- Use `ivfflat` index with `lists = 100` as the default; adjust for larger collections.

## General rules
- All backends must produce `SearchResult` objects from `src.database.base_store` — never invent a parallel type.
- Do not import `faiss`, `chromadb`, or `psycopg2` at module top-level — always inside methods/`__init__` to allow the module to be imported without the optional dependency installed.
- `vector_store.py` is a pure factory/re-export — it must contain no storage logic.
