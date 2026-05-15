# Database Module — Skills

## Factory — pick backend from config
```python
from src.database.vector_store import create_vector_store
store = create_vector_store(cfg.database)   # "faiss" | "chroma" | "postgres" | "docker"
store.add_documents(chunks, embeddings)
store.save()   # FAISS only; no-op for others
```

## FAISS — add documents and save
```python
from src.database.faiss_store import FAISSStore
store = FAISSStore(cfg.database)
doc_ids = store.add_documents(docs, embeddings)   # returns list of "doc_XXXXXX" ids
store.save()
```

## FAISS — load existing index
```python
store.load()   # raises FileNotFoundError if index file is missing
print(f"Loaded {store.size} vectors")
```

## Search by precomputed vector (any backend)
```python
results = store.search_by_vector(query_vec, k=10, threshold=0.65)
for r in results:
    print(r.score, r.metadata.get("tc_id"), r.page_content[:80])
```

## ChromaDB
```yaml
# config.yaml
database:
  backend: chroma
  chroma_persist_dir: data/chroma
  chroma_collection: matter_tc
```
```python
from src.database.chroma_store import ChromaStore
store = ChromaStore(cfg.database)
store.add_documents(docs, embeddings)
```

## PostgreSQL + pgvector
```bash
export POSTGRES_URL=postgresql://user:pass@localhost/matterdb
```
```yaml
database:
  backend: postgres
  postgres_table: matter_embeddings
```

## Docker vector store
```yaml
database:
  backend: docker
  docker_vector_store_url: http://localhost:8001
  docker_timeout: 30
```
```python
from src.database.docker_store import DockerVectorStore
store = DockerVectorStore(cfg.database)
store.load()                             # health-check /health
store.add_documents(docs, embeddings)   # POST /add_documents
results = store.search_by_vector(q_vec, k=10, threshold=0.65)  # POST /search
```

## Retrieve FAISS entry by id
```python
entry = store.get_by_id("doc_000042")
print(entry.page_content, entry.metadata)
```

## Backward-compat alias
```python
from src.database.vector_store import VectorStore   # == FAISSStore
```
