# Hooks Reference

## LangGraph Node Hooks

Each LangGraph node emits lifecycle events. Register hooks in `src/engine/graphs/cli_graph.py`.

### Available Hook Points

| Hook Point | When | Typical Use |
|---|---|---|
| `pre_fetch_documents` | Before document fetch | Validate URLs, check token, resolve sources.json vars |
| `post_fetch_documents` | After fetch | Log doc count per source |
| `pre_process_documents` | Before text cleaning | Validate rules JSON |
| `post_process_documents` | After text cleaning | Log content size delta per doc |
| `pre_chunk_embed_test_plans` | Before KB build/load | Check FAISS index exists |
| `post_chunk_embed_test_plans` | After KB build/load | Log chunk count + vector DB size |
| `pre_chunk_pr` | Before PR chunking | Log PR doc count |
| `post_chunk_pr` | After PR chunking | Log PR chunk count |
| `pre_extract_pr_changes` | Before change extraction | — |
| `post_extract_pr_changes` | After extraction | Log rule-based vs LLM fallback counts |
| `pre_build_knowledge_graph` | Before KG build/load | Clear stale nodes if rebuilding |
| `post_build_knowledge_graph` | After KG build/load | Export graph snapshot |
| `pre_search_test_plan_vector_db` | Before vector search | Log query |
| `post_search_test_plan_vector_db` | After vector search | Log top-k results |
| `pre_search_knowledge_graph` | Before KG search | Log entity extraction |
| `post_search_knowledge_graph` | After KG search | Log KG hits per chunk |
| `pre_analyze_with_llm` | Before LLM analysis | Rate-limit check |
| `post_analyze_with_llm` | After LLM analysis | Log missing_tests / update_candidates counts |
| `pre_cluster_review` | Before cluster-level audit LLM call | — |
| `post_cluster_review` | After audit | Log cluster_review_*.md path |
| `pre_generate_report` | Before report write | Ensure output dir exists |
| `post_generate_report` | After report write | Notify user, open file, upload to Confluence |
| `pre_cleanup` | Before cleanup | — |
| `post_cleanup` | After cleanup | Log total run duration |

---

## State Change Hooks

The pipeline state (`PipelineState`) is a TypedDict. Use a wrapper to audit changes:

```python
def on_state_change(old_state: PipelineState, new_state: PipelineState, node: str):
    changed = {k for k in new_state if new_state[k] != old_state.get(k)}
    logger.debug(f"[{node}] state changed: {changed}")
```

---

## Error Hooks

```python
def on_node_error(node_name: str, error: Exception, state: PipelineState):
    logger.error(f"Node {node_name} failed: {error}")
    # Optionally: write partial state, send alert
```

---

## Conditional Edge Hook (routing after KG build)

```python
def _route_after_kg(state: PipelineState) -> str:
    if state.get("fatal_error"):
        return "cleanup"
    if not state.get("pr_chunks"):
        return "cleanup"   # index-only / build-only run
    return "search_test_plan_vector_db"

graph.add_conditional_edges("build_knowledge_graph", _route_after_kg)
```

---

## External Hooks (shell / CI)

```bash
# Run after pipeline completes
PR_URL=$PR_URL python scripts/run_ghpr_analysis.py --compare-only && \
  python scripts/post_run_hook.py

# post_run_hook.py can:
# - Upload HTML report to Confluence
# - Post summary to Slack
# - Parse logs/ghpr_analysis_*/engine.log for timing
# - Open the generated report file
```
