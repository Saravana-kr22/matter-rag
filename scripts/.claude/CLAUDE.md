# Scripts Module

## Purpose
CLI entry points for the Matter RAG pipeline. The main script is `run_ghpr_analysis.py`
which orchestrates document fetching, spec diff generation, cluster detection, and
multi-cluster pipeline execution.

## Files

| File | Purpose |
|---|---|
| `run_ghpr_analysis.py` | Main pipeline entry point — PR analysis, diff generation, cluster detection |
| `run_coverage_analysis.py` | Test plan coverage gap detection pipeline |
| `run_pics_analysis.py` | PICS code validation pipeline |
| `run_sdk_coverage_analysis.py` | SDK implementation coverage pipeline |
| `run_pipeline.py` | Legacy entry point (backward-compat alias) |
| `generate_test_scripts.py` | Generate Python test scripts from TC specifications |

## Cluster Auto-Detection (`_detect_revised_clusters`)

Located in `run_ghpr_analysis.py`. This function parses a Matter spec diff HTML file
and identifies which clusters have actual content changes (not just renumbering artifacts).

### How Diff HTML Works

The spec build system (`make html-diff`) produces HTML where changes are marked with
`<ins class="diff-new">` (additions) and `<del class="diff-old">` (removals). Each cluster
is a section under an `<h3 id="ref_*">` heading. The function scans for diff tags within
each cluster's section boundaries.

### The Problem: False Positives

The diff HTML produces four types of false positives that the parser must filter:

**1. Paragraph number renumbering**
When a new cluster is inserted (e.g., AV Analysis at chapter 11.9), all subsequent paragraph
numbers shift. Chime (11.8) gets `<del>[11.712]</del><ins>[11.713]</ins>` on every paragraph
even though no content changed. These are filtered by `_PARA_NUM_DIFF_RE`.

**2. ToC chapter number renumbering**
Similar to paragraph numbers, the Table of Contents chapter numbers shift when clusters are
inserted. These appear as `<a href="#..."><ins>8.</ins></a>` — bare digits inside anchor
tags. Filtered by `_TOC_NUM_DIFF_RE`.

**3. Git build metadata**
The diff HTML includes git revision SHAs and build dates that change between builds:
`<del>9c6149a01a</del><ins>0e29405778</ins>`. Filtered by `_GIT_META_DIFF_RE`.

