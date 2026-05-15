# Engine Module

## Purpose
LangGraph-based pipeline orchestrator that wires 18 node functions into a directed `StateGraph` with conditional routing.

## Files

| File | Role |
|---|---|
| `nodes.py` | 18 node functions + `PipelineState` TypedDict + LLM prompt helpers + `_import_graph_bundle()` |
| `pipeline.py` | `MatterRAGPipeline` class + `create_pipeline()` convenience factory |
| `run_context.py` | `RunContext` dataclass + `RunAwareFileHandler` — per-run log routing via `contextvars.ContextVar` |
| `coverage_analysis_nodes.py` | 5-node pipeline for spec requirement coverage gap detection |
| `pics_analysis_nodes.py` | 6-node pipeline for PICS code validation |
| `sdk_coverage_nodes.py` | 6-node pipeline for SDK implementation vs spec requirement coverage |
| `graphs/cli_graph.py` | LangGraph wiring for the main CLI pipeline |
| `graphs/chat_graph.py` | LangGraph wiring for the FastAPI chat path |
| `graphs/coverage_analysis_graph.py` | LangGraph wiring for coverage analysis pipeline |
| `graphs/pics_analysis_graph.py` | LangGraph wiring for PICS analysis pipeline |
| `graphs/sdk_coverage_graph.py` | LangGraph wiring for SDK coverage pipeline |

## Pipeline DAG
```
fetch_documents_node
    → process_documents_node
        → ingest_data_model_node        ← pass-through + inspection JSON (DM XML)
            → build_matter_schema_node  ← extract canonical schema from spec diff HTML
                → chunk_embed_test_plans_node  ← build or load test plan vector DB (once)
                    → chunk_pr_node            ← chunk PR + spec docs
                        → extract_pr_changes_node  ← structured change extraction (rule + LLM)
                            → build_knowledge_graph_node  ← build or load KG (once)
                                │
                                ├─[no pr_chunks] → cleanup_node → END   (index-only / build-only runs)
                                │
                                └─[pr_chunks present]
                                    → search_test_plan_vector_db_node   (top-k FAISS hits)
                                        → search_knowledge_graph_node   (top-k KG nodes)
                                            → analyze_chunks_with_llm_node  (1 LLM call / PR chunk)
                                                → cluster_review_node   (cluster audit, finalizes TC list)
                                                    → second_pass_tc_gen_node  (holistic gap fill)
                                                        → human_outline_expand_node  (3rd pass / --third-pass-expand)
                                                            → write_adoc_updates_node
                                                                → write_updated_testplan_node
                                                                    → generate_report_node
                                                                        → cleanup_node → END
```

`cleanup_node` is **always the last node** on both terminal paths.

The FastAPI debug app exposes a `/pipeline` endpoint that renders this DAG as a Mermaid.js
visualization with live status coloring from the latest `pipeline_progress.json`.

Each run writes a timestamped log directory: `logs/ghpr_analysis_<YYYYMMDD_HHMMSS>/`

## PipelineState (TypedDict, total=False)

