# Matter RAG Pipeline — Claude Code Guide

## Project Purpose
RAG (Retrieval-Augmented Generation) pipeline that:
1. Fetches Matter spec PRs and other documents from pluggable sources
2. Processes documents through configurable text-cleaning rules
3. Loads test plans (`.adoc`, `.pdf`, `.csv`) into chunks
4. Creates BGE embeddings → FAISS vector store
5. Builds a LangGraph knowledge graph
6. Searches with hybrid RAG (vector + knowledge graph entity matching)
7. Compares PR content vs test plans via LLM analysis
8. Generates a report of **missing** or **needs-update** test cases

---

## Repo Layout
```
matter-rag/
├── README.md                    # Project overview and quick-start guide
├── sources.json                 # Data source definitions (fetcher config)
├── .ignore_rules.json           # Global document processing rules
├── config/
│   └── config.yaml              # Master config (edit this for your env)
├── src/
│   ├── config/                  # Config loader → typed Python object
│   ├── fetcher/                 # Pluggable document fetchers
│   │   ├── base_fetcher.py      # BaseFetcher ABC + FetchedDocument
│   │   ├── fetcher_registry.py  # Type→class registry + create_fetcher()
│   │   ├── spec_diff_builder.py # Generate diff HTML from spec PR via Docker make
│   │   ├── docker_base.py       # Pull Docker image, extract pre-built KG/FAISS/DM XMLs
│   │   ├── document_fetcher.py  # Legacy fallback (kept for compat)
│   │   └── sources/             # Source implementations
│   │       ├── github_pr_fetcher.py
│   │       ├── github_repo_fetcher.py
│   │       ├── github_tag_diff_fetcher.py
│   │       ├── local_folder_fetcher.py
│   │       ├── url_fetcher.py
│   │       ├── csv_fetcher.py
│   │       ├── matter_xml_fetcher.py
│   │       └── zap_xml_adapter.py
│   ├── processor/               # Document text-cleaning rule engine
│   │   └── document_processor.py
│   ├── loader/                  # Document loader (PDF / adoc / CSV)
│   ├── embeddings/              # BGE embedding module
│   ├── database/                # Vector store (FAISS-backed)
│   ├── search/                  # FAISS search wrapper + re-ranker
│   ├── knowledge_graph/         # NetworkX knowledge graph builder
│   ├── llm/                     # LLM provider (claude_subprocess | claude_cli | local | lm_studio | gemini)
│   ├── document_updater/        # Write LLM suggestions back to .adoc files
│   │   ├── tc_index_builder.py  # Build TC-ID → adoc path routing index
│   │   ├── adoc_updater.py      # (legacy) AdocUpdater via updater_registry
│   │   └── updater_registry.py  # Extension → updater factory
│   └── engine/                  # LangGraph pipeline + nodes
│       ├── adoc_updater.py      # Write updated/new TC sections into source adocs
│       ├── nodes.py             # 18 node functions
│       ├── pipeline.py          # MatterRAGPipeline orchestrator
│       └── graphs/
│           └── cli_graph.py     # 16-node CLI pipeline graph
├── tests/                       # pytest unit tests per module
├── scripts/                     # CLI entry points
│   ├── run_ghpr_analysis.py     # GitHub PR analysis pipeline
│   ├── run_coverage_analysis.py # Test plan gap detection pipeline
│   ├── run_pics_analysis.py     # PICS code validation pipeline
│   ├── run_sdk_coverage_analysis.py  # SDK implementation coverage pipeline
│   ├── run_pipeline.py          # Legacy entry point (backward-compat alias)
│   ├── generate_test_scripts.py # Generate Python test scripts from TC adoc specs
│   └── helper_scripts/          # Utility / one-off helper scripts
│       ├── build_tc_index.py    # (re-)build data/tc_index.json out-of-band
│       ├── build_docker_image.py # Build Docker image with pre-built KG/FAISS/DM XMLs
│       ├── build_knowledge_base.py
│       ├── export_kg_csv.py
│       ├── adoc_to_diff_html.py # Convert adoc files to diff-annotated HTML
│       ├── generate_spec_diff.py # CLI wrapper for spec_diff_builder (Docker diff generation)
│       ├── audit_kg_viz_tc_bleed.py
│       ├── verify_kg_tc_mapping.py
│       ├── verify_kg_tc_cluster_assignments.py
│       └── fix_llm_call_html.py
├── .github/workflows/
│   ├── nightly-base-build.yml   # Nightly Docker base image build (KG + FAISS)
│   └── pr-analysis.yml          # Per-PR TC generation + PR comment
├── Dockerfile                   # Multi-stage pipeline Docker image
├── docker-compose.yml           # Volume-mounted pipeline execution
├── docker/
│   └── entrypoint.sh            # 3-step Docker orchestration
├── Makefile                     # Convenience targets (build, run-pr, shell, etc.)
├── data/
│   ├── raw/                     # Downloaded / copied source docs
│   ├── processed/               # Chunked + embedded docs
│   ├── faiss_index/             # Persisted FAISS index
│   └── tc_index.json            # TC-ID / prefix / stem → adoc path routing index
├── logs/                        # Per-run timestamped log files
└── reports/                     # Generated comparison reports
    └── matter_rag_reports_<ts>/ # All outputs for one run grouped here
        ├── analysis_trace_<ts>.md
        ├── test_coverage_<ts>.html
        ├── analysis_results_<ts>.json
        ├── adoc_updates/        # AdocUpdater output (legacy path)
        └── updated_testplans/   # write_updated_testplan_node output
```

