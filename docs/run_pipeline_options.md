# Matter RAG Pipeline — CLI Options & Operating Modes

## Quick Reference

```bash
python scripts/run_ghpr_analysis.py [OPTIONS]
```

---

## All CLI Options

### Input Source (mutually exclusive)

| Option | Type | Description |
|---|---|---|
| `--pr-url URL` | string | GitHub PR URL to fetch and analyse. Triggers spec diff builder (Docker-based HTML diff generation). Requires `--spec-repo` or `spec_repo.path` in config. |
| `--pr-number N` | int | PR number (requires `--repo`) |
| `--input-doc FILE` | path | Local HTML or adoc diff file to analyse |
| `--input-doc-dir DIR` | path | Directory of HTML diff files. Pipeline runs once per file. Mutually exclusive with `--input-doc`. |
| `--pr-snippet TEXT` | string | Raw text used as the PR input directly (requires cached KG; use with `--compare-only`) |

### Cluster Filtering

| Option | Type | Description |
|---|---|---|
| `--cluster NAME [NAME ...]` | string(s) | One or more cluster names. Case-insensitive partial match. Repeat or space-separate. Omit to process all clusters. |
| `--auto-detect-clusters` | flag | Parse `--input-doc` and find all cluster sections with change markers automatically; excludes the HTML footer (git revision tags). Runs the pipeline once per detected cluster. |
| `--run-set NAME` | string | Group all per-cluster report folders under `reports/<NAME>/`. Useful when running multiple clusters in one batch. |

### Build Flags

| Option | Default | Description |
|---|---|---|
| `--build-test-plan-vectors` | off | Re-chunk, embed, and save the test plan vector DB (run once after new test plans) |
| `--build-knowledge-graph` | off | Rebuild KG from DM XML + spec + test plan docs and save to disk |
| `--build-data-model` | off | Re-ingest Matter DM XML schema into the KG |
| `--build-knowledge-graph-withLLM` | off | After KG build, run LLM spec-refinement to add cross-cluster edges. Results cached by content hash. |
| `--index-only` | off | Alias: all three `--build-*` flags = True, no PR required |
| `--compare-only` | off | Alias: all build flags False — use cached data (fastest for repeated runs) |

### LLM Context Injection

| Option | Type | Description |
|---|---|---|
| `--spec-sections SECTION_IDS` | string | Comma-separated spec section prefixes (e.g. `11.7.1.8,11.7.2.2`) pulled verbatim from KG SECTION nodes into the 2nd/3rd-pass expand prompt (Tier 2). |
| `--llm-additional-context TEXT` | string | Raw domain knowledge appended to the 2nd/3rd-pass expand prompt as-is (Tier 3). See **Context Injection Tiers** below. |
| `--negative-tests` | flag | Ask the LLM to also generate error-path (negative) test cases — out-of-range writes, access violations, constraint errors. Off by default. |

### Report Content Control

| Option | Type | Description |
|---|---|---|
| `--no-coverage-gaps` | flag | Disable coverage gap TC generation (Section 2 of report). Default: coverage gaps enabled. When set, the report omits the "Coverage Gaps" section that identifies spec requirements with no existing test case coverage. |

### Human-in-the-Loop TC Authoring

| Option | Type | Description |
|---|---|---|
| `--third-pass-expand OUTLINE_JSON` | path | Path to a human-edited TC outline JSON from a previous 2nd-pass run. Re-expands non-existing TCs into full adoc sections and merges with pass-1 results. See **Mode 8** below for the full workflow. |

### Other Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--config FILE` | path | `config/config.yaml` | Config YAML path |
| `--spec-repo DIR` | path | from config | Local clone of spec repo (for `--pr-url` mode). Overrides `spec_repo.path` in config. |
| `--docker-base IMAGE` | string | — | Docker image with pre-built KG, FAISS, DM XMLs, and spec HTML. Pulls the image, extracts data to `data/`, and runs the pipeline using pre-built data. Use with `--pr-url` for full automation. Example: `ghcr.io/your-org/matter-rag-base:latest` |
| `--repo owner/repo` | string | — | GitHub repo (e.g. `project-chip/connectedhomeip`), required with `--pr-number` |
| `--test-plan-dir DIR` | path | — | Local test plan directory (legacy; prefer `sources.json`) |
| `--num-chunks N` | int | 0 (all) | Limit LLM analysis to the first N PR chunks — useful for quick verification |
| `--output DIR` | path | `reports/` | Directory for generated reports. When `--pr-url` is used without `--output`, defaults to `reports/pr_<number>/reports/` with logs in `reports/pr_<number>/logs/`. |
| `--log-level LEVEL` | string | from config | `VERBOSE` / `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--verbose` | flag | off | Shorthand for `--log-level VERBOSE` |

