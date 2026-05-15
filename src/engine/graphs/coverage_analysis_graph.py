"""Coverage gap analysis LangGraph — 5-node pipeline.

Node sequence::

    load_coverage_stores
        → build_cluster_coverage_map
            → run_llm_coverage_analysis
                → aggregate_coverage_findings
                    → generate_coverage_report → END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph  # type: ignore

from src.engine.coverage_analysis_nodes import (
    CoverageAnalysisState,
    aggregate_coverage_findings_node,
    build_cluster_coverage_map_node,
    generate_coverage_report_node,
    load_coverage_stores_node,
    run_llm_coverage_analysis_node,
)

logger = logging.getLogger(__name__)

CompiledGraph = object


def build_coverage_analysis_graph() -> CompiledGraph:
    """Build and compile the 5-node coverage gap analysis graph."""
    graph = StateGraph(CoverageAnalysisState)

    graph.add_node("load_coverage_stores",        load_coverage_stores_node)
    graph.add_node("build_cluster_coverage_map",  build_cluster_coverage_map_node)
    graph.add_node("run_llm_coverage_analysis",   run_llm_coverage_analysis_node)
    graph.add_node("aggregate_coverage_findings", aggregate_coverage_findings_node)
    graph.add_node("generate_coverage_report",    generate_coverage_report_node)

    graph.set_entry_point("load_coverage_stores")

    graph.add_edge("load_coverage_stores",        "build_cluster_coverage_map")
    graph.add_edge("build_cluster_coverage_map",  "run_llm_coverage_analysis")
    graph.add_edge("run_llm_coverage_analysis",   "aggregate_coverage_findings")
    graph.add_edge("aggregate_coverage_findings", "generate_coverage_report")
    graph.add_edge("generate_coverage_report",    END)

    return graph.compile()
