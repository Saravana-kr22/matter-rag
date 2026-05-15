# Embeddings Module

## Purpose
Create dense vector embeddings for `Document` objects and query strings using a BGE sentence-transformer model.
Vectors are used as inputs to the vector store and search modules.

## Key Class: EmbeddingsModule
```python
from src.embeddings.embeddings import EmbeddingsModule
embedder = EmbeddingsModule(config.embeddings)
```

## API

| Method | Input | Output | Notes |
|---|---|---|---|
| `embed_documents(documents)` | `List[Document]` | `np.ndarray (N, dim)` | Uses `doc.page_content` |
| `embed_query(query)` | `str` | `np.ndarray (dim,)` | Prepends BGE query prefix |
| `embed_texts(texts, is_query)` | `List[str]`, `bool` | `np.ndarray (N, dim)` | General purpose |
| `embedding_dim` | — | `int` | Model vector dimension |

## BGE asymmetric retrieval
BGE models use different prefixes for queries and documents:
- **Query prefix**: `"Represent this sentence for searching relevant passages: "`
- **Document prefix**: `""` (no prefix)

Always use `embed_query()` for search queries and `embed_documents()` / `embed_texts(..., is_query=False)` for corpus documents.

## Lazy loading
The sentence-transformer model is **not** loaded until the first `embed_*` call. Safe to import without GPU/CPU overhead at import time.

## Config
```yaml
embeddings:
  model: BAAI/bge-large-en-v1.5   # 1024-dim, best retrieval quality (project default)
  device: mps                      # mps (Apple Silicon GPU) | cuda | cpu
  batch_size: 64
  normalize: true                  # L2-normalise (required for cosine similarity)
  cache_dir: ~/.cache/huggingface/hub
  offline: true                    # true = use cached model, skip HF network check
```

## Model sizes
| Model | Dim | Notes |
|---|---|---|
| `bge-small-en-v1.5` | 384 | Default, fast, ~33M params |
| `bge-base-en-v1.5` | 768 | Better quality |
| `bge-large-en-v1.5` | 1024 | Best quality, slower |
