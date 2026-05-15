# CI/CD Setup — Automated TC Generation for Spec PRs

This guide covers how to set up automated test case generation that runs on every spec PR, posts results as a PR comment, and stores full reports as downloadable artifacts.

## Architecture

Two GitHub Actions workflows work together:

```
Nightly (2 AM UTC)                          Per-PR (on push to PR)
─────────────────                           ──────────────────────
Clone spec repo                             Checkout pipeline repo
Clone SDK repo (DM XMLs)                    Install Python deps
Clone test plans repo                       Pull nightly base image
Build spec HTML                                 ↓
Build test plan HTML                        docker cp → data/
Build KG + FAISS                            (extract KG, FAISS, DM XMLs,
    ↓                                        test plans, spec HTML)
Package into alpine image                       ↓
Push to GHCR                                Parse PR directives
    ↓                                       Run pipeline (native Python)
:latest + :YYYYMMDD tags                        ↓
                                            Upload reports + logs
                                            Post PR comment
```

The base image is a **data-only alpine container** (~50-200MB) — no Python, no pip. The per-PR workflow runs the pipeline natively on the GitHub Actions runner and only uses Docker to fetch pre-built data.

---

## Setup

### 1. Configure the pipeline repo (matter-qa)

The nightly build workflow lives here. It builds the base image and pushes it to GHCR.

**Required secrets:**

| Secret | Purpose |
|--------|---------|
| `SPEC_REPO_TOKEN` | GitHub PAT with read access to the spec repo (for cloning) |

`GITHUB_TOKEN` is auto-provided by GitHub Actions for GHCR push.

**File:** `.github/workflows/nightly-base-build.yml`

This workflow:
- Runs at 2 AM UTC daily (cron) or manually via workflow_dispatch
- Clones spec, SDK, and test plans repos
- Builds spec HTML via Docker, test plan HTML via `make html-all`
- Builds KG + FAISS index
- Packages data into `ghcr.io/<org>/matter-qa/matter-rag-base:latest`
- Also tags with date: `:YYYYMMDD`

### 2. Configure the spec repo (connectedhomeip-spec)

Copy the per-PR workflow to the spec repo:

```bash
# From the spec repo root
mkdir -p .github/workflows
cp /path/to/matter-qa/.github/workflows/pr-analysis.yml .github/workflows/
```

**Required secrets** (set in spec repo Settings > Secrets > Actions):

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude API key for LLM analysis |

`GITHUB_TOKEN` is auto-provided for reading the PR and posting comments.

**Update the `BASE_IMAGE` env var** in the copied workflow if your registry differs:

```yaml
env:
  BASE_IMAGE: ghcr.io/CHIP-Specifications/matter-qa/matter-rag-base:latest
```

### 3. Verify GHCR permissions

The nightly build pushes to GHCR under the pipeline repo's namespace. Ensure:
- The pipeline repo has **Packages: write** permission in workflow settings
- The spec repo can **pull** the image (public repos work automatically)

For private repos, you may need to grant the spec repo read access to the package.

---

## How It Works — Per-PR Flow

When a spec PR is opened or updated with changes to `src/**/*.adoc`:

1. **Checkout spec repo**: The spec repo is checked out with `fetch-depth: 0` (full history needed for `git merge-base`)
2. **Checkout pipeline repo**: The pipeline repo (matter-qa) is checked out alongside
3. **Install**: Python 3.11 + pip dependencies installed on the runner
4. **Pull data**: The nightly base image is pulled and KG/FAISS/DM XMLs/test plans are extracted to `data/`
5. **Parse directives**: PR description is scanned for `<!-- matter-rag: ... -->` blocks (see below)
6. **Run pipeline**: `run_ghpr_analysis.py --pr-url <PR> --compare-only` runs natively. Output lands in `reports/pr_<number>/reports/` and `reports/pr_<number>/logs/`.
7. **Upload artifacts**: The `pipeline/reports/` directory (which contains the `pr_<number>/` subfolder) is uploaded as a workflow artifact (30-day retention)
8. **Post comment**: A summary comment is posted on the PR with generated TCs

---

## PR Author Directives

Spec authors can customize the pipeline run by adding HTML comment directives in the PR description. These are **invisible in the rendered PR** but parsed by the workflow.

### Syntax

```markdown
<!-- matter-rag: <flags> -->
```