---

## Key Modules

| Module | File | Purpose |
|--------|------|---------|
| Config | `src/config/config_loader.py` | Load `config.yaml` → `AppConfig` dataclass |
| BaseFetcher | `src/fetcher/base_fetcher.py` | `FetchedDocument` dataclass + `BaseFetcher` ABC |
| FetcherRegistry | `src/fetcher/fetcher_registry.py` | `create_fetcher()` factory + `load_sources()` |
| GitHubPRFetcher | `src/fetcher/sources/github_pr_fetcher.py` | GitHub PR unified diff |
| GitHubTagDiffFetcher | `src/fetcher/sources/github_tag_diff_fetcher.py` | GitHub compare API |
| LocalFolderFetcher | `src/fetcher/sources/local_folder_fetcher.py` | Recursive local directory walk |
| URLFetcher | `src/fetcher/sources/url_fetcher.py` | Generic HTTP URL (Quip, web pages) |
| CSVFetcher | `src/fetcher/sources/csv_fetcher.py` | CSV rows → FetchedDocument |
| SpecDiffBuilder | `src/fetcher/spec_diff_builder.py` | Generate diff HTML from spec PR via Docker make |
| DockerBaseExtractor | `src/fetcher/docker_base.py` | Pull Docker image, extract pre-built KG/FAISS/DM XMLs |
| BuildDockerImage | `scripts/helper_scripts/build_docker_image.py` | Build + push Docker image with pre-built pipeline data |
| DocumentProcessor | `src/processor/document_processor.py` | Rule-based text cleaning engine |
| Loader | `src/loader/document_loader.py` | Parse PDF / adoc / CSV → `Document` list |
| Embeddings | `src/embeddings/embeddings.py` | BGE sentence-transformer embeddings (MPS/GPU) |
| Database | `src/database/vector_store.py` | FAISS index CRUD |
| Search | `src/search/faiss_search.py` | Semantic search over FAISS |
| Reranker | `src/search/reranker.py` | Structural re-ranking of FAISS candidates |
| Graph | `src/knowledge_graph/matter_kg_builder.py` | Build + query NetworkX knowledge graph |
| KBGraphBuilder | `src/knowledge_graph/kb_graph_builder.py` | Build typed graph nodes + edges from KB records; creates SECTION cross-reference edges (REFERENCES, up to depth 3) |
| DmPicsValidator | `src/knowledge_graph/dm_pics_validator.py` | Parse DM XML → `ClusterPicsSchema` map |
| LLMSpecRefiner | `src/knowledge_graph/llm_spec_refiner.py` | LLM-assisted spec refinement (cross-cluster edges) |
| RuleEngine | `src/knowledge_graph/rule_engine.py` | Requirement classification, TC mode detection, entity matching |
| LLM | `src/llm/llm_provider.py` | `get_llm()` → claude_subprocess / claude_cli / Ollama / LM Studio / Gemini |
| LLMCallLogger | `src/llm/call_logger.py` | Per-call log dispatcher: writes `.jsonl`, `.txt`, and `.html` (dark-terminal, collapsible, live-refresh) |
| Engine | `src/engine/pipeline.py` | LangGraph `StateGraph` orchestrator (16 nodes) |
| Nodes | `src/engine/nodes.py` | Individual LangGraph node functions |
| AdocUpdater | `src/engine/adoc_updater.py` | Write LLM-suggested TC updates back to source `.adoc` files; uses `tc_index.json` for routing (tc_map → prefix_map → stem_map) |
| TcIndexBuilder | `src/document_updater/tc_index_builder.py` | Scan adoc files → build `data/tc_index.json` with three maps (TC-ID, prefix, stem); auto-built by `fetch_documents_node` when adoc sources are loaded |
| CoverageAnalysis | `src/engine/coverage_analysis_nodes.py` | 5-node pipeline: KG gaps (REQ nodes with no TC coverage) |
| PicsAnalysis | `src/engine/pics_analysis_nodes.py` | 6-node pipeline: PICS code validation per cluster |
| SdkCoverage | `src/engine/sdk_coverage_nodes.py` | 6-node pipeline: SDK implementation vs spec requirement coverage |

