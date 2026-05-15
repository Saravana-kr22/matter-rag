"""Pipeline adapter — bridges the FastAPI chat endpoint with the RAG pipeline.

Architecture
------------
This adapter is the **chat client's entrypoint** into the node library.  It:

1. Wraps the user message + loaded stores into a ``PipelineState``.
2. Runs the compiled ``chat_graph`` (3 nodes: search_vector_db → search_kg →
   analyze_with_llm) via ``PipelineRunner``.
3. Extracts the LLM reply and RAG context from the final state.
4. Logs a structured ``rag_queries.jsonl`` record per query (via ``RunContext``).

The ``run_ctx`` is created by ``chat.py`` (one per ``POST /api/chat`` request)
and injected here.  ``PipelineRunner`` injects it into ``PipelineState`` before
invoking the graph so nodes can read ``state["run_ctx"].client``.

Why chat uses a different graph than the CLI
--------------------------------------------
The CLI graph (``cli_graph.py``) fetches documents, builds embeddings, and
constructs the KG from scratch on every run.  The chat client reuses stores
that are loaded once at app startup — so it only needs the retrieval and
analysis nodes.  See ``src/engine/ARCHITECTURE.md``.

Extending this adapter
----------------------
* To add query rewriting or HyDE before retrieval: add a pre-processing node
  to ``chat_graph.py`` — no changes needed here.
* To return streaming output: use ``graph.astream()`` instead of
  ``runner.run()`` and yield tokens back to the caller.
* To add an MCP orchestrator layer: replace ``PipelineRunner`` with an async
  MCP dispatch call that wraps the same ``PipelineState``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from tests.app.services.history_builder import (
    build_prompt_with_history,
    build_relevant_history,
)
from tests.app.services.session_store import MATTER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Maximum number of prior turns included in the LLM prompt.
_HISTORY_MAX = 10

# Default similarity threshold for vector search in chat mode.
# Chat queries are typically shorter than CLI PR-diff chunks, so we use a
# lower threshold to avoid returning zero vector hits for conversational
# queries.  The CLI graph uses config.pipeline.similarity_threshold (0.65).
_VECTOR_THRESHOLD = 0.45


# ---------------------------------------------------------------------------
# ChatPayload — structured input for the adapter
# ---------------------------------------------------------------------------

class ChatPayload:
    """Carries a single user turn plus its session context.

    Constructed by ``chat.py`` from the ``ChatRequest`` Pydantic model and
    passed to ``run_pipeline()``.
    """

    def __init__(
        self,
        session_id: str,
        user_message: str,
        system_prompt: str = MATTER_SYSTEM_PROMPT,
        chat_history: Optional[List[Dict[str, str]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.session_id = session_id
        self.user_message = user_message
        self.system_prompt = system_prompt
        self.chat_history = chat_history or []
        self.metadata = metadata or {}


# ---------------------------------------------------------------------------
# Initial state builder
# ---------------------------------------------------------------------------

def _build_initial_state(payload: ChatPayload, app_state) -> dict:
    """Assemble the ``PipelineState`` that the chat graph receives.

    The chat graph starts at ``search_test_plan_vector_db_node``, which reads
    ``vector_store``, ``embedder``, and ``pr_chunks`` from state.  We wrap the
    user message in a minimal synthetic PR chunk so the existing search node
    works without modification.

    ``run_ctx`` and ``run_dir`` are injected by ``PipelineRunner`` — do not set
    them here.
    """
    from src.loader.base_loader import Document

    # Chat queries are shorter than CLI PR-diff chunks, so cap the threshold
    # at _VECTOR_THRESHOLD (0.45) even if config.pipeline.similarity_threshold
    # is higher (0.65).  This prevents short conversational queries from
    # returning zero vector hits.
    threshold = _VECTOR_THRESHOLD
    if app_state.config is not None:
        cfg = getattr(getattr(app_state.config, "pipeline", None), "similarity_threshold", None)
        if cfg is not None:
            threshold = min(_VECTOR_THRESHOLD, float(cfg))

    # Build relevant history and a full prompt string for the LLM.
    history = build_relevant_history(payload.chat_history, max_messages=_HISTORY_MAX)
    prompt = build_prompt_with_history(history, payload.user_message, rag_context="")

    # Wrap the user query as a synthetic "PR chunk" so search nodes can iterate
    # over ``state["pr_chunks"]`` the same way they do in the CLI graph.
    synthetic_chunk = Document(
        page_content=payload.user_message,
        metadata={
            "doc_id":    f"chat:{payload.session_id}",
            "doc_type":  "chat_query",
            "source_id": "chat",
        },
    )

    return {
        "config":         app_state.config,
        "vector_store":   app_state.vector_store,
        "knowledge_graph": app_state.kg,
        "pr_chunks":      [synthetic_chunk],
        # Pass history + system prompt through state so analyze_with_llm_node
        # can build the final LLM prompt for the chat path.
        "chat_history":   history,
        "chat_prompt":    prompt,
        "system_prompt":  payload.system_prompt,
        # search config
        "similarity_threshold": threshold,
        "errors":         [],
        "fatal_error":    False,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_pipeline(
    payload: ChatPayload,
    app_state,
    run_ctx=None,
) -> tuple[str, str]:
    """Run the chat pipeline and return ``(reply, rag_context)``.

    Executes the compiled ``chat_graph`` (search_vector_db → search_kg →
    analyze_with_llm) via ``PipelineRunner``.

    Args:
        payload:   Structured chat payload built by ``chat.py``.
        app_state: The ``_AppState`` singleton from ``tests.app.main``.
        run_ctx:   ``RunContext`` for this request, created by ``chat.py``.
                   When ``None`` (e.g. in tests), a minimal context is created.

    Returns:
        ``(reply, rag_context)`` — both strings.
        ``rag_context`` is empty when no matching chunks were found.
    """
    if app_state.config is None:
        return "Pipeline is not configured — please check /health.", ""

    logger.info(
        "[chat] session=%s  history_turns=%d  msg_len=%d",
        payload.session_id, len(payload.chat_history), len(payload.user_message),
    )

    # Obtain or create the run context.
    if run_ctx is None:
        # Fallback for tests / direct calls without a request context.
        from src.engine.run_context import create_run_context
        run_ctx = create_run_context("app_chat")
        logger.debug("[chat] created fallback run_ctx  run_id=%s", run_ctx.run_id)

    # Build the initial state for the chat graph.
    initial_state = _build_initial_state(payload, app_state)

    # Run the chat graph synchronously (LangGraph invoke is sync; the async
    # boundary is at the FastAPI handler level).
    from src.engine.graphs.chat_graph import get_chat_graph
    from src.engine.pipeline import PipelineRunner

    runner = PipelineRunner(get_chat_graph(), run_ctx)

    # run_in_executor keeps the event loop unblocked while the graph runs.
    loop = asyncio.get_event_loop()
    import contextvars
    ctx_copy = contextvars.copy_context()
    result = await loop.run_in_executor(
        None,
        lambda: ctx_copy.run(runner.run, initial_state),
    )

    reply = result.llm_reply or ""
    rag_ctx = _format_rag_context(result)

    logger.info(
        "[chat] session=%s  run_id=%s  reply_len=%d  rag_snippets=%d",
        payload.session_id, run_ctx.run_id, len(reply),
        rag_ctx.count("\n\n") + 1 if rag_ctx else 0,
    )

    return reply, rag_ctx


def _format_rag_context(result) -> str:
    """Extract a human-readable RAG context string from a ``PipelineResult``.

    The search nodes store their hits in ``state["search_results"]`` (vector)
    and ``state["graph_results"]`` (KG).  ``PipelineResult.from_state`` does
    not currently surface these directly, so we read them from the result's
    internal state if available; otherwise return empty string.
    """
    # analyze_with_llm_node is expected to populate result.llm_reply for the
    # chat path.  RAG context snippets are logged to rag_queries.jsonl by
    # the search nodes via run_ctx.log_rag_query(); no need to re-format here
    # unless the caller requested include_context=True.
    # Return empty string for now; chat.py passes it as-is to the response.
    return ""