Each directive is a single HTML comment block containing CLI flags for `run_ghpr_analysis.py`.

### Examples

**Add domain-specific context for the LLM:**
```markdown
<!-- matter-rag: --llm-additional-context "This cluster uses HLS streaming. Verify EXT-X-SESSION-KEY tag in playlist after triggering transport." -->
```

**Restrict analysis to a specific cluster:**
```markdown
<!-- matter-rag: --cluster "Push AV Stream Transport Cluster" -->
```

**Include error-path (negative) test cases:**
```markdown
<!-- matter-rag: --negative-tests -->
```

**Skip coverage gap TC generation:**
```markdown
<!-- matter-rag: --no-coverage-gaps -->
```

**Inject specific spec section text into the LLM prompt:**
```markdown
<!-- matter-rag: --spec-sections "11.7.2.2,11.7.1.8" -->
```

### Combining multiple directives

Use one comment block per directive:

```markdown
## Description

This PR adds the Push AV Stream Transport cluster with HLS support.

Changes:
- Added cluster definition (11.7)
- Added transport management commands
- Added stream allocation attributes

<!-- matter-rag: --llm-additional-context "This cluster uses HLS streaming. Verify EXT-X-SESSION-KEY tag in HLS playlist after triggering transport." -->
<!-- matter-rag: --negative-tests -->
<!-- matter-rag: --spec-sections "11.7.2.2,11.7.1.8" -->
```

### Available flags

Any CLI flag from `run_ghpr_analysis.py` can be used. The most useful ones for PR authors:

| Flag | Effect |
|------|--------|
| `--llm-additional-context "text"` | Inject domain hints into the LLM prompt |
| `--cluster "Name"` | Analyze only this cluster (skip others in the diff) |
| `--negative-tests` | Include error-path test cases |
| `--no-coverage-gaps` | Skip coverage gap TC generation (Section 2 of report) |
| `--spec-sections "7.3.1,11.7.2"` | Inject specific spec section text into the expand prompt |
| `--num-chunks N` | Limit to N PR chunks (quick sanity check) |

---

## Viewing Results

### PR Comment

The workflow posts a summary comment directly on the PR:

- **New test cases needed** — count + collapsible details (title, purpose, procedure steps)
- **Existing TCs to update** — list with change summaries
- **Clusters analyzed** — which clusters were detected in the diff

The comment is updated (not duplicated) on subsequent pushes to the PR.

### Full Reports and Logs

Click the link in the PR comment to go to the workflow run, then download from the **Artifacts** section:

```
tc-reports-pr-<PR#>.zip
  pr_<PR#>/
    reports/
      matter_rag_reports_<timestamp>/
        test_coverage_final_*.html       <- main report (open in browser)
        report_data_*.json               <- machine-readable results
        llm_generated_adocs/
          new_updated_TCs/               <- generated AsciiDoc TC files
          updated_testplans/             <- full test plan files with TCs inserted
        llm_calls.html                   <- LLM prompt/response audit log
    logs/
      ghpr_analysis_<context>_<timestamp>/
        engine.log                       <- full pipeline log
        llm_calls.jsonl                  <- per-call LLM log
```

Artifacts are retained for **30 days**.

### GitHub Actions Console

For real-time monitoring or debugging failed runs:
1. Go to the **Actions** tab in the spec repo
2. Click the workflow run
3. Expand the **Generate diff HTML and run analysis** step to see pipeline console output

---

## Manual Trigger

The per-PR workflow supports `workflow_dispatch` — you can trigger it manually from the pipeline repo for any spec PR:

1. Go to the pipeline repo > Actions > "TC Generation"
2. Click **Run workflow**
3. Enter the PR URL: `https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345`
4. Click **Run workflow**

This is useful for:
- Re-running analysis on an older PR
- Testing the pipeline without pushing to a PR
- Running from a fork that doesn't have the workflow

---

## Docker (Local)

Run the pipeline locally via Docker using the Makefile:

```bash
# Build the Docker image
make build

# Analyze a PR
PR_URL=https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
GITHUB_TOKEN=ghp_... make run-pr

# Analyze a single cluster
CLUSTER="On/Off" make run-cluster

# Force rebuild KG + FAISS
make rebuild-index

# Drop into container shell for debugging
make shell

# See all targets
make help
```

### `--docker-base` Flag

