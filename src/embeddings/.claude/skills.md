# Embeddings Module — Skills

## Embed a list of Document chunks
```python
from src.embeddings.embeddings import EmbeddingsModule
embedder = EmbeddingsModule(cfg.embeddings)
vecs = embedder.embed_documents(docs)   # np.ndarray (N, 384) for bge-small
```

## Embed a search query
```python
q_vec = embedder.embed_query("OnOff cluster DUT as Server TC")  # np.ndarray (384,)
```

## Get embedding dimension
```python
dim = embedder.embedding_dim   # 384 for bge-small, 1024 for bge-large
```

## Batch encode arbitrary strings
```python
vecs = embedder.embed_texts(["sentence one", "sentence two"], is_query=False)
```

## Use GPU / Apple Silicon MPS
```yaml
# config.yaml
embeddings:
  device: mps   # Apple Silicon GPU
  # device: cuda  # NVIDIA GPU
```

## Switch to higher-quality model
```yaml
embeddings:
  model: BAAI/bge-large-en-v1.5   # 1024-dim
  batch_size: 16                   # reduce batch size for large model
```
