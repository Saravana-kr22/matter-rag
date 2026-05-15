# Matter RAG — Test Case Generation Pipeline

A RAG (Retrieval-Augmented Generation) pipeline that analyzes Matter protocol specification changes and automatically generates, updates, and validates test cases.

## What It Does

- Compares PR diff HTML against existing test plans using hybrid search (vector + knowledge graph)
- Generates new test cases for uncovered spec changes (AsciiDoc format)
- Identifies existing test cases that need updating
- Validates PICS code correctness across all test cases
- Detects test coverage gaps in the specification
- Generates Python test scripts from TC specifications

## Prerequisites

- Python 3.11+
- [asciidoctor](https://asciidoctor.org/) — `brew install asciidoctor`
- Claude CLI — local `claude` binary on PATH (for `claude_subprocess` provider)
- ~4GB disk for embeddings model + FAISS index + knowledge graph
- GPU recommended (MPS on macOS, CUDA on Linux) — CPU works but embedding is slower

## Quick Start

### 1. Clone and Install

```bash
git clone <repo-url>
cd matter-qa
./install.sh
```

This installs all dependencies (Python venv, pip packages, Asciidoctor), creates data directories, and generates a `.env` template. See the script output for any missing prerequisites.

Or install manually:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

Edit the `.env` file created by `install.sh` (or export variables in your shell):

```bash
# .env — uncomment and set the variables you need
GITHUB_TOKEN=ghp_your_token_here        # Required for --pr-url mode
# ANTHROPIC_API_KEY=sk-ant-...          # Only for claude_cli provider
# GEMINI_API_KEY=AIza...                # Only for gemini provider
```

Load it before running the pipeline:

```bash
source .env
```

The `claude_subprocess` provider (default) requires NO API key — it uses the locally installed `claude` CLI which manages its own authentication.

### 3. Provide Input Data

Place these files in the `data/` directory (created by `install.sh`):

```
data/
  data_model/          ← Matter DM XML cluster files (from connectedhomeip/data_model/)
  test_plans/          ← Test plan HTML files (allclusters.html, index.html)
  matter_spec/         ← Spec HTML files (index.html, appclusters.html)
```

Where to get these files:
- **DM XMLs**: Copy from `connectedhomeip/data_model/clusters/` (the standard Matter SDK repo)
- **Test plans**: Export from the CSA test plan portal or your local test plan repo (HTML format)
- **Spec HTMLs**: Export from the Matter specification documents (HTML format)

### 4. First Run — Build and Analyze

```bash
# Analyze a spec PR directly (recommended — handles everything automatically)
export GITHUB_TOKEN=ghp_your_token_here
python scripts/run_ghpr_analysis.py \
  --build-test-plan-vectors --build-knowledge-graph \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/local/connectedhomeip-spec

# Or analyze a pre-generated diff HTML file
python scripts/run_ghpr_analysis.py \
  --build-test-plan-vectors --build-knowledge-graph \
  --input-doc data/input_doc/appclusters_diff.html
```

This single command does everything:
1. Downloads the BGE embedding model (~1.3GB, first time only)
2. Builds the knowledge graph from DM XML + spec + test plans
3. Embeds test plan chunks into FAISS vector database (uses GPU if available)
4. For `--pr-url`: fetches PR, generates diff HTML via Docker, auto-detects changed clusters
5. Analyzes the diff and generates test cases via LLM
6. Writes reports to `reports/matter_rag_reports_<timestamp>/`

**Prerequisites for `--pr-url`**: Docker Desktop running, `GITHUB_TOKEN` set, local spec repo clone.

**Expected time**: 10-20 minutes on first run (embedding generation uses the underlying GPU architecture — MPS on macOS, CUDA on Linux, or CPU fallback). The Docker spec build adds 15-30 minutes for `--pr-url` mode. The LLM analysis adds 2-5 minutes per cluster.

**What you get**:
```
reports/matter_rag_reports_<timestamp>/
  test_coverage_final_*.html       ← visual report (open in browser)
  report_data_*.json               ← machine-readable results
  llm_generated_adocs/             ← generated TC AsciiDoc files
  llm_calls.html                   ← full LLM prompt/response audit log
```

### 5. Subsequent Runs — Fast Analysis (seconds)

After the first build, use `--compare-only` to skip KG/FAISS rebuild and go straight to analysis:

```bash
# Analyze a specific cluster from a diff file
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# Analyze a GitHub spec PR directly (auto-generates diff HTML via Docker)
export GITHUB_TOKEN=ghp_your_token_here
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/local/connectedhomeip-spec

# Auto-detect all changed clusters in a diff
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --auto-detect-clusters

# Analyze all HTML files in a directory
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc-dir data/input_doc/my_diffs/
```

When using `--pr-url`:
- Docker Desktop must be running (the spec build uses Docker for Asciidoctor)
- `GITHUB_TOKEN` must be set for private spec repos
- `--spec-repo` points to your local clone (or set `spec_repo.path` in config)
- The script fetches the PR, extracts in-progress feature flags from changed adoc files, runs `make html-diff-all`, then auto-detects and analyzes all changed clusters

**Expected time**: 2-5 minutes per cluster for `--compare-only` (LLM calls only). 15-30 minutes for `--pr-url` (includes Docker spec build).

### 6. When to Rebuild

Rebuild the KG and vector DB when your source data changes:

```bash
# Rebuild only KG (new DM XMLs or spec changes)
python scripts/run_ghpr_analysis.py --build-knowledge-graph --input-doc ...

# Rebuild only vector DB (new test plan files)
python scripts/run_ghpr_analysis.py --build-test-plan-vectors --input-doc ...

# Rebuild both
python scripts/run_ghpr_analysis.py --index-only
```

### 7. Run Coverage Analysis

Find spec requirements with no test coverage:

```bash
python scripts/run_coverage_analysis.py
python scripts/run_coverage_analysis.py --cluster "On/Off"
```

### 8. Run PICS Validation

Validate PICS codes across all test cases:

```bash
python scripts/run_pics_analysis.py
python scripts/run_pics_analysis.py --cluster "Door Lock"
```

### 9. Run SDK Coverage Analysis

Cross-check spec requirements against SDK implementation code:

```bash
python scripts/run_sdk_coverage_analysis.py \
  --sdk-dir /path/to/connectedhomeip \
  --cluster "On/Off"
```

### 10. Generate Python Test Scripts

From pipeline output:

```bash
# Using standalone script (bulk generation)
python scripts/generate_test_scripts.py \
  --reports reports/*/report_data*.json \
  --sdk-path /path/to/connectedhomeip \
  --output-dir reports/generated_python_tests
```

### 11. Convert AsciiDoc to Diff HTML

For new cluster specs not yet in the diff format:

```bash
python scripts/helper_scripts/adoc_to_diff_html.py \
  --input /path/to/specs/ \
  --output data/input_doc/
```

This wraps all content in `<ins class="diff-new">` tags so the pipeline treats everything as new additions requiring test cases.

## Understanding the Output

Each run creates a timestamped folder under `reports/`:

```
reports/matter_rag_reports_<timestamp>/
  test_coverage_final_*.html       ← MAIN REPORT: open in browser to see all generated TCs
  test_coverage_pass1_*.html       ← Pass 1 only (before cluster review + consolidation)
  report_data_*.json               ← Machine-readable results (for script generation)
  llm_generated_adocs/
    new_updated_TCs/               ← Generated AsciiDoc TC files (ready to use)
    updated_testplans/             ← Full test plan files with TCs inserted
  llm_calls.html                   ← LLM audit log: every prompt + response (for debugging)
  llm_analysis_of_chunks_*.md      ← Per-chunk analysis trace
  cluster_review_*.md              ← Cluster review findings
  pass1_results_*.json             ← Pass 1 snapshot (for crash recovery)
```

When using `--pr-url`, output is grouped under a PR-specific folder:

```
reports/pr_<number>/
  reports/
    matter_rag_reports_<timestamp>/   ← same structure as above
  logs/
    ghpr_analysis_<context>_<timestamp>/
```

### Debugging Tips

- **TCs look wrong?** Open `llm_calls.html` — it shows every LLM prompt and response in a collapsible format. You can see exactly what context the LLM received and what it produced.
- **Missing cluster?** Check `logs/<run_dir>/engine.log` for `[Pass 1]` entries — it shows which chunks were analyzed and which clusters were detected.
- **KG issues?** Start the debug web app (`python tests/app/run.py`) and browse `/kg/viz` to explore the knowledge graph interactively.
- **Wrong PICS codes?** Run `python scripts/run_pics_analysis.py --cluster "ClusterName"` to validate PICS against the DM XML schema.
- **Embedding slow?** Check `config/config.yaml` → `embeddings.device` is set to `mps` (macOS GPU) or `cuda` (Linux GPU), not `cpu`.

### Pipeline Progress

The console shows pass-by-pass progress:

```
[Pass 1: Per-Chunk] LLM call 1/3 — Attributes
[Pass 1: Per-Chunk] LLM call 2/3 — Commands
[Pass 2: Cluster Review] LLM call 1/1 — On/Off Cluster
[Pass 3: Consolidation] Triggered: 'On/Off Cluster' (proposals=8 [pass1=5 + review=3])
[Pass 3: Coverage Gaps] 'On/Off Cluster' — 3 uncovered requirements
```

The final HTML report includes a collapsible "Pipeline Pass Summary" at the bottom showing how many TCs each pass generated, kept, and removed.

## Configuration

The pipeline is configured via `config/config.yaml`. Key sections:

### `config/config.yaml` — Full Reference

```yaml
llm:
  provider: claude_subprocess    # claude_subprocess | claude_cli | local | lm_studio | gemini
  model: claude-sonnet-4-6       # model name (claude_cli / gemini providers)
  local_model: llama3.2          # Ollama model name (local provider)
  temperature: 0.0               # 0.0 = most deterministic
  max_prompt_chars: 0            # 0 = auto-detect from model context window
  subprocess_timeout: 1200       # seconds before claude subprocess is killed
  lm_studio_url: http://localhost:1234/v1  # LM Studio endpoint
  lm_studio_model: qwen3-5.9b   # model name in LM Studio

embeddings:
  model: BAAI/bge-large-en-v1.5  # BGE model for embeddings
  device: mps                    # mps (GPU) | cpu | cuda
  offline: false                 # true = don't download models

pipeline:
  search_top_k: 10              # FAISS candidates per query
  similarity_threshold: 0.65    # min cosine score to keep a result
  output_dir: reports            # report output directory
  logs_dir: logs                 # per-run log directory
  system_prompt_skills_file: llm_prompts/matter_test_coverage_and_structure.md
  llm_additional_context: llm_prompts/additional_context  # per-pass context dir
  min_chunk_chars: 80           # minimum PR diff chunk size (chars)

knowledge_graph:
  backend: local                 # local (NetworkX) | docker
  graph_store_path: data/knowledge_graph/matter_kg.json
  spec_extractor_workers: 0     # 0 = auto (parallel spec parsing)

analysis:
  max_llm_calls_per_run: 0      # 0 = unlimited; set > 0 to cap LLM cost
  parallel_workers: 4           # concurrent LLM calls for analysis pipelines
  dm_dir: data/data_model       # base DM XML directory
  dm_dirs_additional: []        # additional DM XML directories (overlay)
  sdk_dir: ""                   # connectedhomeip root (for SDK coverage)
  sdk_dirs_additional: []       # additional SDK code directories
  output_dir: reports           # analysis report output
```

### Environment Variables

| Variable | Required For | Purpose |
|----------|-------------|---------|
| `GITHUB_TOKEN` | `--pr-url` mode | GitHub API authentication (read PR diffs) |
| `ANTHROPIC_API_KEY` | `claude_cli` provider | Anthropic API key |
| `GEMINI_API_KEY` | `gemini` provider | Google Gemini API key |

The `claude_subprocess` provider (default) needs no API key — it uses the local `claude` CLI binary which manages its own auth.

### GitHub PR Integration

The pipeline can analyze a spec PR directly — it fetches the PR, generates diff HTML via Docker, and runs the full analysis:

```bash
# Set credentials (required for private repos)
export GITHUB_TOKEN=ghp_your_token_here

# Analyze a spec PR (generates diff HTML + test cases in one command)
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/local/connectedhomeip-spec
```

**Prerequisites for `--pr-url` mode:**
- Docker Desktop running (spec build uses Asciidoctor via Docker)
- `GITHUB_TOKEN` env var set (for GitHub API access)
- Local clone of the spec repo (`--spec-repo` or `spec_repo.path` in config)

**What happens:**
1. Fetches PR metadata (base SHA, head SHA, changed files)
2. Checks out PR head in local spec repo
3. Computes true merge base (`git merge-base origin/main <head>`)
4. Extracts in-progress feature flags from changed `.adoc` files
5. Runs `make html-diff-all BASE=<merge_base>` via Docker
6. Copies diff HTML to `data/input_doc/`
7. Auto-detects changed clusters and runs the analysis pipeline

**Config (`config.yaml`):**
```yaml
spec_repo:
  path: ""           # local clone path; or pass --spec-repo CLI flag
  url: "https://github.com/CHIP-Specifications/connectedhomeip-spec.git"
  docker_image: "ghcr.io/chip-specifications/chip-documentation:21"
```

Without `GITHUB_TOKEN`, the GitHub API rate-limits to 60 requests/hour.

### LLM Provider Selection

| Provider | Config | Auth | Min Context | Best For |
|----------|--------|------|-------------|----------|
| `claude_subprocess` | `provider: claude_subprocess` | Local `claude` CLI (no API key) | 200K tokens | Default — highest quality |
| `claude_cli` | `provider: claude_cli` | `ANTHROPIC_API_KEY` env var | 200K tokens | API access, agentic chat |
| `local` (Ollama) | `provider: local`, `local_model: llama3.2` | None (local) | Model-dependent | Cost-free, offline |
| `lm_studio` | `provider: lm_studio` | None (local) | Model-dependent | Local models via UI |
| `gemini` | `provider: gemini` | `GEMINI_API_KEY` env var | 128K-1M tokens | Google models |

The pipeline auto-detects the model's context window at startup and rejects models below 64K tokens. Prompt sections are automatically sized to fit the available context.

### Customizing LLM Behavior

The pipeline's LLM prompts can be customized at three levels. See [docs/llm_customization.md](docs/llm_customization.md) for the full guide.

**1. Skill file** (permanent, applies to every run):

Edit `llm_prompts/matter_test_coverage_and_structure.md` to add standing rules — TC naming conventions, PICS format guidelines, boundary testing requirements, domain-specific patterns. Changes take effect immediately without rebuilding.

**2. Per-run context** (`--llm-additional-context`):

```bash
# Inline text
--llm-additional-context "This is a new cluster. Generate TCs for all attributes."

# Single file
--llm-additional-context /path/to/context.md

# Directory with per-pass files (all.md, pass1.md, pass2.md, pass3.md)
--llm-additional-context /path/to/context_dir/
```

**3. Re-expand with human edits** (`--third-pass-expand`):

Review the pipeline's TC outline, edit it, then re-run expansion:

```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... \
  --third-pass-expand reports/matter_rag_reports_<ts>/outline_edited.json
```

See [docs/e2eflow.md](docs/e2eflow.md) for the complete configuration reference, prompt assembly details, and context window behavior.

## Extending with Additional Data Sources

Use `--additional-config` to layer extra data sources on top of the base configuration. This is designed for organizations that maintain proprietary cluster extensions alongside the standard Matter specification.

### Why Use It

- Keep proprietary DM XMLs, spec HTMLs, and test plans **outside** this repo
- Build a merged KG containing both standard + extension clusters
- Route all output (reports, logs, KG) to an external directory
- Inject domain-specific LLM context per pass (Pass 1/2/3)

### Setup

**1. Create an external workspace directory:**

```
/external/workspace/
  config/
    overlay_config.yaml    ← overlay config (extends base config)
  data/
    dm_xmls/               ← additional DM XML cluster files
    test_plans/            ← additional test plan HTMLs
    spec_html/             ← additional spec HTMLs
    input_doc/             ← diff-annotated HTML (pipeline input)
    knowledge_graph/       ← merged KG stored here
    faiss_index/           ← merged vector DB stored here
  llm_prompts/             ← per-pass LLM context (all.md, pass1.md, pass2.md, pass3.md)
  reports/                 ← reports written here
  logs/                    ← logs written here
```

Or use the setup script: `python scripts/helper_scripts/setup_workspace.py /external/workspace`

**2. Create the overlay config** (`config/overlay_config.yaml`):

```yaml
pipeline:
  output_dir: ../reports
  logs_dir: ../logs
  llm_additional_context: ../llm_prompts

knowledge_graph:
  graph_store_path: ../data/knowledge_graph/matter_kg.json

analysis:
  dm_dirs_additional:
    - ../data/dm_xmls
  additional_test_plans_dir: ../data/test_plans
  additional_spec_dir: ../data/spec_html
```

All `../` paths resolve relative to the config file's directory (i.e., relative to `config/`).

**3. (Optional) Create per-pass LLM context** in `llm_prompts/`:

```
llm_prompts/
  all.md       ← injected into ALL passes
  pass1.md     ← injected into Pass 1 (analysis) only
  pass2.md     ← injected into Pass 2 (TC generation) only
  pass3.md     ← injected into Pass 3 (expansion) only
```

### Usage

```bash
# Build merged KG (base + extension clusters)
python scripts/run_ghpr_analysis.py --build-knowledge-graph \
  --additional-config /external/workspace/config/overlay_config.yaml

# Run PR analysis with merged data
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc /path/to/diff.html \
  --additional-config /external/workspace/config/overlay_config.yaml

# Run coverage analysis with merged data
python scripts/run_coverage_analysis.py \
  --additional-config /external/workspace/config/overlay_config.yaml

# Run PICS validation with merged data
python scripts/run_pics_analysis.py \
  --additional-config /external/workspace/config/overlay_config.yaml
```

Pass `--additional-config` in every command that needs the extended data. Without it, the pipeline uses only the base config and standard Matter data.

For the full overlay config reference and per-pass LLM context details, see [docs/e2eflow.md](docs/e2eflow.md).

## CLI Flags Reference

| Flag | Effect |
|------|--------|
| `--pr-url URL` | Fetch spec PR, generate diff HTML via Docker, then analyze |
| `--spec-repo DIR` | Local clone of spec repo (for `--pr-url` mode) |
| `--docker-base IMAGE` | Pull pre-built base image and extract data (KG, FAISS, DM XMLs) before running |
| `--build-test-plan-vectors` | Re-chunk + embed test plans into FAISS |
| `--build-knowledge-graph` | Re-build KG from DM XML + spec + test plans |
| `--index-only` | Build both (no PR analysis) |
| `--compare-only` | Use cached KG + FAISS (fastest) |
| `--input-doc FILE` | Analyze a local diff HTML file |
| `--input-doc-dir DIR` | Analyze all HTML files in a directory |
| `--cluster "Name"` | Analyze only this cluster |
| `--auto-detect-clusters` | Find all changed clusters automatically |
| `--negative-tests` | Include error-path test cases |
| `--no-coverage-gaps` | Skip coverage gap TC generation |
| `--llm-additional-context "text or /path"` | Inject extra LLM context (file, directory, or inline text) |
| `--additional-config /path/to/overlay.yaml` | Overlay config for extended data sources |
| `--output /path/to/dir` | Custom output directory |

See [docs/run_pipeline_options.md](docs/run_pipeline_options.md) for the full CLI reference.

## Helper Scripts

| Script | Purpose |
|--------|---------|
| `scripts/helper_scripts/generate_spec_diff.py` | Generate diff HTML from a spec PR (standalone, Docker-based) |
| `scripts/helper_scripts/adoc_to_diff_html.py` | Convert AsciiDoc files to pipeline-ready diff HTML |
| `scripts/helper_scripts/convert_zap_xmls.py` | Convert ZAP-format XMLs to standard DM XML format |
| `scripts/helper_scripts/verify_kg_tc_mapping.py` | Verify TC → cluster → requirement edge integrity |
| `scripts/helper_scripts/verify_kg_tc_cluster_assignments.py` | TC inventory dashboard with DUT role classification |
| `scripts/helper_scripts/audit_kg_viz_tc_bleed.py` | Detect cross-cluster TC contamination in KG |
| `scripts/helper_scripts/build_tc_index.py` | Rebuild TC routing index |
| `scripts/generate_test_scripts.py` | Bulk Python test script generation |

## Debug Web App

Interactive UI for inspecting the knowledge graph and vector database:

```bash
python tests/app/run.py    # starts on port 9000
```

| Endpoint | Description |
|----------|-------------|
| `/` | Dashboard |
| `/pipeline` | Pipeline DAG visualization |
| `/kg/viz` | Interactive knowledge graph explorer |
| `/test-cases` | TC inventory with filters |
| `/cluster/{name}` | Cluster detail (schema + requirements + TCs) |
| `/chat` | Chat UI grounded in KG + FAISS |

## Documentation

| Document | Description |
|----------|-------------|
| [docs/ci_setup.md](docs/ci_setup.md) | CI/CD setup guide — GitHub Actions workflows, PR directives, Docker |
| [docs/e2eflow.md](docs/e2eflow.md) | End-to-end technical flow (chunking, embedding, KG, reranking, LLM passes) |
| [docs/llm_customization.md](docs/llm_customization.md) | How to customize LLM behavior (skill file, per-run context, per-pass files, outline re-expansion) |
| [docs/run_pipeline_options.md](docs/run_pipeline_options.md) | Full CLI options reference |
| [docs/projectflow.md](docs/projectflow.md) | Pipeline architecture |

## Docker & CI/CD

The pipeline can run in Docker for reproducible builds and CI integration.

### Docker (Local)

```bash
# Build the Docker image
make build

# Analyze a PR
PR_URL=https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
GITHUB_TOKEN=ghp_... make run-pr

# Analyze a single cluster
CLUSTER="On/Off" make run-cluster

# Drop into container shell for debugging
make shell
```

Use `--docker-base` to pull a pre-built base image and extract its data before running:

```bash
python scripts/run_ghpr_analysis.py \
  --docker-base ghcr.io/your-org/matter-rag-base:latest \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/connectedhomeip-spec \
  --compare-only
```

See `make help` for all available targets.

### CI/CD (GitHub Actions)

Two workflows automate TC generation for spec PRs:

**1. Nightly base image build** (`.github/workflows/nightly-base-build.yml`):
- Runs at 2 AM UTC daily (or manually via workflow_dispatch)
- Clones spec + SDK repos, builds spec HTML, builds KG + FAISS
- Packages everything into a Docker image and pushes to GHCR
- Tags: `:latest` + `:YYYYMMDD`

**2. Per-PR TC generation** (`.github/workflows/pr-analysis.yml`):
- Triggers on PRs that modify `src/**/*.adoc` in the spec repo
- Checks out the spec repo with `fetch-depth: 0` (full history for merge-base)
- Pulls the nightly base image (pre-built KG + FAISS)
- Runs the analysis pipeline on the PR diff
- Output goes to `reports/pr_<number>/reports/` and `reports/pr_<number>/logs/`
- Parses PR description for pipeline directives (see below)
- Posts a PR comment with generated TCs (collapsible details)
- Updates the comment on subsequent pushes (no duplicates)
- Artifact path is `pipeline/reports/` (contains the `pr_<number>/` subfolder)
- Reports uploaded as workflow artifacts (30-day retention)

**PR author directives:** Spec authors can customize the pipeline run by adding HTML comment directives in the PR description. These are invisible in the rendered PR but parsed by the workflow:

```markdown
<!-- matter-rag: --llm-additional-context "This cluster uses HLS streaming. Verify EXT-X-SESSION-KEY tags." -->
<!-- matter-rag: --cluster "Push AV Stream Transport Cluster" -->
<!-- matter-rag: --negative-tests -->
<!-- matter-rag: --no-coverage-gaps -->
```

Multiple directives are supported (one per comment block). Any CLI flag from `run_ghpr_analysis.py` can be used.

**Setup for the spec repo:**
1. Copy `.github/workflows/pr-analysis.yml` to the spec repo's `.github/workflows/`
2. Set `ANTHROPIC_API_KEY` secret in the spec repo settings
3. The nightly base image stays in this (pipeline) repo

**Manual trigger:** The per-PR workflow also supports `workflow_dispatch` with a PR URL input, so you can trigger it from the pipeline repo for any spec PR.

## Architecture

```
18-node LangGraph pipeline:

fetch → process → ingest_dm → build_schema → chunk_embed → chunk_pr
  → extract_changes → build_kg → search_faiss → search_kg
    → analyze_with_llm (Pass 1) → cluster_review (Pass 2)
      → tc_generation (Pass 3) → human_expand (Pass 4)
        → write_adoc → write_testplan → generate_report → cleanup
```

**Technology stack:** LangGraph, NetworkX, FAISS, BGE-large-en-v1.5, BeautifulSoup4, FastAPI

## Generating Diff HTML Manually

If you prefer to generate diff HTMLs yourself (without `--pr-url`), you can use the spec repo's Makefile directly:

**Prerequisites:** Docker Desktop must be installed and running.

```bash
cd /path/to/connectedhomeip-spec

# Generate diff HTML for appclusters + core spec
make ENABLE_PARAGRAPH_NUMBERING=1 \
  INCLUDE_IN_PROGRESS="<FeatureFlags>" \
  html html-appclusters-book html-diff html-diff-appclusters \
  BASE=<base_commit_id>
```

Replace:
- `<FeatureFlags>` — in-progress feature flags from the PR's adoc files (e.g., `"cameras-e2ee groupcast"`)
- `<base_commit_id>` — the merge base commit SHA (`git merge-base origin/main <pr_head>`)

The output files (`index_diff.html`, `appclusters_diff.html`) are in `build/html/`. Copy them to `data/input_doc/` and run:

```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc-dir data/input_doc/ --auto-detect-clusters
```

For new cluster specs in AsciiDoc format (not yet in the spec repo), use the built-in converter:

```bash
python scripts/helper_scripts/adoc_to_diff_html.py \
  --input /path/to/specs/ --output data/input_doc/
```

You can also use the standalone helper script which automates the Docker make steps:

```bash
python scripts/helper_scripts/generate_spec_diff.py \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/connectedhomeip-spec \
  --output data/input_doc/ \
  --diff-only
```

## Learn More

- **[docs/e2eflow.md](docs/e2eflow.md)** — Deep-dive into how the pipeline works: chunking strategy, embedding, KG construction, reranking, multi-pass LLM analysis, and prompt assembly
- **[docs/projectflow.md](docs/projectflow.md)** — Pipeline architecture diagrams, node responsibilities, and data flow
- **[docs/llm_customization.md](docs/llm_customization.md)** — How to customize LLM behavior (skill file, per-run context, per-pass files, human outline re-expansion)
- **[docs/run_pipeline_options.md](docs/run_pipeline_options.md)** — Full CLI options reference with examples