Use `--docker-base` to pull a pre-built base image and extract its data (KG, FAISS, DM XMLs, spec HTML) before running the pipeline. This is what the CI workflow does internally:

```bash
# Pull a base image and run PR analysis against it
python scripts/run_ghpr_analysis.py \
  --docker-base ghcr.io/your-org/matter-rag-base:latest \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/connectedhomeip-spec \
  --compare-only
```

The image is pulled, data is extracted to `data/`, and the pipeline runs using the pre-built data. Combine with `--pr-url` for full automation.

### docker-compose volumes

All data is volume-mounted so artifacts persist between runs:

| Container path | Host path | Purpose |
|---------------|-----------|---------|
| `/app/data/faiss_index` | `./data/faiss_index` | Vector DB |
| `/app/data/knowledge_graph` | `./data/knowledge_graph` | Knowledge graph |
| `/app/data/input_doc` | `./data/input_doc` | Diff HTML input |
| `/app/data/data_model` | `./data/data_model` | DM XML files |
| `/app/data/matter_spec` | `./data/matter_spec` | Spec HTML |
| `/app/data/test_plans` | `./data/test_plans` | Test plan HTML |
| `/app/reports` | `./reports` | Generated reports |
| `/app/logs` | `./logs` | Pipeline logs |

---

## Building the Base Image Manually

Use `build_docker_image.py` to build the base image locally:

```bash
# Build everything + push to registry
python scripts/helper_scripts/build_docker_image.py \
  --push ghcr.io/your-org/matter-rag-base:latest

# Build with custom branches
python scripts/helper_scripts/build_docker_image.py \
  --spec-branch main \
  --sdk-branch master \
  --test-plans-branch master \
  --push your-registry/matter-rag-base:latest

# Use versioned DM XML path (for SDK repos with versioned data_model layouts)
python scripts/helper_scripts/build_docker_image.py \
  --sdk-dm-path data_model/1.6/clusters \
  --push your-registry/matter-rag-base:latest

# Build data only (no Docker image)
python scripts/helper_scripts/build_docker_image.py --no-docker

# Skip test plans (use pre-existing HTMLs in data/test_plans/)
python scripts/helper_scripts/build_docker_image.py --skip-test-plans --push ...

# Skip spec HTML build (use pre-existing HTMLs in data/matter_spec/)
python scripts/helper_scripts/build_docker_image.py --skip-spec-build --push ...
```

### Build steps

| Step | What | Time |
|------|------|------|
| 1 | Clone spec repo | ~2 min |
| 2 | Clone SDK repo + copy DM XMLs | ~3 min |
| 3 | Clone test plans repo + `make html-all` | ~5 min |
| 4 | Build spec HTML via Docker | ~15-30 min |
| 5 | Build KG + FAISS index | ~10-20 min |
| 6 | Write manifest.json | instant |
| 7 | Build Docker image (alpine + data) | ~1 min |
| 8 | Push to registry | ~2-5 min |

Total: ~40-65 minutes for a full build.

### Prerequisites

- Docker Desktop running
- `GITHUB_TOKEN` env var set (for cloning private repos)
- Python 3.11+ with pipeline dependencies installed
- `docker login` to your target registry

---

## Iterating After a CI Run — Additional Context & Pass 4

The CI pipeline produces an initial set of test cases. After reviewing the output,
you can iterate locally to improve the results by providing additional context or
re-expanding with edits.

### Workflow: Download CI Results → Add Context → Re-run

**Step 1: Download the CI artifact**

From the PR's Actions tab, download `tc-reports-pr-<number>.zip`. Unzip it — you'll
have the full report, generated adocs, and the outline JSON.

**Step 2: Re-run with additional context**

If the generated TCs are missing domain knowledge (e.g., the LLM doesn't know about
HLS streaming or BLE frame formats), inject context:

```bash
# Inline text context
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --llm-additional-context "This cluster uses HLS streaming. Verify EXT-X-SESSION-KEY tag in playlist."

# Context from a file
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Proximity Ranging Cluster" \
  --llm-additional-context /path/to/ble_ranging_context.md

# Per-pass context from a directory (all.md, pass1.md, pass2.md, pass3.md)
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "AV Analysis Cluster" \
  --llm-additional-context /path/to/context_dir/
```