---

## Pipeline Stages
```
fetch_documents_node → process_documents_node
    → ingest_data_model_node → build_matter_schema_node
        → chunk_embed_test_plans_node  ← build or load vector DB (KB pipeline)
            → chunk_pr_node → extract_pr_changes_node
                → build_knowledge_graph_node  ← build or load KG
                    │
                    ├─ [no pr_chunks] → cleanup_node → END
                    │
                    └─ [pr_chunks] → search_test_plan_vector_db_node
                        → search_knowledge_graph_node → analyze_chunks_with_llm_node (Pass 1)
                            → cluster_review_node → second_pass_tc_gen_node (Pass 2)
                                → human_outline_expand_node (Pass 4)
                                    → write_adoc_updates_node
                                        → write_updated_testplan_node → generate_report_node
                                            → cleanup_node → END
```

`fetch_documents_node` auto-builds `data/tc_index.json` when adoc sources are loaded
(mtime-based cache; rebuild manually with `python scripts/helper_scripts/build_tc_index.py` if needed).

`analyze_chunks_with_llm_node` (Pass 1) queries the KG for all existing TC-IDs per cluster
and injects them into every LLM prompt (Section C) so the model avoids duplicating existing
TC numbers and correctly classifies existing TCs as `update_candidates` rather than
`missing_tests`. After reranking, sibling cluster TCs (found via ALIAS_OF edges) are
injected into Section A under a "Sibling Cluster TCs" header. After all per-chunk LLM calls,
`_deduplicate_missing_tc_ids()` resolves any remaining TC number collisions across chunks.
Pass 1 writes a `pass1_results_<ts>.json` snapshot to the output directory immediately after
completing.

`second_pass_tc_gen_node` (Pass 2) has two parts:
- **Consolidation**: dedup-only (no gap-filling). Single LLM call per triggered cluster, no batch splitting. Validates update TC-IDs and deduplicates missing TC entries.
- **Coverage gap TCs**: outline followed by expand for uncovered requirements. Gated by `include_coverage_gaps` flag (default True, disabled via `--no-coverage-gaps`). Results stored in `coverage_gap_tests` (separate from `missing_tests`). Abstract base clusters with siblings are skipped to avoid TC inflation. Protocol clusters are bridged via `_PROTOCOL_CHAPTER_TO_VC` mapping.
- **TC Merge** (Pass 3): After all TCs are generated, a single LLM call consolidates overlapping TCs (e.g., multiple attribute-read TCs, fragmented command TCs, duplicate prefix families). Runs when 2+ total new TCs exist. The LLM identifies which TCs to keep vs. remove, reducing TC inflation. Stats tracked in `_merge_stats`.

A `pipeline_progress.json` file is updated in `run_dir` after 7 key nodes for crash recovery.

All outputs for a run are grouped under `reports/matter_rag_reports_<YYYYMMDD_HHMMSS>/`.

When `--pr-url` is used (and no `--output` override), outputs are grouped under a PR-based
folder: `reports/pr_<number>/reports/` (HTML/JSON reports) and `reports/pr_<number>/logs/`
(per-run log directories). This makes CI artifact zipping straightforward.

---

## Data Sources (`sources.json`)

The fetcher is driven by `sources.json` at the project root. Add/remove sources without touching Python code:

