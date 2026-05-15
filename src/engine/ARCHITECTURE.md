# Pipeline Engine — Architecture

## Design Principle: Node Library + Client-Owned Graphs

Every pipeline stage is a **plain function** (`PipelineState → dict`).  Clients
compose those functions into a LangGraph `StateGraph` according to their own
requirements.  No client is locked into another client's node order or output
handling.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Node Library  (src/engine/nodes.py)                                    │
│                                                                         │
│  fetch_documents_node          search_test_plan_vector_db_node          │
│  process_documents_node        search_knowledge_graph_node              │
│  ingest_data_model_node        analyze_chunks_with_llm_node             │
│  build_matter_schema_node      write_adoc_updates_node                  │
│  chunk_embed_test_plans_node   generate_report_node                     │
│  chunk_pr_node                 cleanup_node                             │
│  extract_pr_changes_node                                                │
│  build_knowledge_graph_node                                             │
└─────────────────────────────────────────────────────────────────────────┘
          ↑                                  ↑
          │  import any nodes needed         │
          │                                  │
┌─────────────────────┐          ┌───────────────────────┐
│  graphs/cli_graph.py│          │  graphs/chat_graph.py  │
│                     │          │                        │
│  14-node full graph │          │  3-node search+analyze │
│  for CLI pipeline   │          │  for FastAPI chat      │
└─────────────────────┘          └───────────────────────┘
          │                                  │
          │  compiled graph                  │  compiled graph
          ▼                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PipelineRunner  (src/engine/pipeline.py)                               │
│                                                                         │
│  __init__(graph, run_ctx)                                               │
│  run(initial_state) → PipelineResult                                    │
│                                                                         │
│  Injects run_ctx into state, invokes graph, extracts result.            │
│  Knows nothing about which nodes are in the graph.                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Roles and Responsibilities

| Layer | File | Owns | Does NOT own |
|---|---|---|---|
| Node library | `nodes.py` | What each stage computes | Ordering, client identity |
| Graph definitions | `graphs/cli_graph.py` etc. | Node ordering, edges, routing | Execution, logging |
| Runner | `pipeline.py` | Execution, logging, result extraction | Graph topology |
| Run context | `run_context.py` | Identity, log dir, query log | Business logic |
| Client entrypoint | `scripts/run_pipeline.py` etc. | RunContext creation, initial state | Node implementations |

---

## PipelineState Ownership Rules

`PipelineState` is a `TypedDict` shared by all graphs.  To keep it from
becoming a dumping ground:

**Rule: every field is owned by the node that writes it, not by the client
that reads it.**

```
fetch_documents_node    → pr_documents, test_plan_fetched, spec_fetched, data_model_fetched
process_documents_node  → spec_diff_html  (processed versions of the above)
chunk_embed_test_plans_node → test_plan_chunks, vector_store, built_knowledge_base
chunk_pr_node           → pr_chunks, spec_chunks
extract_pr_changes_node → pr_changes
build_knowledge_graph_node → knowledge_graph
search_test_plan_vector_db_node → search_results
search_knowledge_graph_node     → graph_results
analyze_chunks_with_llm_node   → analysis_results, missing_tests, update_candidates, llm_reply
generate_report_node    → report_path
write_adoc_updates_node → adoc_output_paths
```

**Never add a field to PipelineState to serve a specific client.**  If a node
produces different output shapes for different clients, use `run_ctx.client`
to branch inside the node.

---

## RunContext

Every pipeline invocation carries a `RunContext` that flows through
`PipelineState["run_ctx"]`:

```python
@dataclass
class RunContext:
    run_id:          str          # e.g. "app_chat_04132026_143022"
    run_dir:         Path         # logs/<run_id>/
    client:          str          # "matter_rag_pipeline" | "app_chat" | ...
    nodes_executed:  List[str]    # populated by @log_node as each node runs
```

Two delivery mechanisms — both set by the client:

| Mechanism | Used for |
|---|---|
| `state["run_ctx"]` | Node behavior branching (`run_ctx.client`) |
| `contextvars.ContextVar` | Transparent log routing (`RunAwareFileHandler`) |

---

## Log Layout

Each run writes its own directory:

```
logs/
  matter_rag_pipeline_<ts>/     ← CLI run
    master.log                  ← every log record
    engine.log                  ← src.engine.*
    knowledge_graph.log         ← src.knowledge_graph.*
    llm.log                     ← src.llm.*
    rag_queries.jsonl           ← one JSON record per RAG query
    pr_changes.json             ← structured PR change extraction
    matter_schema.json          ← canonical schema snapshot

  app_chat_<ts>/                ← FastAPI chat request (one dir per request)
    master.log
    engine.log
    app.log                     ← tests.app.*
    rag_queries.jsonl
```

---

## Adding a New Client

1. Create `src/engine/graphs/<client>_graph.py`
2. Import the nodes you need from `nodes.py`
3. Build a `StateGraph`, add nodes + edges, `compile()`
4. In your client entrypoint:
   ```python
   run_ctx = create_run_context("my_client")
   token   = set_run_context(run_ctx)        # for logging
   runner  = PipelineRunner(build_my_graph(), run_ctx)
   result  = runner.run(initial_state)
   run_ctx.close()
   _current_run_ctx.reset(token)
   ```
5. If a node needs to behave differently for your client, add a branch on
   `state["run_ctx"].client` inside that node — do not add a new field to
   `PipelineState`.

---

## Concurrency Safety

| Scenario | Why it's safe |
|---|---|
| Multiple concurrent FastAPI requests | Each `graph.invoke()` gets its own `PipelineState` dict; LangGraph never shares state between invocations |
| `contextvars.ContextVar` | Each asyncio task has an isolated context copy — `RunAwareFileHandler` routes to the right log dir per request |
| Multiple CLI processes | Separate OS processes; no shared memory |
| Thread pool (`run_in_executor`) | Use `contextvars.copy_context().run(fn)` to propagate context into threads |

---

## CI Usage

```bash
# Build indexes (no LLM calls)
python scripts/run_pipeline.py --index-only

# Full analysis against a PR
PR_URL=https://github.com/project-chip/connectedhomeip/pull/1234 \
  python scripts/run_pipeline.py --compare-only

# Test a single node in isolation (no graph needed)
python -m pytest tests/test_nodes.py::test_search_node -v

# Test a whole graph
python -m pytest tests/test_graphs.py::test_cli_graph_compiles -v
```

Nodes are pure functions — a test can call one directly with a minimal state
dict, no `StateGraph` needed:

```python
def test_analyze_node_chat_client():
    state = {
        "config": mock_config,
        "run_ctx": RunContext(client="app_chat", ...),
        "pr_chunks": [...],
        "search_results": {...},
        "graph_results": {},
    }
    result = analyze_chunks_with_llm_node(state)
    assert "llm_reply" in result          # chat path
    assert "report_path" not in result    # CLI path not taken
```
