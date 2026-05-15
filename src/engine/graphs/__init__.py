"""Client-specific LangGraph graph definitions.

Each module in this package builds and returns a compiled ``StateGraph`` for a
specific client.  Clients import the builder function and pass the result to
``PipelineRunner``.

Available graphs
----------------
``cli_graph.build_cli_graph()``
    14-node full pipeline used by ``scripts/run_pipeline.py``.
    Covers: fetch → process → schema → embed → chunk → extract → KG →
            search → analyze → report → cleanup.

``chat_graph.build_chat_graph()``
    3-node search-and-answer pipeline used by the FastAPI chat endpoint.
    Assumes the vector store and knowledge graph are already loaded into
    ``initial_state`` — does not fetch or rebuild anything.
    Covers: search_vector_db → search_kg → analyze_with_llm.

Adding a new graph
------------------
1. Create ``src/engine/graphs/<client>_graph.py``.
2. Import the nodes you need from ``src.engine.nodes``.
3. Define ``def build_<client>_graph() -> CompiledGraph``.
4. Wire edges, set entry point, call ``graph.compile()`` and return it.
5. Use ``PipelineRunner(build_<client>_graph(), run_ctx)`` in your entrypoint.

See ``src/engine/ARCHITECTURE.md`` for the full design rationale.
"""

from src.engine.graphs.cli_graph import build_cli_graph
from src.engine.graphs.chat_graph import build_chat_graph

__all__ = ["build_cli_graph", "build_chat_graph"]