```json
{
  "sources": [
    {
      "id": "test_plans_local",
      "type": "local_folder",
      "role": "test_plan",
      "path": "data/test_plans"
    },
    {
      "id": "matter_spec_local",
      "type": "local_folder",
      "role": "spec",
      "path": "data/matter_spec"
    },
    {
      "id": "matter_data_model",
      "type": "matter_xml",
      "role": "data_model",
      "path": "data/data_model"
    }
  ]
}
```

Supported `type` values: `github_pr`, `github_repo`, `github_tag_diff`, `local_folder`, `url`, `csv`, `matter_xml`

Note: PR input is now typically provided via `--pr-url` (which uses `spec_diff_builder` to generate diff HTML) or `--input-doc` (local diff file), rather than a `github_pr` source entry in `sources.json`.

`role` = `"pr"` → docs go to `pr_documents`; `role` = `"test_plan"` → docs go to `test_plan_fetched`; `role` = `"spec"` → docs go to `spec_fetched`; `role` = `"data_model"` → docs go to `data_model_fetched`; `role` = `"test_plans_adoc_folder"` → docs go to `test_plan_adoc_sources` (raw adoc files used by `write_updated_testplan_node`)

`${VAR}` tokens are substituted from environment variables at runtime.

---

## Document Processing Rules (`.ignore_rules.json`)

Global rules applied to every fetched document before chunking:

```json
{
  "rules": [
    { "type": "strip_regex", "pattern": "(?i)^.*(copyright|spdx-license).*$", "scope": "line", "apply_to": [".adoc", ".md", ".txt"] },
    { "type": "strip_regex", "pattern": "^\\s*//(?!/).*$", "scope": "line", "apply_to": [".adoc"] },
    { "type": "normalize_whitespace" }
  ]
}
```

Per-source rules can be added under `process_rules` in `sources.json`. Global rules run first.

Supported rule types: `strip_regex`, `strip_block_between`, `strip_first_lines`, `strip_last_lines`, `normalize_whitespace`, `replace_regex`

---

## Running the Pipeline

> **Full CLI reference**: see `docs/run_pipeline_options.md`

```bash
# Local diff file, single cluster (fast, uses cached data)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# GitHub spec PR (generates diff HTML via Docker, then analyses)
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/local/connectedhomeip-spec

# Auto-detect all changed clusters, group outputs
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --auto-detect-clusters --run-set pr12603

# Limit to N chunks (quick sanity check)
python scripts/run_ghpr_analysis.py \
  --compare-only --input-doc ... --cluster "On/Off" --num-chunks 3

# Include negative (error-path) test cases
python scripts/run_ghpr_analysis.py \
  --compare-only --input-doc ... --cluster "Door Lock" --negative-tests

# Inject targeted spec sections into the expand prompt (Tier 2)
python scripts/run_ghpr_analysis.py \
  --compare-only --input-doc ... --cluster "Push AV Stream Transport Cluster" \
  --spec-sections "11.7.2.2,11.7.1.8"

# One-off domain hint injected into the expand prompt (Tier 3)
python scripts/run_ghpr_analysis.py \
  --compare-only --input-doc ... --cluster "Push AV Stream Transport Cluster" \
  --llm-additional-context "Verify EXT-X-SESSION-KEY tag in HLS playlist after triggering transport."

# Full rebuild + LLM spec refinement
python scripts/run_ghpr_analysis.py \
  --build-test-plan-vectors --build-knowledge-graph --build-knowledge-graph-withLLM

# Index only (rebuild FAISS + KG, no PR)
python scripts/run_ghpr_analysis.py --index-only

# Process all HTML diffs in a directory (one pipeline run per file)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc-dir data/input_doc/pr_diffs/ \
  --auto-detect-clusters

# Rebuild TC routing index out-of-band (after adding/renaming .adoc files)
python scripts/helper_scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src

# Use an overlay config for additional DM XMLs, test plans, spec sections
python scripts/run_ghpr_analysis.py \
  --compare-only --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --additional-config path/to/overlay_config.yaml
```

Each run writes logs to `logs/ghpr_analysis_<context>_<YYYYMMDD_HHMMSS>/` (where `<context>`
includes PR snippet and cluster name, e.g. `ghpr_analysis_pr12603_push_av_stream_transport_20260430_143022`)
and reports to `reports/matter_rag_reports_<YYYYMMDD_HHMMSS>/`.
When `--pr-url` is used, both logs and reports are grouped under `reports/pr_<number>/`.