| Field | Type | Set by |
|---|---|---|
| `config` | `AppConfig` | caller |
| `pr_url` | `str` | caller |
| `input_doc` | `str` | caller |
| `cluster_filter` | `str` | caller (`""` = all clusters) |
| `build_test_plan_vectors` | `bool` | caller |
| `build_knowledge_graph` | `bool` | caller |
| `build_knowledge_graph_with_llm` | `bool` | caller — run LLM spec-refinement pass after KG build |
| `output_dir` | `str` | caller |
| `run_dir` | `str` | `pipeline.run()` |
| `pr_documents` | `List[FetchedDocument]` | `fetch_documents_node` |
| `test_plan_fetched` | `List[FetchedDocument]` | `fetch_documents_node` |
| `spec_fetched` | `List[FetchedDocument]` | `fetch_documents_node` (role="spec") |
| `data_model_fetched` | `List[FetchedDocument]` | `fetch_documents_node` (role="data_model") |
| `matter_schema` | `Dict[str, Any]` | `build_matter_schema_node` |
| `test_plan_chunks` | `List[Document]` | `chunk_embed_test_plans_node` |
| `spec_chunks` | `List[Document]` | `chunk_pr_node` |
| `pr_chunks` | `List[Document]` | `chunk_pr_node` |
| `pr_changes` | `List[Dict]` | `extract_pr_changes_node` |
| `vector_store` | `VectorStore` | `chunk_embed_test_plans_node` |
| `knowledge_graph` | `BaseKnowledgeGraph` | `build_knowledge_graph_node` |
| `built_knowledge_base` | `Any` | `chunk_embed_test_plans_node` — cached `KnowledgeBase`, reused by `build_knowledge_graph_node` |
| `search_results` | `Dict[str, List[SearchResult]]` | `search_test_plan_vector_db_node` |
| `graph_results` | `Dict[str, List[GraphNode]]` | `search_knowledge_graph_node` |
| `graph_coverage_notes` | `Dict[str, str]` | `search_knowledge_graph_node` (chat path coverage summary per chunk) |
| `chat_query_intent` | `str` | `search_knowledge_graph_node` (chat path — planner intent, e.g. `"list_test_cases"`) |
| `analysis_results` | `List[dict]` | `analyze_chunks_with_llm_node` |
| `missing_tests` / `update_candidates` | `List[dict]` | `analyze_chunks_with_llm_node` |
| `llm_reply` | `str` | `analyze_chunks_with_llm_node` (chat path — returned to session) |
| `chat_history` | `List[dict]` | caller (chat path — session message history) |
| `system_prompt` | `str` | caller (chat path) |
| `adoc_output_paths` | `List[str]` | `write_adoc_updates_node` |
| `report_path` | `str` | `generate_report_node` |
| `errors` | `List[str]` | any node |
| `fatal_error` | `bool` | any node |
| `include_coverage_gaps` | `bool` | caller (default True; `--no-coverage-gaps` sets False) |
| `coverage_gap_tests` | `List[dict]` | `second_pass_tc_gen_node` — TCs for pre-existing uncovered requirements (separate from `missing_tests`) |
| `pass_stats` | `Dict[str, Any]` | `analyze_chunks_with_llm_node` / `cluster_review_node` / `second_pass_tc_gen_node` — per-pass TC counts for the pipeline funnel summary in the HTML report |

## Node Responsibilities

