#!/usr/bin/env python3
"""CLI entry point — PICS code validation analysis pipeline.

Loads the cached knowledge graph and DM XML schema, then sends each cluster's
test cases to the LLM to identify PICS code issues (wrong side, non-existent IDs,
missing feature/protocol PICS, step mismatches).

Usage::

    # Analyze all clusters (uses cached KG from data/knowledge_graph/matter_kg.json)
    python scripts/run_pics_analysis.py

    # Limit to a single cluster
    python scripts/run_pics_analysis.py --cluster "On/Off"

    # Cost control — stop after N LLM calls
    python scripts/run_pics_analysis.py --max-llm-calls 10

    # Custom output directory
    python scripts/run_pics_analysis.py --output reports/debug

    # Custom config file
    python scripts/run_pics_analysis.py --config config/config.yaml

Log directory: logs/pics_analysis_<MMDDYYYY_HHMMSS>/

Prerequisites:
    The knowledge graph must already be built:
        python scripts/run_ghpr_analysis.py --build-knowledge-graph
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
        description="Matter RAG — PICS code validation analysis"
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config YAML (default: config/config.yaml)",
    )
    parser.add_argument(
        "--cluster", metavar="CLUSTER_NAME",
        help="Limit analysis to a single cluster (case-insensitive partial match).",
    )
    parser.add_argument(
        "--max-llm-calls", type=int, default=None,
        help="Maximum LLM calls to make (overrides config.analysis.max_llm_calls_per_run).",
    )
    parser.add_argument(
        "--dm-dir", metavar="DIR",
        help="Directory containing Matter DM XML files (overrides config.analysis.dm_dir).",
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

    config = load_config(args.config)

    if args.log_level:
        config.logging.level = args.log_level.upper()
    elif args.verbose:
        config.logging.level = "VERBOSE"

    if args.dm_dir:
        config.analysis.dm_dir = args.dm_dir

    run_dir = configure_pipeline_logging(config, pipeline_name="pics_analysis")

    from src.engine.run_context import RunContext, set_run_context, _current_run_ctx
    run_ctx = RunContext(
        run_id=run_dir.name,
        run_dir=run_dir,
        client="pics_analysis",
    )
    run_token = set_run_context(run_ctx)

    logger = logging.getLogger(__name__)
    logger.info("Run logs: %s", run_dir)

    output_dir = args.output or config.analysis.output_dir
    max_llm_calls = args.max_llm_calls or config.analysis.max_llm_calls_per_run

    from src.engine.graphs.pics_analysis_graph import build_pics_analysis_graph
    from src.engine.pics_analysis_nodes import PicsAnalysisState

    graph = build_pics_analysis_graph()
    initial_state: PicsAnalysisState = {
        "config": config,
        "run_ctx": run_ctx,
        "run_dir": str(run_dir),
        "output_dir": output_dir,
        "cluster_filter": args.cluster or "",
        "max_llm_calls": max_llm_calls,
        "errors": [],
    }

    final_state: PicsAnalysisState = graph.invoke(initial_state)
    run_ctx.close()
    _current_run_ctx.reset(run_token)

    # Print summary
    errors = final_state.get("errors", [])
    total_issues = final_state.get("total_issues", 0)
    clusters_analyzed = len(final_state.get("cluster_findings", []))
    report_path = final_state.get("report_path", "")

    print("\n" + "=" * 60)
    print("Matter RAG — PICS Validation Analysis")
    print("=" * 60)
    print(f"Run log dir:        {run_dir}")
    if report_path:
        print(f"Report:             {report_path}")
    print(f"Clusters analyzed:  {clusters_analyzed}")
    print(f"Total PICS issues:  {total_issues}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
    print("=" * 60 + "\n")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
