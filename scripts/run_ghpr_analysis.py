#!/usr/bin/env python3
"""CLI entry point — GitHub PR analysis pipeline.

Fetches a GitHub PR diff, compares it against the test plan vector DB and
knowledge graph, and generates a report of missing or needs-update test cases.

Replaces: run_pipeline.py (kept as a backward-compatibility alias).

Multi-cluster usage
-------------------
Pass multiple ``--cluster`` values to run the pipeline once per cluster and
group all output folders under a named run-set::

    python scripts/run_ghpr_analysis.py \\
        --compare-only --input-doc data/input_doc/appclusters_diff.html \\
        --cluster "On/Off Cluster" "Level Control Cluster" \\
        --run-set appclusters_rev5

Auto-detect clusters that have changes in the diff::

    python scripts/run_ghpr_analysis.py \\
        --compare-only --input-doc data/input_doc/appclusters_diff.html \\
        --auto-detect-clusters \\
        --run-set appclusters_rev5
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# macOS / Apple Silicon: set threading env vars BEFORE any native library
# (faiss, numpy, OpenBLAS) is imported.  Without this, faiss-cpu segfaults
# on arm64 due to OpenBLAS spawning threads that conflict with Python's
# multiprocessing (loky).  Must be done before all other imports.
# ---------------------------------------------------------------------------
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# huggingface_hub reads HF_HUB_OFFLINE at *import time* into a module-level
# constant, so we must set it here — before any import — if offline mode is
# configured.  We do a cheap pre-parse of the YAML with only stdlib yaml.
try:
    import sys as _sys, yaml as _yaml
    _cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "config.yaml",
    )
    with open(_cfg_path) as _f:
        _raw = _yaml.safe_load(_f)
    if _raw.get("embeddings", {}).get("offline", False):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    del _sys, _yaml, _cfg_path, _raw, _f
except Exception:
    pass
# ---------------------------------------------------------------------------

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List

# Allow running as `python scripts/run_ghpr_analysis.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.config_loader import load_config
from src.engine.pipeline import MatterRAGPipeline
from src.logging_config import configure_pipeline_logging


# ---------------------------------------------------------------------------
# Cluster auto-detection
# ---------------------------------------------------------------------------

_CLUSTER_H_RE   = re.compile(
    r'<h[23][^>]*id="ref_[^"]*"[^>]*>(.*?Cluster.*?)</h[23]>',
    re.DOTALL | re.IGNORECASE,
)
_CLUSTER_H_POS  = re.compile(
    r'<h[23][^>]*id="ref_[^"]*Cluster[^"]*"',
    re.IGNORECASE,
)
_DIFF_TAG_RE    = re.compile(r'<(?:ins|del)\s', re.IGNORECASE)
# Diff tags that only contain paragraph numbers, ToC chapter numbers, or git metadata.
# Paragraph numbers: [11.712]
# ToC chapter numbers: bare digits like "8." or "10.1." — but ONLY when they appear
# inside an <a> anchor (ToC links), not in content text. We match the broader pattern
# and check context in the detection loop.
_PARA_NUM_DIFF_RE = re.compile(
    r'<(ins|del)[^>]*>\s*\[[\d.]+\]\s*</(ins|del)>',
    re.IGNORECASE,
)
# ToC chapter number diffs inside anchor tags: <a ...><ins>8.</ins>...</a>
_TOC_NUM_DIFF_RE = re.compile(
    r'<a\s[^>]*>\s*<(ins|del)[^>]*>\s*\d+(?:\.\d+)*\.?\s*</(ins|del)>',
    re.IGNORECASE,
)
_GIT_META_DIFF_RE = re.compile(
    r'<(ins|del)[^>]*>\s*(?:[0-9a-f]{8,40}|20\d{2}-\d{2}-\d{2}[\s\d:+-]*)\s*</(ins|del)>',
    re.IGNORECASE,
)
_TAG_RE         = re.compile(r'<[^>]+>')
_WS_RE          = re.compile(r'\s+')

# Patterns to strip <ins>/<del> wrappers around cluster headings so that
# newly-added or removed clusters are still detected as section boundaries.
_DIFF_WRAP_OPEN_RE  = re.compile(r'<(ins|del)[^>]*>\s*(<h[23])', re.IGNORECASE)
_DIFF_WRAP_CLOSE_RE = re.compile(r'(</h[23]>)\s*</(ins|del)>', re.IGNORECASE)


def _detect_revised_clusters(html_path: str) -> List[str]:
    """Parse a Matter spec appclusters diff HTML and return cluster names that contain changes.

    Strategy
    --------
    1. Strip ``<ins>``/``<del>`` wrappers around ``<h2>``/``<h3>`` headings so
       that newly-added clusters are visible as section boundaries.
    2. Locate every ``<h2/h3 id="ref_*Cluster*">`` heading (one per cluster).
    3. For each heading, take the raw HTML up to the next cluster heading.
    4. Check whether that section contains any ``<ins`` or ``<del`` diff tags.
    5. Return the deduplicated list of cluster names that have at least one diff tag.
    """
    raw = Path(html_path).read_text(encoding="utf-8", errors="replace")

    # Unwrap diff tags around headings so new/removed clusters are detected
    raw = _DIFF_WRAP_OPEN_RE.sub(r'\2', raw)
    raw = _DIFF_WRAP_CLOSE_RE.sub(r'\1', raw)

    # Find all cluster heading positions and names
    headings: List[tuple] = []  # (position, cleaned_name)
    for m in _CLUSTER_H_RE.finditer(raw):
        name = _TAG_RE.sub(" ", m.group(1))
        name = _WS_RE.sub(" ", name).strip()
        # Drop leading section number (e.g. "1.5. On/Off Cluster" → "On/Off Cluster")
        name = re.sub(r'^\d+(?:\.\d+)*\.\s*', '', name).strip()
        headings.append((m.start(), name))

    if not headings:
        return []

    # Find footer position to exclude from diff detection
    # (the footer contains git revision <ins>/<del> tags that are not spec content)
    _footer_pos = raw.find('<div id="footer">')
    if _footer_pos < 0:
        _footer_pos = len(raw)

    # For each cluster section, check for <ins> or <del> tags (excluding footer).
    # Strip paragraph-number-only diffs and git metadata diffs first — these are
    # caused by paragraph renumbering when content is inserted elsewhere, not by
    # actual changes in this cluster.
    # Also filter cross-reference table spillover: if ALL remaining diff content
    # references a different cluster name, it's spillover from a new cluster's
    # entry in a shared table (e.g., cluster ID enumeration table).
    all_cluster_names = {n.lower() for _, n in headings}
    clusters: List[str] = []
    seen: set = set()
    for idx, (start, name) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else _footer_pos
        section_html = raw[start:end]
        cleaned = _PARA_NUM_DIFF_RE.sub('', section_html)
        cleaned = _TOC_NUM_DIFF_RE.sub('<a ', cleaned)
        cleaned = _GIT_META_DIFF_RE.sub('', cleaned)
        if not _DIFF_TAG_RE.search(cleaned) or name in seen:
            continue
        # Check for cross-reference spillover: when a small number of diffs in a
        # section only mention another cluster's name, it's likely a shared table
        # entry (e.g., cluster ID enumeration) rather than real content changes.
        # Only apply this filter for low diff counts — large sections are real.
        diff_texts = re.findall(
            r'<(?:ins|del)[^>]*>(.*?)</(?:ins|del)>', cleaned, re.DOTALL | re.IGNORECASE,
        )
        if diff_texts and len(diff_texts) < 10:
            combined = " ".join(t.strip() for t in diff_texts).lower()
            combined_clean = _TAG_RE.sub(" ", combined).strip()
            combined_clean = _WS_RE.sub(" ", combined_clean)
            name_lower = name.lower().replace(" cluster", "")
            other_names = {n.replace(" cluster", "") for n in all_cluster_names} - {name_lower}
            is_spillover = any(
                other in combined_clean for other in other_names
                if len(other) > 3
            ) and name_lower not in combined_clean
            if is_spillover:
                continue
        seen.add(name)
        clusters.append(name)

    return clusters


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Matter RAG — compare PR changes against test plans",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config YAML (default: config/config.yaml)"
    )
    parser.add_argument(
        "--additional-config",
        metavar="FILE",
        default="",
        help="Path to overlay config YAML that extends the base config. "
             "Values are deep-merged on top of the base config. Use for "
             "additional DM XMLs, test plans, spec sources, and custom paths."
    )
    parser.add_argument("--pr-url", help="GitHub PR URL")
    parser.add_argument("--pr-number", help="PR number (requires --repo)")
    parser.add_argument("--repo", help="owner/repo (e.g. project-chip/connectedhomeip)")
    parser.add_argument(
        "--spec-repo", metavar="DIR", default="",
        help="Path to local clone of the spec repo (for --pr-url mode). "
             "Overrides config.spec_repo.path.",
    )
    parser.add_argument(
        "--docker-base", metavar="IMAGE", default="",
        help="Docker image with pre-built KG, FAISS, DM XMLs, and spec HTML. "
             "The image is pulled, data is extracted to data/, and the pipeline "
             "runs using the pre-built data. Use with --pr-url for full automation. "
             "Example: ghcr.io/your-org/matter-rag-base:latest",
    )
    parser.add_argument(
        "--input-doc",
        metavar="FILE",
        help="Local HTML (or adoc) file to analyse as the change input, "
             "instead of fetching from GitHub. "
             "Mutually exclusive with --pr-url / --pr-number.",
    )
    parser.add_argument(
        "--input-doc-dir",
        metavar="DIR",
        default="",
        help="Directory containing HTML diff files. The pipeline runs once per file "
             "(combined with --auto-detect-clusters or --cluster). "
             "Mutually exclusive with --input-doc.",
    )
    parser.add_argument(
        "--cluster",
        metavar="CLUSTER_NAME",
        nargs="+",
        default=[],
        help="One or more cluster names to analyse, e.g. 'On/Off Cluster' "
             "'Level Control Cluster'.  Repeat or space-separate.  "
             "When multiple clusters are given the pipeline runs once per cluster "
             "and each run gets its own timestamped report folder.  "
             "Case-insensitive partial match.  Omit to process all clusters.",
    )
    parser.add_argument(
        "--auto-detect-clusters",
        action="store_true",
        help="Parse the --input-doc HTML diff and automatically find all cluster "
             "sections that contain [CHANGED:]/[ADDED:]/[REMOVED:] markers.  "
             "The detected list is printed and then the pipeline runs for each.  "
             "Mutually exclusive with explicit --cluster values.",
    )
    parser.add_argument(
        "--run-set",
        metavar="NAME",
        default="",
        help="Group all per-cluster report folders under reports/<NAME>/.  "
             "Useful when running multiple clusters in one batch so the outputs "
             "are co-located.  Each folder is still individually timestamped and "
             "named with the cluster slug.",
    )
    parser.add_argument("--test-plan-dir", help="Local directory with test plan documents")
    parser.add_argument(
        "--build-test-plan-vectors", action="store_true",
        help="Re-chunk, embed, and save the test plan vector DB (run once after new test plans)"
    )
    parser.add_argument(
        "--build-knowledge-graph", action="store_true",
        help="Rebuild and save the knowledge graph from spec + test plan docs (run once after updates)"
    )
    parser.add_argument(
        "--build-data-model", action="store_true",
        help="Re-ingest Matter DM XML schema into the knowledge graph (run once after XML updates)"
    )
    parser.add_argument(
        "--build-knowledge-graph-withLLM", dest="build_kg_with_llm",
        action="store_true",
        help="Run LLM-assisted spec refinement after building the KG to add cross-cluster "
             "dependency and entity-reference edges. Implies --build-knowledge-graph. "
             "Results are cached by content hash so unchanged sections are free on re-runs."
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="Build vector DB + knowledge graph + data model without comparing a PR "
             "(alias for --build-test-plan-vectors --build-knowledge-graph --build-data-model)"
    )
    parser.add_argument(
        "--compare-only", action="store_true",
        help="Use cached vector DB and KG; only fetch and analyse the PR (fast)"
    )
    parser.add_argument(
        "--num-chunks",
        type=int,
        default=0,
        metavar="N",
        help="Limit LLM analysis to the first N PR chunks (0 = process all). "
             "Useful for quick verification runs.",
    )
    parser.add_argument("--output", help="Output directory for reports (overrides config)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (trace-level) logging")
    parser.add_argument(
        "--log-level",
        default=None,
        metavar="LEVEL",
        help="Override log level: VERBOSE | DEBUG | INFO | WARNING | ERROR",
    )
    parser.add_argument(
        "--third-pass-expand",
        metavar="OUTLINE_JSON",
        default="",
        help="Path to a human-modified TC outline JSON produced by a previous second-pass run. "
             "Re-expands all non-existing TCs in the outline into full adoc sections and "
             "merges them with the pass-1 results. Enables 'human-in-the-loop' TC authoring.",
    )
    parser.add_argument(
        "--pr-snippet",
        metavar="TEXT",
        default="",
        help="Raw text to use as the PR change input instead of fetching from GitHub or "
             "--input-doc.  The text is split into Document chunks and injected directly, "
             "bypassing fetch and chunking stages.  Requires a cached KG (use with "
             "--compare-only).  Mutually exclusive with --pr-url / --input-doc.",
    )
    parser.add_argument(
        "--negative-tests",
        action="store_true",
        default=False,
        help="Ask the LLM to also generate error-path (negative) test cases for each changed "
             "entity — out-of-range writes, access violations, constraint errors, etc. "
             "Disabled by default to keep prompt size and LLM cost low.",
    )
    parser.add_argument(
        "--no-coverage-gaps",
        action="store_true",
        default=False,
        help="Disable coverage gap TC generation (Section 7 of report)",
    )
    parser.add_argument(
        "--spec-sections",
        metavar="SECTION_IDS",
        default="",
        help="Comma-separated spec section path prefixes to pull verbatim into the 2nd/3rd-pass "
             "expand prompt (Tier 2 context injection).  "
             "Example: '11.7.1.8,11.7.2.2' injects those sections from the KG SECTION nodes "
             "into the expand prompt so the LLM can reference full protocol prose.",
    )
    parser.add_argument(
        "--llm-additional-context",
        metavar="TEXT",
        default="",
        help="Raw domain knowledge appended to the 2nd/3rd-pass expand prompt as-is "
             "(Tier 3 context injection).  "
             "Example: 'Use KID=0x0000000000000001; verify EXT-X-SESSION-KEY before EXT-X-MAP.'",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Single-cluster run helper
# ---------------------------------------------------------------------------

def _run_one_cluster(
    *,
    pipeline: MatterRAGPipeline,
    config,
    pr_url: str,
    input_doc: str,
    pr_snippet: str,
    cluster_filter: str,
    build_tp: bool,
    build_kg: bool,
    build_dm: bool,
    build_kg_with_llm: bool,
    output_dir: str,
    run_set_dir: str,
    num_chunks: int,
    third_pass: str,
    negative_tests: bool,
    include_coverage_gaps: bool,
    logger: logging.Logger,
) -> int:
    """Run the pipeline for one cluster and print a summary.  Returns 0 on success."""
    from src.engine.run_context import RunContext, set_run_context, _current_run_ctx

    # Build a descriptive pipeline_name for the log folder:
    #   ghpr_analysis_pr12603_PAVST  or  ghpr_analysis_appclusters_diff
    _log_parts = ["ghpr_analysis"]
    if pr_snippet:
        _log_parts.append(pr_snippet)
    elif input_doc:
        import os
        _log_parts.append(os.path.splitext(os.path.basename(input_doc))[0])
    if cluster_filter:
        import re as _re
        _slug = _re.sub(r"[^a-z0-9]+", "_", cluster_filter.lower()).strip("_")[:30]
        _log_parts.append(_slug)
    _pipeline_name = "_".join(_log_parts)

    run_dir = configure_pipeline_logging(config, pipeline_name=_pipeline_name)
    run_ctx = RunContext(
        run_id=run_dir.name,
        run_dir=run_dir,
        client="ghpr_analysis",
    )
    run_token = set_run_context(run_ctx)
    logger.info("Run logs: %s", run_dir)

    # Determine the effective output directory:
    #   --run-set overrides --output; both override config default.
    effective_output = run_set_dir or output_dir or None

    # Build a human-readable label for the output folder:
    #   <input_doc_stem>  or  <input_doc_stem>_<cluster>  (cluster only if filtered)
    _doc_stem = Path(input_doc).stem if input_doc else ""
    _label_parts = [p for p in [_doc_stem, cluster_filter] if p]
    _run_label = "_".join(_label_parts)

    result = pipeline.run(
        pr_url=pr_url,
        input_doc=input_doc,
        pr_snippet=pr_snippet,
        cluster_filter=cluster_filter,
        build_test_plan_vectors=build_tp,
        build_knowledge_graph=build_kg,
        build_data_model=build_dm,
        build_knowledge_graph_with_llm=build_kg_with_llm,
        output_dir=effective_output,
        run_label=_run_label,
        run_ctx=run_ctx,
        max_pr_chunks=num_chunks,
        third_pass_outline_path=third_pass,
        generate_negative_tests=negative_tests,
        include_coverage_gaps=include_coverage_gaps,
    )
    run_ctx.close()
    _current_run_ctx.reset(run_token)

    label = f"[{cluster_filter}]" if cluster_filter else "[all clusters]"
    print("\n" + "=" * 60)
    print(f"Matter RAG Pipeline — Results {label}")
    print("=" * 60)
    print(f"Run log dir:        {run_dir}")
    if result.report_path:
        print(f"Report:             {result.report_path}")
    print(f"Missing tests:      {len(result.missing_tests)}")
    print(f"Update candidates:  {len(result.update_candidates)}")
    if result.negative_tests:
        print(f"Negative tests:     {len(result.negative_tests)}")
    print(f"PR chunks:          {result.num_pr_chunks}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  - {err}")
    print("=" * 60 + "\n")

    return 0 if not result.errors else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    config = load_config(args.config, additional_config=args.additional_config or None)

    if args.log_level:
        config.logging.level = args.log_level.upper()
    elif args.verbose:
        config.logging.level = "VERBOSE"

    # Tier 2/3 context injection — CLI overrides config.yaml
    if args.spec_sections:
        config.pipeline.spec_sections = [s.strip() for s in args.spec_sections.split(",") if s.strip()]
    if args.llm_additional_context:
        config.pipeline.llm_additional_context = args.llm_additional_context

    logger = logging.getLogger(__name__)

    # Resolve PR URL from --pr-number + --repo if needed
    pr_url = args.pr_url or ""
    if not pr_url and args.pr_number:
        repo = args.repo or config.fetcher.default_repo
        pr_url = f"https://github.com/{repo}/pull/{args.pr_number}"

    # When --pr-url is used and no --output is specified, default to a PR-based
    # output folder (reports/pr_<number>/) so all artifacts are grouped by PR.
    # Logs also go inside this folder for easy CI artifact zipping.
    pr_number_str = ""
    if pr_url:
        _pr_match = re.search(r'/pull/(\d+)', pr_url)
        if _pr_match:
            pr_number_str = _pr_match.group(1)
    if pr_number_str and not args.output:
        pr_output_dir = f"reports/pr_{pr_number_str}"
        args.output = f"{pr_output_dir}/reports"
        config.pipeline.output_dir = f"{pr_output_dir}/reports"
        config.pipeline.logs_dir = f"{pr_output_dir}/logs"
        Path(f"{pr_output_dir}/reports").mkdir(parents=True, exist_ok=True)
        Path(f"{pr_output_dir}/logs").mkdir(parents=True, exist_ok=True)
        logger.info("PR output directory: %s", pr_output_dir)

    # ── Docker base image extraction ───────────────────────────────────────
    # When --docker-base is provided, pull the image and extract pre-built
    # data (KG, FAISS, DM XMLs, spec HTML, manifest) to local data/ directory.
    if args.docker_base:
        from src.fetcher.docker_base import extract_docker_base
        manifest = extract_docker_base(args.docker_base, Path("data"))
        if manifest:
            # Use the manifest's spec commit for the spec repo checkout
            logger.info("Docker base loaded: spec=%s sdk=%s built=%s",
                        manifest.get("spec_commit", "")[:10],
                        manifest.get("sdk_commit", "")[:10],
                        manifest.get("built_at", ""))
            # If --pr-url is also provided, the spec repo in the Docker image
            # provides the BASE commit for diff generation
            if not args.spec_repo and not config.spec_repo.path:
                logger.info("No --spec-repo specified — spec repo from Docker image will be used")

    # ── Spec PR diff generation ─────────────────────────────────────────────
    # When --pr-url is provided, generate diff HTML from the spec PR via Docker
    # then continue with the pipeline using the generated diff files.
    if pr_url and not args.input_doc and not args.input_doc_dir:
        from src.fetcher.spec_diff_builder import build_spec_diff

        # Override spec_repo.path from CLI if provided
        if args.spec_repo:
            config.spec_repo.path = args.spec_repo

        logger.info("Fetching spec PR and generating diff HTML: %s", pr_url)
        output_dir = Path("data/input_doc")
        # Clear previous diff HTMLs to avoid stale data
        if output_dir.exists():
            for old_diff in output_dir.glob("*_diff.html"):
                old_diff.unlink(missing_ok=True)

        diff_files = build_spec_diff(
            pr_url=pr_url,
            config=config,
            output_dir=output_dir,
            log_dir=Path(config.pipeline.logs_dir) if hasattr(config.pipeline, "logs_dir") else None,
            token=os.environ.get("GITHUB_TOKEN", ""),
        )

        if not diff_files:
            logger.error("Spec diff generation produced no output — check Docker and PR URL")
            return 1

        # Switch to --input-doc-dir mode with auto-detect clusters
        args.input_doc_dir = str(output_dir)
        args.auto_detect_clusters = True
        pr_url = ""  # Clear so the pipeline doesn't try to fetch via API

    # Validate --input-doc
    input_doc = args.input_doc or ""
    input_doc_dir = args.input_doc_dir or ""
    if input_doc and input_doc_dir:
        logger.error("--input-doc and --input-doc-dir are mutually exclusive.")
        return 1
    if input_doc and pr_url:
        logger.error("--input-doc and --pr-url / --pr-number are mutually exclusive.")
        return 1
    if input_doc:
        p = Path(input_doc)
        if not p.is_file():
            logger.error("--input-doc: file not found: %s", input_doc)
            return 1
        input_doc = str(p.resolve())

    # Expand --input-doc-dir into a list of HTML files to process sequentially
    input_doc_files: List[str] = []
    if input_doc_dir:
        d = Path(input_doc_dir)
        if not d.is_dir():
            logger.error("--input-doc-dir: directory not found: %s", input_doc_dir)
            return 1
        input_doc_files = sorted(str(f.resolve()) for f in d.rglob("*.html"))
        if not input_doc_files:
            logger.error("--input-doc-dir: no .html files found in %s", input_doc_dir)
            return 1
        logger.info("--input-doc-dir: found %d HTML file(s) in %s", len(input_doc_files), input_doc_dir)

    pr_snippet = args.pr_snippet or ""
    if pr_snippet and (pr_url or input_doc):
        logger.error("--pr-snippet is mutually exclusive with --pr-url / --input-doc.")
        return 1

    build_tp = args.build_test_plan_vectors or args.index_only
    build_kg = args.build_knowledge_graph or args.index_only or args.build_kg_with_llm
    build_dm = args.build_data_model or args.index_only

    if not (build_tp or build_kg or build_dm) and not pr_url and not input_doc and not input_doc_files and not pr_snippet:
        logger.error(
            "Provide one of: --pr-url, --pr-number, --input-doc <file>, --input-doc-dir <dir>, "
            "--pr-snippet '<text>', or use --index-only / --build-knowledge-graph / "
            "--build-test-plan-vectors to rebuild cached data without analysing a PR."
        )
        return 1

    pipeline = MatterRAGPipeline(config)

    # ── --input-doc-dir: loop over each HTML file ──────────────────────────
    if input_doc_files:
        # Run-set output directory
        run_set_dir = ""
        if args.run_set:
            base = Path(args.output or config.pipeline.output_dir)
            run_set_dir = str(base / args.run_set)
            Path(run_set_dir).mkdir(parents=True, exist_ok=True)

        print(f"\nProcessing {len(input_doc_files)} HTML file(s) from --input-doc-dir:\n")
        all_exit_codes: List[int] = []

        # ── Upfront cluster scan: detect clusters across ALL files first ──
        file_cluster_map: Dict[str, List[str]] = {}
        if args.auto_detect_clusters and not args.cluster:
            total_clusters = 0
            for doc_file in input_doc_files:
                if not Path(doc_file).is_file():
                    continue
                detected = _detect_revised_clusters(doc_file)
                file_cluster_map[doc_file] = detected
                total_clusters += len(detected)

            print(f"{'=' * 60}")
            print(f"  Cluster Detection Summary ({total_clusters} cluster(s) across {len(input_doc_files)} file(s))")
            print(f"{'=' * 60}")
            for doc_file, clusters in file_cluster_map.items():
                fname = Path(doc_file).name
                if clusters:
                    print(f"\n  {fname}:")
                    for c in clusters:
                        print(f"    - {c}")
                else:
                    print(f"\n  {fname}: (no changes detected — will skip)")
            print(f"\n  Total: {total_clusters} cluster(s) to analyze")
            print(f"{'=' * 60}\n")

        for file_idx, doc_file in enumerate(input_doc_files, 1):
            if not Path(doc_file).is_file():
                logger.warning("Skipping missing file: %s", doc_file)
                continue
            print(f"\n{'=' * 60}")
            print(f"  File {file_idx}/{len(input_doc_files)}: {Path(doc_file).name}")
            print(f"{'=' * 60}")

            # Auto-detect clusters per file if requested
            file_clusters: List[str] = list(args.cluster)
            if args.auto_detect_clusters:
                if file_clusters:
                    logger.error(
                        "--auto-detect-clusters and explicit --cluster values are mutually exclusive."
                    )
                    return 1
                # Reuse pre-computed results from upfront scan
                file_clusters = file_cluster_map.get(doc_file, [])
                if file_clusters:
                    print(f"  Auto-detected {len(file_clusters)} cluster(s):")
                    for c in file_clusters:
                        print(f"    - {c}")
                else:
                    print(f"  No clusters with changes detected — skipping {Path(doc_file).name}")
                    continue

            if len(file_clusters) <= 1:
                cluster_filter = file_clusters[0] if file_clusters else ""
                code = _run_one_cluster(
                    pipeline=pipeline, config=config,
                    pr_url=pr_url, input_doc=doc_file, pr_snippet=pr_snippet,
                    cluster_filter=cluster_filter,
                    build_tp=build_tp, build_kg=build_kg, build_dm=build_dm,
                    build_kg_with_llm=args.build_kg_with_llm,
                    output_dir=args.output or "", run_set_dir=run_set_dir,
                    num_chunks=args.num_chunks,
                    third_pass=args.third_pass_expand or "",
                    negative_tests=args.negative_tests,
                    include_coverage_gaps=not args.no_coverage_gaps,
                    logger=logger,
                )
                all_exit_codes.append(code)
            else:
                for c_idx, cluster in enumerate(file_clusters, 1):
                    print(f"  Cluster {c_idx}/{len(file_clusters)}: {cluster}")
                    code = _run_one_cluster(
                        pipeline=pipeline, config=config,
                        pr_url=pr_url, input_doc=doc_file, pr_snippet=pr_snippet,
                        cluster_filter=cluster,
                        build_tp=build_tp, build_kg=build_kg, build_dm=build_dm,
                        build_kg_with_llm=args.build_kg_with_llm,
                        output_dir=args.output or "", run_set_dir=run_set_dir,
                        num_chunks=args.num_chunks,
                        third_pass=args.third_pass_expand or "",
                        negative_tests=args.negative_tests,
                        include_coverage_gaps=not args.no_coverage_gaps,
                        logger=logger,
                    )
                    all_exit_codes.append(code)

        errors = sum(1 for c in all_exit_codes if c != 0)
        total = len(all_exit_codes)
        print(f"\n{'=' * 60}")
        print(f"--input-doc-dir complete: {total} run(s), {errors} error(s)")
        if run_set_dir:
            print(f"All reports in: {run_set_dir}")
        print(f"{'=' * 60}\n")
        return 0 if errors == 0 else 1

    # ── Cluster list resolution ──────────────────────────────────────────────
    clusters: List[str] = list(args.cluster)

    if args.auto_detect_clusters:
        if clusters:
            logger.error(
                "--auto-detect-clusters and explicit --cluster values are mutually exclusive."
            )
            return 1
        if not input_doc:
            logger.error("--auto-detect-clusters requires --input-doc.")
            return 1
        clusters = _detect_revised_clusters(input_doc)
        if clusters:
            print(f"\nAuto-detected {len(clusters)} cluster(s) with changes:")
            for c in clusters:
                print(f"  • {c}")
            print()
        else:
            print("No clusters with changes detected in the diff.  Nothing to do.")
            return 0

    # ── Run-set output directory ─────────────────────────────────────────────
    run_set_dir = ""
    if args.run_set:
        base = Path(args.output or config.pipeline.output_dir)
        run_set_dir = str(base / args.run_set)
        Path(run_set_dir).mkdir(parents=True, exist_ok=True)
        print(f"Run-set output dir: {run_set_dir}")

    pipeline = MatterRAGPipeline(config)

    # ── Single-cluster or no-filter run (original behaviour) ────────────────
    if len(clusters) <= 1:
        cluster_filter = clusters[0] if clusters else ""
        return _run_one_cluster(
            pipeline=pipeline,
            config=config,
            pr_url=pr_url,
            input_doc=input_doc,
            pr_snippet=pr_snippet,
            cluster_filter=cluster_filter,
            build_tp=build_tp,
            build_kg=build_kg,
            build_dm=build_dm,
            build_kg_with_llm=args.build_kg_with_llm,
            output_dir=args.output or "",
            run_set_dir=run_set_dir,
            num_chunks=args.num_chunks,
            third_pass=args.third_pass_expand or "",
            negative_tests=args.negative_tests,
            include_coverage_gaps=not args.no_coverage_gaps,
            logger=logger,
        )

    # ── Multi-cluster run ────────────────────────────────────────────────────
    print(f"Running pipeline for {len(clusters)} cluster(s)…\n")
    exit_codes: List[int] = []
    for idx, cluster in enumerate(clusters, 1):
        print(f"─── Cluster {idx}/{len(clusters)}: {cluster} ───")
        code = _run_one_cluster(
            pipeline=pipeline,
            config=config,
            pr_url=pr_url,
            input_doc=input_doc,
            pr_snippet=pr_snippet,
            cluster_filter=cluster,
            build_tp=build_tp,
            build_kg=build_kg,
            build_dm=build_dm,
            build_kg_with_llm=args.build_kg_with_llm,
            output_dir=args.output or "",
            run_set_dir=run_set_dir,
            num_chunks=args.num_chunks,
            third_pass=args.third_pass_expand or "",
            negative_tests=args.negative_tests,
            include_coverage_gaps=not args.no_coverage_gaps,
            logger=logger,
        )
        exit_codes.append(code)

    errors = sum(1 for c in exit_codes if c != 0)
    print(f"\n{'=' * 60}")
    print(f"Multi-cluster run complete: {len(clusters)} cluster(s), {errors} error(s)")
    if run_set_dir:
        print(f"All reports in: {run_set_dir}")
    print(f"{'=' * 60}\n")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
