# Knowledge Graph Module — Rules

## BaseKnowledgeGraph rules
- All shared types (`NodeType`, `EdgeType`, `GraphNode`, `GraphEdge`) live in `base_graph.py` — never duplicate them in implementation files.
- Every new backend must subclass `BaseKnowledgeGraph` and implement all abstract methods.
- `graph_builder.py` is a backward-compat shim — never add logic to it; all code goes in `matter_kg_builder.py`.

## Graph construction rules
- Node IDs must be **stable** across runs (derived from path + section, not random UUIDs) so warm loads are consistent.
- Check `_graph.has_node(node_id)` before adding — never overwrite an existing node.
- `add_spec_documents()` defaults to `NodeType.REQUIREMENT` for all nodes (normative text) — override only for structural sections.
- `add_pr_documents()` always prefixes node IDs with `"pr_"` to avoid collisions.

## Persistence rules
- `export_json()` must be called **before** `add_pr_documents()` — PR nodes are transient and must not be persisted.
- `load_from_json()` must reconstruct `GraphNode` objects and attach them as `obj` attributes on NetworkX nodes.
- Both export methods must create parent directories (`Path(path).parent.mkdir(parents=True, exist_ok=True)`).

## Entity extraction rules
- Regex extraction is always run; LLM-based extraction is gated on `relationship_extraction: true`.
- Never make LLM calls inside the graph module at import time.
- Entity node IDs must be prefixed: `cluster_`, `cmd_`, `attr_` to avoid collisions with document nodes.

## Coverage gap rules
- `get_coverage_gaps()` checks outgoing edges of `PR_CHANGE` nodes for `COVERS`, `REFERENCES`, `TESTS` edge types.
- An empty edge list counts as a gap — no partial credit for unrecognised edge types.

## Dependency rules
- The graph module must not import from `src.database` or `src.search`.
- `base_graph.py` must not import from `matter_kg_builder.py` or `docker_graph.py` (avoids circular imports).
- LLM calls for entity extraction must go through `src.llm.llm_provider.get_llm()`.

## Factory rules
- Always use `create_knowledge_graph(config)` in pipeline nodes — never instantiate `MatterKGBuilder` directly.
- Docker backend calls `kg.load()` immediately after construction as a health-check.
