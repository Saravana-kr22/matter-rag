# Knowledge Graph Module

## Purpose
Build and query a directed knowledge graph over Matter protocol documents (DM XML, spec, test plans, PR changes).
Provides canonical schema nodes (from authoritative XML), requirement/behavior nodes (from spec), test coverage nodes
(from test plans), and PR change nodes (transient). Identifies coverage gaps via two-hop entity lookup.
Provides the **graph retrieval leg** of the hybrid RAG search.

## Files

| File | Class(es) | Role |
|---|---|---|
| `base_graph.py` | `BaseKnowledgeGraph`, `NodeType`, `EdgeType`, `GraphNode`, `GraphEdge` | ABC + shared types |
| `matter_kg_builder.py` | `MatterKGBuilder` (`KnowledgeGraphBuilder` alias) | NetworkX Matter protocol KG |
| `graph_builder.py` | — | Backward-compat shim re-exporting from `matter_kg_builder` |
| `graph_factory.py` | `create_knowledge_graph()` | Factory — dispatches to local or Docker backend |
| `docker_graph.py` | `DockerKnowledgeGraph` | HTTP client for remote Docker KG service |
| `knowledge_base.py` | `KnowledgeBaseBuilder`, `KnowledgeBase` | **Primary build path** — typed KB with spec_extractor + test_plan_extractor + kb_graph_builder |
| `schema.py` | `CanonicalSchema`, `SectionRecord`, `SpecRecord`, `TestCaseRecord`, `VectorChunkRecord`, `GraphBundle` | Typed KB data model; `TestCaseRecord.pics_codes` stores PICS codes extracted from TC HTML |
| `dm_pics_validator.py` | `ClusterPicsSchema`, `build_pics_map()`, `validate_pics_code()` | Parse DM XML → PICS prefix map; validate PICS code format, side, and entity existence |
| `spec_extractor.py` | — | Extracts `SectionRecord` + `SpecRecord` from spec HTML/text; cluster-inheritance via breadcrumb |
| `test_plan_extractor.py` | — | Extracts `TestCaseRecord` from test plan HTML/adoc; extracts PICS codes from TC step tables |
| `kb_graph_builder.py` | — | Builds `GraphBundle` (typed nodes + edges) from KB records; stores `pics_codes`, `entity_refs`, `adoc_section` in TC node properties; creates SECTION cross-reference edges (REFERENCES, depth 3) |
| `vector_chunk_gen.py` | — | Generates up to 4 `VectorChunkRecord` chunk types per `TestCaseRecord` |
| `rule_engine.py` | — | Requirement classification, TC mode detection, protocol prefix detection, entity matching, condition/effect extraction |

## Node Types (NodeType enum)

| Value | Meaning | Source |
|---|---|---|
| `CLUSTER` | Matter cluster entity | DM XML (authoritative) |
| `ATTRIBUTE` | Matter attribute entity | DM XML |
| `COMMAND` | Matter command entity | DM XML |
| `EVENT` | Matter event entity | DM XML |
| `FEATURE` | Matter feature-map entity | DM XML |
| `REQUIREMENT` | Spec requirement (shall/must sentence) | Matter spec HTML/adoc |
| `BEHAVIOR_RULE` | Behavioral constraint or conformance rule | Matter spec HTML/adoc |
| `TEST_CASE` | Test case section node | Test plan adoc/HTML |
| `SECTION` | Generic AsciiDoc section or file node | any doc |
| `PR_CHANGE` | Changed file from a GitHub PR | PR diff (transient) |
| `PROMPT_SECTION` | Consolidated spec text for system prompt injection | Built from SECTION nodes during KG build |

### `PROMPT_SECTION` Nodes

`build_prompt_sections()` on `MatterKGBuilder` consolidates spec sections on Conformance,
Access, and Other Qualities into three `PROMPT_SECTION` nodes (one per major section).
Called automatically during `build_knowledge_graph_node` before `export_json`.
Node ID format: `PROMPT_SECTION::<label>` (e.g. `PROMPT_SECTION::Conformance`).

