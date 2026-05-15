# Helper Scripts -- Usage Patterns

## KG Health Check

```bash
# Export KG to CSV
python scripts/helper_scripts/export_kg_csv.py

# Structural verification (all TCs)
python scripts/helper_scripts/verify_kg_tc_mapping.py

# Single cluster
python scripts/helper_scripts/verify_kg_tc_mapping.py --cluster "Door Lock"

# Single TC deep-dive
python scripts/helper_scripts/verify_kg_tc_mapping.py --tc TC-DL-2.1

# TC inventory dashboard (HTML)
python scripts/helper_scripts/verify_kg_tc_cluster_assignments.py
```

## Convert AsciiDoc to Pipeline Input

```bash
# Single file
python scripts/helper_scripts/adoc_to_diff_html.py /path/to/spec.adoc

# Directory (recursive)
python scripts/helper_scripts/adoc_to_diff_html.py /path/to/specs/ -o data/input_doc/

# Then analyze
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/spec_diff.html --cluster "My Cluster"
```

## Convert ZAP XMLs

```bash
python scripts/helper_scripts/convert_zap_xmls.py \
  --input /path/to/zap_xmls/ --output data/data_model/ \
  --pics-map "MyCluster=MYCL"
```

## Extension Workspace Setup

```bash
python scripts/helper_scripts/setup_workspace.py /path/to/workspace
# Then build with: --additional-config /path/to/workspace/config/config.yaml
```

## Docker Image Build

```bash
# Data only (no Docker)
python scripts/helper_scripts/build_docker_image.py --no-docker

# Build + push
GITHUB_TOKEN=ghp_... python scripts/helper_scripts/build_docker_image.py \
  --push ghcr.io/org/matter-rag-base:latest

# Custom branches
python scripts/helper_scripts/build_docker_image.py \
  --spec-branch main --test-plans-branch master --push ...
```

## Rebuild TC Index

```bash
python scripts/helper_scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src
```
