# Knowledge Graph Module — Skills

## Build and persist graph (build-once pattern)
```python
from src.knowledge_graph.graph_factory import create_knowledge_graph
kg = create_knowledge_graph(cfg.knowledge_graph)

# Build spec + test plan sub-graphs and save (no PR nodes yet)
kg.add_spec_documents(spec_chunks)
kg.add_test_plan_documents(test_plan_chunks)
kg.extract_matter_entities(spec_chunks + test_plan_chunks)
kg.export_json("data/knowledge_graph/matter_kg.json")

# Add transient PR nodes (not persisted)
kg.add_pr_documents(pr_chunks)
print(f"{kg.num_nodes} nodes, {kg.num_edges} edges")
```

## Warm load (reuse saved graph)
```python
kg = create_knowledge_graph(cfg.knowledge_graph)
kg.load_from_json("data/knowledge_graph/matter_kg.json")
kg.add_pr_documents(pr_chunks)  # always re-add fresh
```

## Search by Matter entities (hybrid RAG — graph leg)
```python
results = kg.search_by_entities(pr_chunk_text, max_results=10)
for node in results:
    print(node.node_type, node.label, node.properties.get("content", "")[:80])
```

## Find coverage gaps
```python
gaps = kg.get_coverage_gaps()
for gap in gaps:
    print(f"No test coverage: {gap.label} ({gap.properties.get('pr_url', '')})")
```

## Link PR change to test case manually
```python
from src.knowledge_graph.base_graph import EdgeType
kg.link_pr_to_test_cases("pr_node_id", ["tc_node_id1", "tc_node_id2"], EdgeType.COVERS)
```

## Export to JSON and Graphviz
```python
kg.export_json("reports/graph.json")
kg.export_graphviz("reports/graph.dot")
```

## Visualise with Graphviz CLI
```bash
dot -Tpng reports/graph.dot -o reports/graph.png
```

## Traverse neighbours (raw NetworkX)
```python
G = kg._graph   # nx.DiGraph
neighbors = list(G.successors("some_node_id"))
```

## Use Docker backend
```yaml
# config.yaml
knowledge_graph:
  backend: docker
  docker_url: http://localhost:8002
  docker_timeout: 30
```
```python
kg = create_knowledge_graph(cfg.knowledge_graph)  # DockerKnowledgeGraph; calls /health
kg.add_test_plan_documents(test_plan_chunks)       # POST /add_test_plan_documents
```

## Backward-compat import (old name still works)
```python
from src.knowledge_graph.graph_builder import KnowledgeGraphBuilder  # shim re-export
# canonical:
from src.knowledge_graph.matter_kg_builder import MatterKGBuilder
from src.knowledge_graph.base_graph import NodeType, EdgeType, GraphNode, GraphEdge
```

## Get all test cases from graph
```python
test_cases = kg.get_all_test_cases()   # List[GraphNode] where node_type == TEST_CASE
```
