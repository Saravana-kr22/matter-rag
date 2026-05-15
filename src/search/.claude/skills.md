# Search Module — Skills

## Text query search (any backend)
```python
from src.search.faiss_search import FAISSSearch
from src.embeddings.embeddings import EmbeddingsModule
from src.database.vector_store import create_vector_store

store = create_vector_store(cfg.database)
store.load()   # FAISS: load from disk; Chroma/Postgres: no-op
embedder = EmbeddingsModule(cfg.embeddings)
searcher = FAISSSearch(store, embedder)

results = searcher.search("OnOff cluster DUT as Server", k=10, threshold=0.65)
for r in results:
    print(f"[{r.score:.3f}] {r.metadata.get('tc_id')} — {r.page_content[:80]}")
```

## Search with precomputed vector
```python
query_vec = embedder.embed_query("Level control step")
results = searcher.search_by_vector(query_vec, k=5, threshold=0.6)
```

## Batch search multiple queries
```python
all_results = searcher.batch_search(
    ["OnOff test", "Level control step"],
    k=5,
    threshold=0.5,
)
for query_results in all_results:
    for r in query_results:
        print(r.score, r.metadata.get("tc_id"))
```

## Filter by TC metadata post-search
```python
results = searcher.search("OnOff cluster", k=20)
oo_results = [r for r in results if r.metadata.get("cluster_name") == "OO"]
```

## Import SearchResult
```python
from src.search.faiss_search import SearchResult    # re-export, backward compat
from src.database.base_store import SearchResult    # canonical location
```