**Build flags:**

| Flag | Effect |
|------|--------|
| `--build-test-plan-vectors` | Re-chunk + embed test plans into FAISS (slow, do once) |
| `--build-knowledge-graph` | Re-build KG from DM XML + spec + test plans and save to disk |
| `--build-knowledge-graph-withLLM` | After KG build, run an LLM spec-refinement pass to enrich KG nodes. Uses `knowledge_graph.llm_refinement_provider` if set (supports local Ollama for cost savings). |
| `--index-only` | Shorthand for both build flags, no PR required |
| `--compare-only` | Skip all builds, use cached vector DB + KG (fastest) |
| `--no-coverage-gaps` | Disable coverage gap TC generation (Section 2 of report) |
| `--docker-base IMAGE` | Pull a Docker image with pre-built KG, FAISS, DM XMLs, and spec HTML; extract to `data/` and use as pipeline base. Combine with `--pr-url` for full automation. Example: `ghcr.io/your-org/matter-rag-base:latest` |
| `--additional-config FILE` | Overlay YAML config deep-merged on top of base config. Use for additional DM XMLs, test plans, spec sources, and custom paths. Relative paths in the overlay are resolved relative to the overlay file's directory. |

**LLM context injection:**

| Mechanism | Scope | How |
|---|---|---|
| `llm_prompts/matter_spec_skill.md` | Every run — appended to system prompt (~17k chars of coverage guidelines, PICS format, TC naming conventions, boundary testing rules) | Edit the file; no rebuild needed |
| `--spec-sections 11.7.2.2,11.7.1.8` | This run — spec prose from KG injected into expand prompt | CLI flag |
| `--llm-additional-context "text"` | This run — inline text injected into all passes (Pass 1 system prompt + Pass 2/3 expand prompt) | CLI flag |
| `--llm-additional-context /path/to/file.md` | This run — file content injected into all passes | CLI flag (file path) |
| `--llm-additional-context /path/to/dir/` | This run — per-pass context from directory (reads `all.md`, `pass1.md`, `pass2.md`, `pass3.md`) | CLI flag (directory path) |

**How LLM prompts work:**

The system prompt is assembled by `_build_analysis_system_prompt()` from three sources:
1. **PROMPT_SECTION nodes** — spec sections 7.3/7.6/7.7 baked into the KG at build time
2. **Skill file** (`pipeline.system_prompt_skills_file`) — standing instructions appended to every prompt. Default: `llm_prompts/matter_spec_skill.md`. Edit this file to add persistent rules (boundary testing patterns, TC naming conventions, PICS format guidelines). Changes take effect immediately — no rebuild needed.
3. **Additional context** (`--llm-additional-context`) — per-run context injected into the system prompt (Pass 1) and expand prompt (Pass 2/3). When a directory is provided, `all.md` is injected into all passes, and `pass1.md`/`pass2.md`/`pass3.md` are injected into their respective passes only.

`llm_prompts/matter_spec_skill.md` is the right place for standing rules
(cluster-specific testing patterns, style requirements, protocol verification steps you always
want included). Use `--llm-additional-context` for one-off hints or domain-specific context
that varies between runs.

**Pass 4 — Re-expanding a human-edited outline:**

Pass 4 (`human_outline_expand_node`) re-expands a human-modified TC outline JSON into full
AsciiDoc test cases. This lets you review the Pass 3 outline, edit TC titles/types/scope,
then re-run expansion with your changes:

```bash
# 1. Run the pipeline normally — generates outline JSON in the output dir
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... --cluster "On/Off"

# 2. Edit the outline JSON (add/remove/rename TCs, change test_type, adjust scope)
#    The outline is at: reports/matter_rag_reports_<ts>/outline_*.json

# 3. Re-run with --third-pass-expand pointing to your edited outline
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... --cluster "On/Off" \
  --third-pass-expand reports/matter_rag_reports_<ts>/outline_edited.json
```

Pass 4 only expands TCs that don't already exist in the KG. It merges results with Pass 1
output, so you get both PR-driven TCs and your manually curated TCs in one report.

## Running the Analysis Pipelines