| Node | Key actions |
|---|---|
| `fetch_documents_node` | Load sources from `sources.json`; accept `--input-doc` local file; route by role → `pr_documents` / `test_plan_fetched` / `spec_fetched` / `data_model_fetched`. Also loads additional/overlay sources from `config.analysis` fields (`additional_sources_file`, `additional_test_plans_dir`, `additional_spec_dir`). |
| `process_documents_node` | Apply `.ignore_rules.json` + per-source rules; expand matter_diff HTML |
| `ingest_data_model_node` | Write `data_model_schema.json`; pass `data_model_fetched` through |
| `build_matter_schema_node` | Extract canonical entity tables from spec diff HTML |
| `chunk_embed_test_plans_node` | **Build path**: runs `KnowledgeBaseBuilder.build()` → TC-aware chunks (4 types/TC) → embed → FAISS; caches `KnowledgeBase`. **Load path**: `store.load()` from disk. |
| `chunk_pr_node` | Chunk `pr_documents` → `pr_chunks`; chunk `spec_fetched` → `spec_chunks`; apply `cluster_filter`. Sections < `min_chunk_chars` are rejected and logged. |
| `extract_pr_changes_node` | Run `ChangeExtractor` on each PR chunk (rule-based first, LLM fallback when confidence < threshold); write `pr_changes.json` |
| `build_knowledge_graph_node` | **Build path**: reuses `built_knowledge_base` from state if available (no double-build); otherwise runs `KnowledgeBaseBuilder.build()` fresh. Imports `GraphBundle` into `MatterKGBuilder` via `_import_graph_bundle()`. Builds `PROMPT_SECTION` nodes (spec sections 7.3/7.6/7.7 consolidated for system prompt). Saves to `graph_store_path` + per-source sub-graphs. Adds transient PR_CHANGE nodes. Optionally runs `LLMSpecRefiner`. **Load path**: `kg.load_from_json()` + add PR nodes. |
| `search_test_plan_vector_db_node` | Per-PR-chunk vector search: top-`search_top_k` FAISS hits |
| `search_knowledge_graph_node` | **CLI path**: `search_by_structured_change()` or `search_by_entities()`. **Chat path**: calls `_plan_chat_query()` (1 LLM call) → structured plan → KG dispatcher; saves intent in `chat_query_intent` state field. |
| `analyze_chunks_with_llm_node` | **CLI path**: rerank + 1 LLM call per PR chunk. Context injection: Tier 1 (cluster spec prose from KG), Tier 2 (`--spec-sections`), Tier 3 (`--llm-additional-context`). **Chat path**: delegates to `_analyze_chat_path()`. |
| `cluster_review_node` | Cluster-level LLM audit pass: checks symmetry gaps, missing test types, duplicate coverage; writes `cluster_review_<ts>.md` |
| `second_pass_tc_gen_node` | Two-part: (1) **Consolidation** — dedup-only single LLM call that removes duplicates from Pass 1 proposals (no gap-filling). (2) **Coverage gaps** — if `include_coverage_gaps` is True, queries KG for uncovered requirements, runs outline → expand to generate gap TCs stored in `coverage_gap_tests`. Skips abstract base clusters with siblings. Protocol clusters bridged via `_PROTOCOL_CHAPTER_TO_VC`. |
| `human_outline_expand_node` | 3rd-pass re-expansion of a human-edited TC outline JSON (`--third-pass-expand`). Re-expands only non-existing TCs and merges with pass-1 results. |
| `write_adoc_updates_node` | Write LLM-suggested TC updates back to `.adoc` source files. Uses deduplicated `state["missing_tests"]` + `state["update_candidates"]` (NOT raw `analysis_results`) to avoid duplicate TC headings. |
| `write_updated_testplan_node` | Write per-cluster updated `.adoc` test plan files to `reports/updated_testplans_<ts>/`; requires `role='test_plans_adoc_folder'` source |
| `generate_report_node` | Write Markdown + structured JSON + HTML report to `output_dir` |
| `cleanup_node` | **Always last**. Releases MPS/CUDA memory, runs GC, logs run summary. |

## Cross-Cluster TC Filtering

`_collect_cluster_tc_nodes(kg, cluster_name)` post-filters `kg.get_test_cases_for_cluster()`
results by the TC's own `cluster` property. This prevents TC contamination from KG Pass-2
edge traversal: TCs from other clusters that reference On/Off PICS codes (e.g. TC-CC-*,
TC-BIND-*) have `tests → CLUSTER::On/Off` edges and would otherwise appear in On/Off results.

`_format_graph_results(nodes, primary_cluster)` (Section B) applies the same filter and
renders TCs in readable form (`**TC-ID** — title / Purpose: ...`) instead of raw KG notation.

## `second_pass_tc_gen_node` — Design Decisions

This node runs after `cluster_review_node` to holistically generate TCs for clusters that are
sparse or heavily gapped. It uses an outline → expand 2-call LLM flow per cluster.

### Trigger conditions (per cluster)

| Condition | Action | Rationale |
|---|---|---|
| `pass1_missing > 5` | **Always trigger** | First-pass found >5 gaps in this cluster — the PR itself created heavy changes here; holistic coverage clearly needed regardless of existing TC count. |
| `existing_count < 5 AND is_pr_relevant` | **Trigger** | Cluster is sparse AND the current PR actually touched it (appeared in `pr_changes`, `missing_tests`, `update_candidates`, `analysis_results`, or `cluster_review_additions`). |
| Any other case | **Skip** | Well-covered cluster with few gaps, or sparse cluster the PR didn't touch — no second pass needed. |

**Why `is_pr_relevant` matters**: Without this guard, every sparse cluster in the entire KG
triggers second pass on every PR run, even if the PR has nothing to do with those clusters.
A 1-chunk commissioning PR was generating TCs for Account Login (2 existing TCs, 0 gaps) and
Actions (3 existing TCs, 0 gaps) — burning LLM calls with zero value. The fix: sparse-TC trigger
only fires for clusters that appear somewhere in the current run's analysis output.

### `is_pr_relevant` computation