Matching is **number-agnostic**: section numbers are stripped from both the `path_prefix`
config value and the KG `section_path` before comparing.  `"Data Model Specification > Conformance"`
matches `"7. Data Model Specification > 7.3. Conformance"`, `"8. Data Model Specification > 8.3. Conformance"`, etc.
This means the config survives spec chapter renumbering without any edits.

Old configs with numbers (e.g. `"7. Data Model Specification > 7.3. Conformance"`) continue
to work — numbers are stripped before comparison so the match still succeeds.

`_build_analysis_system_prompt()` reads these nodes directly (O(3) lookup) instead of scanning
all SECTION nodes with regex. Falls back to regex scan for old KG JSONs without PROMPT_SECTION
nodes (backward-compatible).

Verify after a KG rebuild:
```bash
python3 -c "
import json
kg = json.load(open('data/knowledge_graph/matter_kg.json'))
ps = [n for n in kg['nodes'] if n.get('node_type') == 'PROMPT_SECTION']
for n in ps: print(n['id'], n['properties']['chars'], 'chars')
"
```

## Edge Types (EdgeType enum)

| Value | Meaning |
|---|---|
| `covers` | Test case covers a requirement or entity |
| `references` | Node references another (includes SECTION cross-references) |
| `derived_from` | Node derived from another |
| `conflicts_with` | Conflicting nodes |
| `belongs_to` | Entity belongs to a cluster |
| `tests` | Test case tests an entity |
| `impacts` | PR change impacts an entity |
| `implements` | Test case implements a requirement |
| `validates` | Node validates another |
| `related_to` | Generic relationship |
| `alias_of` | Cluster is an alias of a base cluster (derived → parent) |

### Section Cross-Reference Edges

`_add_section_crossref_edges()` runs after all SECTION nodes are created during KG build.
It parses each SECTION node's `full_text` for patterns like "See Section 4.3.7", "refer to 7.3.2",
"clause 11.7.1.8" and creates `REFERENCES` edges between SECTION nodes.

- Max 10 outgoing references per section (avoids noise from heavily cross-referenced sections)
- `_collect_cluster_section_text()` follows these edges up to depth 3 when collecting
  spec text for the expand prompt (BFS traversal, no circular references)

## Primary Build Path — `KnowledgeBaseBuilder`

The pipeline (since April 2026) uses `KnowledgeBaseBuilder.build()` as the single entry point
for both the KG and the vector DB. It runs three extraction stages in sequence:

```
KnowledgeBaseBuilder.build(data_model_docs, spec_docs, test_plan_docs)
    │
    ├── DataModelExtractor  → CanonicalSchema  (clusters, attributes, commands, events, features)
    │
    ├── spec_extractor.extract_spec_sections_and_records()
    │       HTML spec parsed via html_semantic_parser.parse_spec()
    │       Section-cluster inheritance: heading → breadcrumb guard (section_path key)
    │       → List[SectionRecord], List[SpecRecord]
    │
    ├── test_plan_extractor.extract_test_cases()
    │       → List[TestCaseRecord]
    │
    ├── kb_graph_builder.build_graph()
    │       → GraphBundle (List[GraphNodeRecord], List[GraphEdgeRecord])
    │
    └── vector_chunk_gen.generate_vector_chunks(tc_records)
            → List[VectorChunkRecord]  ← up to 4 chunk types per TC
```

Returns a `KnowledgeBase` dataclass with:
- `canonical_schema: CanonicalSchema`
- `section_records: List[SectionRecord]`
- `spec_records: List[SpecRecord]`
- `test_case_records: List[TestCaseRecord]`
- `graph: GraphBundle`
- `vector_chunks: List[VectorChunkRecord]`

## Vector Chunk Types (per TestCaseRecord)

`vector_chunk_gen.generate_vector_chunks()` produces up to **4 chunk types** per TC:

| Type | Content | Metadata |
|---|---|---|
| `full` | title + cluster + purpose + prerequisites + procedure steps + outcomes | tc_id, cluster, intents, entity_refs |
| `intent_summary` | TC-ID + title + cluster + intents + entity refs (dense, short) | same |
| `procedure` | Numbered procedure steps only | same |
| `setup` | Prerequisites + test environment + DUT type | same |

