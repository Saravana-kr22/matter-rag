# Database Module

## Purpose
Multi-backend vector store for Matter TC chunk embeddings and rich JSON metadata.
When embeddings are searched the full TC context is returned to the LLM so it can identify
exactly which test cases need to be updated or created.

## Files

| File | Class | Backend |
|---|---|---|
| `base_store.py` | `BaseVectorStore`, `SearchResult` | ABC + shared types |
| `faiss_store.py` | `FAISSStore`, `StoredEntry` | FAISS flat-file (default) |
| `chroma_store.py` | `ChromaStore` | ChromaDB persistent |
| `postgres_store.py` | `PostgresStore` | PostgreSQL + pgvector |
| `docker_store.py` | `DockerVectorStore` | HTTP client for remote Docker service |
| `vector_store.py` | `create_vector_store()` | Factory + `VectorStore` alias |

## Factory usage
```python
from src.database.vector_store import create_vector_store
store = create_vector_store(config.database)   # picks backend from config.database.backend
store.add_documents(chunks, embeddings)
store.save()   # FAISS only; no-op for Chroma/Postgres/Docker
results = store.search_by_vector(query_vec, k=10, threshold=0.65)
```

## SearchResult
```python
@dataclass
class SearchResult:
    score: float        # cosine similarity 0–1
    doc_id: str
    page_content: str
    metadata: dict      # tc_id, cluster_name, pics_codes, section_type, path, …
    rank: int
```

## Backends

### FAISS (`backend: faiss`) — default
- File-based, no server required
- Binary index + JSON metadata sidecar
- `IndexFlatIP` (exact cosine) or `IndexIVFFlat` (approximate)
- `save()` / `load()` persist to `faiss_index_path` + `metadata_path`

### ChromaDB (`backend: chroma`)
- Persistent local vector DB; no server required
- List fields (e.g. `pics_codes`) serialised as JSON strings
- Cosine space: `hnsw:space: cosine`

### PostgreSQL + pgvector (`backend: postgres`)
- Requires running PostgreSQL with pgvector extension
- Metadata stored as JSONB; `ivfflat` index on vector column
- `postgres_url` env var: `postgresql://user:pass@host/db`

### Docker (`backend: docker`)
- `DockerVectorStore` — HTTP client calling a pre-built Docker service on port 8001
- `load()` is a health-check (GET /health); `save()` is a no-op
- Endpoints: `/health`, `/add_documents`, `/search`, `/size`
- Config: `docker_vector_store_url`, `docker_timeout`

## Metadata stored per chunk
All chunkers write these fields; the database stores them verbatim:

| Field | Type | Example |
|---|---|---|
| `tc_id` | str | `"TC-OO-2.1"` |
| `cluster_name` | str | `"OO"` |
| `pics_codes` | list[str] | `["OO.S", "OO.C.00.Rsp"]` |
| `section_type` | str | `"steps"` |
| `path` | str | `"TC-OO-2.1.adoc"` |
| `doc_type` | str | `"test_plan"` |
| `chunk_index` | int | `0` |

## Backward compatibility
`from src.database.vector_store import VectorStore` still works — `VectorStore = FAISSStore`.
`from src.search.faiss_search import SearchResult` still works — re-exported from `base_store`.
