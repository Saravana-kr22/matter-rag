# Engine Module — Skills

## Run pipeline (sources.json-driven, warm cache)
```python
import os
os.environ["PR_URL"] = "https://github.com/project-chip/connectedhomeip/pull/1234"
os.environ["GITHUB_TOKEN"] = "ghp_..."

from src.engine.pipeline import create_pipeline
pipeline = create_pipeline("config/config.yaml")
result = pipeline.run()   # uses cached vector DB and KG
print(f"Report: {result.report_path}")
print(f"Missing tests: {len(result.missing_tests)}")
```

## Build vector DB + KG from scratch, then compare
```python
result = pipeline.run(
    pr_url="https://github.com/project-chip/connectedhomeip/pull/1234",
    build_test_plan_vectors=True,
    build_knowledge_graph=True,
)
```

## Index-only (no PR — build and cache everything)
```python
result = pipeline.run(build_test_plan_vectors=True, build_knowledge_graph=True)
# or from CLI:
# python scripts/run_ghpr_analysis.py --index-only
```

## Rebuild only the vector DB (test plans changed)
```python
result = pipeline.run(
    pr_url="...",
    build_test_plan_vectors=True,   # re-embed
    build_knowledge_graph=False,    # reuse cached KG
)
# or from CLI:
# python scripts/run_ghpr_analysis.py --build-test-plan-vectors --pr-url ...
```

## Rebuild only the knowledge graph (spec/test plans changed)
```python
result = pipeline.run(
    pr_url="...",
    build_test_plan_vectors=False,
    build_knowledge_graph=True,
)
# or from CLI:
# python scripts/run_ghpr_analysis.py --build-knowledge-graph --pr-url ...
```

## Force rebuild via config
```yaml
# config.yaml
pipeline:
  build_test_plan_vectors: true
  build_knowledge_graph: true
```

## Inspect per-run log files
```bash
# Most recent run
ls -t logs/ghpr_analysis_*/engine.log | head -1 | xargs tail -50
grep -rh "ERROR" logs/ghpr_analysis_*/
```

## Add a new pipeline node
1. Write `def my_node(state: PipelineState) -> PipelineState` in `nodes.py`.
2. Register: `graph.add_node("my_node", my_node)` in `src/engine/graphs/cli_graph.py`.
3. Add edges with `graph.add_edge(...)` or `graph.add_conditional_edges(...)`.

## Debug PipelineState mid-run
```python
def debug_node(state: PipelineState) -> PipelineState:
    import json
    print(json.dumps({k: type(v).__name__ for k, v in state.items()}, indent=2))
    return state
```

## Inspect LLM call log
```python
import json
with open("logs/llm_calls.jsonl") as f:
    for line in f:
        c = json.loads(line)
        print(f"#{c['call_id']} {c['duration_s']:.1f}s success={c['success']}")
```

## Backward-compat: index_only / compare_only still work
```python
result = pipeline.run(index_only=True)    # → build_test_plan_vectors=True + build_knowledge_graph=True
result = pipeline.run(compare_only=True)  # → both flags False (use cache)
```