These are converted to `Document` objects in `chunk_embed_test_plans_node` and embedded into FAISS.
Rich metadata (`tc_id`, `cluster`, `intents`, `entity_refs`, `chunk_type`) is stored in the FAISS
sidecar JSON so search results can be decoded without hitting the KG.

## Parallel Spec Extraction

`extract_spec_sections_and_records()` uses `ProcessPoolExecutor` to process multiple spec HTML
documents in parallel, which gives near-linear speedup for builds with multiple spec files
(e.g. 2 docs → ~2x faster than serial 671 s → ~335 s).

```python
section_records, spec_records, rejected = extract_spec_sections_and_records(
    spec_docs,
    canonical_schema=canonical_schema,
    max_workers=0,      # 0 = auto (min(doc_count, cpu_count, 8))
    output_dir=run_dir, # write rejected log to <run_dir>/spec_extractor_rejected_records.txt
)
```

Worker count is capped at `min(doc_count, requested_workers, 8)`.  macOS uses `spawn` as the
multiprocessing start method; the `_init_worker_paths()` module-level initializer adds the
project root to `sys.path` in each worker so internal imports resolve correctly.

After all workers finish, `_renumber_spec_records()` assigns globally unique sequential IDs
across all documents (worker-local counters start at 0 and would collide without renumbering).

## Debug Log Files (written to `run_dir` during KB build)

| File | Stage | Contents |
|---|---|---|
| `spec_extractor_rejected_records.txt` | spec_extractor | Sentences filtered as non-normative: reason summary + per-entry detail (too_short, glossary_row, table_fragment, etc.) |
| `vector_chunks_ignored_or_rejected.txt` | vector_chunk_gen | `TestCaseRecord`s that produced zero chunks (all 4 chunk types empty) |
| `pr_chunks_ignored_or_rejected.txt` | semantic_chunker | PR diff segments rejected as too-short (< 80 chars) |

These files are written only when `output_dir` is non-empty and when at least one item was rejected.
They use the naming convention `<stage>_ignored_or_rejected.txt` for easy discovery.



`extract_spec_sections_and_records()` tracks a `current_cluster_ctx` forward-pass variable
so that subsections like `11.7.5. Cluster ID` inherit their parent's cluster
(`11.7. Push AV Stream Transport Cluster`):

1. Try direct heading match against `cluster_name_set`
2. Try breadcrumb path (`section_path`) match — fixed April 2026 to read the correct key
   (`section_path`) from `html_semantic_parser.parse_spec()` output (was reading `path`)
3. Inherit from `current_cluster_ctx` if the breadcrumb still contains that cluster name
4. Reset context when breadcrumb no longer includes the previous cluster

## MatterKGBuilder API (legacy / FastAPI app)

Used by the FastAPI debug app and warm-load path. The `KnowledgeBaseBuilder` output is
bridged into `MatterKGBuilder` via `_import_graph_bundle()` in `src/engine/nodes.py`.

```python
from src.knowledge_graph.graph_factory import create_knowledge_graph
kg = create_knowledge_graph(cfg.knowledge_graph)  # returns MatterKGBuilder or DockerKnowledgeGraph

# Warm run: restore from disk + re-add PR nodes
kg.load_from_json(graph_store_path)
kg.add_pr_documents(pr_chunks)

# Query — precise (preferred when pr_changes available)
results = kg.search_by_structured_change(cluster, entity_type, entity_name, max_results=10)

# Query — regex fallback
results = kg.search_by_entities(pr_text, max_results=10)   # List[GraphNode]

# Coverage
gaps = kg.get_coverage_gaps()              # PR_CHANGE nodes with no linked TEST_CASE nodes
related = kg.find_related(node_id, depth=2)

# Chat-path queries (called by LLM query planner in search_knowledge_graph_node)
all_tcs = kg.get_test_cases_for_cluster("On/Off Cluster")   # all TCs for a cluster (no top-k cap)
cov = kg.find_entity_coverage("On/Off Cluster", "attribute", "OnOff")   # entity coverage status
reqs = kg.find_requirements_and_coverage(keywords, cluster="On/Off Cluster")
deps = kg.get_cluster_dependencies("On/Off Cluster", direction="incoming_depends_on")
```

