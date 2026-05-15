# Helper Scripts

## Overview

Standalone utility scripts for building, verifying, converting, and auditing
pipeline components. Run from the project root.

## Scripts by Category

### Build

| Script | Purpose |
|--------|---------|
| `build_docker_image.py` | End-to-end Docker image builder (spec + DM + test plans + KG + FAISS) |
| `build_knowledge_base.py` | Standalone KnowledgeBase builder |
| `build_tc_index.py` | Build TC-ID/prefix/stem routing index from adoc files |
| `generate_spec_summaries.py` | LLM-generated Markdown summaries of protocol spec sections |

### Verify

| Script | Purpose |
|--------|---------|
| `verify_kg_tc_mapping.py` | 5-check structural verification of TC nodes (cluster, TESTS edges, PICS, REQ coverage) |
| `verify_kg_tc_cluster_assignments.py` | TC inventory dashboard with DUT role detection (HTML report) |

### Audit

| Script | Purpose |
|--------|---------|
| `audit_kg_viz_tc_bleed.py` | Measure foreign-TC bleed in KG ego-graph visualization |

### Convert

| Script | Purpose |
|--------|---------|
| `adoc_to_diff_html.py` | AsciiDoc to diff-annotated HTML (wraps content in `<ins>` tags) |
| `convert_zap_xmls.py` | ZAP extension XML to standard DM XML format |
| `generate_spec_diff.py` | GitHub PR to diff HTML via Docker spec build |

### Export

| Script | Purpose |
|--------|---------|
| `export_kg_csv.py` | Export KG to CSV (nodes + edges + health check) |

### Fix

| Script | Purpose |
|--------|---------|
| `fix_llm_call_html.py` | Patch LLM call log HTML with smart refresh + pane state persistence |

### Setup

| Script | Purpose |
|--------|---------|
| `setup_workspace.py` | Create external workspace directory for pipeline extensions |

## Key Scripts Detail

### `build_docker_image.py`

8-step build pipeline: clone spec → clone SDK (DM XMLs) → clone test plans + `make html-all` → build spec HTML via Docker → build KG + FAISS → write manifest → build alpine Docker image → push to registry.

**Key flags:** `--push REGISTRY:TAG`, `--no-docker`, `--skip-test-plans`, `--skip-spec-build`, `--spec-branch`, `--sdk-branch`, `--test-plans-branch`

### `verify_kg_tc_mapping.py`

5 checks per TC: (1) cluster node exists, (2) has TESTS edge, (3) TESTS targets consistent, (4) PICS prefix resolves, (5) has requirement coverage. Protocol TCs get WARN not FAIL for missing cluster edges.

**Key flags:** `--tc TC-ID` (single TC deep-dive), `--cluster "Name"`, `--only-issues`

### `adoc_to_diff_html.py`

Converts adoc → HTML via asciidoctor, then wraps all content elements in `<ins class="diff-new">` so the pipeline treats everything as new. Useful for analyzing entire new cluster specs.

### `generate_spec_diff.py`

Fetches PR metadata → checkout PR head → extract feature flags → compute merge-base → Docker `make html-diff` → copy diff HTMLs. Patches Makefile `--failure-level` and uses background watcher for `build/base/Makefile`.