All analysis pipelines require the KG to be built first (`--build-knowledge-graph`).

```bash
# Test plan coverage gap analysis — finds spec REQUIREMENTs with no TEST_CASE coverage
python scripts/run_coverage_analysis.py
python scripts/run_coverage_analysis.py --cluster "On/Off"     # single cluster
python scripts/run_coverage_analysis.py --max-llm-calls 10     # cost control

# PICS code validation — finds wrong-side/non-existent/missing PICS codes in test cases
python scripts/run_pics_analysis.py
python scripts/run_pics_analysis.py --cluster "Door Lock"
python scripts/run_pics_analysis.py --dm-dir data/data_model  # custom DM XML dir

# SDK coverage analysis — cross-checks spec requirements against SDK implementation code
python scripts/run_sdk_coverage_analysis.py
python scripts/run_sdk_coverage_analysis.py --cluster "On/Off"          # single cluster
python scripts/run_sdk_coverage_analysis.py --sdk-dir /path/to/connectedhomeip
python scripts/run_sdk_coverage_analysis.py --max-llm-calls 20
```

Reports are written to `reports/` as HTML + JSON. Log dirs:
- `logs/coverage_analysis_<YYYYMMDD_HHMMSS>/`
- `logs/pics_analysis_<YYYYMMDD_HHMMSS>/`
- `logs/sdk_coverage_analysis_<YYYYMMDD_HHMMSS>/`

---

## FastAPI Debug App

Interactive web UI for inspecting the FAISS vector store and knowledge graph.

```bash
# Start from project root (default port 9000)
python tests/app/run.py
```

### Inspecting a TC's KG Links

**Visual graph (best for exploring edges):**
Open `/kg/viz`, enter the TC node ID in the **Center** field, set Hops, click Load:
```
http://localhost:9000/kg/viz
# Center: TC-OO-2.1   Hops: 2   Source: merged
```
Shows the TC with all edges (CLUSTER, REQUIREMENT, SECTION, etc.) radiating out.
Always use **Source = merged** — the `test_plan` sub-graph has no CLUSTER nodes.

**TC detail page (neighbours as JSON):**
```
http://localhost:9000/test-cases/TC-OO-2.1
```

**Full neighbour list with edge types:**
```
http://localhost:9000/kg/node/TC-OO-2.1
```

**Subgraph JSON (for scripting/debugging):**
```
http://localhost:9000/kg/graph?center=TC-OO-2.1&hops=2&source=merged
```

### Other Useful Endpoints

| Endpoint | Description |
|---|---|
| `/` | Dashboard — component health + stats |
| `/chunks?tc_id=TC-OO&cluster=On/Off` | Paginate vector chunks; filter by tc_id, cluster, source, doc_type, chunk_type, text. HTML view by default. |
| `/test-cases?cluster=On/Off` | HTML table of all TCs; filter by cluster or TC-ID prefix |
| `/test-cases?format=json` | Same as above but raw JSON |
| `/kg/nodes?node_type=TEST_CASE` | Paginate all KG nodes by type |
| `/kg/node/{node_id}` | Single KG node + all in/out edges |
| `/kg/viz` | Interactive force-directed KG visualization (vis.js) |
| `/stats` | Node-type distribution + chunk breakdowns |
| `/cluster/{name}` | Cluster summary: DM schema + requirements + test cases |
| `/reload` | Force reload KG and vector store from disk (POST) |
| `/pipeline` | Pipeline DAG visualization (Mermaid.js) with live run status |
| `/chat` | React chat UI — ask questions grounded in FAISS + KG |

---