### `get_test_cases_for_cluster(cluster_name) -> List[GraphNode]`

Returns all TEST_CASE nodes whose cluster property matches `cluster_name` (case-insensitive
substring). Falls back to edge traversal (TESTS / IN_CONTEXT edges to CLUSTER node) for TCs
that don't carry the cluster property. **No top-k cap** — all matching TCs are returned so
the LLM can give an accurate count.

### `find_entity_coverage(cluster, entity_type, entity_name) -> dict`

Returns:
```python
{
    "entity_exists": bool,
    "covered": bool,
    "entity_node": GraphNode | None,
    "test_cases": List[GraphNode],
}
```

### `find_requirements_and_coverage(keywords, cluster, requirement_types, ...) -> dict`

Returns:
```python
{
    "covered": {req_node_id: [tc_nodes...]},   # REQs with linked TCs
    "uncovered": [req_nodes...],               # REQs with no TC coverage
}
```

### `get_cluster_dependencies(cluster_name, direction) -> List[GraphNode]`

Traverses DEPENDS_ON / REQUIRES / REFERENCES / RELATED_TO edges to find related CLUSTER nodes.

| `direction` | Returns |
|---|---|
| `"incoming_depends_on"` (default) | Clusters that depend **on** the named cluster (callers) |
| `"outgoing_depends_on"` | Clusters that the named cluster depends **on** (dependencies) |

Used by the chat query planner when intent = `"graph_traversal"` — e.g. *"what clusters
depend on On/Off cluster?"*

## Session Fixes (2026-05-01)

### KG Builder Edge Quality Fixes

**Cross-cluster bleeding fixed** (`test_plan_extractor.py`): `_link_spec_records()` now uses
entity name (`parts[2]`) for spec_index lookup instead of cluster name (`parts[1]`), plus a
cluster guard that only links requirements matching the TC's own cluster. Eliminates ~5,500
spurious cross-cluster `verifies_requirement` edges (e.g., Mode clusters → Window Covering).

**Cluster name normalization** (`test_plan_extractor.py`): `_normalize_cluster_key()` strips
trailing " Cluster" suffix for consistent index lookup. Reduces unverified requirements from
77% toward ~30-40%.

**`verifies_attribute` type guard** (`rule_engine.py`): If `chosen_edge == VERIFIES_ATTRIBUTE`
and entity type is COMMAND, overrides to `TESTS_COMMAND`. Fixes 132 semantically incorrect edges.

### VirtualCluster Completeness

`_PROTOCOL_TC_TO_AREA_SLUG` expanded with 9 new prefixes: DT, SU, BR, GC, SM, RR, ICDB,
WEBRTC, PAVSTI. Deferred creation pass after TC layer creates VirtualCluster nodes for any
`CLUSTER::VirtualCluster-*` edge targets that don't have corresponding nodes.

### Deterministic Search Results

All 12 KG search methods (14 sort operations) now sort results by `node_id` before returning.
Score-sorted methods use `(-score, node_id)` as tiebreaker. Eliminates prompt-level
non-determinism from dict iteration order.

### HTML Parser Fixes

**TC heading regex**: `_TC_HEADING_RE` changed from `\[TC-...\]` to `\[TC-...\]?` —
tolerant of missing closing bracket. Recovers TC-PAVST-2.12/2.13.

**`_SKIP_TITLE_RE`**: `|toc|` changed to `|\btoc\b|` (word boundaries). Was incorrectly
skipping TCs with "Protocol", "AutoCloseTime", "MoveToClosestFrequency" in titles.
Recovers 7 TCs (ICDB-1.1/1.2/1.3, LVL-7.1, VALCC-4.3/4.4, WEBRTCP-2.26).

### Vector Chunk Splitting

`vector_chunk_gen.py`: `full` and `procedure` chunks exceeding `_MAX_CHUNK_CHARS` (1800)
are split at paragraph/sentence boundaries with TC context prefix
(`[TC-ID] Title — Cluster (part N/M)`). `intent_summary` and `setup` never split.
Fixes 48% of `full` chunks that exceeded the BGE 512-token embedding limit.

## Build-once Persistence

