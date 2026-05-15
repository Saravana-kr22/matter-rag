# Engine Module — Hooks

## When you add or remove a pipeline node
- Update `pipeline.py::_build_graph()` edges — a missing edge silently skips the node.
- Update the DAG in `CLAUDE.md`.
- Add integration test in `tests/test_pipeline.py` verifying the node runs and its output key is present in state.

## When you add a new source role (e.g. role="spec")
- Add routing logic in `fetch_documents_node` to assign fetched docs to the new state key.
- Add the new state key to `PipelineState` TypedDict.
- Update the `sources.json` example entry in `src/fetcher/.claude/CLAUDE.md`.

## When you change `chunk_embed_test_plans_node`
- Test both paths: `build_test_plan_vectors=True` (embed + save) and `False` (load).
- Test auto-build path: delete `data/faiss_index/matter.index` and run with flag=False.
- Run `pytest tests/test_engine.py -k chunk_embed`.

## When you change `build_knowledge_graph_node`
- Test both paths: `build_knowledge_graph=True` (build + save) and `False` (load).
- Verify PR nodes are NOT in the saved JSON (only spec + test plan nodes).
- Verify PR nodes ARE added after loading.
- Delete `data/knowledge_graph/matter_kg.json` to test auto-build path.

## When you change `_format_test_cases` or `_format_graph_results`
- Inspect captured prompt via `logging.DEBUG` to verify LLM receives correct context.
- If chunker metadata keys change (`tc_id`, `cluster_name`), update format helpers.
- Run `pytest tests/test_engine.py -k format`.

## When you change `_ANALYSIS_SYSTEM_PROMPT` or `_ANALYSIS_PROMPT_TEMPLATE`
- Do a manual quality check with a real PR before merging — prompt regressions are hard to catch with unit tests.
- Update `_parse_llm_response` if section headings in the expected output format change.

## When you change `PipelineState`
- Update the TypedDict in `nodes.py` AND the table in `CLAUDE.md` simultaneously.
- `vector_store` must remain typed as `BaseVectorStore`; `knowledge_graph` as `BaseKnowledgeGraph`.

## When you change the log file output
- `run_pipeline.py` adds the `FileHandler` after `_configure_logging()`.
- All `logger.*` calls in all nodes automatically go to the file.

## Downstream impact
`nodes.py` is the integration point for every module. Changes here may require coordinating with:
- `src/fetcher/` — if `fetch_documents_node` source resolution changes
- `src/processor/` — if `process_documents_node` rule loading changes
- `src/loader/` — if `chunk_embed_test_plans_node` or `chunk_pr_node` chunking changes
- `src/database/` — if `chunk_embed_test_plans_node` backend changes
- `src/search/` — if `search_test_plan_vector_db_node` parameters change
- `src/knowledge_graph/` — if `build_knowledge_graph_node` or `search_knowledge_graph_node` changes
- `src/llm/` — if `analyze_chunks_with_llm_node` prompt format changes
- `src/document_updater/` — if `write_adoc_updates_node` updater registry changes
