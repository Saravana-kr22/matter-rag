"""Build a trimmed conversation history for LLM context.

Keeps the last ``max_messages`` turns from the session (most recent wins
when trimming).  The system prompt is always sent separately — never
included in the returned list.
"""
from __future__ import annotations

from typing import Dict, List


def build_relevant_history(
    messages: List[Dict[str, str]],
    max_messages: int = 10,
) -> List[Dict[str, str]]:
    """Return the last ``max_messages`` turns as ``[{"role": ..., "content": ...}, ...]``.

    Args:
        messages:     Full session message list (user + assistant turns).
        max_messages: Maximum number of messages to include.  A message is
                      one ``{"role": ..., "content": ...}`` dict.

    Returns:
        Trimmed copy suitable for passing directly to the LLM ``complete()``
        call as multi-turn context.
    """
    if not messages:
        return []
    return list(messages[-max_messages:])


def build_prompt_with_history(
    history: List[Dict[str, str]],
    user_message: str,
    rag_context: str = "",
) -> str:
    """Assemble a single string prompt that includes history + optional RAG context.

    The format mirrors a typical Claude multi-turn prompt:

        Human: <prior_user_turn>
        Assistant: <prior_assistant_turn>
        ...
        Human: <rag_preamble>\n<user_message>

    Args:
        history:      Prior turns (from ``build_relevant_history``), NOT
                      including the current user message.
        user_message: The current user message.
        rag_context:  Optional text retrieved from the vector DB / KG to
                      prepend as context.

    Returns:
        A single combined prompt string.
    """
    parts: List[str] = []

    for turn in history:
        role = turn["role"].capitalize()  # "Human" or "Assistant"
        if role == "User":
            role = "Human"
        parts.append(f"{role}: {turn['content']}")

    # Build final human turn with optional RAG context
    current_parts: List[str] = []
    if rag_context and rag_context.strip():
        current_parts.append(
            "Relevant context retrieved from the Matter knowledge base (vector DB + knowledge graph):\n"
            "---\n"
            f"{rag_context}\n"
            "---\n"
        )
    else:
        current_parts.append(
            "NOTE: No relevant results were found in the current knowledge base "
            "(vector DB or knowledge graph) for this query. If you can answer from "
            "your Matter specification knowledge, use the required disclosure prefix.\n"
        )
    current_parts.append(user_message)

    parts.append("Human: " + "\n".join(current_parts))
    parts.append("Assistant:")

    return "\n\n".join(parts)