| Sub-graph | Nodes | Persisted? |
|---|---|---|
| DM XML schema | CLUSTER, ATTRIBUTE, COMMAND, EVENT, FEATURE | Yes — included in `graph_store_path` |
| Matter spec | REQUIREMENT, BEHAVIOR_RULE, SECTION | Yes |
| Test plans | TEST_CASE, SECTION | Yes |
| PR changes | PR_CHANGE | No — added fresh each run |

`build_knowledge_graph_node` auto-builds if `graph_store_path` file is absent (first-run self-heal).

## Precise KG Search — `search_by_structured_change(cluster, entity_type, entity_name, max_results)`

Used by `search_knowledge_graph_node` when `pr_changes` are available:

1. Normalise `cluster` and `entity_name` to slugs (lowercase, spaces→underscores)
2. Look up stable node ID: `attr_{cluster_slug}_{name_slug}`, `cmd_...`, etc.
3. Build 2-hop undirected ego_graph around the schema node
4. Collect all TEST_CASE / REQUIREMENT nodes within 2 hops
5. Return up to `max_results` nodes

## Regex Search — `search_by_entities(text, max_results)`

Fallback when no structured change record is available:

1. Extract Matter entities from text via regex (cluster, command, attribute names)
2. Score each TEST_CASE / REQUIREMENT node by entity-name hits in label + content
3. Return top `max_results` by score

## Backends

### Local (`backend: local`) — default
NetworkX `DiGraph` running in-process. JSON export/import via `export_json()` / `load_from_json()`.
`load_from_json()` uses `nx.node_link_graph()` and reconstructs `GraphNode` objects with `NodeType` enums.

### Docker (`backend: docker`)
`DockerKnowledgeGraph` — HTTP client calling a pre-built Docker service on `docker_url` (default `http://localhost:8002`).
`add_data_model_documents()` → POST `/add_data_model_documents`.
`load_from_json()` is a no-op (service manages its own state).

## `KnowledgeBaseBuilder.build()` Signature

```python
kb = KnowledgeBaseBuilder().build(
    data_model_docs=data_model_fetched,   # FetchedDocument list, role="data_model"
    spec_docs=spec_chunks,                # Document list, role="spec"
    test_plan_docs=test_plan_fetched,     # FetchedDocument list, role="test_plan"
    output_dir=run_dir,                   # write per-stage rejected logs here
    max_workers=cfg.knowledge_graph.spec_extractor_workers,  # parallel spec parsing
)
```

`output_dir` and `max_workers` are threaded through to:
- `extract_spec_sections_and_records()` (parallel parsing + rejected log)
- `generate_vector_chunks()` (ignored TC log)

## Config
```yaml
knowledge_graph:
  backend: local
  graph_store_path: data/knowledge_graph/matter_kg.json
  spec_extractor_workers: 0   # 0 = auto; set 4–8 to control parallel spec HTML parsing
  max_depth: 3
  docker_url: http://localhost:8002
  docker_timeout: 30
```

## PICS Validation Support

`dm_pics_validator.py` parses every DM XML file in `config.analysis.dm_dir` and builds a map
keyed by PICS prefix (e.g. `"OO"`) → `ClusterPicsSchema`:

```python
from src.knowledge_graph.dm_pics_validator import build_pics_map
pics_map = build_pics_map(Path("data/data_model"))
oo = pics_map["OO"]
print(oo.cluster_name)          # "On/Off Cluster"
print(list(oo.server_attrs.items())[:2])  # [(0, "OnOff"), (16384, "GlobalSceneControl")]
```

`ClusterPicsSchema` fields: `pics_code`, `cluster_name`, `server_attrs`, `client_attrs`,
`server_cmds`, `client_cmds`, `features`.

`format_schema_text()` annotates each entity with conformance tags: `(M)` for mandatory (all
devices must implement), `(O)` for optional (entity PICS required, no feature PICS required),
and `(X)` for disallowed/deprecated entities.

`TestCaseRecord.pics_codes` is a `List[str]` populated by `test_plan_extractor` from `<td>` cells
matching `r'\b([A-Z]{1,8}\.[SCM]\.[ACEF][0-9A-Fa-f]{4,6})\b'`. Stored in TC KG node properties.