---

## Context Injection Tiers

The 2nd-pass TC expand prompt supports three tiers of injected context (applied in order):

| Tier | Source | How to supply |
|---|---|---|
| 1 — Spec prose | KG SECTION nodes matching the cluster | Automatic — pulled from KG during the expand step |
| 2 — Targeted spec sections | KG SECTION nodes by path prefix | `--spec-sections 11.7.1.8,11.7.2.2` |
| 3 — Raw domain knowledge | Freeform text | `--llm-additional-context "..."` |

### Skill File — Standing Instructions for Every Run

`config/skills/matter_spec_skill.md` is appended to the **system prompt** on every run
(after the spec reference sections 7.3 / 7.6 / 7.7). Use it for guidance that should apply
consistently across all future runs — cluster-specific patterns, testing style notes, or
protocol verification requirements you've identified as gaps.

```markdown
# config/skills/matter_spec_skill.md

For HLS Interface-2 encryption test cases:
- After AllocatePushTransport, set status to Active via SetTransportStatus, then trigger
  via ManuallyTriggerTransport.
- TH acts as CMAF ingest endpoint: verify EXT-X-SESSION-KEY tag in the multi-variant
  playlist (METHOD=AES-256-GCM, URI = SchemeURI + ":KID_" + uppercase_hex(KID) + ".key").
- Confirm no separate key file is uploaded to the TH endpoint.
- All init and media segments must be encrypted (16-byte IV prefix + 16-byte GCM auth tag).
```

The skill file is read at runtime — no rebuild required.  Edit it and re-run to see the
effect immediately. Lines are included verbatim (including Markdown `#` comments).
Leave the file with only `#`-comment lines to inject nothing.

> **Tip:** Use `--llm-additional-context` for one-off, run-specific guidance.
> Use the skill file for standing rules you want every run to follow.

---

## Operating Modes

### Mode 1 — Full Pipeline (analyse a local diff file)

```bash
# Single cluster — full workflow with coverage gaps
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster"

# Single cluster — skip coverage gap section in report
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --no-coverage-gaps

# All clusters (one LLM call per changed section)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html

# Auto-detect changed clusters and run each
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --auto-detect-clusters \
  --run-set pr12603
```

### Mode 2 — Analyse a GitHub Spec PR

```bash
# Fetch PR, generate diff HTML via Docker, auto-detect clusters, and analyse
GITHUB_TOKEN=ghp_... \
python scripts/run_ghpr_analysis.py \
  --pr-url https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12345 \
  --spec-repo /path/to/local/connectedhomeip-spec
```

When using `--pr-url`:
- Docker Desktop must be running (the spec build uses Docker for Asciidoctor)
- `GITHUB_TOKEN` must be set for private spec repos
- `--spec-repo` points to your local clone (or set `spec_repo.path` in config)
- The script fetches the PR, extracts in-progress feature flags from changed adoc files, runs `make html-diff-all`, then auto-detects and analyses all changed clusters

### Mode 3 — Rebuild Everything (no PR analysis, no LLM calls)

```bash
python scripts/run_ghpr_analysis.py --index-only
```

Equivalent to `--build-test-plan-vectors --build-knowledge-graph --build-data-model`.

### Mode 4 — Rebuild Knowledge Graph Only

```bash
python scripts/run_ghpr_analysis.py --build-knowledge-graph
```

Leaves FAISS index untouched. No PR → routes to END. **No LLM calls.**

### Mode 5 — Rebuild KG with LLM Spec Refinement

```bash
python scripts/run_ghpr_analysis.py --build-knowledge-graph-withLLM
```

