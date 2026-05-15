# Test Skills

## Run by Module

```bash
pytest tests/test_config.py            # config loader + models
pytest tests/test_fetcher.py           # document fetcher
pytest tests/test_llm.py               # LLM provider factory
pytest tests/test_vector_store.py      # FAISS vector store
pytest tests/test_chunker.py           # TC chunker + generic chunker
pytest tests/test_loader.py            # document loaders
pytest tests/test_change_extractor.py  # change classification
```

## Run by Category

```bash
# All quality flag tests
pytest tests/test_change_extractor.py -k "quality or volatile"

# All LLM-related tests
pytest tests/ -k "llm" --ignore=tests/app

# All HTML-related tests
pytest tests/ -k "html"

# All TC record / chunker tests
pytest tests/test_chunker.py -k "tc_record or tc_id"
```

## Debugging

```bash
pytest tests/ --ignore=tests/app -x        # stop on first failure
pytest tests/ --ignore=tests/app --lf      # rerun last failed
pytest tests/ --ignore=tests/app -v --tb=long -l  # verbose + local vars
```
