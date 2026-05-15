"""PICS validation analysis LangGraph — 6-node pipeline.

Node sequence::

    load_pics_stores
        → build_pics_map
            → prepare_cluster_batches
                → run_llm_pics_analysis
                    → aggregate_pics_findings
                        → generate_pics_report → END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph  # type: ignore

from src.engine.pics_analysis_nodes import (
    PicsAnalysisState,
    aggregate_pics_findings_node,
    build_pics_map_node,
    generate_pics_report_node,
    load_pics_stores_node,
    prepare_cluster_batches_node,
    run_llm_pics_analysis_node,
)

logger = logging.getLogger(__name__)

CompiledGraph = object


def build_pics_analysis_graph() -> CompiledGraph:
    """Build and compile the 6-node PICS validation analysis graph."""
    graph = StateGraph(PicsAnalysisState)

    graph.add_node("load_pics_stores",        load_pics_stores_node)
    graph.add_node("build_pics_map",          build_pics_map_node)
    graph.add_node("prepare_cluster_batches", prepare_cluster_batches_node)
    graph.add_node("run_llm_pics_analysis",   run_llm_pics_analysis_node)
    graph.add_node("aggregate_pics_findings", aggregate_pics_findings_node)
    graph.add_node("generate_pics_report",    generate_pics_report_node)

    graph.set_entry_point("load_pics_stores")

    graph.add_edge("load_pics_stores",        "build_pics_map")
    graph.add_edge("build_pics_map",          "prepare_cluster_batches")
    graph.add_edge("prepare_cluster_batches", "run_llm_pics_analysis")
    graph.add_edge("run_llm_pics_analysis",   "aggregate_pics_findings")
    graph.add_edge("aggregate_pics_findings", "generate_pics_report")
    graph.add_edge("generate_pics_report",    END)

    return graph.compile()