Runs an LLM-assisted spec refinement pass after KG build to add cross-cluster
dependency and entity-reference edges. Results cached by content hash — unchanged
sections are free on re-runs. Configure a cheaper local LLM for this step:

```yaml
knowledge_graph:
  llm_refinement_provider: local
  llm_refinement_local_model: llama3.2
```

### Mode 6 — Quick Verification (limit chunks)

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --num-chunks 3
```

Runs exactly 3 LLM calls. Good for checking prompt/output quality without a full run.

### Mode 7 — Negative Tests

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Door Lock" \
  --negative-tests
```

Adds error-path test cases (constraint violations, out-of-range writes, access control
failures) alongside the standard positive TCs. Increases LLM output size per chunk.

### Mode 8 — Human-in-the-Loop (3rd Pass)

After a 2nd-pass run produces TC outline JSONs in the report folder:

1. Edit the outline JSON to add, remove, or modify TC entries
2. Re-run with `--third-pass-expand`:

```bash
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --third-pass-expand reports/.../second_pass_outline_PAVST.json
```

The pipeline re-expands only the non-existing TCs in the outline and merges
them with pass-1 results into the final adoc output.

**Typical `--input-doc` + `--cluster` + `--third-pass-expand` workflow:**

```bash
# Step 1: Initial run — generates pass-1 + pass-2 outline
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# Step 2: Review and edit the 2nd-pass outline JSON
#   reports/matter_rag_reports_<TS>/second_pass_outline_OnOff.json
#   - Remove TCs you don't want
#   - Edit titles, descriptions, or step outlines
#   - Add new TC entries if needed

# Step 3: Re-expand with edited outline
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --third-pass-expand reports/matter_rag_reports_<TS>/second_pass_outline_OnOff.json
```

---

## Pipeline DAG (current, 19 nodes)

```
fetch_documents_node
    → process_documents_node
        → ingest_data_model_node
            → build_matter_schema_node
                → chunk_embed_test_plans_node   ← build or load test plan vector DB
                    → chunk_pr_node             ← chunk PR diff sections
                        → extract_pr_changes_node  ← structured change extraction
                            → build_knowledge_graph_node  ← build or load KG
                                │
                                ├─[no pr_chunks] ──────────────► cleanup_node → END
                                │                (index/build-only modes)
                                └─[pr_chunks present]
                                    → search_test_plan_vector_db_node  ← top-k FAISS hits
                                        → search_knowledge_graph_node  ← KG entity lookup
                                            → analyze_chunks_with_llm_node  ← 1 call/chunk
                                                → cluster_review_node   ← cluster audit
                                                    → second_pass_tc_gen_node  ← holistic gaps
                                                        → human_outline_expand_node ← 3rd pass
                                                            → write_adoc_updates_node
                                                                → write_updated_testplan_node
                                                                    → generate_report_node
                                                                        → cleanup_node → END
```

---

## Build-Once Caching

| Artifact | Path | Rebuilt when |
|---|---|---|
| FAISS vector index | `data/faiss_index/matter.index` | `--build-test-plan-vectors` or file absent |
| FAISS sidecar JSON | `data/faiss_index/matter_store.json` | Same |
| Knowledge graph | `data/knowledge_graph/matter_kg.json` | `--build-knowledge-graph` or file absent |

**Auto-build**: if the file is missing on disk, the node builds automatically even when the
flag is `False` (first-run self-heal — no flags needed on the very first run).

---

## LLM Call Count

| Scenario | LLM calls |
|---|---|
| `--index-only` | 0 |
| `--build-knowledge-graph` only | 0 (unless `--build-knowledge-graph-withLLM`) |
| `--compare-only --cluster X --input-doc FILE` | ~1 per changed section in cluster X |
| `--compare-only --input-doc FILE` (all clusters) | ~1 per changed section across all clusters |
| `--num-chunks 3` | Exactly 3 (capped) |

> A large new cluster (e.g. PAVST with 140+ changed sections) will produce ~140 LLM calls
> in a single run. Use `--num-chunks` to sample before committing to a full run.

---

## Analysis Pipeline Scripts