Built from five state fields (all lowercased, substring-matched against `cluster_lower`):
```python
pr_relevant_clusters = {
    cluster from pr_changes,
    cluster from missing_tests,
    cluster from update_candidates,
    cluster from cluster_review_additions,
    cluster from analysis_results,
}
```

### Lifecycle flow test cases (`test_type=lifecycle_flow`)

The outline prompt explicitly asks the LLM to propose at least one `lifecycle_flow` TC per cluster
that chains multi-step operations (allocate → configure → activate → trigger → verify → cleanup).
When the expand prompt receives `test_type=lifecycle_flow`, `_LIFECYCLE_FLOW_GUIDANCE` (a 9-step
skeleton) is injected into the prompt. This generates end-to-end flow TCs rather than only
feature-isolated unit tests.

TC types accepted in outline JSON: `"unit"`, `"lifecycle_flow"`, `"negative"`.

---

## `write_adoc_updates_node` — TC Numbering Design

**Bug context**: When multiple PR chunks trigger LLM calls in `analyze_chunks_with_llm_node`,
each call independently assigns TC IDs (e.g. TC-PAVST-2.12, TC-PAVST-2.12 again for a different
chunk). `_deduplicate_missing_tc_ids()` runs after all chunks to resolve these collisions by
incrementing minor version numbers.

**Critical invariant**: `write_adoc_updates_node` must use the *deduplicated* state fields
(`state["missing_tests"]`, `state["update_candidates"]`) — **never** the raw `analysis_results`
list (which still carries pre-dedup duplicate TC IDs). The node builds a synthetic single-item
results list from the already-deduplicated state:

```python
synthetic_results = [{
    "missing_tests": state.get("missing_tests", []),
    "update_candidates": state.get("update_candidates", []),
}]
paths = updater.write_updates(synthetic_results, search_results, output_dir, tc_index=tc_index)
```

If you ever refactor this node, preserve this invariant or duplicate TC headings will reappear.

---

## LLM Call Count

One LLM call per PR chunk batch in `analyze_chunks_with_llm_node`, plus optional LLM calls in `extract_pr_changes_node`.
`cluster_review_node` adds 1 call per cluster.
`second_pass_tc_gen_node` adds per triggered cluster: 1 consolidation call (dedup-only) + if coverage gaps enabled: 1 outline call + N expand calls (1 per gap TC).
`human_outline_expand_node` adds 1 call per TC entry in the human outline.

**Prompt truncation**: `_truncate_prompt_if_needed(prompt, config, label)` truncates prompt tail
if it exceeds `config.llm.max_prompt_chars`. Applied to cluster_review and Pass 2 consolidation prompts.

**Crash recovery**: `pass1_results_<ts>.json` written to output_dir after `analyze_chunks_with_llm_node`.
`pipeline_progress.json` updated in run_dir after 7 key nodes (fetch, chunk_pr, build_kg, analyze, cluster_review, second_pass, generate_report).

**Deterministic TC numbering**: `_deduplicate_missing_tc_ids` assigns sequential minor versions by alphabetically sorting title text (stripped of TC-ID). Same TCs always get same numbers regardless of LLM generation order.

**Template echo detection**: Warn-only — never discards LLM data. Logs WARNING + sets `template_echo_warning` in metadata.

**JSON extraction (`_parse_structured_response`)**: Uses a 4-layer parsing strategy:
1. **Code-fenced JSON** — regex extraction from ` ```json ... ``` ` blocks.
2. **Balanced-brace extraction** — `_extract_json_object()` tracks brace depth (respects string escaping) to find the first complete top-level JSON object, replacing the old `find('{')`/`rfind('}')` approach.
3. **Truncation recovery** — if braces are unbalanced (LLM output was cut off), appends the missing closing braces and retries parse.
4. **Nested format recovery** — if the parsed JSON has no top-level `missing_tests`/`update_candidates` but wraps them inside a `recommendation` object, hoists them to the top level.

All layers also try `_repair_json()` (fixes unquoted TC-IDs and trailing commas) on parse failure.

