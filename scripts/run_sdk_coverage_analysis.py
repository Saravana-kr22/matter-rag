#!/usr/bin/env python3
"""CLI entry point — SDK coverage analysis pipeline.

Loads the cached knowledge graph, resolves spec REQUIREMENT nodes to their
corresponding SDK cluster implementation files (connectedhomeip src/app/clusters/),
and uses the LLM to assess which requirements are implemented, partial, or missing
in the SDK code. Generates an HTML + JSON report.

Usage::

    # Analyze all clusters (uses cached KG + SDK at config.analysis.sdk_dir)
    python scripts/run_sdk_coverage_analysis.py

    # Limit to a single cluster
    python scripts/run_sdk_coverage_analysis.py --cluster "On/Off"

    # Provide SDK root directly (overrides config)
    python scripts/run_sdk_coverage_analysis.py --sdk-dir /path/to/connectedhomeip

    # Cost control — stop after N LLM calls
    python scripts/run_sdk_coverage_analysis.py --max-llm-calls 10

    # Custom output directory
    python scripts/run_sdk_coverage_analysis.py --output reports/debug

    # Custom config file
    python scripts/run_sdk_coverage_analysis.py --config config/config.yaml

Log directory: logs/sdk_coverage_analysis_<YYYYMMDD_HHMMSS>/

Prerequisites:
    The knowledge graph must already be built:
        python scripts/run_ghpr_analysis.py --build-knowledge-graph
    connectedhomeip must be cloned locally and sdk_dir set in config or --sdk-dir flag.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Threading env vars BEFORE any native library import (faiss, numpy, OpenBLAS)
# ---------------------------------------------------------------------------
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.config_loader import load_config
from src.logging_config import configure_pipeline_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Matter RAG — SDK coverage analysis"
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config YAML (default: config/config.yaml)",
    )
    parser.add_argument(
        "--additional-config", metavar="FILE", default="",
        help="Overlay config YAML (deep-merged on top of base config).",
    )
    parser.add_argument(
        "--cluster", metavar="CLUSTER_NAME",
        help="Limit analysis to a single cluster (case-insensitive partial match).",
    )
    parser.add_argument(
        "--sdk-dir", metavar="DIR",
        help="Root of the connectedhomeip SDK repo (overrides config.analysis.sdk_dir).",
    )
    parser.add_argument(
        "--sdk-dirs-additional", metavar="DIR", nargs="+", default=[],
        help="Additional SDK code directories to search (flat structure with .cpp/.h files). "
             "Use for proprietary cluster implementations that live outside the main SDK tree.",
    )
    parser.add_argument(
        "--max-llm-calls", type=int, default=None,
        help="Maximum LLM calls to make (overrides config.analysis.max_llm_calls_per_run).",
    )
    parser.add_argument(
        "--output", metavar="DIR",
        help="Output directory for reports (overrides config.analysis.output_dir).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    parser.add_argument(
        "--log-level", default=None, metavar="LEVEL",
        help="Override log level: VERBOSE | DEBUG | INFO | WARNING | ERROR",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = load_config(args.config, additional_config=args.additional_config or None)

    if args.log_level:
        config.logging.level = args.log_level.upper()
    elif args.verbose:
        config.logging.level = "VERBOSE"

    if args.sdk_dir:
        config.analysis.sdk_dir = args.sdk_dir
    if args.sdk_dirs_additional:
        config.analysis.sdk_dirs_additional = list(args.sdk_dirs_additional)

    run_dir = configure_pipeline_logging(config, pipeline_name="sdk_coverage_analysis")

    from src.engine.run_context import RunContext, set_run_context, _current_run_ctx
    run_ctx = RunContext(
        run_id=run_dir.name,
        run_dir=run_dir,
        client="sdk_coverage_analysis",
    )
    run_token = set_run_context(run_ctx)

    logger = logging.getLogger(__name__)
    logger.info("Run logs: %s", run_dir)

    output_dir = args.output or config.analysis.output_dir
    max_llm_calls = args.max_llm_calls or config.analysis.max_llm_calls_per_run

    from src.engine.graphs.sdk_coverage_graph import build_sdk_coverage_graph
    from src.engine.sdk_coverage_nodes import SdkCoverageAnalysisState

    graph = build_sdk_coverage_graph()
    initial_state: SdkCoverageAnalysisState = {
        "config": config,
        "run_ctx": run_ctx,
        "run_dir": str(run_dir),
        "output_dir": output_dir,
        "cluster_filter": args.cluster or "",
        "max_llm_calls": max_llm_calls,
        "errors": [],
    }

    final_state: SdkCoverageAnalysisState = graph.invoke(initial_state)
    run_ctx.close()
    _current_run_ctx.reset(run_token)

    # Print summary
    errors = final_state.get("errors", [])
    total_not_implemented = final_state.get("total_not_implemented", 0)
    total_partial = final_state.get("total_partial", 0)
    clusters_analyzed = len(final_state.get("cluster_findings", []))
    report_path = final_state.get("report_path", "")

    print("\n" + "=" * 60)
    print("Matter RAG — SDK Coverage Analysis")
    print("=" * 60)
    print(f"Run log dir:         {run_dir}")
    if report_path:
        print(f"Report:              {report_path}")
    print(f"Clusters analyzed:   {clusters_analyzed}")
    print(f"Not implemented:     {total_not_implemented}")
    print(f"Partial:             {total_partial}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
    print("=" * 60 + "\n")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
