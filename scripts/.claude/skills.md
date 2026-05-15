# Skills — Matter RAG Pipeline Scripts

Common invocation patterns for the Matter RAG CLI scripts. All commands
assume the working directory is the project root (`matter-qa/`).

---

## 1. PR Analysis Pipeline (`run_ghpr_analysis.py`)

### Quick compare using a local diff file (fastest — uses cached data)

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"
```

### Compare with a raw text snippet instead of a diff file

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --pr-snippet "Added new attribute OnTime (0x4001) to On/Off cluster, type uint16, access R V"
```

### Analyze a GitHub PR end-to-end (generates diff HTML via Docker, then analyzes)

```bash
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/connectedhomeip-spec
```

### Auto-detect all changed clusters in a diff file

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --auto-detect-clusters \
  --run-set pr12345
```

### Multi-cluster analysis (explicit list)

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" "Level Control" "Door Lock" \
  --run-set my_batch
```

### Process all diff HTML files in a directory

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc-dir data/input_doc/pr_diffs/ \
  --auto-detect-clusters \
  --run-set pr12345
```

### Limit to N chunks for a quick sanity check

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --num-chunks 3
```

### Include negative (error-path) test cases

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --negative-tests
```

### Skip coverage gap TC generation (Section 2 of report)

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --no-coverage-gaps
```

### Inject spec sections into the expand prompt (Tier 2 context)

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --spec-sections "5.2.1.8,5.2.2.2"
```

### Inject one-off domain hints into all LLM passes (Tier 3 context)

```bash
# Inline text
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --llm-additional-context "Verify credential index wraps correctly at MaxCredentials."

# From a file
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --llm-additional-context /path/to/context.md

# From a directory (reads all.md, pass1.md, pass2.md, pass3.md)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --llm-additional-context /path/to/context_dir/
```

### Re-expand a human-edited TC outline (Pass 4)

```bash
# 1. Run the pipeline normally to get an outline JSON
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# 2. Edit the outline JSON (reports/matter_rag_reports_<ts>/outline_*.json)

# 3. Re-run with --third-pass-expand pointing to the edited outline
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --third-pass-expand reports/matter_rag_reports_<ts>/outline_edited.json
```

### Use an overlay config for additional data sources

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --additional-config path/to/overlay_config.yaml
```

---

## 2. Building Indexes (run once, then use --compare-only)

### Rebuild FAISS vector DB + knowledge graph + data model

```bash
python scripts/run_ghpr_analysis.py --index-only
```

### Rebuild only the knowledge graph

```bash
python scripts/run_ghpr_analysis.py --build-knowledge-graph
```

### Rebuild KG with LLM-assisted spec refinement

```bash
python scripts/run_ghpr_analysis.py --build-knowledge-graph-withLLM
```

### Rebuild only the FAISS vector DB

```bash
python scripts/run_ghpr_analysis.py --build-test-plan-vectors
```

### Rebuild only the data model from DM XMLs

```bash
python scripts/run_ghpr_analysis.py --build-data-model
```

---

## 3. Analysis Pipelines

All analysis pipelines require the KG to be built first (`--index-only` or
`--build-knowledge-graph`).

### Coverage gap analysis -- finds spec requirements with no test coverage

```bash
# All clusters
python scripts/run_coverage_analysis.py

# Single cluster
python scripts/run_coverage_analysis.py --cluster "On/Off"

# Cost control
python scripts/run_coverage_analysis.py --max-llm-calls 10

# Custom output
python scripts/run_coverage_analysis.py --output reports/coverage_debug
```

### PICS code validation -- finds wrong/missing/non-existent PICS codes

```bash
# All clusters
python scripts/run_pics_analysis.py

# Single cluster
python scripts/run_pics_analysis.py --cluster "Door Lock"

# Custom DM XML directory
python scripts/run_pics_analysis.py --dm-dir data/data_model

# Cost control
python scripts/run_pics_analysis.py --max-llm-calls 10
```

### SDK coverage analysis -- cross-checks spec requirements vs SDK implementation

