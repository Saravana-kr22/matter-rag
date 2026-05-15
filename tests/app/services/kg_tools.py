"""KG and vector-DB tool definitions for the agentic chat path.

Wraps in-memory MatterKGBuilder query methods + FAISS search as tool
definitions so the LLM can call them interactively during a chat turn.

Tools are defined in **OpenAI function-calling format** — the most portable
schema across providers:
  - Ollama (local)        accepts OpenAI format natively
  - LM Studio             accepts OpenAI format via openai SDK
  - ClaudeProvider        converts internally (OpenAI → Anthropic)
  - GeminiProvider        converts internally (OpenAI → Gemini FunctionDeclaration)

Tools exposed
-------------
get_test_cases_for_cluster    — all TCs for a cluster (no top-k cap)
find_entity_coverage          — coverage status for a specific entity
find_requirements_and_coverage — spec requirements + TC coverage map
get_cluster_dependencies      — related/dependent clusters
search_kg_by_keywords         — general KG keyword search
search_vector_db              — semantic FAISS search

Usage (from mcp_chat.py)::

    from tests.app.services.kg_tools import KG_TOOLS, execute_kg_tool
    result_text = execute_kg_tool(kg, vs, embedder, cfg, "get_test_cases_for_cluster",
                                  {"cluster_name": "On/Off Cluster"})
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions — OpenAI function-calling format (portable across providers)
# ---------------------------------------------------------------------------

KG_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_test_cases_for_cluster",
            "description": (
                "Return ALL test cases (TC-*) defined for a specific Matter cluster. "
                "Use when the user asks which test cases cover a cluster, wants a count, "
                "or wants a list. Returns TC id, title, purpose, intents, and DUT type."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cluster_name": {
                        "type": "string",
                        "description": (
                            "Matter cluster name, e.g. 'On/Off Cluster', 'Door Lock'. "
                            "Partial names supported (case-insensitive substring match)."
                        ),
                    },
                },
                "required": ["cluster_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_entity_coverage",
            "description": (
                "Check whether a specific attribute, command, event, or feature of a "
                "cluster is covered by an existing test case. Returns coverage status, "
                "the entity node, and any linked test cases."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cluster_name": {
                        "type": "string",
                        "description": "Matter cluster name, e.g. 'On/Off Cluster'.",
                    },
                    "entity_type": {
                        "type": "string",
                        "enum": ["attribute", "command", "event", "feature"],
                        "description": "Type of entity to check.",
                    },
                    "entity_name": {
                        "type": "string",
                        "description": (
                            "CamelCase entity name, e.g. 'OnOff', 'Toggle', 'StartUpOnOff'."
                        ),
                    },
                },
                "required": ["cluster_name", "entity_type", "entity_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_requirements_and_coverage",
            "description": (
                "Find Matter spec requirements (SHALL/MUST sentences) for a cluster and "
                "determine which are covered by test cases and which are gaps. Use when "
                "the user asks about spec compliance, behavioral requirements, or coverage gaps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cluster_name": {
                        "type": "string",
                        "description": "Matter cluster name to scope the requirement search.",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional 2–6 keywords to filter requirements, "
                            "e.g. ['timing', 'persistence'] or ['access control', 'fabric']. "
                            "Omit to retrieve all requirements for the cluster."
                        ),
                    },
                },
                "required": ["cluster_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cluster_dependencies",
            "description": (
                "Find Matter clusters that depend on a given cluster, or clusters that the "
                "given cluster depends on. Useful for understanding cross-cluster relationships "
                "and transitive test coverage needs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cluster_name": {
                        "type": "string",
                        "description": "Matter cluster name to analyze.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["incoming_depends_on", "outgoing_depends_on"],
                        "description": (
                            "'incoming_depends_on': clusters that depend ON the named cluster; "
                            "'outgoing_depends_on': clusters that the named cluster depends on."
                        ),
                    },
                },
                "required": ["cluster_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kg_by_keywords",
            "description": (
                "General keyword search across the knowledge graph. Returns matching "
                "test cases, requirements, and cluster nodes. Use as a fallback when "
                "the question does not target a specific cluster or entity, or when "
                "the user asks a broad question spanning multiple clusters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query or space-separated keywords.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return (default 10, max 30).",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vector_db",
            "description": (
                "Semantic similarity search over the FAISS test-plan vector database. "
                "Returns the most semantically similar test cases to the query. "
                "Complements knowledge-graph search — use when the user asks about "
                "test procedures, step details, setup/prerequisites, or when KG search "
                "returns few results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of the test scenario or content.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 15).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution dispatcher
# ---------------------------------------------------------------------------

def execute_kg_tool(
    kg,
    vector_store,
    embedder,
    config,
    tool_name: str,
    tool_input: Dict[str, Any],
) -> str:
    """Execute a KG/vector tool call and return a formatted result string.

    Args:
        kg:           MatterKGBuilder instance (in-memory knowledge graph).
        vector_store: FAISS vector store instance (may be None).
        embedder:     EmbeddingsModule instance (may be None).
        config:       AppConfig instance (may be None).
        tool_name:    Name of the tool to execute (must be in KG_TOOLS).
        tool_input:   Parameters dict matching the tool's input_schema.

    Returns:
        Human-readable string injected back to the LLM as the tool result.
    """
    try:
        if tool_name == "get_test_cases_for_cluster":
            return _exec_get_test_cases(kg, tool_input)
        if tool_name == "find_entity_coverage":
            return _exec_find_entity_coverage(kg, tool_input)
        if tool_name == "find_requirements_and_coverage":
            return _exec_find_requirements(kg, tool_input)
        if tool_name == "get_cluster_dependencies":
            return _exec_get_dependencies(kg, tool_input)
        if tool_name == "search_kg_by_keywords":
            return _exec_kg_keyword_search(kg, tool_input)
        if tool_name == "search_vector_db":
            return _exec_vector_search(vector_store, embedder, config, tool_input)
        return f"Unknown tool: {tool_name}"
    except Exception as exc:
        logger.warning("[kg_tools] %s(%s) failed: %s", tool_name, tool_input, exc)
        return f"Tool execution error ({tool_name}): {exc}"


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

def _exec_get_test_cases(kg, inp: dict) -> str:
    cluster = inp.get("cluster_name", "")
    if not hasattr(kg, "get_test_cases_for_cluster"):
        return "Knowledge graph does not support get_test_cases_for_cluster."
    nodes = kg.get_test_cases_for_cluster(cluster)
    if not nodes:
        return f"No test cases found for cluster '{cluster}'."
    lines = [f"Found {len(nodes)} test case(s) for '{cluster}':\n"]
    for n in sorted(nodes, key=lambda x: x.properties.get("tc_id") or x.label or ""):
        tc_id   = n.properties.get("tc_id") or n.label
        title   = n.properties.get("title") or n.properties.get("content", "")[:100]
        dut     = n.properties.get("dut_type", "")
        intents = ", ".join(n.properties.get("intents") or [])
        purpose = n.properties.get("purpose", "")[:120]
        lines.append(f"- **{tc_id}** — {title}")
        if purpose:
            lines.append(f"  Purpose: {purpose}")
        if dut:
            lines.append(f"  DUT: {dut}")
        if intents:
            lines.append(f"  Intents: {intents}")
    return "\n".join(lines)


def _exec_find_entity_coverage(kg, inp: dict) -> str:
    cluster     = inp.get("cluster_name", "")
    entity_type = inp.get("entity_type", "attribute")
    entity_name = inp.get("entity_name", "")
    if not hasattr(kg, "find_entity_coverage"):
        return "Knowledge graph does not support find_entity_coverage."
    cov = kg.find_entity_coverage(cluster, entity_type, entity_name)
    if not cov["entity_exists"]:
        return (
            f"Entity '{entity_name}' ({entity_type}) in '{cluster}' does NOT exist "
            f"in the knowledge graph — it may be a new or unrecognised entity with "
            f"no existing schema entry."
        )
    if not cov["covered"]:
        return (
            f"COVERAGE GAP: '{entity_name}' ({entity_type}) in '{cluster}' exists in "
            f"the knowledge graph but has NO test case directly covering it."
        )
    tcs = cov["test_cases"]
    tc_list = ", ".join(n.properties.get("tc_id") or n.label for n in tcs[:10])
    return (
        f"'{entity_name}' ({entity_type}) in '{cluster}' is covered by "
        f"{len(tcs)} test case(s): {tc_list}."
    )


def _exec_find_requirements(kg, inp: dict) -> str:
    cluster  = inp.get("cluster_name", "")
    keywords = inp.get("keywords") or []
    if not hasattr(kg, "find_requirements_and_coverage"):
        return "Knowledge graph does not support find_requirements_and_coverage."
    result    = kg.find_requirements_and_coverage(keywords, cluster=cluster)
    covered   = result.get("covered", {})
    uncovered = result.get("uncovered", [])
    lines: List[str] = []
    if covered:
        lines.append(f"**Covered requirements ({len(covered)}):**")
        for req_id, tcs in list(covered.items())[:20]:
            tc_ids = ", ".join(n.properties.get("tc_id") or n.label for n in tcs[:4])
            lines.append(f"- {req_id} → {tc_ids}")
    if uncovered:
        lines.append(f"\n**Uncovered requirements / gaps ({len(uncovered)}):**")
        for req_node in uncovered[:20]:
            text = (
                req_node.properties.get("normative_text")
                or req_node.properties.get("content")
                or ""
            )[:160]
            lines.append(f"- {req_node.node_id}: {text}")
    if not lines:
        return f"No requirements found for cluster '{cluster}' with keywords {keywords}."
    return "\n".join(lines)


def _exec_get_dependencies(kg, inp: dict) -> str:
    cluster   = inp.get("cluster_name", "")
    direction = inp.get("direction", "incoming_depends_on")
    if not hasattr(kg, "get_cluster_dependencies"):
        return "Knowledge graph does not support get_cluster_dependencies."
    deps = kg.get_cluster_dependencies(cluster, direction=direction)
    if not deps:
        label = (
            "that depend on" if direction == "incoming_depends_on"
            else "that the cluster depends on"
        )
        return f"No clusters found {label} '{cluster}'."
    label = (
        "that depend on" if direction == "incoming_depends_on"
        else f"that '{cluster}' depends on"
    )
    names = [n.properties.get("name") or n.label for n in deps]
    return f"Clusters {label} '{cluster}' ({len(deps)}): {', '.join(names[:20])}."


def _exec_kg_keyword_search(kg, inp: dict) -> str:
    query       = inp.get("query", "")
    max_results = min(int(inp.get("max_results", 10)), 30)
    if not hasattr(kg, "search_by_entities"):
        return "Knowledge graph does not support keyword search."
    nodes = kg.search_by_entities(query, max_results=max_results)
    if not nodes:
        return f"No knowledge graph results for query: '{query}'."
    lines = [f"Knowledge graph results for '{query}' ({len(nodes)} matches):\n"]
    for n in nodes:
        nt      = n.node_type.value if hasattr(n.node_type, "value") else str(n.node_type)
        label   = n.label or n.node_id
        content = (
            n.properties.get("normative_text")
            or n.properties.get("content")
            or ""
        )[:120]
        lines.append(f"- [{nt}] **{label}**: {content}")
    return "\n".join(lines)


def _exec_vector_search(vector_store, embedder, config, inp: dict) -> str:
    query = inp.get("query", "")
    top_k = min(int(inp.get("top_k", 5)), 15)
    if vector_store is None or embedder is None:
        return "Vector database is not available."
    try:
        from src.search.faiss_search import FAISSSearch
        threshold = 0.40
        if config is not None:
            cfg_thresh = getattr(
                getattr(config, "pipeline", None), "similarity_threshold", None
            )
            if cfg_thresh is not None:
                threshold = min(0.45, float(cfg_thresh))
        searcher = FAISSSearch(vector_store, embedder)
        results  = searcher.search(query, top_k=top_k, threshold=threshold)
        if not results:
            return f"No vector DB results for query: '{query}'."
        lines = [f"Vector search results for '{query}' ({len(results)} matches):\n"]
        for r in results:
            tc_id      = r.metadata.get("tc_id", "")
            title      = r.metadata.get("title", "")
            chunk_type = r.metadata.get("chunk_type", "")
            score      = getattr(r, "score", None)
            score_str  = f" score={score:.3f}" if score is not None else ""
            content    = r.page_content[:200]
            lines.append(f"- **{tc_id}** [{chunk_type}{score_str}]: {title}")
            lines.append(f"  {content}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("[kg_tools] vector_search failed: %s", exc)
        return f"Vector search error: {exc}"
