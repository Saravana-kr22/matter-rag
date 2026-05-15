"""CLI pipeline graph -- full 18-node Matter RAG pipeline.

Used by ``scripts/run_pipeline.py`` via::

    from src.engine.graphs.cli_graph import build_cli_graph
    from src.engine.pipeline import PipelineRunner

    runner = PipelineRunner(build_cli_graph(), run_ctx)
    result = runner.run(initial_state)

Node sequence
-------------
::

    fetch_documents
        -> process_documents
            -> ingest_data_model
                -> build_matter_schema
                    -> chunk_embed_test_plans
                        -> chunk_pr
                            -> extract_pr_changes
                                -> build_knowledge_graph
                                    |
                                    +- [no pr_chunks] -> cleanup -> END
                                    |
                                    +- [pr_chunks] -> search_test_plan_vector_db
                                        -> search_knowledge_graph
                                            -> analyze_chunks_with_llm  (pass 1)
                                                -> cluster_review        (pass 2)
                                                    -> second_pass_tc_gen  (pass 3: holistic KG)
                                                        -> human_outline_expand (pass 4: human outline)
                                                            -> write_adoc_updates
                                                                -> write_updated_testplan
                                                                    -> generate_report
                                                                        -> cleanup -> END

Pass ordering
-------------
``analyze_chunks_with_llm`` (pass 1) runs one LLM call per PR chunk independently.
``cluster_review`` (pass 2) then audits the full per-cluster picture and adds any
symmetry gaps or missing test types.
``second_pass_tc_gen`` (pass 3) runs a holistic KG-driven outline + expand loop for
clusters that are thin (< 5 existing TCs) or have many gaps (> 5 missing from pass 1).
``human_outline_expand`` (pass 4) re-expands a human-modified outline JSON if
``state["third_pass_outline_path"]`` is set; otherwise it is a transparent no-op.
The adoc writers run after all passes so ``.adoc`` files reflect the final TC list.

Routing decision
----------------
After ``build_knowledge_graph``, ``_route_after_kg`` checks whether
``state["pr_chunks"]`` is non-empty:

* **search** -- PR chunks present; continue to retrieval + analysis.
* **report** -- PR/input-doc was provided but produced no chunks
               (e.g. cluster filter matched nothing); skip search/analyze
               and jump straight to generate_report with an explanation.
* **end**    -- Pure build-only / index-only run (no PR given at all).

``cleanup`` is always the final node on both paths.  It releases GPU/MPS
memory and logs a one-line run summary.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph  # type: ignore

from src.engine.nodes import (
    PipelineState,
    analyze_chunks_with_llm_node,
    build_knowledge_graph_node,
    build_matter_schema_node,
    chunk_embed_test_plans_node,
    chunk_pr_node,
    cleanup_node,
    cluster_review_node,
    extract_pr_changes_node,
    fetch_documents_node,
    generate_report_node,
    ingest_data_model_node,
    process_documents_node,
    search_knowledge_graph_node,
    search_test_plan_vector_db_node,
    second_pass_tc_gen_node,
    human_outline_expand_node,
    write_adoc_updates_node,
    write_updated_testplan_node,
)

logger = logging.getLogger(__name__)

# Type alias for a compiled LangGraph graph (avoids importing internals).
CompiledGraph = object


def build_cli_graph() -> CompiledGraph:
    """Build and compile the full 18-node CLI pipeline graph.

    Returns a compiled ``StateGraph`` ready to be passed to ``PipelineRunner``.
    Call this once per CLI invocation -- compilation is cheap but not free.
    """
    graph = StateGraph(PipelineState)

    # ---- Register all nodes ----
    graph.add_node("fetch_documents",            fetch_documents_node)
    graph.add_node("process_documents",          process_documents_node)
    graph.add_node("ingest_data_model",          ingest_data_model_node)
    graph.add_node("build_matter_schema",        build_matter_schema_node)
    graph.add_node("chunk_embed_test_plans",     chunk_embed_test_plans_node)
    graph.add_node("chunk_pr",                   chunk_pr_node)
    graph.add_node("extract_pr_changes",         extract_pr_changes_node)
    graph.add_node("build_knowledge_graph",      build_knowledge_graph_node)
    graph.add_node("search_test_plan_vector_db", search_test_plan_vector_db_node)
    graph.add_node("search_knowledge_graph",     search_knowledge_graph_node)
    graph.add_node("analyze_chunks_with_llm",    analyze_chunks_with_llm_node)
    graph.add_node("cluster_review",             cluster_review_node)
    graph.add_node("second_pass_tc_gen",         second_pass_tc_gen_node)
    graph.add_node("human_outline_expand",          human_outline_expand_node)
    graph.add_node("write_adoc_updates",         write_adoc_updates_node)
    graph.add_node("write_updated_testplan",     write_updated_testplan_node)
    graph.add_node("generate_report",            generate_report_node)
    graph.add_node("cleanup",                    cleanup_node)

    # ---- Entry point ----
    graph.set_entry_point("fetch_documents")

    # ---- Fixed edges (always run in this order) ----
    graph.add_edge("fetch_documents",            "process_documents")
    graph.add_edge("process_documents",          "ingest_data_model")
    graph.add_edge("ingest_data_model",          "build_matter_schema")
    graph.add_edge("build_matter_schema",        "chunk_embed_test_plans")
    graph.add_edge("chunk_embed_test_plans",     "chunk_pr")
    graph.add_edge("chunk_pr",                   "extract_pr_changes")
    graph.add_edge("extract_pr_changes",         "build_knowledge_graph")

    # ---- Conditional edge after KG build ----
    graph.add_conditional_edges(
        "build_knowledge_graph",
        _route_after_kg,
        {
            "search": "search_test_plan_vector_db",
            "report": "generate_report",
            "end":    "cleanup",
        },
    )

    # ---- Analysis path ----
    # Pass 1: per-chunk LLM analysis
    graph.add_edge("search_test_plan_vector_db", "search_knowledge_graph")
    graph.add_edge("search_knowledge_graph",     "analyze_chunks_with_llm")
    # Pass 2: cluster-level review
    graph.add_edge("analyze_chunks_with_llm",    "cluster_review")
    # Pass 3: holistic TC generation from KG (auto-triggered for thin/gap-heavy clusters)
    graph.add_edge("cluster_review",             "second_pass_tc_gen")
    # Pass 4: human-modified outline re-expansion (no-op when third_pass_outline_path not set)
    graph.add_edge("second_pass_tc_gen",         "human_outline_expand")
    # Adoc writers run after all passes -- use the final complete TC list
    graph.add_edge("human_outline_expand",          "write_adoc_updates")
    graph.add_edge("write_adoc_updates",         "write_updated_testplan")
    graph.add_edge("write_updated_testplan",     "generate_report")
    graph.add_edge("generate_report",            "cleanup")

    # ---- Terminal edge (both paths converge here) ----
    graph.add_edge("cleanup", END)

    return graph.compile()


def _route_after_kg(state: PipelineState) -> str:
    """Routing function called after ``build_knowledge_graph``.

    Returns:
      ``"search"``  -- PR chunks present; run full analysis pipeline.
      ``"report"``  -- PR/input-doc was provided but produced no chunks
                      (e.g. cluster filter matched nothing); skip search/analyze
                      and jump straight to generate_report with an explanation.
      ``"end"``     -- Pure build-only / index-only run (no PR given at all).
    """
    if state.get("pr_chunks"):
        return "search"

    if state.get("pr_url") or state.get("input_doc"):
        logger.info(
            "[cli_graph] PR/input-doc provided but produced no chunks "
            "(cluster filter may have excluded all content) -- generating empty report."
        )
        return "report"

    logger.info(
        "[cli_graph] No PR chunks in state -- stopping after KG build "
        "(index-only / build-only run)."
    )
    return "end"
