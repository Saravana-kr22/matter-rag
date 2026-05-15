# Commands Reference

## Main Entry Point: `scripts/run_ghpr_analysis.py`

```
usage: run_ghpr_analysis.py [-h] [--config CONFIG]
                            [--pr-url PR_URL] [--input-doc INPUT_DOC]
                            [--cluster CLUSTER]
                            [--build-test-plan-vectors] [--build-knowledge-graph]
                            [--build-knowledge-graph-withLLM] [--build-data-model]
                            [--index-only] [--compare-only]
                            [--test-plan-dir TEST_PLAN_DIR]
                            [--output OUTPUT] [--verbose] [--log-level LOG_LEVEL]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | `config/config.yaml` | Path to config YAML |
| `--pr-url` | — | Full GitHub PR URL (or set `PR_URL` env var) |
| `--input-doc` | — | Local diff HTML or adoc file to analyse |
| `--cluster` | — | Restrict analysis to one cluster (e.g. `"On/Off"`) |
| `--build-test-plan-vectors` | false | Re-chunk + embed test plans into FAISS (run once) |
| `--build-knowledge-graph` | false | Rebuild KG from DM XML + spec + test plans and save |
| `--build-knowledge-graph-withLLM` | false | After KG build, run LLM spec-refinement pass |
| `--build-data-model` | false | Re-ingest Matter DM XML schema into KG |
| `--index-only` | false | Shorthand: both build flags = true, no PR required |
| `--compare-only` | false | Skip builds, use cached vector DB + KG (fastest) |
| `--test-plan-dir` | — | Override test plan directory (legacy) |
| `--output` | `reports/` | Output directory for reports |
| `--verbose` | false | Debug logging |
| `--log-level` | `VERBOSE` | `VERBOSE` \| `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

---

## Examples

```bash
# Local diff file, single cluster (fastest — no PR fetch needed)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# GitHub PR analysis (sources.json-driven)
PR_URL=https://github.com/project-chip/connectedhomeip/pull/1234 \
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py --compare-only

# Full pipeline: rebuild everything then compare a PR
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/project-chip/connectedhomeip/pull/1234 \
  --build-knowledge-graph --build-test-plan-vectors

# Full rebuild including LLM spec-refinement pass on KG
python scripts/run_ghpr_analysis.py \
  --build-test-plan-vectors --build-knowledge-graph --build-knowledge-graph-withLLM

# Index only (rebuild FAISS + KG, no PR)
python scripts/run_ghpr_analysis.py --index-only

# Compare only (FAISS + KG already built)
python scripts/run_ghpr_analysis.py --compare-only --pr-url <github-pr-url>
```

Each run writes logs to `logs/ghpr_analysis_<YYYYMMDD_HHMMSS>/` and reports to
`reports/matter_rag_reports_<YYYYMMDD_HHMMSS>/`.

---

## Analysis Pipeline Scripts

### `scripts/run_coverage_analysis.py` — Test Plan Gap Detection
Find spec REQUIREMENTs with no TEST_CASE coverage.
```bash
python scripts/run_coverage_analysis.py
python scripts/run_coverage_analysis.py --cluster "On/Off"
python scripts/run_coverage_analysis.py --max-llm-calls 10
python scripts/run_coverage_analysis.py --output reports/debug
```
Log dir: `logs/coverage_analysis_<TS>/`

### `scripts/run_pics_analysis.py` — PICS Code Validation
Find wrong-side, non-existent, or missing PICS codes in test cases.
```bash
python scripts/run_pics_analysis.py
python scripts/run_pics_analysis.py --cluster "Door Lock"
python scripts/run_pics_analysis.py --dm-dir data/data_model
```
Log dir: `logs/pics_analysis_<TS>/`

### `scripts/run_sdk_coverage_analysis.py` — SDK Coverage Analysis
Cross-check spec REQUIREMENTs against `connectedhomeip/src/app/clusters/` implementation.
```bash
python scripts/run_sdk_coverage_analysis.py
python scripts/run_sdk_coverage_analysis.py --cluster "On/Off"
python scripts/run_sdk_coverage_analysis.py --sdk-dir /path/to/connectedhomeip
python scripts/run_sdk_coverage_analysis.py --max-llm-calls 20
```
Log dir: `logs/sdk_coverage_analysis_<TS>/`

Requires `analysis.sdk_dir` set in `config/config.yaml`.

### `scripts/helper_scripts/build_tc_index.py` — Rebuild TC Routing Index
```bash
python scripts/helper_scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src
```
Rebuilds `data/tc_index.json` (TC-ID / prefix / stem → adoc file path).
Also runs automatically at pipeline start when adoc sources are loaded.

### `scripts/run_pipeline.py` — Legacy alias
Backward-compatible wrapper around `run_ghpr_analysis.py`. All flags pass through.

---

## Python API

```python
from src.config.config_loader import load_config
from src.engine.pipeline import MatterRAGPipeline

config = load_config("config/config.yaml")
pipeline = MatterRAGPipeline(config)

# Compare only (use cached vector DB + KG)
result = pipeline.run(compare_only=True)

# Full rebuild + compare
result = pipeline.run(
    pr_url="https://github.com/project-chip/connectedhomeip/pull/1234",
    build_test_plan_vectors=True,
    build_knowledge_graph=True,
)

print(result.report_path)
print(len(result.missing_tests))
print(len(result.update_candidates))
```

---

## Environment Setup

```bash
# Install dependencies (includes openai for LM Studio support)
pip install -r requirements.txt

# Required env vars
export GITHUB_TOKEN=ghp_...
export PR_URL=https://github.com/project-chip/connectedhomeip/pull/1234
export ANTHROPIC_API_KEY=sk-ant-...   # claude_cli provider only; not needed for claude_subprocess
export OLLAMA_HOST=http://localhost:11434  # local (Ollama) provider only

# Run tests
pytest tests/ -v
```

---

## LLM Provider Quick Switch

```yaml
# config/config.yaml — llm: section
llm:
  provider: claude_subprocess   # local claude CLI (default, no API key needed)
  provider: claude_cli          # Anthropic SDK (needs ANTHROPIC_API_KEY)
  provider: local               # Ollama (needs Ollama running)
  provider: lm_studio           # LM Studio (needs LM Studio server running)

# LM Studio specific (provider: lm_studio)
lm_studio_url: http://localhost:1234/v1
lm_studio_model: qwen3-5.9b   # must match model name in LM Studio
```

Run `/setup-lm-studio` in Claude Code for step-by-step LM Studio setup instructions.

---

## Log Files

Per-run log directory: `logs/ghpr_analysis_<YYYYMMDD_HHMMSS>/`

| File | Contents |
|---|---|
| `master.log` | All modules merged |
| `engine.log` | Node entry/exit, routing decisions |
| `llm.log` | Prompt + response preview per LLM call |
| `pr_changes.json` | Structured change records from `extract_pr_changes_node` |
| `data_model_schema.json` | DM XML canonical schema snapshot |

`logs/llm_calls.jsonl` — full prompt + response JSONL log, shared across all runs
(configurable via `llm.call_log_path`).