The `--llm-additional-context` flag is injected into:
- **Pass 1** system prompt (all LLM analysis calls)
- **Pass 2** cluster review prompt
- **Pass 3** consolidation + coverage gap expand prompts

### Workflow: Edit the TC Outline → Re-expand (Pass 4)

If you want to add/remove/rename specific TCs, edit the outline JSON and re-expand:

**Step 1: Find the outline JSON from the previous run**

```
reports/pr_<number>/reports/matter_rag_reports_<ts>/outline_*.json
```

If no outline exists (Pass 3 didn't produce one), create one manually:

```json
[
  {"tc_id": "TC-AVA-2.1", "title": "AV Analysis Attributes", "test_type": "unit",
   "scope": "Read all server attributes, validate types and constraints"},
  {"tc_id": "TC-AVA-2.2", "title": "EstablishAnalysisStream Command", "test_type": "unit",
   "scope": "Positive flow: allocate stream, verify response fields"},
  {"tc_id": "TC-AVA-3.1", "title": "Analysis Event Lifecycle", "test_type": "lifecycle_flow",
   "scope": "AnalysisSessionStart → PerceivedContext → AnalysisSessionEnd full sequence"}
]
```

**Step 2: Edit the outline**

- Add new TC entries with title + scope
- Remove TCs you don't want
- Change `test_type` (`"unit"`, `"lifecycle_flow"`, `"negative"`)
- Adjust scope descriptions

**Step 3: Re-run with the edited outline (Pass 4)**

```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "AV Analysis Cluster" \
  --third-pass-expand /path/to/edited_outline.json
```

Pass 4 only expands TCs that don't already exist in the KG. Results merge with
any Pass 1 output, giving you both PR-driven and manually curated TCs.

**Step 4: Combine with additional context**

```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "AV Analysis Cluster" \
  --third-pass-expand /path/to/edited_outline.json \
  --llm-additional-context "Focus on cross-cluster interactions with Zone Management."
```

### Per-Pass Context Directory Structure

For fine-grained control over what context each pass receives:

```
my_context/
  all.md       ← injected into ALL passes (global rules, PICS format, naming)
  pass1.md     ← Pass 1 only (analysis hints, entity descriptions)
  pass2.md     ← Pass 2 only (cluster review checklist, test types to cover)
  pass3.md     ← Pass 3 only (consolidation rules, coverage gap priorities)
```

```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Proximity Ranging Cluster" \
  --llm-additional-context my_context/
```

### CI + Local Iteration Loop

Typical workflow for a spec PR author:

1. **Push PR** → CI generates initial TCs automatically (PR comment)
2. **Review** the PR comment — are the TCs reasonable?
3. **Download artifact** if you want to iterate
4. **Re-run locally** with `--llm-additional-context` for domain hints
5. **Edit outline** if you want specific TCs added/removed
6. **Re-run with `--third-pass-expand`** for the final set
7. **Update PR description** with `<!-- matter-rag: -->` directives for future CI runs

All of this works with `--docker-base` too — no need to rebuild KG/FAISS locally:

```bash
python scripts/run_ghpr_analysis.py \
  --docker-base ghcr.io/chip-specifications/matter-qa/matter-rag-base:latest \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --llm-additional-context "This cluster uses HLS. Verify segment encryption."
```

---

## Troubleshooting

**Pipeline fails with "No diff HTMLs found":**
The PR may not have changes to `src/**/*.adoc` files. The pipeline needs spec diff HTML to analyze.

**LLM calls fail:**
Check that `ANTHROPIC_API_KEY` is set correctly in the spec repo's secrets. The secret name must match exactly.

**Base image pull fails:**
Verify the image exists: `docker pull ghcr.io/<org>/matter-qa/matter-rag-base:latest`. If the nightly build hasn't run yet, trigger it manually from the pipeline repo's Actions tab.

**PR comment not appearing:**
The workflow needs `pull-requests: write` permission. Check the workflow's `permissions` block.

**Stale data (new clusters missing from KG):**
The nightly build refreshes data daily at 2 AM UTC. For immediate updates, trigger the nightly build manually with workflow_dispatch.

**Large PR with many clusters — timeout:**
The workflow has a 2-hour timeout. For very large PRs, use `--cluster "Name"` in a PR directive to focus on specific clusters:
```markdown
<!-- matter-rag: --cluster "Push AV Stream Transport Cluster" -->
```