**Pass stats (`pass_stats`)**: Accumulated across three nodes into a single dict with keys `pass1`, `pass2`, `pass3`. Used by `_build_pass_funnel_html()` to render a collapsible pipeline funnel summary in the HTML report showing chunk counts, new TCs, updates, duplicates removed, and coverage gap TCs per pass.

**Chat path**: 2 calls per user message — 1 for `_plan_chat_query()` + 1 for `_analyze_chat_path()`.
**Exception**: `list_test_cases` intent bypasses the second LLM call entirely (see below).

## System Prompt — Skill File + PROMPT_SECTION Nodes

`_build_analysis_system_prompt(kg, config)` assembles the system prompt in three parts:

1. **Header** — fixed instructions for TC analysis
2. **Spec sections 7.3 / 7.6 / 7.7** — pulled from `PROMPT_SECTION` nodes in the KG (built
   once during KG build from SECTION nodes). Falls back to a regex scan over SECTION nodes for
   old KG JSONs that pre-date PROMPT_SECTION support.
3. **Skill file** (`config/skills/matter_spec_skill.md`) — appended verbatim when non-empty.
   Edit this file and re-run; no KG rebuild required.

The skill file is the right place for standing rules that should apply to every run:
cluster-specific testing patterns, media-plane verification steps, style requirements, etc.
Use `--llm-additional-context` for one-off per-run hints instead.

## Chat Query Planner (`_plan_chat_query`)

When `run_ctx.client == "app_chat"`, `search_knowledge_graph_node` makes one cheap LLM call
to parse the user's question into a structured plan.

```
User question
    ↓
_plan_chat_query(query_text, llm)
    → {"intent": "list_test_cases", "cluster": "On/Off Cluster", ...}
    ↓
KG dispatcher (by intent):
    "list_test_cases"    → get_test_cases_for_cluster()    (no top-k cap — all TCs returned)
    "entity_coverage"    → find_entity_coverage()
    "requirement_lookup" → find_requirements_and_coverage()
    "graph_traversal"    → get_cluster_dependencies()
    "general_qa"         → search_by_keywords()
```

The `chat_query_intent` is stored in `PipelineState` so `_analyze_chat_path` can read it.

**Plan JSON fields:**

| Field | Type | Description |
|---|---|---|
| `intent` | str | One of the 5 intents above |
| `cluster` | str\|null | Canonical cluster name (e.g. `"On/Off Cluster"`) |
| `entity_type` | str\|null | `attribute`, `command`, `event`, or `feature` |
| `entity_name` | str\|null | CamelCase entity name |
| `traverse` | str\|null | `incoming_depends_on` or `outgoing_depends_on` (graph_traversal only) |
| `keywords` | List[str] | 2-6 content keywords for general_qa fallback |

## `list_test_cases` LLM Bypass

When `chat_query_intent == "list_test_cases"`, `_analyze_chat_path` **skips the LLM call entirely**
and formats a Markdown table directly from `graph_results`:

```
**N test case(s) found:**

| Test Case | Title |
|-----------|-------|
| TC-OO-2.1 | Attributes with DUT as Server |
...
```

This guarantees a complete, accurate enumeration without LLM truncation. The coverage_summary
header is suppressed (the count is already in the heading; the header might carry stale numbers
from before a KG rebuild).

## LLM Provider Override for KG Refinement

`build_knowledge_graph_node` checks `config.knowledge_graph.llm_refinement_provider`:
- If set (e.g. `"local"`), builds a copy of `LLMConfig` with the override provider/model
  and passes it to `LLMSpecRefiner`
- If empty, uses `config.llm` (global frontier LLM)

This lets the hundreds of per-section LLM refinement calls run on a cheap local Ollama model
while the rest of the pipeline (PR analysis, chat) continues to use Claude.

```yaml
knowledge_graph:
  llm_refinement_provider: local
  llm_refinement_local_model: llama3.2
```

## Analysis Pipelines

### Coverage Gap Analysis (`coverage_analysis_nodes.py`)

Finds spec REQUIREMENT nodes with no TEST_CASE coverage.

```
load_coverage_stores_node
    → build_cluster_coverage_map_node
        → run_llm_coverage_analysis_node   ← 1 LLM call per cluster
            → aggregate_coverage_findings_node
                → generate_coverage_report_node → END
```

