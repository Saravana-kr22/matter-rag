# Engine Module — Rules

## PipelineState rules
- Every node must return a **complete** `PipelineState` dict — use `{**state, "new_key": value}` to preserve existing keys.
- Never mutate `state` in-place — always return a new dict.
- Optional fields must use `state.get("key", default)` — never direct key access on a `total=False` TypedDict.
- Errors must be appended to `state.get("errors", [])` and returned — never silently swallowed or re-raised inside a node.

## Node function rules
- Every node must be a **pure function** of `state` — no global variables, no module-level side effects.
- Long-running operations (model load, file I/O) belong inside node functions, not at module import time.
- Each node logs its primary action at `INFO` level with a `[node_name]` prefix and details at `DEBUG`.

## `chunk_embed_test_plans_node` rules
- Always chunk (cheap, no GPU needed) even when `build_test_plan_vectors=False`.
- Build flag sources (highest wins): explicit `state["build_test_plan_vectors"]` → `config.pipeline.build_test_plan_vectors` → `config.pipeline.rebuild_index` (compat alias).
- Auto-build when FAISS index file is absent — never skip embedding on first run.
- Must call `store.save()` after embedding; must call `store.load()` on warm path.

## `build_knowledge_graph_node` rules
- Export JSON **before** calling `add_pr_documents()` — PR nodes are transient and must not be persisted.
- On warm path (load): `load_from_json()` first, then `add_pr_documents()`.
- Auto-build when KG JSON file is absent (local backend only).
- Always creates a fresh `create_knowledge_graph()` instance — never reuse across pipeline runs.

## `fetch_documents_node` / `process_documents_node` rules
- `fetch_documents_node` routes by `role`: `"pr"` → `pr_documents`, `"test_plan"` → `test_plan_fetched`, `"spec"` → `spec_fetched`.
- `process_documents_node` must read per-source rules from `doc.metadata.get("_process_rules", [])`.
- Both populate fetched-doc state keys, not chunk keys.

## LLM prompt rules
- `_format_test_cases` must include `tc_id`, `cluster_name`, `pics_codes`, `section_type` — key signals for the LLM.
- `_format_graph_results` must include node label, node type, and first 400 chars of content.
- Truncate `page_content` in prompts: 400 chars per TC chunk, 2000 chars per PR chunk.

## Vector store rules
- `chunk_embed_test_plans_node` must always call `create_vector_store(config.database)` — never instantiate `FAISSStore` directly.
- `search_test_plan_vector_db_node` must check `store.is_empty` before searching.

## Report rules
- `generate_report_node` creates `output_dir` if missing.
- Both `.md` and `.json` reports must be written.
