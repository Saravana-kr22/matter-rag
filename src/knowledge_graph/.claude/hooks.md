# Knowledge Graph Module — Hooks

## When you add a new NodeType or EdgeType
- Add the enum value to `NodeType` / `EdgeType` in `base_graph.py` (not `matter_kg_builder.py`).
- Update the tables in `CLAUDE.md`.
- Update `get_coverage_gaps()` in `matter_kg_builder.py` if the new edge type should count as coverage.
- Update `DockerKnowledgeGraph` if it serialises edge types over the wire.
- Run `pytest tests/test_knowledge_graph.py -v`.

## When you add a new method to BaseKnowledgeGraph
- Add the abstract method to `base_graph.py`.
- Implement it in `matter_kg_builder.py`.
- Add a no-op or HTTP delegation in `docker_graph.py`.
- Update `CLAUDE.md` API table.

## When you change `add_spec_documents`
- `spec_chunks` come from `chunk_pr_node` (state key `spec_chunks`).
- Default node type is `REQUIREMENT` (normative text) — change only if needed.
- Re-run with `--build-knowledge-graph` to rebuild the saved graph.

## When you change `load_from_json` / `export_json`
- Verify round-trip: save → load → node/edge count unchanged.
- `export_json` uses `networkx.node_link_data()` format; `load_from_json` uses `node_link_graph()`.
- Delete `data/knowledge_graph/matter_kg.json` after schema changes — stale file will be re-read.

## When you change entity extraction (regex patterns)
- Update test fixtures in `tests/test_knowledge_graph.py`.
- Run a manual quality check on a real spec file with `relationship_extraction: true`.

## When you change export format
- `export_json` output must remain parseable by `networkx.node_link_graph()`.
- `export_graphviz` output must remain processable by `dot -Tpng`.

## Downstream impact
- `build_knowledge_graph_node` in `nodes.py` calls `add_spec_documents()`, `add_test_plan_documents()`, `export_json()`, `load_from_json()`.
- `search_knowledge_graph_node` in `nodes.py` calls `search_by_entities()`.
- Changes to `get_coverage_gaps()` change the `missing_tests` list in the final report.
