"""SDK coverage analysis LangGraph — 6-node pipeline.

Node sequence::

    load_sdk_stores
        → resolve_sdk_files
            → build_requirements_map
                → run_llm_sdk_analysis
                    → aggregate_sdk_findings
                        → generate_sdk_report → END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph  # type: ignore

from src.engine.sdk_coverage_nodes import (
    SdkCoverageAnalysisState,
    aggregate_sdk_findings_node,
    build_requirements_map_node,
    generate_sdk_report_node,
    load_sdk_stores_node,
    resolve_sdk_files_node,
    run_llm_sdk_analysis_node,
)

logger = logging.getLogger(__name__)

CompiledGraph = object


def build_sdk_coverage_graph() -> CompiledGraph:
    """Build and compile the 6-node SDK coverage analysis graph."""
    graph = StateGraph(SdkCoverageAnalysisState)

    graph.add_node("load_sdk_stores",        load_sdk_stores_node)
    graph.add_node("resolve_sdk_files",      resolve_sdk_files_node)
    graph.add_node("build_requirements_map", build_requirements_map_node)
    graph.add_node("run_llm_sdk_analysis",   run_llm_sdk_analysis_node)
    graph.add_node("aggregate_sdk_findings", aggregate_sdk_findings_node)
    graph.add_node("generate_sdk_report",    generate_sdk_report_node)

    graph.set_entry_point("load_sdk_stores")

    graph.add_edge("load_sdk_stores",        "resolve_sdk_files")
    graph.add_edge("resolve_sdk_files",      "build_requirements_map")
    graph.add_edge("build_requirements_map", "run_llm_sdk_analysis")
    graph.add_edge("run_llm_sdk_analysis",   "aggregate_sdk_findings")
    graph.add_edge("aggregate_sdk_findings", "generate_sdk_report")
    graph.add_edge("generate_sdk_report",    END)

    return graph.compile()