## Config Quick Reference
Edit `config/config.yaml`. Key sections:
- `llm.provider`: `claude_cli` (default — Anthropic SDK) or `claude_subprocess` (local `claude` CLI) or `local` (Ollama) or `lm_studio` (LM Studio local server) or `gemini` (Google Gemini)
- `llm.subprocess_timeout`: subprocess call timeout in seconds (default `600`)
- `llm.lm_studio_url`: LM Studio server endpoint (default `http://localhost:1234/v1`; `lm_studio` provider only)
- `llm.lm_studio_model`: model name shown in LM Studio (default `qwen3-5.9b`; `lm_studio` provider only)
- `llm.lm_studio_timeout`: HTTP timeout in seconds per LM Studio LLM call (default `3600`; `lm_studio` provider only)
- `llm.gemini_model`: Gemini model name (default `gemini-1.5-flash`; `gemini` provider only)
- `llm.gemini_api_key`: Google Gemini API key (or set `GEMINI_API_KEY` env var; `gemini` provider only)
- `llm.call_log_path`: path for per-call log files (default `logs/llm_calls.jsonl`); also generates `.txt` and `.html` variants
- `llm.temperature`: temperature for all LLM providers (default `0.1`); `-t` flag only passed to subprocess when > 0
- `llm.max_prompt_chars`: max assembled prompt chars (default `80000`; auto-adjusted downward from model context window at startup)
- `embeddings.model`: BGE model name (default `BAAI/bge-large-en-v1.5`)
- `embeddings.device`: `mps` (Apple Silicon GPU), `cuda`, or `cpu`
- `embeddings.offline`: `true` prevents HuggingFace model downloads
- `fetcher.github_timeout`: HTTP timeout in seconds (default `60`)
- `pipeline.chunk_size` / `chunk_overlap`: chunking parameters
- `pipeline.logs_dir`: base directory for per-run log folders (default `logs`)
- `pipeline.tc_index_path`: path to TC routing index JSON (default `data/cache/tc_index.json`); rebuilt automatically at run start
- `pipeline.system_prompt_skills_file`: path to skill file appended to the LLM system prompt on every run (default `llm_prompts/matter_spec_skill.md`); edit without rebuilding
- `pipeline.min_chunk_chars`: minimum content length (chars) for a PR diff section to be kept as a chunk (default `80`); raise to e.g. `200` to drop very short section-header-only diffs
- `pipeline.expand_section_max_chars`: max chars of spec section text injected into the TC expand prompt (default `15000`; 0 = no limit)
- `pipeline.second_pass_expand_cap`: max TC expand calls per cluster in second_pass (default `20`)
- `knowledge_graph.spec_extractor_workers`: parallel workers for spec HTML parsing (0 = auto, max 8)
- `knowledge_graph.llm_refinement_provider`: override LLM provider for KG spec-refinement step only (e.g. `"local"` for Ollama). Leave empty to use the global `llm:` config.
- `knowledge_graph.llm_refinement_local_model`: Ollama model name when `llm_refinement_provider: local`
- `analysis.max_llm_calls_per_run`: cost-control cap (default `9999`); set low to limit LLM calls per run
- `analysis.parallel_workers`: concurrent LLM calls for PICS/coverage/SDK analysis (default `4`; 1 = sequential)
- `analysis.dm_dir`: directory containing Matter DM XML files (default `data/data_model`)
- `analysis.dm_dirs_additional`: list of additional DM XML directories (overlay clusters merged with base)
- `analysis.additional_sources_file`: path to additional sources.json (entries appended to base sources at fetch time)
- `analysis.additional_test_plans_dir`: additional test plan HTML/adoc directory (merged with base test plans)
- `analysis.additional_spec_dir`: additional spec HTML directory (merged with base spec sources)
- `analysis.output_dir`: report output directory (default `reports`)
- `analysis.sdk_dir`: root of connectedhomeip repo; set to enable SDK coverage analysis
- `analysis.sdk_dirs_additional`: list of additional SDK code directories (flat or nested; merged with base)
- `analysis.tasks`: which analysis tasks to run (`["gaps", "pics"]`)
- `spec_repo.path`: local clone of spec repo for `--pr-url` mode (or pass `--spec-repo` CLI flag)
- `spec_repo.url`: spec repo git URL for auto-cloning (default `https://github.com/CHIP-Specifications/connectedhomeip-spec.git`)
- `spec_repo.docker_image`: Docker image for Asciidoctor spec build (default `ghcr.io/chip-specifications/chip-documentation:21`)

---

## Report Output Structure

The HTML report (`test_coverage_<ts>.html`) has two distinct sections:
- **Section 1: PR-Driven Test Cases** -- from Pass 1 + cluster review + consolidation dedup
- **Section 2: Coverage Gap Test Cases** (purple styling) -- from coverage gap outline followed by expand (only present when `--no-coverage-gaps` is not set)

When `--pr-url` is used, the output tree is:
```
reports/pr_<number>/
├── reports/
│   └── matter_rag_reports_<ts>/   # HTML, JSON, adoc outputs
└── logs/
    └── ghpr_analysis_<context>_<ts>/  # Per-run log directory
```