**4. Cross-reference table spillover**
The spec has shared tables (e.g., cluster ID enumeration) that span multiple cluster sections.
When a new cluster is added, its entry appears as `<ins>` tags inside an adjacent cluster's
section boundary. Example: AV Analysis (0x0557) appears in the Commissioning Proxy section's
cluster ID table. Filtered by the spillover heuristic (only when < 10 diff tags and all
diff content references another cluster's name).

### The Problem: False Negatives

**New clusters not detected**
A brand-new cluster (entirely `<ins>`) may have a heading ID that doesn't follow the
`ref_*Cluster*` naming convention. Example: `<h3 id="ref_AVAnalysis">` instead of
`ref_AVAnalysisCluster`. The heading regex matches `id="ref_*"` with "Cluster" in the
body text (not the ID), catching both conventions.

Also, diff tags may wrap headings: `<ins><h3 id="ref_X">...</h3></ins>`. The unwrap
regexes (`_DIFF_WRAP_OPEN_RE`, `_DIFF_WRAP_CLOSE_RE`) strip these wrappers before scanning
so new/removed cluster headings are always visible as section boundaries.

### Detection Pipeline

```
Raw HTML
    |
    v
1. Unwrap <ins>/<del> around <h2>/<h3> headings
    |  (so new cluster headings become section boundaries)
    v
2. Find all cluster headings: <h2/h3 id="ref_*"> with "Cluster" in body text
    |  (builds list of section boundaries with cleaned names)
    v
3. For each section (heading to next heading or footer):
    |
    +-- Strip paragraph-number diffs: <ins>[11.713]</ins> -> removed
    +-- Strip ToC chapter-number diffs: <a ...><ins>8.</ins> -> removed
    +-- Strip git metadata diffs: <ins>9c6149a01a</ins> -> removed
    |
    +-- Check if any <ins>/<del> tags remain
    |     No  -> skip (no real changes)
    |     Yes -> continue
    |
    +-- Spillover check (only if < 10 diff tags):
    |     Extract all diff text, normalize whitespace
    |     If ALL diff text mentions another cluster's name
    |       AND does NOT mention this cluster's name
    |       -> skip (cross-reference table spillover)
    |
    +-- Cluster has real changes -> add to detected list
```

### Upfront Cluster Summary

Before processing any files, the script pre-computes `file_cluster_map: Dict[str, List[str]]`
by running `_detect_revised_clusters()` once per file. It prints a summary table showing all
detected clusters across all HTML files, then reuses the pre-computed map during processing
(no second detection call). This ensures the summary and processing always agree.

### Regex Reference

| Regex | Purpose |
|---|---|
| `_CLUSTER_H_RE` | Match cluster headings: `<h2/3 id="ref_*">..Cluster..</h2/3>` |
| `_DIFF_TAG_RE` | Detect any `<ins` or `<del` tag |
| `_PARA_NUM_DIFF_RE` | Match paragraph-number-only diffs: `<ins>[11.713]</ins>` |
| `_TOC_NUM_DIFF_RE` | Match ToC chapter-number diffs inside anchors: `<a ...><ins>8.</ins>` |
| `_GIT_META_DIFF_RE` | Match git SHA or date-only diffs |
| `_DIFF_WRAP_OPEN_RE` | Match `<ins/del>` wrapping a heading open tag |
| `_DIFF_WRAP_CLOSE_RE` | Match heading close tag followed by `</ins/del>` |

### Spillover Heuristic Details

The spillover check only runs when a section has fewer than 10 diff tags (large sections
are always real changes). It works by:

1. Extracting all diff tag content into a single string
2. Normalizing whitespace (collapse newlines, strip HTML tags)
3. Checking if any other cluster name (length > 3 chars) appears in the combined text
4. Checking if this cluster's own name does NOT appear in the combined text
5. If both conditions are true: the diffs are about another cluster, not this one

The length > 3 filter prevents false matches on very short cluster name fragments.

### Edge Cases

- **Footer exclusion**: The footer (`<div id="footer">`) is excluded from scanning
  because it contains git revision diff tags that aren't spec content.
- **`--auto-detect-clusters` + `--cluster`**: Mutually exclusive — the script errors
  if both are provided.
- **No clusters detected**: The file is skipped with a warning message.
- **Missing file**: If a file disappears between scan and processing, it's skipped with a warning.
- **`index_diff.html` vs `appclusters_diff.html`**: The spec produces separate diff
  HTMLs for the core spec and app clusters. Both are processed when using `--input-doc-dir`.

## Spec Diff Builder Integration

When `--pr-url` is provided, `run_ghpr_analysis.py` calls `spec_diff_builder.build_spec_diff()`
which:

1. Fetches PR metadata from GitHub API (changed files, head SHA, base SHA)
2. Checks out the PR head in the local spec repo
3. Computes merge-base: tries `origin/master` first, then `origin/main` (the spec repo
   uses `master`). Falls back to API-provided base SHA if both fail.
4. Extracts `ifdef::in-progress,<flag>` feature flags from changed adoc files
5. Patches the Makefile to downgrade `--failure-level=INFO` to `WARNING`
   (prevents unrelated broken cross-references from aborting the build)
6. Runs `make html html-diff html-diff-appclusters BASE=<sha>` via a wrapper
   script with a background watcher that also patches `build/base/Makefile`.
   Arguments are shell-escaped via `shlex.quote()`. Make output is streamed to
   both console (live progress) and `spec_diff_build.log` (tee behavior).
7. Copies `*_diff.html` files to `data/input_doc/`
8. Restores the original Makefile

The Makefile patching is necessary because the spec repo's Makefile uses
`--failure-level=INFO` which treats Asciidoctor INFO messages (like `possible invalid
reference`) as fatal errors. The wrapper script also patches `build/base/Makefile`
(the base commit's Makefile, checked out by the diff target) via a background file watcher.

## Multi-File Pipeline Execution

When `--input-doc-dir` is used (or `--pr-url` generates multiple diff files):

```
Phase 1 — Upfront scan (pre-compute file_cluster_map):
  for each HTML file:
    _detect_revised_clusters() → cached in file_cluster_map

Phase 2 — Print cluster detection summary (all files, all clusters)

Phase 3 — Process:
  for each HTML file in input_doc_dir:
    1. Check file exists (skip if missing with warning)
    2. Look up clusters from pre-computed file_cluster_map (no re-detection)
    3. For each detected cluster:
       - Run full pipeline (fetch → process → ... → report)
       - Each cluster gets its own output directory
    4. Collect exit codes
```

Files are processed sequentially. Each cluster within a file is also processed
sequentially (no parallelism at the cluster level).