CLI: `python scripts/run_coverage_analysis.py [--cluster X] [--max-llm-calls N]`
Log dir: `logs/coverage_analysis_<TS>/`

### PICS Validation Analysis (`pics_analysis_nodes.py`)

Validates PICS codes in test cases (wrong-side, non-existent, missing protocol PICS).

```
load_pics_stores_node
    → build_pics_map_node             ← DM XML → ClusterPicsSchema map
        → prepare_cluster_batches_node
            → run_llm_pics_analysis_node   ← 1 LLM call per cluster (parallel via ThreadPoolExecutor)
                → aggregate_pics_findings_node
                    → generate_pics_report_node → END
```

`run_llm_pics_analysis_node` uses `ThreadPoolExecutor` with `config.analysis.parallel_workers`
concurrent workers (default 4). Prompt construction is factored into `_build_pics_prompt()`.
DM schema text uses three annotations: `(M)` for mandatory entities, `(O)` for optional entities
(entity PICS required, no feature PICS required), and `(X)` for disallowed/deprecated entities.

CLI: `python scripts/run_pics_analysis.py [--cluster X] [--dm-dir DIR]`
Log dir: `logs/pics_analysis_<TS>/`

### SDK Coverage Analysis (`sdk_coverage_nodes.py`)

Cross-checks spec REQUIREMENT nodes against SDK implementation code in `connectedhomeip/src/app/clusters/`.

```
load_sdk_stores_node
    → resolve_sdk_files_node          ← find cluster dirs, read .cpp/.h files
        → build_requirements_map_node ← pull REQUIREMENT nodes per cluster from KG
            → run_llm_sdk_analysis_node  ← 1 LLM call per cluster
                → aggregate_sdk_findings_node
                    → generate_sdk_report_node → END
```

CLI: `python scripts/run_sdk_coverage_analysis.py [--cluster X] [--sdk-dir DIR] [--max-llm-calls N]`
Log dir: `logs/sdk_coverage_analysis_<TS>/`
Requires: `analysis.sdk_dir` in config.yaml pointing to the `connectedhomeip` repo root.

## Build-Once Control Flags

| Flag | When `True` | When `False` (default) |
|---|---|---|
| `build_test_plan_vectors` | Run KB pipeline → rich TC chunks → embed → save FAISS | Load existing FAISS index |
| `build_knowledge_graph` | Build KG from DM XML + spec + test plan docs, save to disk | Load existing KG JSON |
| `build_knowledge_graph_with_llm` | After KG build, run `LLMSpecRefiner` to add cross-cluster edges | Skip LLM refinement |
| `build_data_model` | Re-ingest Matter DM XML schema into KG | Use schema baked into saved KG |

**Auto-build**: if FAISS index or KG JSON is absent, the node builds automatically regardless of flags.

## Operating Modes

| CLI | LLM calls | What happens |
|---|---|---|
| `--index-only` | None | Rebuilds vector DB + KG + DM; stops at cleanup |
| `--build-knowledge-graph` | None | Rebuilds KG only; loads vector DB from disk |
| `--build-knowledge-graph-withLLM` | Yes (KG refinement) | After KG build, runs LLM spec-refinement pass |
| `--input-doc FILE` or `--pr-url URL` | **Yes** (1/chunk + 1/cluster review) | Full 16-node pipeline |
| `--compare-only --input-doc FILE` | **Yes** | Same but uses cached vector DB + KG (fastest) |

## Log Files (per run in `logs/matter_rag_pipeline_<TS>/`)

| File | Contents |
|---|---|
| `master.log` | All modules merged |
| `engine.log` | Node entry/exit, routing decisions |
| `llm.log` | Prompt preview + response preview per LLM call |
| `pr_changes.json` | Structured change records |
| `data_model_schema.json` | DM XML canonical schema snapshot |
| `spec_extractor_rejected_records.txt` | Spec sentences filtered as non-normative |
| `vector_chunks_ignored_or_rejected.txt` | TestCaseRecords that produced zero vector chunks |
| `pr_chunks_ignored_or_rejected.txt` | PR diff segments rejected as too-short |

`logs/llm_calls.jsonl` — full prompt + response JSONL log, shared across all runs.
