# Tests Directory

## Overview

Pytest unit tests for the core `src/` modules. Each test file targets a specific module.
The `tests/app/` subdirectory has its own `.claude/` and covers the FastAPI debug app.

## Test Files

| File | Module Under Test | Covers |
|------|-------------------|--------|
| `test_config.py` | `src/config/` | YAML loading, defaults, env-var substitution, `MATTER_RAG__*` overrides, bool coercion |
| `test_fetcher.py` | `src/fetcher/` | Local directory fetching, PR URL parsing |
| `test_llm.py` | `src/llm/` | `get_llm()` factory: Claude/Ollama instantiation (mocked), invalid provider error |
| `test_vector_store.py` | `src/database/` | FAISS add/size, save/load round-trip, mismatch error. Requires `faiss-cpu` (auto-skipped). |
| `test_chunker.py` | `src/chunker/` | GenericChunker, MatterTCChunker (TC-ID, cluster, PICS, steps, ignore rules, TCRecord) |
| `test_loader.py` | `src/loader/` | Factory dispatch, TextLoader, AdocLoader, CSVLoader, HTMLLoader, chunk overlap |
| `test_change_extractor.py` | `src/processor/` | Rule-based ChangeExtractor: quality flags, conformance, access, datatype, fallback, priority, LLM fallback |

## Running Tests

```bash
pytest tests/ --ignore=tests/app          # unit tests only
pytest tests/                              # all tests including app
pytest tests/test_chunker.py               # single file
pytest tests/test_change_extractor.py::TestQualityFlagAdded  # single class
pytest tests/ --ignore=tests/app --cov=src --cov-report=term-missing  # with coverage
```

## Patterns

- LLM providers always mocked — no real LLM calls
- Test inputs defined inline as module constants (`textwrap.dedent()`)
- No external fixture files or network calls
- `pytest.importorskip("faiss")` for optional FAISS dependency
- Test classes (`class Test*`) in change_extractor and chunker; flat functions elsewhere

## Fixtures

| Fixture | File | Purpose |
|---------|------|---------|
| `fetcher` | `test_fetcher.py` | `DocumentFetcher` with default config |
| `loader_cfg` | `test_loader.py` | `LoaderConfig(chunk_size=200, chunk_overlap=20)` |
| `factory` | `test_loader.py` | `DocumentLoaderFactory` for dispatch tests |
| `store` | `test_vector_store.py` | `VectorStore` with temp FAISS path |