Three standalone analysis pipelines run independently of any PR:

```bash
# Spec requirement coverage gaps — REQUIREMENTs with no TEST_CASE coverage
python scripts/run_coverage_analysis.py
python scripts/run_coverage_analysis.py --cluster "On/Off"
python scripts/run_coverage_analysis.py --max-llm-calls 10

# PICS code validation — wrong-side / non-existent / missing PICS codes
python scripts/run_pics_analysis.py
python scripts/run_pics_analysis.py --cluster "Door Lock"
python scripts/run_pics_analysis.py --dm-dir data/data_model

# SDK coverage — spec requirements vs connectedhomeip implementation code
python scripts/run_sdk_coverage_analysis.py
python scripts/run_sdk_coverage_analysis.py --cluster "On/Off"
python scripts/run_sdk_coverage_analysis.py --sdk-dir /path/to/connectedhomeip
python scripts/run_sdk_coverage_analysis.py --sdk-dirs-additional /path/to/extra/clusters
python scripts/run_sdk_coverage_analysis.py --max-llm-calls 20
```

All three require the KG to be built first. Reports written to `reports/` as HTML + JSON.
Log dirs: `logs/coverage_analysis_<TS>/`, `logs/pics_analysis_<TS>/`, `logs/sdk_coverage_analysis_<TS>/`.

---

## Per-Run Log Artifacts

Every run creates: `logs/ghpr_analysis_<YYYYMMDD_HHMMSS>/`

| File | Contents |
|---|---|
| `master.log` | All modules merged |
| `engine.log` | Node entry/exit, routing decisions |
| `llm.log` | Prompt previews + response previews per LLM call |
| `matter_diff_sections.json` | All PR diff sections found and processed |
| `pr_changes.json` | Structured change records extracted from PR chunks |
| `data_model_schema.json` | DM XML canonical schema snapshot |
| `spec_extractor_rejected_records.txt` | Spec sentences filtered as non-normative |
| `vector_chunks_ignored_or_rejected.txt` | TestCaseRecords that produced zero vector chunks |
| `pr_chunks_ignored_or_rejected.txt` | PR diff segments rejected as too short (< `min_chunk_chars`) |

`logs/llm_calls.jsonl` — full prompt + response JSONL, shared across all runs (not per-run).
`reports/matter_rag_reports_<TS>/llm_calls.html` — rendered HTML of all LLM calls for that run.

---

## Useful One-Liners

```bash
# First time: build everything from scratch
python scripts/run_ghpr_analysis.py --index-only

# Standard warm run for a single cluster
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster"

# Same, but inject spec prose for HLS encryption sections
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --spec-sections "11.7.2.2,11.7.1.8"

# One-off domain hint (doesn't persist across runs)
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport Cluster" \
  --llm-additional-context "Verify EXT-X-SESSION-KEY in HLS multi-variant playlist. \
Check METHOD=AES-256-GCM and URI = SchemeURI+':KID_'+hex(KID)+'.key'. \
No separate key file should be uploaded."

# Quick 3-chunk sanity check
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --num-chunks 3 \
  --verbose

# Rebuild KG + run analysis in one command
python scripts/run_ghpr_analysis.py \
  --build-knowledge-graph \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"

# All clusters, batched under one folder
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --auto-detect-clusters \
  --run-set pr12603

# Full workflow: input-doc + cluster + human-in-the-loop 3rd pass
python scripts/run_ghpr_analysis.py \
  --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --third-pass-expand reports/matter_rag_reports_20260430_120000/second_pass_outline_OnOff.json
```

---

## Known Issues

### `Invalid GitHub PR URL:` during `--index-only`

**Symptom:** Log shows `[fetch_documents_node] source 'pr_changes' failed: Invalid GitHub PR URL:`

**Cause:** A `github_pr` source entry in `sources.json` has `"pr_url": "${PR_URL}"` and `PR_URL` is not set. (The default `sources.json` no longer includes a `github_pr` entry in the active array, so this only affects custom configurations.)

**Impact:** None — pipeline completes successfully. Harmless log noise.

**Workaround:** Remove the `github_pr` entry from `sources.json`, or set `PR_URL=skip`.
