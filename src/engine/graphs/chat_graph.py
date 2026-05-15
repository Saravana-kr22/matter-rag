"""FastAPI chat pipeline graph — lightweight search-and-answer graph.

Used by ``tests/app/services/pipeline_adapter.py`` via::

    from src.engine.graphs.chat_graph import build_chat_graph
    from src.engine.pipeline import PipelineRunner

    runner = PipelineRunner(build_chat_graph(), run_ctx)
    result = runner.run(initial_state)

Design intent
-------------
The chat client does **not** fetch or build anything.  The FAISS vector store
and knowledge graph are loaded once at FastAPI startup and passed directly in
``initial_state``.  Each chat request only runs the retrieval and analysis
steps.

Node sequence
-------------
::

    search_test_plan_vector_db
        → search_knowledge_graph
            → analyze_with_llm → END

``analyze_chunks_with_llm_node`` branches on ``state["run_ctx"].client``:

* ``"app_chat"``              → returns ``{"llm_reply": <str>}``  (used by the session)
* anything else (CLI default) → returns ``{"analysis_results": [...], "missing_tests": [...], ...}``

Required initial_state keys
---------------------------
``config``, ``run_ctx``, ``vector_store``, ``knowledge_graph``, ``pr_chunks``
(the user query is wrapped in a synthetic PR chunk by ``pipeline_adapter.py``).

Extending this graph
--------------------
If a future chat feature needs pre-processing (e.g. query rewriting, HyDE),
add a node here before ``search_test_plan_vector_db`` — no other file changes
needed.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph  # type: ignore

from src.engine.nodes import (
    PipelineState,
    analyze_chunks_with_llm_node,
    search_knowledge_graph_node,
    search_test_plan_vector_db_node,
)

logger = logging.getLogger(__name__)

# Type alias — avoids importing LangGraph internals into callers.
CompiledGraph = object


def build_chat_graph() -> CompiledGraph:
    """Build and compile the 3-node chat search-and-answer graph.

    Returns a compiled ``StateGraph`` ready to be passed to ``PipelineRunner``.

    This graph is compiled once at module import time (``_CHAT_GRAPH``) and
    reused for every chat request — compilation is idempotent and the compiled
    graph is stateless.
    """
    graph = StateGraph(PipelineState)

    # ---- Nodes ----
    # Only the retrieval and analysis stages.  Fetch / build stages are skipped
    # because the stores are already loaded at app startup.
    graph.add_node("search_test_plan_vector_db", search_test_plan_vector_db_node)
    graph.add_node("search_knowledge_graph",     search_knowledge_graph_node)
    graph.add_node("analyze_chunks_with_llm",           analyze_chunks_with_llm_node)

    # ---- Edges ----
    graph.set_entry_point("search_test_plan_vector_db")
    graph.add_edge("search_test_plan_vector_db", "search_knowledge_graph")
    graph.add_edge("search_knowledge_graph",     "analyze_chunks_with_llm")
    graph.add_edge("analyze_chunks_with_llm",           END)

    return graph.compile()


# Compile once — reused across all concurrent chat requests.
# Each request passes its own PipelineState so there is no shared mutable state.
_CHAT_GRAPH: CompiledGraph = build_chat_graph()


def get_chat_graph() -> CompiledGraph:
    """Return the singleton compiled chat graph.

    Use this instead of ``build_chat_graph()`` inside request handlers to avoid
    recompiling the graph on every request.
    """
    return _CHAT_GRAPH