```bash
# All clusters (sdk_dir set in config.yaml)
python scripts/run_sdk_coverage_analysis.py

# Single cluster
python scripts/run_sdk_coverage_analysis.py --cluster "On/Off"

# Provide SDK root directly
python scripts/run_sdk_coverage_analysis.py --sdk-dir /path/to/connectedhomeip

# Additional SDK dirs for out-of-tree cluster implementations
python scripts/run_sdk_coverage_analysis.py \
  --sdk-dir /path/to/connectedhomeip \
  --sdk-dirs-additional /path/to/extra/clusters

# Cost control
python scripts/run_sdk_coverage_analysis.py --max-llm-calls 20

# With overlay config
python scripts/run_sdk_coverage_analysis.py \
  --additional-config path/to/overlay_config.yaml \
  --cluster "Door Lock"
```

---

## 4. Test Script Generation

Generate Python test scripts from pipeline report output using the Claude CLI.

```bash
# From a report (generates all TCs)
python scripts/generate_test_scripts.py \
  --reports reports/matter_rag_reports_20260505_123456/report_data*.json \
  --sdk-path /path/to/connectedhomeip \
  --output-dir reports/generated_python_tests

# Single TC only
python scripts/generate_test_scripts.py \
  --reports reports/matter_rag_reports_<ts>/report_data.json \
  --sdk-path /path/to/connectedhomeip \
  --tc TC-OO-2.5 \
  --output-dir reports/generated_python_tests

# With additional context and parallel workers
python scripts/generate_test_scripts.py \
  --reports reports/matter_rag_reports_<ts>/report_data.json \
  --sdk-path /path/to/connectedhomeip \
  --context /path/to/context.md \
  --output-dir reports/generated_python_tests \
  --workers 4
```

---

## 5. Helper Scripts

### Rebuild TC routing index (after adding/renaming .adoc files)

```bash
python scripts/helper_scripts/build_tc_index.py
python scripts/helper_scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src
python scripts/helper_scripts/build_tc_index.py --output data/cache/tc_index.json --verbose
```

### Generate spec diff HTML from a PR (standalone, without full pipeline)

```bash
GITHUB_TOKEN=ghp_... \
python scripts/helper_scripts/generate_spec_diff.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345

# Diff only (no TC generation)
python scripts/helper_scripts/generate_spec_diff.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345 \
  --diff-only

# Use specific local spec repo clone
python scripts/helper_scripts/generate_spec_diff.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/connectedhomeip-spec
```

### Build Docker image with pre-built KG + FAISS (for nightly CI)

```bash
# Build and push
python scripts/helper_scripts/build_docker_image.py \
  --push myregistry/matter-rag-base:latest

# Build with custom branches
python scripts/helper_scripts/build_docker_image.py \
  --spec-branch main --sdk-branch master \
  --push myregistry.com/org/matter-rag-base:latest

# Local build only (no push)
python scripts/helper_scripts/build_docker_image.py \
  --tag matter-rag-base:local

# Build data only (no Docker image)
python scripts/helper_scripts/build_docker_image.py --no-docker
```

### Set up an external workspace for pipeline extensions

```bash
python scripts/helper_scripts/setup_workspace.py /path/to/my-workspace
python scripts/helper_scripts/setup_workspace.py /path/to/my-workspace --name "My Extensions"
```

### Export KG to CSV for inspection

```bash
python scripts/helper_scripts/export_kg_csv.py
```

### Verify KG TC mappings

```bash
python scripts/helper_scripts/verify_kg_tc_mapping.py
python scripts/helper_scripts/verify_kg_tc_cluster_assignments.py
```

---

## 6. Docker-Based Runs

### Use a pre-built Docker base image with --pr-url

```bash
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345 \
  --docker-base ghcr.io/your-org/matter-rag-base:latest \
  --spec-repo /path/to/connectedhomeip-spec
```

The `--docker-base` flag pulls the image, extracts pre-built KG, FAISS,
DM XMLs, and spec HTML to `data/`, then the pipeline runs using that
pre-built data. The image's manifest provides the BASE commit for diff
generation when combined with `--pr-url`.