Protocol-level PICS (BLE, Thread, WiFi, TCP, QR commissioning) are **not** in DM XML — the LLM
uses its own Matter protocol knowledge to detect missing protocol PICS in test steps.

## Rule Engine (`rule_engine.py`)

Provides requirement classification, TC mode detection, and entity matching used during KB build.

### `_DEFINITELY_PROTOCOL_TC_PREFIXES`

Frozenset of TC ID prefixes that identify protocol-behavior tests (not cluster-centric). When a TC ID
matches, `_infer_cluster` returns `""` immediately (no entity matching), preventing wrong cluster assignments.
A backward-compat alias `_PROTOCOL_TC_PREFIXES` is kept for external callers.

```python
_DEFINITELY_PROTOCOL_TC_PREFIXES = frozenset({
    "IDM",     # Interaction Data Model
    "SC",      # Secure Channel
    "BDX",     # Bulk Data eXchange
    "DD",      # Device Discovery / commissioning flows
    "DA",      # Device Attestation
    "ACE",     # Access Control
    "MC",      # Multicast / commissioning
    "JFADMIN", # Joining Fabric Administrator
    "JF",      # Joining Fabric (short alias)
    "MCORE",   # Matter Core protocol
    "DT",      # Device Type
    "SU",      # Software Update
})
```

`_is_protocol_prefix(prefix)` is the public API for checking prefixes. It uses a 4-step
decision: (1) check `_DEFINITELY_PROTOCOL_TC_PREFIXES`, (2) check DM-XML-derived cluster
prefixes (if configured), (3) check `_known_cluster_prefixes` (runtime-configurable), (4)
heuristic fallback. `configure_known_cluster_prefixes()` sets the runtime cluster prefix set
(thread-safety: call once at startup before parallel builds).

### `_TC_PREFIX_TO_PROTO_AREA` (in `test_plan_extractor.py`)

Maps TC prefixes to protocol-area phrases used to link spec REQUIREMENT nodes via the `_proto:` secondary
index in `_build_spec_index`. Both `_PROTOCOL_TC_PREFIXES` and this dict must be kept in sync when adding
new protocol TC families.

### `_build_spec_index` secondary `_proto:` index

`_build_spec_index` creates both cluster-keyed and `_proto:<chapter>` keyed entries. Protocol-level
REQUIREMENT nodes (no cluster) are keyed by the first 5 words of their spec section path (e.g.
`_proto:interaction data model read`). `_link_spec_records` uses token-set intersection against protocol
area words to find matching spec records for protocol-family TCs.

### Requirement rescue rules (in `classify_requirements`)

- **Case A** (too_short, has subject): short sentence with modal verb + ≥4 words → valid
- **Case B** (too_short, fragment-modal): starts with SHALL/MUST + ≥4 words + `source_section` available
  → reconstructed as `"<section heading>: <SHALL predicate>"`
- **Table fragment tail**: `_MULTI_PIPE_RE` tail check uses `_NORMATIVE_WORD_RE` (includes
  `mandatory`, `required`, `prohibited`, `forbidden`) in addition to SHALL/MUST

## LLM Spec Refinement (`llm_spec_refiner.py`)

Optional pass over spec HTML that uses the LLM to add cross-cluster `DEPENDS_ON` + `REQ→entity REFERENCES`
edges that rule-based extraction cannot find. Controlled by:

```yaml
knowledge_graph:
  llm_refinement_enabled: false          # true → always run on KG build
  llm_refinement_max_sections: 200       # cost cap
  llm_refinement_cache_path: data/knowledge_graph/spec_refiner_cache.json
  llm_refinement_provider: ""            # "" = global llm config | "local" | "claude_cli" | "claude_subprocess"
  llm_refinement_local_model: ""         # Ollama model name when provider = "local"
```

`build_knowledge_graph_node` (in `src/engine/nodes.py`) copies `LLMConfig` with the override
provider/model before passing it to `LLMSpecRefiner`, so refinement can run on Ollama while the rest
of the pipeline uses a frontier Claude model. Also triggered by `--build-knowledge-graph-withLLM` CLI flag.