### report_data.json Fields

| Field | Description |
|-------|-------------|
| `first_pass_missing_count` | Number of missing TCs from Pass 1 |
| `second_pass_missing_count` | Number of missing TCs after Pass 2 consolidation |
| `coverage_gap_tests_count` | Number of coverage gap TCs generated |
| `coverage_gap_tests` | Full list of coverage gap TC entries |
| `parse_failed_count` | Number of LLM responses that failed JSON parsing |
| `template_echo_warning_count` | Number of LLM responses flagged as template echoes |

---

## Crash Recovery

Three mechanisms support crash recovery and observability:

| File | Written When | Purpose |
|------|-------------|---------|
| `pass1_results_<ts>.json` | After Pass 1 completes (in output_dir) | Snapshot of Pass 1 analysis results for recovery |
| `pipeline_progress.json` | After each of 7 major nodes (in run_dir) | Track pipeline progress for resume |
| `llm_calls.jsonl` | Appended per LLM call | Per-call LLM audit log (pre-existing) |

---

## LLM Call Counts

| Stage | Calls |
|-------|-------|
| Pass 1 (analyze_chunks_with_llm_node) | 1 per PR chunk |
| Pass 2 consolidation | 1 per triggered cluster (no batch splitting) |
| Pass 2 coverage gaps | 1 outline + N expand per cluster (when enabled) |
| Pass 3 TC Merge | 1 call when 2+ total new TCs exist (consolidates overlapping TCs) |

---

## Deterministic Behaviors

- **TC numbering**: `_deduplicate_missing_tc_ids` assigns sequential minor versions based on alphabetical sort of title text (stripped of TC-ID). Same TCs always get same numbers regardless of LLM generation order.
- **Reranker score banding**: Sort key is `(-round(score, 0.01), tc_id)` for deterministic ordering within 0.01 score bands.
- **Template echo detection**: Warn-only -- never drops LLM data. Logs WARNING and sets `template_echo_warning` flag in metadata. Only checks for actual placeholder text (`"one-sentence description of what changed"`, `"<describe"`), not for `|` in action field.
- **JSON extraction**: `_parse_structured_response` and consolidation/outline paths use `_extract_json_object()` (balanced-brace depth-tracking extractor) instead of greedy regex. Also applies `_repair_json()` for unquoted TC-IDs and trailing commas. Two additional recovery layers: (1) **truncation recovery** -- if braces are unbalanced, appends missing closing braces and retries parse; (2) **nested format recovery** -- if LLM wraps `missing_tests`/`update_candidates` inside a `recommendation` object, hoists them to the top level.

---

## Adding a New Data Source Type
1. Create `src/fetcher/sources/<name>_fetcher.py` subclassing `BaseFetcher`
2. Register it in `src/fetcher/fetcher_registry.py::REGISTRY`
3. Add source entry to `sources.json` with the new `type` value
4. Add test in `tests/test_fetcher.py`

## Adding a New Processing Rule Type
1. Add a `_apply_<type>` handler in `src/processor/document_processor.py`
2. Document the new `type` key in `.ignore_rules.json` comments
3. Add entry to `.ignore_rules.json` if needed globally

## Adding a New File Format
1. Add a loader method in `src/loader/document_loader.py`
2. Register its extension in `LOADER_REGISTRY`
3. Add a test in `tests/test_loader.py`

## Adding a New LLM Provider
1. Add a class implementing `complete()`, `stream()`, and `complete_with_tools()` in `src/llm/llm_provider.py`
2. Add a branch in `get_llm()`
3. Add provider config section to `config.yaml`
4. Add test in `tests/test_llm.py`

---

## Environment Variables
| Variable | Used By | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | fetcher / spec_diff_builder | GitHub API auth (required for `--pr-url` with private repos) |
| `ANTHROPIC_API_KEY` | llm | Claude API key (claude_cli provider) |
| `GEMINI_API_KEY` | llm | Google Gemini API key (gemini provider) |
| `OLLAMA_HOST` | llm | Ollama server URL (local provider) |
| `POSTGRES_URL` | database | PostgreSQL connection string (postgres backend) |
| `HF_HUB_OFFLINE` | embeddings | Set to `1` to disable HuggingFace downloads (auto-set from config) |