### CI/nightly flow

```bash
# 1. Nightly: build + push the base image
python scripts/helper_scripts/build_docker_image.py \
  --push ghcr.io/your-org/matter-rag-base:latest

# 2. Per-PR: pull base image + analyze
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/project-chip/connectedhomeip-spec/pull/12345 \
  --docker-base ghcr.io/your-org/matter-rag-base:latest \
  --auto-detect-clusters \
  --run-set pr12345
```

---

## 7. Debugging Tips

### Enable verbose logging

```bash
# Verbose mode (trace-level)
python scripts/run_ghpr_analysis.py ... --verbose

# Specific log level
python scripts/run_ghpr_analysis.py ... --log-level DEBUG
```

Log files are written to `logs/<pipeline_name>_<YYYYMMDD_HHMMSS>/`.

### Inspect the FastAPI debug app

```bash
python tests/app/run.py
# Open http://localhost:9000
```

Useful debug endpoints:
- `/kg/viz` -- interactive KG visualization (set Center to a TC-ID, e.g. `TC-OO-2.1`)
- `/kg/node/TC-OO-2.1` -- full neighbour list with edge types
- `/chunks?tc_id=TC-OO&cluster=On/Off` -- paginate vector chunks with filters
- `/test-cases?cluster=On/Off` -- HTML table of all TCs for a cluster
- `/stats` -- node-type distribution and chunk breakdowns
- `/reload` -- force reload KG and vector store from disk (POST)

### Check LLM call log

All LLM calls are logged to `logs/llm_calls.jsonl` (one JSON object per line).
Inspect with:

```bash
# Count calls
wc -l logs/llm_calls.jsonl

# Last 5 calls
tail -5 logs/llm_calls.jsonl | python -m json.tool
```

### Verify pipeline progress after a crash

```bash
cat reports/matter_rag_reports_<ts>/pipeline_progress.json | python -m json.tool
```

Shows which of the 7 major nodes completed. Re-run with `--compare-only`
to resume from cached state.

### Check Pass 1 snapshot after a mid-run failure

```bash
cat reports/matter_rag_reports_<ts>/pass1_results_*.json | python -m json.tool
```

This file is written immediately after Pass 1 completes, so its data
survives later-stage crashes.

### Quick test with minimal LLM cost

```bash
# Use --num-chunks to limit PR chunks processed
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --num-chunks 1 \
  --no-coverage-gaps

# Use --max-llm-calls on analysis pipelines
python scripts/run_coverage_analysis.py --cluster "On/Off" --max-llm-calls 5
```

### Common flag combinations

| Goal | Flags |
|------|-------|
| Fastest possible run (cached data, single cluster) | `--compare-only --cluster "On/Off"` |
| Full rebuild from scratch | `--index-only` (then re-run with `--compare-only`) |
| Scan all clusters automatically | `--auto-detect-clusters --run-set <name>` |
| Cost-controlled test run | `--num-chunks 2 --no-coverage-gaps` |
| Negative + positive TCs | `--negative-tests` |
| Custom config overlay | `--additional-config path/to/overlay.yaml` |

---

## 8. Output Locations

| Output | Path |
|--------|------|
| Run logs | `logs/<pipeline_name>_<YYYYMMDD_HHMMSS>/` |
| HTML report | `reports/matter_rag_reports_<ts>/test_coverage_<ts>.html` |
| JSON results | `reports/matter_rag_reports_<ts>/report_data_<ts>.json` |
| Analysis trace | `reports/matter_rag_reports_<ts>/analysis_trace_<ts>.md` |
| Updated test plans | `reports/matter_rag_reports_<ts>/updated_testplans/` |
| Pass 1 snapshot | `reports/matter_rag_reports_<ts>/pass1_results_<ts>.json` |
| Pipeline progress | `reports/matter_rag_reports_<ts>/pipeline_progress.json` |
| LLM call log | `logs/llm_calls.jsonl` |
| FAISS index | `data/faiss_index/` |
| Knowledge graph | `data/knowledge_graph/matter_kg.json` |
| TC routing index | `data/tc_index.json` |
