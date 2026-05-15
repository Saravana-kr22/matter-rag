# Config Module — Skills

## Load config from default path
```python
from src.config.config_loader import load_config
cfg = load_config("config/config.yaml")
print(cfg.llm.provider)                        # "claude_subprocess"
print(cfg.database.backend)                    # "faiss"
print(cfg.chunker.chunker_type)                # "matter_tc"
print(cfg.pipeline.build_test_plan_vectors)    # False
print(cfg.knowledge_graph.graph_store_path)    # "data/knowledge_graph/matter_kg.json"
```

## Import models without file I/O
```python
from src.config.models import AppConfig, LLMConfig, ChunkerConfig, DatabaseConfig, PipelineConfig
# Safe to use in type annotations — no YAML is loaded
```

## Programmatic overrides (highest priority)
```python
cfg = load_config(overrides={
    "llm": {"provider": "local"},
    "pipeline": {
        "build_test_plan_vectors": True,
        "build_knowledge_graph": True,
        "search_top_k": 5,
    },
    "database": {"backend": "chroma"},
})
```

## Environment variable overrides
```bash
MATTER_RAG__LLM__PROVIDER=local \
MATTER_RAG__DATABASE__BACKEND=docker \
MATTER_RAG__PIPELINE__BUILD_TEST_PLAN_VECTORS=true \
MATTER_RAG__PIPELINE__BUILD_KNOWLEDGE_GRAPH=true \
python scripts/run_pipeline.py ...
```

## Switch to Docker vector store backend
```yaml
# config.yaml
database:
  backend: docker
  docker_vector_store_url: http://localhost:8001
  docker_timeout: 30
```

## Switch to Docker knowledge graph backend
```yaml
knowledge_graph:
  backend: docker
  docker_url: http://localhost:8002
  docker_timeout: 30
```

## Switch to ChromaDB or Postgres
```yaml
database:
  backend: chroma
  chroma_persist_dir: data/chroma
  chroma_collection: matter_tc
# or:
database:
  backend: postgres
  postgres_url: ${POSTGRES_URL}
  postgres_table: matter_embeddings
```

## Add a new config section
1. Add dataclass to `models.py`
2. Add `field: NewConfig` to `AppConfig`
3. Add `_build(NewConfig, raw.get("section_key", {}))` in `config_loader.py::load_config()`
4. Add YAML block to `config/config.yaml`
5. Add `"section_key": NewConfig` to `_SECTION_MAP` in `config_loader.py`
