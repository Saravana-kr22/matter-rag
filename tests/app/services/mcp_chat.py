"""Agentic (MCP-style) chat service for the Matter RAG debug app.

Replaces the two-step chat path (LLM query-planner → fixed KG dispatch →
second LLM call) with a single tool-use LLM loop in which the model itself
decides which KG / vector-DB tools to call and in what order.

Architecture
------------
::

    POST /api/chat
        → mcp_chat.run_mcp_chat(payload, app_state, run_ctx)
            → ClaudeProvider.complete_with_tools(
                  messages, system, tools=KG_TOOLS,
                  tool_executor=partial(execute_kg_tool, kg, vs, emb, cfg),
              )
              # Loop until stop_reason != "tool_use":
              #   model decides which tools to call
              #   execute_kg_tool() runs the KG/FAISS methods
              #   results fed back; model continues
            → returns final text reply

This path is used when the LLM provider is ``claude_cli``
(``ClaudeProvider``), which is the only provider that supports the Anthropic
tool-use API.  All other providers (``claude_subprocess``, ``local``,
``lm_studio``) fall back to the original two-step pipeline in
``pipeline_adapter.py``.

Benefits over the old two-step approach
----------------------------------------
* Multi-intent:  "list TCs for On/Off AND find coverage gaps for Toggle"
* Multi-hop:     model can call search_vector_db then correlate with KG results
* No planner LLM call:  the model is the planner — one turn instead of two
* Extensible:    add new tools without touching pipeline routing logic
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from functools import partial
from typing import Any, Dict, List, Optional

from tests.app.services.kg_tools import KG_TOOLS, execute_kg_tool
from tests.app.services.session_store import MATTER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# System prompt fragment that explains the available tools to the LLM.
_TOOL_USE_SYSTEM_ADDON = """
You have access to the following tools to query the Matter test-plan knowledge base:
- get_test_cases_for_cluster: list all TCs for a cluster (use for coverage counts / listings)
- find_entity_coverage: check if a specific attribute/command/event/feature has TC coverage
- find_requirements_and_coverage: find spec requirements and identify coverage gaps
- get_cluster_dependencies: find clusters that depend on or are depended on by a cluster
- search_kg_by_keywords: broad keyword search across the knowledge graph
- search_vector_db: semantic FAISS search for test procedures and step-level details

Strategy:
1. For count/listing questions → get_test_cases_for_cluster
2. For entity-level coverage → find_entity_coverage
3. For spec compliance / gaps → find_requirements_and_coverage
4. For cluster relationships → get_cluster_dependencies
5. For broad or ambiguous questions → search_kg_by_keywords + search_vector_db
6. Call multiple tools when the question spans more than one topic.
Always cite tool results directly (TC IDs, requirement text, etc.).
"""


def supports_tool_use(llm) -> bool:
    """Return True if the underlying provider has a native ``complete_with_tools`` method.

    Unwraps ``LoggingLLMProvider`` so the check reflects the inner provider,
    not the logging proxy (which forwards the call and would raise on unsupported providers).
    """
    from src.llm.llm_provider import LoggingLLMProvider
    inner = llm._provider if isinstance(llm, LoggingLLMProvider) else llm
    return callable(getattr(inner, "complete_with_tools", None))


async def run_mcp_chat(
    user_message: str,
    system_prompt: str,
    chat_history: List[Dict[str, str]],
    app_state,
    run_ctx,
) -> tuple[str, str]:
    """Run the agentic tool-use chat and return ``(reply, rag_context)``.

    Args:
        user_message:  Current user message string.
        system_prompt: Session system prompt (from session_store).
        chat_history:  Prior conversation turns (may be empty for new sessions).
        app_state:     ``_AppState`` singleton from ``tests.app.main``.
        run_ctx:       ``RunContext`` for this request.

    Returns:
        ``(reply, rag_context)`` — both strings.  ``rag_context`` is empty
        (tool call details are logged to ``run_ctx`` instead).
    """
    from src.llm.llm_provider import get_llm

    config = app_state.config
    kg     = getattr(app_state, "kg", None)

    llm = _get_chat_llm(config, run_ctx)
    if not supports_tool_use(llm):
        raise NotImplementedError(
            "Active LLM provider does not support tool use. "
            "Set provider: claude_cli in config.yaml to enable agentic chat."
        )

    # Build Anthropic messages list from history + current message.
    messages = _build_messages(chat_history, user_message)

    # Build combined system prompt.
    combined_system = (system_prompt or MATTER_SYSTEM_PROMPT) + _TOOL_USE_SYSTEM_ADDON

    # Bind the app_state objects into the tool executor.
    def _executor(tool_name: str, tool_input: dict) -> str:
        result = execute_kg_tool(
            kg=kg,
            vector_store=getattr(app_state, "vector_store", None),
            embedder=getattr(app_state, "embedder", None),
            config=config,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        # Log to run_ctx rag_queries for observability.
        if run_ctx is not None and hasattr(run_ctx, "log_rag_query"):
            try:
                run_ctx.log_rag_query(
                    query=f"[tool:{tool_name}] {tool_input}",
                    results=[result[:500]],
                )
            except Exception:
                pass
        return result

    logger.info(
        "[mcp_chat] session=%s tool_use_loop starts history_turns=%d",
        getattr(run_ctx, "run_id", "?"), len(chat_history),
    )

    # Run the tool-use loop in a thread executor (llm call is synchronous).
    loop = asyncio.get_event_loop()
    ctx_copy = contextvars.copy_context()
    reply = await loop.run_in_executor(
        None,
        lambda: ctx_copy.run(
            llm.complete_with_tools,
            messages,
            combined_system,
            KG_TOOLS,
            _executor,
        ),
    )

    logger.info(
        "[mcp_chat] session=%s reply_len=%d",
        getattr(run_ctx, "run_id", "?"), len(reply),
    )

    return reply, ""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_chat_llm(config, run_ctx):
    """Return an LLM instance with per-run call logging."""
    from pathlib import Path
    import copy
    from src.llm.llm_provider import get_llm

    llm_cfg = copy.copy(config.llm)
    run_dir = getattr(run_ctx, "run_dir", "") if run_ctx else ""
    if run_dir and llm_cfg.call_log_path:
        llm_cfg.call_log_path = str(Path(run_dir) / "llm_calls.jsonl")
    return get_llm(llm_cfg)


def _build_messages(history: List[Dict[str, str]], user_message: str) -> List[Dict]:
    """Convert session history + current user message to Anthropic messages format.

    Anthropic requires alternating user/assistant turns.  We normalise the
    history to enforce this (merge consecutive same-role messages with newline).
    """
    msgs: List[Dict] = []
    for turn in history:
        role    = turn.get("role", "user")
        content = turn.get("content", "")
        if role not in ("user", "assistant"):
            role = "user"
        # Merge consecutive same-role turns.
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})

    # Append the current user message.
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n\n" + user_message
    else:
        msgs.append({"role": "user", "content": user_message})

    return msgs
