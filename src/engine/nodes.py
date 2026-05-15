"""Node library for the Matter RAG pipeline.

Every public function in this module is a **self-contained pipeline stage**:

    signature:  (state: PipelineState) -> dict
    contract:   read from state, return only the keys you changed

Nodes are independent of each other and of any specific client graph.
Clients compose them into a LangGraph ``StateGraph`` in whichever order and
subset they need (see ``src/engine/graphs/`` for examples).

Ownership rule
--------------
Every field in ``PipelineState`` is owned by the node that writes it, not by
the client that reads it.  Never add a state field to serve one specific client.
If a node must behave differently for different clients (e.g. chat vs CLI),
branch on ``state["run_ctx"].client`` inside the node.

See ``src/engine/ARCHITECTURE.md`` for the full design rationale.
"""

from __future__ import annotations

import json as _json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logging_config import log_node, PipelineFatalError

from src.config.config_loader import AppConfig
from src.database.vector_store import VectorStore, create_vector_store
from src.embeddings.embeddings import EmbeddingsModule
from src.fetcher.base_fetcher import FetchedDocument
from src.fetcher.document_fetcher import DocumentFetcher  # legacy fallback
from src.fetcher.fetcher_registry import create_fetcher, load_sources
from src.knowledge_graph.base_graph import BaseKnowledgeGraph, EdgeType, GraphNode
from src.knowledge_graph.graph_factory import create_knowledge_graph
from src.llm.llm_provider import get_llm
from src.loader.base_loader import Document
from src.loader.document_loader import DocumentLoader
from src.processor.document_processor import DocumentProcessor
from src.search.faiss_search import FAISSSearch, SearchResult
from src.search.reranker import RankedCandidate, RerankerWeights, rerank_candidates

# Real import (not TYPE_CHECKING only) because LangGraph calls get_type_hints()
# on PipelineState at runtime to build its state schema channels.
# run_context.py has no imports from nodes.py so there is no circular dependency.
from src.engine.run_context import RunContext

logger = logging.getLogger(__name__)
# Dedicated loggers for search results → routed to separate files by RunAwareFileHandler.
_vector_search_logger = logging.getLogger("src.search.vector")
_kg_search_logger = logging.getLogger("src.search.kg")


# ---------------------------------------------------------------------------
# LLM factory helper — routes call log into the per-run directory
# ---------------------------------------------------------------------------

def _get_run_llm(config: AppConfig, run_dir: str):
    """Return an LLM provider with call logging redirected to the per-run directory.

    When ``run_dir`` is set (normal pipeline execution), ``call_log_path`` is
    overridden to ``<run_dir>/llm_calls.jsonl`` so the text + HTML logs land
    alongside the other per-run logs.  Falls back to the config value when
    ``run_dir`` is empty (unit tests, standalone scripts).

    ``LLMCallLogger`` seeds its in-memory entries from any existing JSONL at
    that path, so all calls within a run accumulate into one HTML file even
    though ``get_llm()`` may be called several times (once per node).
    """
    import copy
    from src.config.config_loader import LLMConfig
    llm_cfg = copy.copy(config.llm)
    if run_dir and llm_cfg.call_log_path:
        llm_cfg.call_log_path = str(Path(run_dir) / "llm_calls.jsonl")
    return get_llm(llm_cfg)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pipeline progress writer — lightweight JSON updated by key nodes
# ---------------------------------------------------------------------------

def _update_pipeline_progress(state: dict, node_name: str, **extra) -> None:
    """Write a lightweight progress JSON to the run directory."""
    run_dir = state.get("run_dir", "")
    if not run_dir:
        return
    import json as _pj
    from datetime import datetime as _pdt
    progress = {
        "current_node": node_name,
        "timestamp": _pdt.now().isoformat(),
        **extra,
    }
    try:
        Path(run_dir, "pipeline_progress.json").write_text(
            _pj.dumps(progress, indent=2, default=str), encoding="utf-8"
        )
    except Exception as _prog_exc:
        logger.warning("[_update_pipeline_progress] Failed to write progress: %s", _prog_exc)

# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "each", "all", "both",
    "any", "not", "but", "and", "or", "nor", "so", "yet", "either",
    "neither", "this", "that", "these", "those", "which", "who", "what",
    "when", "where", "how", "why", "there", "here", "i", "me", "my", "we",
    "our", "you", "your", "he", "his", "she", "her", "it", "its", "they",
    "their", "them", "us", "if", "then", "than", "case", "test", "please",
    "give", "find", "show", "list", "get", "want", "know", "tell", "need",
    "describe", "explain", "verify", "check", "about", "related", "relevant",
    "based", "using", "used", "use", "like", "such", "also", "per",
}


def _extract_keywords(text: str) -> List[str]:
    """Return meaningful lowercase keywords from a natural-language query."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.lower())
    return [w for w in words if len(w) >= 3 and w not in _STOP_WORDS]


def _parse_dm_xml_string(
    dm_xml_str: str,
    source_file: Path,
    base_dir: Path,
) -> List[FetchedDocument]:
    """Parse a DM XML string (from ZAP adapter) into FetchedDocument objects.

    Uses MatterXMLFetcher's cluster parsing logic to produce schema-bearing
    FetchedDocuments consistent with standard DM XML loading.
    """
    import xml.etree.ElementTree as ET
    from src.fetcher.sources.matter_xml_fetcher import MatterXMLFetcher

    docs: List[FetchedDocument] = []
    fetcher = MatterXMLFetcher(path=str(base_dir))

    # The converted XML may contain multiple <cluster> elements separated by blank lines
    # Wrap in a root element for valid XML parsing
    wrapped = f"<clusters>{dm_xml_str}</clusters>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError as exc:
        logger.warning("[_parse_dm_xml_string] Failed to parse converted XML from %s: %s",
                       source_file, exc)
        return docs

    for cluster_el in root:
        if MatterXMLFetcher._strip_ns(cluster_el.tag) != "cluster":
            continue
        schema = fetcher._parse_cluster_element(cluster_el)
        if schema:
            doc = fetcher._cluster_to_document(source_file, schema)
            docs.append(doc)

    return docs


# ---------------------------------------------------------------------------
# LLM-driven chat query planner
# ---------------------------------------------------------------------------
# Instead of fragile heuristics, we ask the LLM to parse the user's question
# into a structured plan that tells the KG dispatcher what to do.
# One cheap LLM call replaces ~180 lines of regex patterns.

_QUERY_PLAN_SYSTEM = (
    "You are a query planner for a Matter protocol knowledge graph. "
    "Given a user question, return ONLY a single-line JSON object describing "
    "the best retrieval strategy. No prose, no markdown fences, just JSON."
)

_QUERY_PLAN_PROMPT = """User question: {query}

Choose ONE intent from:
- "list_test_cases"    : user wants to list/count all test cases for a cluster
- "entity_coverage"    : user asks if a specific attribute/command/event/feature has test coverage
- "requirement_lookup" : user asks about spec requirements/rules/normative text for a cluster
- "graph_traversal"    : user asks about cluster dependencies or relationships (what depends on X, what does X use)
- "general_qa"         : any other question about Matter

Return JSON with these fields:
{{
  "intent": "<one of the five above>",
  "cluster": "<canonical cluster name e.g. 'On/Off Cluster', or null>",
  "entity_type": "<attribute|command|event|feature, or null>",
  "entity_name": "<CamelCase entity name e.g. 'OnOff', or null>",
  "traverse": "<incoming_depends_on|outgoing_depends_on, or null>",
  "keywords": ["<key", "words>"]
}}

Rules:
- "cluster" must end with " Cluster" if present (e.g. "On/Off Cluster", "Door Lock Cluster")
- "traverse" = "incoming_depends_on" when user asks what clusters depend ON the named cluster
- "traverse" = "outgoing_depends_on" when user asks what clusters the named cluster depends on
- "keywords" = 2-6 most important content words from the question (lowercase, no stop words)
- Set unused fields to null
- Expand common abbreviations in "cluster": DIAG → "Diagnostics", OCC → "Occupancy Sensing",
  TSTAT → "Thermostat", LVL → "Level Control", OO → "On/Off", DL → "Door Lock",
  WNCV → "Window Covering", FAN → "Fan Control", CNET → "Network Commissioning",
  DGGEN → "General Diagnostics", DGTHREAD → "Thread Network Diagnostics",
  DGWIFI → "Wi-Fi Network Diagnostics", DGSW → "Software Diagnostics",
  DGETH → "Ethernet Network Diagnostics"
- When the user uses a partial name like "diagnostics" or "DIAG" that matches multiple clusters,
  use the shared word as the cluster value (e.g. "Diagnostics") — the search will substring-match
  all clusters containing that word"""


def _plan_chat_query(query_text: str, llm) -> dict:
    """Call the LLM once to get a structured query plan for the KG dispatcher.

    Returns a dict with keys: intent, cluster, entity_type, entity_name,
    traverse, keywords.  On any error (LLM failure, bad JSON) returns a
    safe fallback plan that routes to general_qa keyword search.
    """
    _fallback = {
        "intent": "general_qa",
        "cluster": None,
        "entity_type": None,
        "entity_name": None,
        "traverse": None,
        "keywords": _extract_keywords(query_text),
    }
    try:
        prompt = _QUERY_PLAN_PROMPT.format(query=query_text)
        if hasattr(llm, "set_next_label"):
            llm.set_next_label("Chat — query planner")
        raw = llm.complete(prompt, system=_QUERY_PLAN_SYSTEM)
        # Strip any accidental markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        plan = _json.loads(raw)
        # Validate intent
        _valid_intents = {
            "list_test_cases", "entity_coverage", "requirement_lookup",
            "graph_traversal", "general_qa",
        }
        if plan.get("intent") not in _valid_intents:
            plan["intent"] = "general_qa"
        # Ensure keywords is always a list
        if not isinstance(plan.get("keywords"), list):
            plan["keywords"] = _extract_keywords(query_text)
        logger.debug("[_plan_chat_query] plan=%s", plan)
        return plan
    except Exception as exc:
        logger.warning("[_plan_chat_query] plan failed (%s) — using general_qa fallback", exc)
        return _fallback



# ---------------------------------------------------------------------------
# Shared system prompt for the LLM analysis step
# ---------------------------------------------------------------------------

_ANALYSIS_SYSTEM_PROMPT = """You are a senior software test engineer specializing in the Matter
(formerly Project CHIP) connectivity standard. Your task is to analyze PR changes against
existing test plans and identify:
1. NEW test cases that need to be created for new functionality
2. EXISTING test cases that need to be updated due to spec changes
3. Test cases that appear to already cover the PR changes adequately

Be specific: reference section numbers, cluster names, commands, and attributes.
Format your output as structured Markdown."""

_ANALYSIS_PROMPT_TEMPLATE = """## PR Change
**File**: {path}
**Status**: {status}

### Change Content (unified diff — lines starting with `+` are additions, `-` are removals)
{content}

---

## Relevant Existing Test Cases

### A. Semantic Search Results (vector similarity)
{test_cases}

### B. Knowledge Graph Context (entity-matched test cases and requirements)
{graph_context}

---

## Analysis Task
Based on the PR change above and the existing test cases retrieved from **both** semantic
search and the knowledge graph, provide:

### 1. Missing Test Cases
List any new test cases that MUST be created. For each, include:
- Test case title
- Cluster / command / attribute affected
- What should be verified
- Suggested test steps (brief)

### 2. Test Cases Needing Updates
List existing test cases that need modification. For each, include:
- Test case identifier (if available)
- What needs to change and why

### 3. Already Covered
List PR changes already adequately covered by existing test cases.

---

## JSON Output (machine-readable)

After your analysis above, append a JSON block using exactly this schema.
The `adoc_section` field must contain complete, valid AsciiDoc following Matter TC format:
- TC heading: `== TC-<CLUSTER>-<X>.<Y> [DUT as Server]`
- Sub-sections: `=== Purpose`, `=== PICS`, `=== Test Environment`, `=== Procedure`, `=== Expected Results`
- Numbered steps: `1. Step description`

```json
{{
  "missing_tests": [
    {{
      "title": "TC-CLUSTER-X.Y",
      "cluster": "CLUSTER_ABBREVIATION",
      "adoc_section": "== TC-CLUSTER-X.Y [DUT as Server]\\n\\n=== Purpose\\nVerify ...\\n\\n=== PICS\\n[PICS.CLUSTER.S]\\n\\n=== Procedure\\n1. Commission DUT to TH.\\n2. ..."
    }}
  ],
  "update_candidates": [
    {{
      "tc_id": "TC-OO-2.1",
      "change_summary": "One-line description of what changed and why",
      "adoc_section": "== TC-OO-2.1 [DUT as Server]\\n\\n=== Purpose\\nVerify ...\\n\\n=== PICS\\n[PICS.OO.S]\\n\\n=== Procedure\\n1. Commission DUT to TH.\\n2. ..."
    }}
  ]
}}
```
"""


# ---------------------------------------------------------------------------
# State definition (TypedDict)
# ---------------------------------------------------------------------------

from typing import TypedDict


class PipelineState(TypedDict, total=False):
    """Shared state dict that flows through every node in a pipeline run.

    Fields are grouped by the node that writes them.  ``total=False`` means
    every field is optional — nodes must guard against missing keys rather than
    assuming they exist.

    Do not add fields here to serve a specific client.  See ARCHITECTURE.md.
    """

    # ---- Set by the client before invoking the graph ----
    config: AppConfig
    run_ctx: RunContext          # per-run identity; client branches on run_ctx.client
    pr_url: Optional[str]
    input_doc: Optional[str]       # local HTML/adoc file used as change input (alt to pr_url)
    cluster_filter: str            # "" = all clusters; "Push AV cluster" = one cluster
    test_plan_dir: Optional[str]
    build_test_plan_vectors: bool  # True → re-chunk, embed, and save test plan vector DB
    build_knowledge_graph: bool    # True → rebuild and save the knowledge graph
    build_data_model: bool         # True → re-ingest Matter DM XML schema into KG
    build_knowledge_graph_with_llm: bool  # True → run LLM spec refinement after KG build
    output_dir: str
    run_dir: str                   # per-run log directory (set by PipelineRunner)
    max_pr_chunks: int             # 0 = process all chunks; N > 0 = limit to first N chunks
    generate_negative_tests: bool  # True → ask LLM to suggest error-path TCs (off by default)

    # ---- Written by cluster_review_node ----
    cluster_review_additions: List[Dict]  # new TC suggestions from the cluster-level review pass

    # ---- Written by fetch_documents_node ----
    pr_documents: List[FetchedDocument]
    test_plan_fetched: List[FetchedDocument]
    spec_fetched: List[FetchedDocument]              # role="spec" Matter spec docs
    data_model_fetched: List[FetchedDocument]        # role="data_model" Matter DM XML docs
    test_plan_adoc_sources: List[FetchedDocument]    # role="test_plans_adoc_folder" raw adoc files for in-place update

    # ---- Written by process_documents_node ----
    spec_diff_html: List[FetchedDocument]  # original matter_diff HTML before expansion

    # ---- Written by build_matter_schema_node ----
    matter_schema: Dict[str, Any]          # canonical Matter data-model entities

    # ---- Written by chunk_embed_test_plans_node ----
    test_plan_chunks: List[Document]
    vector_store: VectorStore
    built_knowledge_base: Any              # cached KnowledgeBase; reused by build_knowledge_graph_node

    # ---- Written by chunk_pr_node ----
    pr_chunks: List[Document]
    spec_chunks: List[Document]

    # ---- Written by extract_pr_changes_node ----
    pr_changes: List[Dict[str, Any]]       # structured change records per PR chunk
    pr_requirements: List[Dict[str, Any]]  # behavioural/timing requirements per PR chunk

    # ---- Written by build_knowledge_graph_node ----
    knowledge_graph: BaseKnowledgeGraph

    # ---- Written by search_test_plan_vector_db_node ----
    search_results: Dict[str, List[SearchResult]]  # pr_chunk_id → vector results

    # ---- Written by search_knowledge_graph_node ----
    graph_results: Dict[str, List[GraphNode]]       # pr_chunk_id → KG nodes
    graph_coverage_notes: Dict[str, str]            # pr_chunk_id → coverage summary (chat path only)
    chat_query_intent: str                          # LLM planner intent (chat path only: list_test_cases, etc.)

    # ---- Written by analyze_chunks_with_llm_node ----
    # CLI path: structured report data
    analysis_results: List[Dict]
    missing_tests: List[Dict]
    update_candidates: List[Dict]
    negative_tests: List[Dict]      # error-path TCs (only when generate_negative_tests=True)
    # Chat path: plain reply string returned to the session
    llm_reply: str
    # Chat path: conversation history and system prompt
    chat_history: List[dict]
    system_prompt: str

    # ---- LLM resilience tracking (set by analyze_chunks_with_llm_node) ----
    llm_failed_chunks: int     # number of chunks where LLM call threw an exception
    llm_aborted_at: Optional[int]  # chunk index if a fatal error cut the loop short
    llm_total_chunks: int      # total pr_chunks fed to the LLM loop

    # ---- Written by write_adoc_updates_node ----
    adoc_output_paths: List[str]   # paths of written _matter_ai_rag_update.adoc files

    # ---- Written by cluster_review_node ----
    cluster_review_path: str       # path to cluster_review_<ts>.md audit file

    # ---- Set by the client; read by human_outline_expand_node ----
    third_pass_outline_path: str   # path to human-edited outline JSON; "" = skip 3rd pass

    # ---- Written by second_pass_tc_gen_node ----
    second_pass_outlines: List[str]  # paths to saved TC outline JSON files (one per triggered cluster)
    include_coverage_gaps: bool       # True → generate coverage gap TCs (Section 2 of report)
    coverage_gap_tests: List[dict]    # TCs from coverage gap analysis (separate from missing_tests)
    pass_stats: Dict[str, Any]       # per-pass TC counts for the pipeline funnel summary

    # ---- Written by generate_report_node ----
    report_path: str

    # ---- Written by any node on error ----
    errors: List[str]
    fatal_error: bool              # True → pipeline halted; subsequent nodes are skipped

    # ---- Kept for backward compatibility (unused by new nodes) ----
    pr_embeddings: Any             # np.ndarray
    test_plan_embeddings: Any      # np.ndarray


# ---------------------------------------------------------------------------
# Adoc-to-HTML conversion helper
# ---------------------------------------------------------------------------

def _maybe_convert_adoc_to_html(doc: FetchedDocument) -> FetchedDocument:
    """Run ``asciidoctor`` on *doc* if it is an ``.adoc`` file.

    Returns a new ``FetchedDocument`` whose:
      - ``content``   is the generated HTML string
      - ``path``      has the extension replaced with ``.html``
      - ``metadata``  gains ``_adoc_converted: True``

    If ``asciidoctor`` is not installed or conversion fails, the original
    document is returned unchanged and a warning is logged.
    """
    import subprocess
    import tempfile

    if doc.extension != ".adoc":
        return doc

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".adoc", mode="w", encoding="utf-8", delete=False
        ) as tmp_in:
            tmp_in.write(doc.content)
            tmp_in_path = tmp_in.name

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp_out:
            tmp_out_path = tmp_out.name

        result = subprocess.run(
            ["asciidoctor", "--no-header-footer", "-o", tmp_out_path, tmp_in_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "[process_documents_node] asciidoctor failed for %s: %s",
                doc.path, result.stderr.strip(),
            )
            return doc

        with open(tmp_out_path, encoding="utf-8") as f:
            html_content = f.read()

        new_path = str(Path(doc.path).with_suffix(".html"))
        new_meta = {**doc.metadata, "_adoc_converted": True}
        logger.debug("[process_documents_node] Converted %s → %s (%d chars)",
                     doc.path, new_path, len(html_content))
        return FetchedDocument(path=new_path, content=html_content, metadata=new_meta)

    except FileNotFoundError:
        logger.warning(
            "[process_documents_node] 'asciidoctor' not found — skipping conversion for %s. "
            "Install it with: gem install asciidoctor",
            doc.path,
        )
        return doc
    except Exception as exc:
        logger.warning(
            "[process_documents_node] adoc-to-HTML conversion failed for %s: %s",
            doc.path, exc,
        )
        return doc
    finally:
        import os
        for p in (locals().get("tmp_in_path"), locals().get("tmp_out_path")):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Inspection helper — write Matter diff sections to the run log directory
# ---------------------------------------------------------------------------

def _write_matter_diff_inspection(
    docs: List[FetchedDocument],
    run_dir: str,
) -> None:
    """Write an inspection file listing all Matter diff sections extracted this run.

    The file is written to ``<run_dir>/matter_diff_sections.json`` so you can
    review exactly what was extracted from the spec diff HTML before the
    pipeline proceeds to the search and LLM stages.

    Format::

        {
          "total_sections": 12,
          "sections": [
            {
              "index": 0,
              "cluster": "On/Off",
              "section_title": "Attributes",
              "section_level": 3,
              "is_new_section": false,
              "source_html": "appclusters_diff.html",
              "content_preview": "first 300 chars of annotated text …",
              "content_length": 1234
            },
            …
          ]
        }

    Only docs whose ``doc_type`` metadata equals ``"matter_spec_diff"`` are
    included — regular PR docs and spec docs are omitted.
    """
    import json as _json

    diff_docs = [d for d in docs if d.metadata.get("doc_type") == "matter_spec_diff"]
    if not diff_docs:
        return

    sections_data = []
    for doc in diff_docs:
        meta = doc.metadata
        sections_data.append({
            "index":         meta.get("section_index", ""),
            "cluster":       meta.get("cluster", ""),
            "section_title": meta.get("section_title", ""),
            "section_level": meta.get("section_level", 0),
            "is_new_section": meta.get("is_new_section", False),
            "source_html":   meta.get("source_html", ""),
            "content_preview": doc.content[:300] + ("…" if len(doc.content) > 300 else ""),
            "content_length": len(doc.content),
        })

    payload = {
        "total_sections": len(sections_data),
        "sections": sections_data,
    }

    # Resolve output path
    if run_dir:
        out_path = Path(run_dir) / "matter_diff_sections.json"
    else:
        Path("logs").mkdir(exist_ok=True)
        out_path = Path("logs") / "matter_diff_sections.json"

    try:
        out_path.write_text(
            _json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "[process_documents_node] Matter diff inspection written → %s  (%d sections)",
            out_path, len(sections_data),
        )
    except Exception as exc:
        logger.warning(
            "[process_documents_node] Could not write matter_diff_sections.json: %s", exc
        )


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------


@log_node
def fetch_documents_node(state: PipelineState) -> PipelineState:
    """Node 1: Fetch documents from all configured sources.

    Source role routing:
      - 'pr'                     → pr_documents
      - 'test_plan'              → test_plan_fetched
      - 'spec'                   → spec_fetched  (Matter specification documents)
      - 'data_model'             → data_model_fetched (Matter DM XML definitions)
      - 'test_plans_adoc_folder' → test_plan_adoc_sources (raw .adoc files for in-place update)
    """
    config = state["config"]
    errors = list(state.get("errors", []))
    pr_docs: List[FetchedDocument] = []
    test_docs: List[FetchedDocument] = []
    spec_docs: List[FetchedDocument] = []
    data_model_docs: List[FetchedDocument] = []
    adoc_source_docs: List[FetchedDocument] = []

    _project_root = Path(__file__).resolve().parent.parent.parent
    _sources_cwd = Path("sources.json")
    _sources_root = _project_root / "sources.json"
    _sources_path = str(_sources_cwd if _sources_cwd.is_file() else _sources_root)
    sources = load_sources(_sources_path)

    if sources:
        for src in sources:
            src_id = src.get("id", src.get("type", "?"))
            role = src.get("role", "test_plan")
            try:
                fetcher = create_fetcher(src, config)
                docs = fetcher.fetch()
                if role == "pr":
                    pr_docs.extend(docs)
                elif role == "spec":
                    spec_docs.extend(docs)
                elif role == "data_model":
                    data_model_docs.extend(docs)
                elif role == "test_plans_adoc_folder":
                    adoc_source_docs.extend(docs)
                else:
                    test_docs.extend(docs)
                logger.info("[fetch_documents_node] source '%s' (role=%s) → %d docs",
                            src_id, role, len(docs))
            except Exception as exc:
                msg = f"[fetch_documents_node] source '{src_id}' failed: {exc}"
                logger.error(msg)
                errors.append(msg)
    else:
        # Legacy fallback: --pr-url / --test-plan-dir CLI args
        fetcher = DocumentFetcher(config.fetcher)
        if state.get("pr_url"):
            try:
                pr_docs = fetcher.fetch_pr(state["pr_url"])
                logger.info("[fetch_documents_node] PR files: %d", len(pr_docs))
            except Exception as exc:
                msg = f"Failed to fetch PR: {exc}"
                logger.error(msg)
                errors.append(msg)
        if state.get("test_plan_dir"):
            try:
                test_docs = fetcher.fetch_local(state["test_plan_dir"])
                logger.info("[fetch_documents_node] Test plan files: %d", len(test_docs))
            except Exception as exc:
                msg = f"Failed to fetch test plans: {exc}"
                logger.error(msg)
                errors.append(msg)

    # --input-doc: load a local file as PR change input (alternative to --pr-url)
    input_doc = state.get("input_doc")
    if input_doc:
        try:
            p = Path(input_doc).resolve()
            try:
                content = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = p.read_text(encoding="latin-1")
            meta: dict = {
                "source":        "local",
                "source_id":     "input_doc",
                "absolute_path": str(p),
                "file_size":     p.stat().st_size,
                "_process_rules": [],
            }
            # HTML input docs are treated as Matter spec diff HTML so that
            # ProcessMatterHtmlDoc expands them into per-section FetchedDocuments.
            if p.suffix.lower() in (".html", ".htm"):
                meta["matter_diff"] = True
            pr_docs.append(FetchedDocument(path=p.name, content=content, metadata=meta))
            logger.info(
                "[fetch_documents_node] input_doc '%s' loaded (%d chars)",
                p.name, len(content),
            )
        except Exception as exc:
            msg = f"[fetch_documents_node] Failed to load --input-doc '{input_doc}': {exc}"
            logger.error(msg)
            errors.append(msg)

    # ---- Load additional/overlay sources from config.analysis fields ----
    if hasattr(config, 'analysis'):
        # Additional sources file — entries appended to base sources
        additional_sources_file = getattr(config.analysis, 'additional_sources_file', '')
        if additional_sources_file and Path(additional_sources_file).is_file():
            try:
                extra_sources = load_sources(additional_sources_file)
                for src in extra_sources:
                    src_id = src.get("id", src.get("type", "?"))
                    role = src.get("role", "test_plan")
                    try:
                        fetcher = create_fetcher(src, config)
                        docs = fetcher.fetch()
                        if role == "pr":
                            pr_docs.extend(docs)
                        elif role == "spec":
                            spec_docs.extend(docs)
                        elif role == "data_model":
                            data_model_docs.extend(docs)
                        elif role == "test_plans_adoc_folder":
                            adoc_source_docs.extend(docs)
                        else:
                            test_docs.extend(docs)
                        logger.info(
                            "[fetch_documents_node] additional source '%s' (role=%s) → %d docs",
                            src_id, role, len(docs),
                        )
                    except Exception as exc:
                        msg = f"[fetch_documents_node] additional source '{src_id}' failed: {exc}"
                        logger.error(msg)
                        errors.append(msg)
                logger.info(
                    "[fetch_documents_node] Loaded %d additional source entries from %s",
                    len(extra_sources), additional_sources_file,
                )
            except Exception as exc:
                logger.warning(
                    "[fetch_documents_node] Failed to load additional sources file '%s': %s",
                    additional_sources_file, exc,
                )

        # Additional test plans directory — synthetic local_folder source with role=test_plan
        additional_tp_dir = getattr(config.analysis, 'additional_test_plans_dir', '')
        if additional_tp_dir and Path(additional_tp_dir).is_dir():
            try:
                synthetic_tp_src = {
                    "id": "additional_test_plans",
                    "type": "local_folder",
                    "role": "test_plan",
                    "path": additional_tp_dir,
                }
                fetcher = create_fetcher(synthetic_tp_src, config)
                docs = fetcher.fetch()
                test_docs.extend(docs)
                logger.info(
                    "[fetch_documents_node] Loaded %d additional test plan docs from %s",
                    len(docs), additional_tp_dir,
                )
            except Exception as exc:
                msg = f"[fetch_documents_node] additional_test_plans_dir failed: {exc}"
                logger.error(msg)
                errors.append(msg)

        # Additional spec directory — synthetic local_folder source with role=spec
        additional_spec_dir = getattr(config.analysis, 'additional_spec_dir', '')
        if additional_spec_dir and Path(additional_spec_dir).is_dir():
            try:
                synthetic_spec_src = {
                    "id": "additional_spec",
                    "type": "local_folder",
                    "role": "spec",
                    "path": additional_spec_dir,
                }
                fetcher = create_fetcher(synthetic_spec_src, config)
                docs = fetcher.fetch()
                spec_docs.extend(docs)
                logger.info(
                    "[fetch_documents_node] Loaded %d additional spec docs from %s",
                    len(docs), additional_spec_dir,
                )
            except Exception as exc:
                msg = f"[fetch_documents_node] additional_spec_dir failed: {exc}"
                logger.error(msg)
                errors.append(msg)

        # Additional DM XML directories — synthetic matter_xml sources with role=data_model
        # Handles both standard DM XML files and ZAP-format (<configurator>) files.
        dm_dirs_additional = getattr(config.analysis, 'dm_dirs_additional', []) or []
        for dm_dir in dm_dirs_additional:
            if dm_dir and Path(dm_dir).is_dir():
                try:
                    from src.fetcher.sources.zap_xml_adapter import (
                        convert_zap_to_dm_xml,
                        is_zap_format,
                    )
                    dm_path = Path(dm_dir)
                    zap_files = []
                    standard_files = []
                    for xml_file in dm_path.rglob("*.xml"):
                        if is_zap_format(xml_file):
                            zap_files.append(xml_file)
                        else:
                            standard_files.append(xml_file)

                    # Load standard DM XML files via MatterXMLFetcher as before
                    if standard_files:
                        synthetic_dm_src = {
                            "id": f"additional_dm_{dm_path.name}",
                            "type": "matter_xml",
                            "role": "data_model",
                            "path": dm_dir,
                        }
                        fetcher = create_fetcher(synthetic_dm_src, config)
                        docs = fetcher.fetch()
                        data_model_docs.extend(docs)
                        logger.info(
                            "[fetch_documents_node] Loaded %d standard DM XML docs from %s",
                            len(docs), dm_dir,
                        )

                    # Convert ZAP-format files and parse via MatterXMLFetcher
                    if zap_files:
                        # Build cluster_name_lookup from already-loaded DM docs
                        cluster_name_lookup: Dict[int, str] = {}
                        for doc in data_model_docs:
                            s = doc.metadata.get("schema", {})
                            cid_str = s.get("cluster_id", "")
                            cname = s.get("cluster_name", "")
                            if cid_str and cname:
                                try:
                                    cid_int = int(cid_str, 16) if cid_str.startswith("0x") else int(cid_str)
                                    cluster_name_lookup[cid_int] = cname
                                except (ValueError, TypeError):
                                    pass

                        zap_doc_count = 0
                        for zap_file in zap_files:
                            try:
                                dm_xml_str = convert_zap_to_dm_xml(zap_file, cluster_name_lookup)
                                if not dm_xml_str:
                                    continue
                                zap_docs = _parse_dm_xml_string(
                                    dm_xml_str, zap_file, dm_path,
                                )
                                data_model_docs.extend(zap_docs)
                                zap_doc_count += len(zap_docs)
                            except Exception as exc:
                                logger.warning(
                                    "[fetch_documents_node] ZAP conversion failed for %s: %s",
                                    zap_file, exc,
                                )
                        if zap_doc_count:
                            logger.info(
                                "[fetch_documents_node] Converted %d ZAP XML → %d DM cluster docs from %s",
                                len(zap_files), zap_doc_count, dm_dir,
                            )
                except Exception as exc:
                    msg = f"[fetch_documents_node] additional DM dir '{dm_dir}' failed: {exc}"
                    logger.error(msg)
                    errors.append(msg)

    logger.info(
        "[fetch_documents_node] Total: pr=%d  test_plan=%d  spec=%d  data_model=%d  adoc_sources=%d",
        len(pr_docs), len(test_docs), len(spec_docs), len(data_model_docs), len(adoc_source_docs),
    )

    # Build / refresh the TC routing index from loaded adoc sources so
    # write_updated_testplan_node can find the correct file for every TC-ID.
    # This is a fast content scan — skipped automatically when the index is
    # already up-to-date (no adoc file newer than the index timestamp).
    if adoc_source_docs:
        try:
            from src.document_updater.tc_index_builder import build_tc_index_from_docs
            tc_index_path = (
                config.pipeline.tc_index_path
                if config and hasattr(config.pipeline, "tc_index_path")
                else "data/tc_index.json"
            )
            build_tc_index_from_docs(adoc_source_docs, output_path=tc_index_path)
        except Exception as exc:
            logger.warning("[fetch_documents_node] TC index build failed: %s", exc)

    _update_pipeline_progress(state, "fetch_documents", pr_docs=len(pr_docs), test_plan_docs=len(test_docs))
    return {
        **state,
        "pr_documents": pr_docs,
        "test_plan_fetched": test_docs,
        "spec_fetched": spec_docs,
        "data_model_fetched": data_model_docs,
        "test_plan_adoc_sources": adoc_source_docs,
        "errors": errors,
    }



@log_node
def process_documents_node(state: PipelineState) -> PipelineState:
    """Node 2: Apply text-cleaning rules to all fetched documents.

    Rules are loaded from:
      1. .ignore_rules.json  — global rules for every document
      2. doc.metadata['_process_rules'] — per-source rules set by each fetcher

    When ``config.pipeline.convert_adoc_to_html`` is True, any ``.adoc``
    document is converted to HTML via ``asciidoctor`` before returning.
    The converted document gains a ``.html`` extension so the loader stage
    routes it through ``HTMLLoader`` instead of ``AdocLoader``.

    HTML documents whose metadata contains ``"matter_diff": true`` are passed
    through ``ProcessMatterHtmlDoc``, which expands them into one
    ``FetchedDocument`` per diff section (each carrying annotated text with
    [ADDED:], [REMOVED:], [CHANGED:] markers ready for vector + KG search).

    Applies to PR documents, test plan documents, and Matter spec documents.
    """
    from src.processor.matter_html_processor import ProcessMatterHtmlDoc

    # Preflight: verify beautifulsoup4 is available before processing any HTML
    try:
        import bs4  # noqa: F401
    except ImportError:
        raise PipelineFatalError(
            "beautifulsoup4 is not installed — required to parse Matter spec HTML. "
            "Run: pip install beautifulsoup4 lxml"
        )

    config = state["config"]
    processor = DocumentProcessor(".ignore_rules.json")
    convert_adoc = getattr(config.pipeline, "convert_adoc_to_html", False)

    # Capture original matter_diff HTML docs BEFORE expansion — needed by build_matter_schema_node
    spec_diff_html_originals: List[FetchedDocument] = []

    def _expand_matter_html(docs: List[FetchedDocument]) -> List[FetchedDocument]:
        """Replace Matter diff HTML docs with their extracted diff sections.

        Also appends each original HTML doc to ``spec_diff_html_originals`` so
        the schema extractor node can parse entity tables from the full HTML.
        """
        # CLI --cluster flag (or pipeline.run cluster_filter) takes priority over
        # per-doc matter_diff_cluster metadata.
        global_cluster_filter = state.get("cluster_filter") or ""

        expanded: List[FetchedDocument] = []
        for doc in docs:
            if doc.extension in (".html", ".htm") and doc.metadata.get("matter_diff"):
                spec_diff_html_originals.append(doc)
                cluster_filter  = global_cluster_filter or doc.metadata.get("matter_diff_cluster", "")
                section_filter  = doc.metadata.get("matter_diff_section", "")
                html_proc = ProcessMatterHtmlDoc(
                    cluster_filter=cluster_filter,
                    section_filter=section_filter,
                )
                sections = html_proc.process(doc)
                if sections:
                    logger.info(
                        "[process_documents_node] Expanded Matter HTML diff %s → %d sections%s",
                        doc.path, len(sections),
                        f" (cluster_filter={cluster_filter!r})" if cluster_filter else "",
                    )
                    expanded.extend(sections)
                else:
                    logger.warning(
                        "[process_documents_node] No diff sections found in %s%s — keeping as-is",
                        doc.path,
                        f" for cluster {cluster_filter!r}" if cluster_filter else "",
                    )
                    expanded.append(doc)
            else:
                expanded.append(doc)
        return expanded

    def _process_list(docs: List[FetchedDocument]) -> List[FetchedDocument]:
        processed = [
            processor.process(d, d.metadata.get("_process_rules", []))
            for d in docs
        ]
        if convert_adoc:
            processed = [_maybe_convert_adoc_to_html(d) for d in processed]
        # Expand Matter HTML diff docs last (after text cleaning)
        processed = _expand_matter_html(processed)
        return processed

    pr_docs = _process_list(state.get("pr_documents", []))
    test_docs = _process_list(state.get("test_plan_fetched", []))
    spec_docs = _process_list(state.get("spec_fetched", []))

    # ---- Write Matter diff inspection file to the run log directory ----
    _write_matter_diff_inspection(pr_docs + spec_docs, state.get("run_dir", ""))

    logger.info(
        "[process_documents_node] Processed: PR=%d  test_plan=%d  spec=%d  (convert_adoc_to_html=%s)",
        len(pr_docs), len(test_docs), len(spec_docs), convert_adoc,
    )
    return {
        **state,
        "pr_documents": pr_docs,
        "test_plan_fetched": test_docs,
        "spec_fetched": spec_docs,
        "spec_diff_html": spec_diff_html_originals,
    }



@log_node
def ingest_data_model_node(state: PipelineState) -> PipelineState:
    """Node 2c: Ingest Matter DM XML schema into the knowledge graph.

    Reads ``data_model_fetched`` documents (role="data_model" from sources.json)
    and ingests them into the knowledge graph as canonical schema nodes:
    CLUSTER / ATTRIBUTE / COMMAND / EVENT / FEATURE.

    This node is a **build-once** step: it only calls ``add_data_model_documents()``
    when ``build_data_model=True`` (explicit flag) or when the KG store file does
    not yet exist on disk (first-run auto-build).

    When neither condition is met the node is a no-op — the existing persisted
    KG already contains the schema nodes from the previous build run.

    The knowledge graph object is NOT populated here; that happens in
    ``build_knowledge_graph_node``.  This node only writes a structured JSON
    snapshot of the data-model for inspection::

        <run_dir>/data_model_schema.json

    and stores the pre-processed list back in state so
    ``build_knowledge_graph_node`` can call ``add_data_model_documents()`` on it.
    """
    import json as _json

    config = state["config"]
    data_model_docs: List[FetchedDocument] = list(state.get("data_model_fetched", []))
    run_dir = state.get("run_dir", "")

    # Scan additional DM XML directories if not already loaded by fetch_documents_node.
    # This handles the case where dm_dirs_additional is set but no matter_xml source
    # entry exists in sources.json for those directories.
    if hasattr(config, 'analysis'):
        dm_dirs_additional = getattr(config.analysis, 'dm_dirs_additional', []) or []
        # Collect paths already loaded to avoid double-loading
        already_loaded_paths = {
            doc.metadata.get("absolute_path", "") for doc in data_model_docs
        }
        for dm_dir in dm_dirs_additional:
            if dm_dir and Path(dm_dir).is_dir():
                try:
                    from src.fetcher.sources.zap_xml_adapter import (
                        convert_zap_to_dm_xml,
                        is_zap_format,
                    )
                    dm_path = Path(dm_dir)
                    zap_files = []
                    has_standard = False
                    for xml_file in dm_path.rglob("*.xml"):
                        if is_zap_format(xml_file):
                            zap_files.append(xml_file)
                        else:
                            has_standard = True

                    # Standard DM XML files via MatterXMLFetcher
                    if has_standard:
                        synthetic_src = {
                            "id": f"additional_dm_{dm_path.name}",
                            "type": "matter_xml",
                            "role": "data_model",
                            "path": dm_dir,
                        }
                        fetcher = create_fetcher(synthetic_src, config)
                        docs = fetcher.fetch()
                        new_docs = [
                            d for d in docs
                            if d.metadata.get("absolute_path", "") not in already_loaded_paths
                        ]
                        if new_docs:
                            data_model_docs.extend(new_docs)
                            already_loaded_paths.update(
                                d.metadata.get("absolute_path", "") for d in new_docs
                            )
                            logger.info(
                                "[ingest_data_model_node] Loaded %d standard DM XML docs from %s",
                                len(new_docs), dm_dir,
                            )

                    # ZAP-format files via adapter conversion
                    if zap_files:
                        cluster_name_lookup: Dict[int, str] = {}
                        for doc in data_model_docs:
                            s = doc.metadata.get("schema", {})
                            cid_str = s.get("cluster_id", "")
                            cname = s.get("cluster_name", "")
                            if cid_str and cname:
                                try:
                                    cid_int = int(cid_str, 16) if cid_str.startswith("0x") else int(cid_str)
                                    cluster_name_lookup[cid_int] = cname
                                except (ValueError, TypeError):
                                    pass

                        for zap_file in zap_files:
                            if str(zap_file.resolve()) in already_loaded_paths:
                                continue
                            try:
                                dm_xml_str = convert_zap_to_dm_xml(zap_file, cluster_name_lookup)
                                if not dm_xml_str:
                                    continue
                                zap_docs = _parse_dm_xml_string(dm_xml_str, zap_file, dm_path)
                                if zap_docs:
                                    data_model_docs.extend(zap_docs)
                                    already_loaded_paths.update(
                                        d.metadata.get("absolute_path", "") for d in zap_docs
                                    )
                                    logger.info(
                                        "[ingest_data_model_node] Converted ZAP file %s → %d cluster docs",
                                        zap_file.name, len(zap_docs),
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "[ingest_data_model_node] ZAP conversion failed for %s: %s",
                                    zap_file, exc,
                                )
                except Exception as exc:
                    logger.warning(
                        "[ingest_data_model_node] Failed to load additional DM dir '%s': %s",
                        dm_dir, exc,
                    )

    if not data_model_docs:
        logger.info("[ingest_data_model_node] No data_model documents — skipping.")
        return state

    # Write inspection JSON for the loaded schemas
    schemas = []
    for doc in data_model_docs:
        s = doc.metadata.get("schema")
        if s:
            schemas.append({
                "cluster_name": s.get("cluster_name", ""),
                "cluster_id":   s.get("cluster_id", ""),
                "revision":     s.get("revision", ""),
                "attr_count":   len(s.get("attributes", [])),
                "cmd_count":    len(s.get("commands", [])),
                "evt_count":    len(s.get("events", [])),
                "feat_count":   len(s.get("features", [])),
            })

    if schemas:
        out_dir = Path(run_dir) if run_dir else Path("logs")
        out_path = out_dir / "data_model_schema.json"
        try:
            out_path.write_text(
                _json.dumps({"total_clusters": len(schemas), "clusters": schemas},
                            indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(
                "[ingest_data_model_node] Data model snapshot → %s  (%d clusters)",
                out_path, len(schemas),
            )
        except Exception as exc:
            logger.warning("[ingest_data_model_node] Could not write data_model_schema.json: %s", exc)

    logger.info("[ingest_data_model_node] %d data model document(s) ready for KG build.",
                len(data_model_docs))
    return {**state, "data_model_fetched": data_model_docs}


@log_node
def build_matter_schema_node(state: PipelineState) -> PipelineState:
    """Node 2b: Build canonical Matter data-model schema from spec diff HTML.

    Reads the original ``appclusters_diff.html`` document(s) captured by
    ``process_documents_node`` (stored in ``spec_diff_html`` before they were
    expanded into annotated-text sections) and extracts structured entity tables:

    - **Attributes** — one entry per attribute row with diff_status
    - **Commands**   — one entry per command row with diff_status
    - **Events**     — one entry per event row with diff_status
    - **Features**   — one entry per feature-map row with diff_status

    Output::

        matter_schema = {
          "clusters": [
            {
              "name": "On/Off",
              "diff_status": "changed",
              "attributes": [{"id": "0x0000", "name": "OnOff", "diff_status": "unchanged", ...}, ...],
              "commands": [...],
              "events": [...],
              "features": [...]
            }
          ]
        }

    The schema is written to ``<run_dir>/matter_schema.json`` for inspection and
    stored in ``state["matter_schema"]`` for downstream nodes.

    If no matter_diff HTML documents are present (e.g. the run only uses a GitHub PR
    without a spec diff HTML), this node is a no-op and returns an empty schema.
    """
    import json as _json
    from src.processor.matter_schema_extractor import MatterSchemaExtractor

    spec_diff_docs: List[FetchedDocument] = state.get("spec_diff_html", [])
    run_dir = state.get("run_dir", "")

    if not spec_diff_docs:
        logger.info(
            "[build_matter_schema_node] No spec diff HTML documents — skipping schema extraction."
        )
        return {**state, "matter_schema": {"clusters": []}}

    extractor = MatterSchemaExtractor()
    merged_clusters: List[Dict] = []

    for doc in spec_diff_docs:
        logger.info(
            "[build_matter_schema_node] Extracting schema from %s (%d chars)",
            doc.path, len(doc.content),
        )
        schema = extractor.extract(doc.content)
        merged_clusters.extend(schema.get("clusters", []))

    matter_schema: Dict[str, Any] = {"clusters": merged_clusters}

    # Deduplicate clusters by name (keep last occurrence, which wins any merge conflict)
    seen: Dict[str, Dict] = {}
    _entity_keys = ("attributes", "commands", "events", "features")
    for cluster in merged_clusters:
        name = cluster.get("name", "")
        if name in seen:
            for key in _entity_keys:
                seen[name][key].extend(cluster.get(key, []))
        else:
            seen[name] = {**cluster, **{k: list(cluster.get(k, [])) for k in _entity_keys}}
    matter_schema = {"clusters": list(seen.values())}

    total_entities = sum(
        len(c.get("attributes", [])) + len(c.get("commands", [])) +
        len(c.get("events", [])) + len(c.get("features", []))
        for c in matter_schema["clusters"]
    )
    logger.info(
        "[build_matter_schema_node] Schema: %d cluster(s), %d total entity rows",
        len(matter_schema["clusters"]), total_entities,
    )

    # Write inspection file
    if run_dir:
        out_path = Path(run_dir) / "matter_schema.json"
    else:
        Path("logs").mkdir(exist_ok=True)
        out_path = Path("logs") / "matter_schema.json"

    try:
        out_path.write_text(
            _json.dumps(matter_schema, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("[build_matter_schema_node] Matter schema written → %s", out_path)
    except Exception as exc:
        logger.warning("[build_matter_schema_node] Could not write matter_schema.json: %s", exc)

    return {**state, "matter_schema": matter_schema}



@log_node
def chunk_embed_test_plans_node(state: PipelineState) -> PipelineState:
    """Node 3: Chunk test plan docs and build or load the vector store.

    Build path (when ``build_test_plan_vectors=True`` or no index on disk):
      1. Chunk ``test_plan_fetched`` docs → ``test_plan_chunks``
      2. Embed chunks with BGE model
      3. ``store.add_documents()`` + ``store.save()`` to disk

    Load path (existing index, flags both False):
      1. Chunk ``test_plan_fetched`` docs → ``test_plan_chunks`` (cheap; needed for KG)
      2. ``store.load()`` — restores FAISS index from disk (fast)

    The chunking step always runs so ``test_plan_chunks`` is populated regardless
    of whether the vector DB is rebuilt.  Embedding is the expensive part.
    """
    config = state["config"]
    loader = DocumentLoader(config.loader, getattr(config, "chunker", None))

    # Always chunk test plans (needed for KG build even when not rebuilding vector DB)
    test_plan_chunks = loader.load_all(state.get("test_plan_fetched", []))
    for chunk in test_plan_chunks:
        chunk.metadata["doc_type"] = "test_plan"

    logger.info("[chunk_embed_test_plans_node] Test plan chunks: %d", len(test_plan_chunks))

    store = create_vector_store(config.database)
    backend = getattr(config.database, "backend", "faiss")

    # Determine whether we need to build the vector DB
    build_flag = (
        state.get("build_test_plan_vectors", False)
        or config.pipeline.build_test_plan_vectors
        or config.pipeline.rebuild_index  # backward compat
    )
    # Auto-build if FAISS index file is absent (first run)
    if not build_flag and backend == "faiss":
        index_path = Path(getattr(config.database, "faiss_index_path",
                                  "data/faiss_index/matter.index"))
        if not index_path.exists():
            logger.info(
                "[chunk_embed_test_plans_node] No FAISS index at %s — building.", index_path
            )
            build_flag = True

    if build_flag:
        # Use KnowledgeBaseBuilder to generate structured, TC-aware vector chunks.
        # These are richer than raw document loader chunks: each TestCaseRecord produces
        # up to 4 chunk types (full / intent_summary / procedure / setup) with tc_id,
        # cluster, intents, and entity_refs as FAISS sidecar metadata.
        from src.knowledge_graph.knowledge_base import KnowledgeBaseBuilder

        kb_builder = KnowledgeBaseBuilder()
        kb = kb_builder.build(
            data_model_docs=state.get("data_model_fetched") or None,
            spec_docs=state.get("spec_fetched") or None,
            test_plan_docs=state.get("test_plan_fetched") or None,
            output_dir=state.get("run_dir", ""),
            max_workers=config.knowledge_graph.spec_extractor_workers,
        )

        # Convert VectorChunkRecord → Document for FAISS
        rich_chunks: List[Document] = []
        for vc in kb.vector_chunks:
            meta: dict = dict(vc.metadata)
            meta["doc_type"] = "test_plan"
            meta["chunk_type"] = (
                vc.chunk_type.value if hasattr(vc.chunk_type, "value") else str(vc.chunk_type)
            )
            meta["chunk_id"] = vc.chunk_id
            meta["tc_id"] = vc.tc_id
            rich_chunks.append(Document(page_content=vc.text, metadata=meta))

        if rich_chunks:
            test_plan_chunks = rich_chunks
            logger.info(
                "[chunk_embed_test_plans_node] KB pipeline produced %d rich TC chunks "
                "(full/intent_summary/procedure/setup per TC), replacing raw loader chunks",
                len(test_plan_chunks),
            )
        else:
            logger.warning(
                "[chunk_embed_test_plans_node] KB pipeline produced no vector chunks — "
                "falling back to raw document loader chunks (%d)",
                len(test_plan_chunks),
            )
            kb = None  # don't cache a useless kb

        if test_plan_chunks:
            embedder = EmbeddingsModule(config.embeddings)
            logger.info("[chunk_embed_test_plans_node] Embedding %d chunks...",
                        len(test_plan_chunks))
            embeddings = embedder.embed_documents(test_plan_chunks)
            store.add_documents(test_plan_chunks, embeddings)
            store.save()
            logger.info("[chunk_embed_test_plans_node] Vector DB saved (%d vectors)",
                        len(test_plan_chunks))
        else:
            logger.warning(
                "[chunk_embed_test_plans_node] build_test_plan_vectors=True but no test plan"
                " docs available."
            )
        # Cache the KnowledgeBase in state so build_knowledge_graph_node can reuse it
        # without running KnowledgeBaseBuilder.build() a second time.
        return {**state, "test_plan_chunks": test_plan_chunks, "vector_store": store,
                "built_knowledge_base": kb}

    # Load path: warm run — restore existing FAISS index from disk
    logger.info("[chunk_embed_test_plans_node] Loading existing vector DB...")
    try:
        store.load()
    except FileNotFoundError:
        from src.logging_config import PipelineFatalError
        raise PipelineFatalError(
            f"No FAISS index found at {config.database.faiss_index_path}. "
            "Run with --build-test-plan-vectors first to create it."
        )
    logger.info("[chunk_embed_test_plans_node] Loaded vector DB (%d vectors)", store.size)
    return {**state, "test_plan_chunks": test_plan_chunks, "vector_store": store}



@log_node
def chunk_pr_node(state: PipelineState) -> PipelineState:
    """Node 4: Chunk PR change documents using semantic chunking.

    Uses ``SemanticPRChunker`` to split PR documents into coherent change units
    (by cluster / section / change type) rather than arbitrary token windows.

    Also chunks Matter spec documents from ``spec_fetched`` for use in the KG.

    Produces:
      - ``pr_chunks``   — semantically chunked PR changes with ``doc_type="pr_change"``
      - ``spec_chunks`` — Matter spec chunks with ``doc_type="spec"``

    Short-circuits when ``pr_chunks`` is already present in state (e.g. injected
    via ``--pr-snippet``), leaving existing chunks untouched.
    """
    if state.get("pr_chunks"):
        # Still need to chunk spec docs even when pr_chunks are pre-injected
        from src.processor.semantic_chunker import SemanticPRChunker
        spec_docs = state.get("spec_fetched") or []
        spec_chunks = []
        if spec_docs:
            _chunker = SemanticPRChunker()
            for doc in spec_docs:
                spec_chunks.extend(_chunker.chunk(doc))
        _update_pipeline_progress(state, "chunk_pr", pr_chunks=len(state["pr_chunks"]))
        return {"spec_chunks": spec_chunks}

    from src.processor.semantic_chunker import SemanticPRChunker

    config = state["config"]
    run_dir = state.get("run_dir", "")
    loader = DocumentLoader(config.loader, getattr(config, "chunker", None))
    chunker = SemanticPRChunker()

    pr_docs = state.get("pr_documents", [])
    pr_chunks: List[Document] = []
    if pr_docs:
        semantic_chunks = chunker.chunk_all_with_log(
            pr_docs, output_dir=run_dir, label="pr_chunks"
        )
        for i, sc in enumerate(semantic_chunks):
            d = sc.to_document(chunk_index=i)
            d.metadata["doc_type"] = "pr_change"
            # Preserve original doc metadata (path, pr_url, status, etc.) not yet in chunk
            for k, v in sc.metadata.items():
                if k not in d.metadata:
                    d.metadata[k] = v
            # Propagate source file path so prompt shows a meaningful filename
            if "path" not in d.metadata:
                d.metadata["path"] = sc.source_path or d.metadata.get("source_html", "")
            pr_chunks.append(d)

    # If semantic chunker produced nothing, fall back to document loader
    if not pr_chunks and pr_docs:
        pr_chunks = loader.load_all(pr_docs)
        for chunk in pr_chunks:
            chunk.metadata["doc_type"] = "pr_change"

    spec_chunks = loader.load_all(state.get("spec_fetched", []))
    for chunk in spec_chunks:
        chunk.metadata["doc_type"] = "spec"

    # Secondary cluster filter — safety net after chunking
    cluster_filter = state.get("cluster_filter", "")
    if cluster_filter:
        before = len(pr_chunks)
        pr_chunks = [c for c in pr_chunks if _chunk_matches_cluster(c, cluster_filter)]
        logger.info(
            "[chunk_pr_node] cluster_filter=%r: %d → %d PR chunks",
            cluster_filter, before, len(pr_chunks),
        )

    logger.info("[chunk_pr_node] PR chunks: %d  spec chunks: %d",
                len(pr_chunks), len(spec_chunks))
    _update_pipeline_progress(state, "chunk_pr", pr_chunks=len(pr_chunks))
    return {**state, "pr_chunks": pr_chunks, "spec_chunks": spec_chunks}



@log_node
def extract_pr_changes_node(state: PipelineState) -> PipelineState:
    """Node 4b: Extract structured change records from each PR chunk.

    For each PR chunk, produces a ``StructuredChange`` dict describing:
      - ``change_kind``  — type of change (ADD_ATTRIBUTE, MODIFY_COMMAND, …)
      - ``cluster``      — affected cluster
      - ``entities``     — list of {type, name, id} dicts
      - ``conditions``   — conformance / access changes
      - ``effects``      — downstream behavioral implications
      - ``old_value`` / ``new_value`` — for modifications
      - ``confidence``   — rule-based confidence score (0–1)
      - ``ambiguous``    — True when LLM fallback was needed

    Strategy:
      1. Rule-based extraction using regex + [ADDED/REMOVED/CHANGED] annotations
         (fast, no LLM cost).
      2. LLM fallback only when confidence < ``config.pipeline.llm_confidence_threshold``
         (defaults to 0.6).

    Writes ``<run_dir>/pr_changes.json`` for inspection.
    Results stored in ``state["pr_changes"]``.
    """
    import json as _json
    from src.processor.change_extractor import ChangeExtractor
    from src.knowledge_graph.rule_engine import extract_pr_requirements

    config = state["config"]
    run_dir = state.get("run_dir", "")
    pr_chunks: List[Document] = state.get("pr_chunks", [])

    if not pr_chunks:
        return {**state, "pr_changes": []}

    threshold = getattr(config.pipeline, "llm_confidence_threshold", 0.6)
    # Lazy-init LLM only if we need it (avoids loading model for high-confidence chunks)
    _llm = None

    def _get_llm():
        nonlocal _llm
        if _llm is None:
            _llm = _get_run_llm(config, run_dir)
        return _llm

    extractor_no_llm = ChangeExtractor(llm_provider=None, confidence_threshold=threshold)

    # Try to obtain canonical_schema for entity-aware requirement extraction.
    # Available when chunk_embed_test_plans_node ran this session (build path).
    _kb = state.get("built_knowledge_base")
    _canonical_schema = getattr(_kb, "canonical_schema", None) if _kb else None

    pr_changes: List[Dict] = []
    pr_requirements: List[Dict] = []
    llm_calls = 0

    for idx, chunk in enumerate(pr_chunks):
        meta = chunk.metadata
        change = extractor_no_llm.extract(
            text=chunk.page_content,
            cluster_hint=meta.get("cluster", ""),
            section_hint=meta.get("section", ""),
            change_types_hint=meta.get("change_types", []),
        )
        method = "rule-based"
        if change.ambiguous and change.confidence < threshold:
            # Use LLM fallback
            try:
                llm_extractor = ChangeExtractor(llm_provider=_get_llm(),
                                                confidence_threshold=threshold)
                change = llm_extractor.extract(
                    text=chunk.page_content,
                    cluster_hint=meta.get("cluster", ""),
                    section_hint=meta.get("section", ""),
                    change_types_hint=meta.get("change_types", []),
                )
                llm_calls += 1
                method = "llm-fallback"
            except Exception as exc:
                logger.warning("[extract_pr_changes_node] LLM fallback failed: %s", exc)

        record = change.to_dict()
        record["pr_chunk_index"] = idx  # always use enumerate index, not metadata
        record["pr_path"] = meta.get("path", meta.get("source_path", ""))
        pr_changes.append(record)

        # ── Behavioural requirement extraction (parallel track) ──────────────
        # Extracts normative sentences that ChangeExtractor ignores — e.g.
        # timing rules ("shall terminate after 900 s"), conditional behaviour
        # ("if Occupancy == false, event shall not be sent"), etc.
        try:
            chunk_idx = meta.get("chunk_index", len(pr_changes) - 1)
            req_records = extract_pr_requirements(
                text=chunk.page_content,
                canonical_schema=_canonical_schema,
                source_chunk_idx=chunk_idx,
            )
            for rr in req_records:
                d = rr.to_dict()
                d["pr_path"] = record["pr_path"]
                pr_requirements.append(d)
        except Exception as exc:
            logger.warning("[extract_pr_changes_node] pr_requirements extraction failed for chunk: %s", exc)

        # Per-chunk trace
        entities_str = ", ".join(
            e.get("name", "?") for e in record.get("entities", [])[:3]
        ) or "—"
        logger.info(
            "[extract_pr_changes_node] chunk %d/%d | %s | cluster=%r kind=%s "
            "confidence=%.2f entities=[%s] method=%s",
            len(pr_changes), len(pr_chunks),
            meta.get("path", "?"),
            record.get("cluster", ""),
            record.get("change_kind", "UNKNOWN"),
            record.get("confidence", 0.0),
            entities_str,
            method,
        )

    logger.info(
        "[extract_pr_changes_node] Extracted %d change records (%d via LLM fallback), "
        "%d behavioural requirement sentences",
        len(pr_changes), llm_calls, len(pr_requirements),
    )

    # Write inspection file
    if run_dir:  # write inspection file when run_dir is available
        out_dir = Path(run_dir) if run_dir else Path("logs")
        out_path = out_dir / "pr_changes.json"
        try:
            out_path.write_text(
                _json.dumps({"total": len(pr_changes), "changes": pr_changes},
                            indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("[extract_pr_changes_node] PR change records → %s", out_path)
        except Exception as exc:
            logger.warning("[extract_pr_changes_node] Could not write pr_changes.json: %s", exc)

    return {**state, "pr_changes": pr_changes, "pr_requirements": pr_requirements}



@log_node
def build_knowledge_graph_node(state: PipelineState) -> PipelineState:
    """Node 5: Build or load the unified knowledge graph.

    Build path (when ``build_knowledge_graph=True`` or no KG file on disk):
      Sub-graph 1 — Matter specification:   REQUIREMENT/SECTION nodes from ``spec_chunks``
      Sub-graph 2 — Test plans:             TEST_CASE/SECTION nodes from ``test_plan_chunks``
      Both sub-graphs are merged and saved to ``graph_store_path``.
      PR_CHANGE nodes are then added from ``pr_chunks`` (transient — not saved).

    Load path (existing KG file, flag False):
      Load the persisted spec+test-plan graph from ``graph_store_path``.
      PR_CHANGE nodes are always added fresh from the current PR.
    """
    config = state["config"]
    run_dir = state.get("run_dir", "")
    graph_store_path = Path(
        getattr(config.knowledge_graph, "graph_store_path",
                "data/knowledge_graph/matter_kg.json")
    )
    kg_backend = getattr(config.knowledge_graph, "backend", "local")

    build_flag = (
        state.get("build_knowledge_graph", False)
        or config.pipeline.build_knowledge_graph
    )
    # Auto-build if KG file is absent (first run) — only for local backend
    if not build_flag and kg_backend == "local" and not graph_store_path.exists():
        logger.info(
            "[build_knowledge_graph_node] No KG file at %s — building.", graph_store_path
        )
        build_flag = True

    # When build_flag is explicitly set, delete existing KG file to force fresh build
    # (prevents stale cached data from being reused via load_from_json)
    if build_flag and kg_backend == "local" and graph_store_path.exists():
        logger.info(
            "[build_knowledge_graph_node] --build-knowledge-graph set — removing stale KG at %s",
            graph_store_path,
        )
        graph_store_path.unlink()

    kg = create_knowledge_graph(config.knowledge_graph)

    test_plan_chunks: List[Document] = state.get("test_plan_chunks", [])
    spec_chunks: List[Document] = state.get("spec_chunks", [])
    pr_chunks: List[Document] = state.get("pr_chunks", [])
    data_model_docs: List[FetchedDocument] = state.get("data_model_fetched", [])

    if build_flag:
        # Reuse the KnowledgeBase already built in chunk_embed_test_plans_node when
        # both build flags were True (avoids running KnowledgeBaseBuilder.build() twice).
        cached_kb = state.get("built_knowledge_base")
        if cached_kb is not None:
            logger.info(
                "[build_knowledge_graph_node] Reusing KnowledgeBase cached from "
                "chunk_embed_test_plans_node (data_model=%d  spec=%d  test_cases=%d  "
                "graph_nodes=%d  vector_chunks=%d)",
                len(cached_kb.canonical_schema.clusters),
                len(cached_kb.spec_records),
                len(cached_kb.test_case_records),
                len(cached_kb.graph.nodes),
                len(cached_kb.vector_chunks),
            )
            kb = cached_kb
        else:
            logger.info(
                "[build_knowledge_graph_node] Building knowledge graph via new KB pipeline "
                "(data_model=%d  spec=%d  test_plan=%d)…",
                len(data_model_docs), len(state.get("spec_fetched", [])), len(state.get("test_plan_fetched", [])),
            )
            from src.knowledge_graph.knowledge_base import KnowledgeBaseBuilder
            spec_fetched_docs: List[FetchedDocument] = state.get("spec_fetched", [])
            test_plan_fetched_docs: List[FetchedDocument] = state.get("test_plan_fetched", [])
            kb_builder = KnowledgeBaseBuilder()
            kb = kb_builder.build(
                data_model_docs=data_model_docs or None,
                spec_docs=spec_fetched_docs or None,
                test_plan_docs=test_plan_fetched_docs or None,
                output_dir=run_dir,
                max_workers=config.knowledge_graph.spec_extractor_workers,
            )

        # Bridge: import typed GraphBundle into MatterKGBuilder so the existing
        # FastAPI app, search logic, and export/load code continue working unchanged.
        _import_graph_bundle(kg, kb.graph)
        logger.info(
            "[build_knowledge_graph_node] GraphBundle imported — %d nodes, %d edges",
            kg.num_nodes, kg.num_edges,
        )

        # ── Save base KG to disk BEFORE LLM refinement ──────────────────────
        # Writing now ensures data_model_kg.json / spec_kg.json / test_plan_kg.json
        # are available even if the LLM refinement step is killed mid-run.
        if kb.graph.nodes:
            # Consolidate configured spec sections into self-contained PROMPT_SECTION
            # nodes so prompt-build time is O(node_type_filter) instead of O(regex_scan).
            _ps_configs = getattr(config.knowledge_graph, "prompt_sections", None) or []
            ps_count = kg.build_prompt_sections(section_configs=_ps_configs or None)
            if ps_count:
                logger.info(
                    "[build_knowledge_graph_node] built %d PROMPT_SECTION nodes for system prompt",
                    ps_count,
                )
            graph_store_path.parent.mkdir(parents=True, exist_ok=True)
            kg.export_json(str(graph_store_path))
            logger.info(
                "[build_knowledge_graph_node] Base KG saved to %s (%d nodes, %d edges)",
                graph_store_path, kg.num_nodes, kg.num_edges,
            )
            for _src in ("data_model", "spec", "test_plan"):
                _comp_path = graph_store_path.parent / f"{_src}_kg.json"
                try:
                    kg.export_component(str(_comp_path), source_filter=_src)
                    logger.info(
                        "[build_knowledge_graph_node] Saved %s", _comp_path.name,
                    )
                except Exception as _e:
                    logger.warning(
                        "[build_knowledge_graph_node] Could not export %s component: %s", _src, _e
                    )

        # ── Optional LLM-assisted spec refinement ───────────────────────────
        # Runs AFTER base files are on disk so a killed LLM run is recoverable.
        llm_refine_flag = (
            state.get("build_knowledge_graph_with_llm", False)
            or getattr(config.knowledge_graph, "llm_refinement_enabled", False)
        )
        if llm_refine_flag:
            try:
                from src.knowledge_graph.llm_spec_refiner import LLMSpecRefiner
                # Build the LLM for refinement — use a provider-specific override when
                # knowledge_graph.llm_refinement_provider is set so users can run the
                # (potentially hundreds of) refinement calls on a cheap local model
                # instead of a frontier API model.
                _kg_cfg = config.knowledge_graph
                _refine_provider = getattr(_kg_cfg, "llm_refinement_provider", "")
                if _refine_provider:
                    import copy
                    _refine_llm_cfg = copy.copy(config.llm)
                    _refine_llm_cfg.provider = _refine_provider
                    _local_model = getattr(_kg_cfg, "llm_refinement_local_model", "")
                    if _local_model:
                        _refine_llm_cfg.local_model = _local_model
                    logger.info(
                        "[build_knowledge_graph_node] LLM refinement using override provider=%r model=%r",
                        _refine_provider, _local_model or _refine_llm_cfg.local_model,
                    )
                    _refine_llm = get_llm(_refine_llm_cfg)
                else:
                    _refine_llm = _get_run_llm(config, run_dir)
                refiner = LLMSpecRefiner(
                    llm=_refine_llm,
                    canonical_schema=kb.canonical_schema,
                    cache_path=Path(getattr(
                        config.knowledge_graph,
                        "llm_refinement_cache_path",
                        "data/knowledge_graph/spec_refiner_cache.json",
                    )),
                    max_sections=getattr(
                        config.knowledge_graph, "llm_refinement_max_sections", 200
                    ),
                )
                extra_edges = refiner.refine(kb.spec_records, kb.section_records)
                if extra_edges:
                    added = 0
                    for edge_rec in extra_edges:
                        src_id = edge_rec.source
                        tgt_id = edge_rec.target
                        if (
                            kg._graph.has_node(src_id)
                            and kg._graph.has_node(tgt_id)
                            and not kg._graph.has_edge(src_id, tgt_id)
                        ):
                            kg._graph.add_edge(
                                src_id,
                                tgt_id,
                                edge_type=edge_rec.edge_type,
                                source="spec_llm",
                                **{k: v for k, v in edge_rec.properties.items()
                                   if k not in ("edge_type", "source")},
                            )
                            added += 1
                    logger.info(
                        "[build_knowledge_graph_node] LLM refinement added %d new edges "
                        "(%d total from refiner, %d duplicates/missing-node skipped)",
                        added, len(extra_edges), len(extra_edges) - added,
                    )
                    # Save spec_llm component + re-save master KG with LLM edges included.
                    _llm_comp_path = graph_store_path.parent / "spec_llm_kg.json"
                    try:
                        kg.export_component(str(_llm_comp_path), source_filter="spec_llm")
                        logger.info(
                            "[build_knowledge_graph_node] Saved %s", _llm_comp_path.name,
                        )
                    except Exception as _e:
                        logger.warning(
                            "[build_knowledge_graph_node] Could not export spec_llm component: %s",
                            _e,
                        )
                    kg.export_json(str(graph_store_path))
                    logger.info(
                        "[build_knowledge_graph_node] Re-saved master KG with LLM edges — "
                        "%d nodes, %d edges",
                        kg.num_nodes, kg.num_edges,
                    )
            except Exception as _llm_exc:
                logger.warning(
                    "[build_knowledge_graph_node] LLM spec refinement failed (skipped): %s",
                    _llm_exc,
                )

        # Add PR_CHANGE nodes after saving (transient, not persisted)
        if pr_chunks:
            kg.add_pr_documents(pr_chunks)
            kg.extract_matter_entities(pr_chunks)

    else:
        # Load path — prefer component files (preserve cross-graph edges) over master JSON.
        # Canonical order: data_model → spec → test_plan → spec_llm (LLM edges last).
        # spec_llm_kg.json is optional — present only when --build-knowledge-graph-withLLM
        # has been run. Load whatever component files exist; fall back to master JSON only
        # if none of the component files are present.
        _candidate_components = [
            graph_store_path.parent / f"{_src}_kg.json"
            for _src in ("data_model", "spec", "test_plan", "spec_llm")
        ]
        _available_components = [p for p in _candidate_components if p.exists()]
        if _available_components:
            logger.info(
                "[build_knowledge_graph_node] Loading KG from %d component file(s): %s",
                len(_available_components),
                ", ".join(p.name for p in _available_components),
            )
            kg.load_from_components([str(p) for p in _available_components])
        else:
            logger.info(
                "[build_knowledge_graph_node] Loading existing KG from %s", graph_store_path
            )
            kg.load_from_json(str(graph_store_path))

        # Always add current PR changes as transient nodes
        if pr_chunks:
            kg.add_pr_documents(pr_chunks)
            kg.extract_matter_entities(pr_chunks)

    logger.info(
        "[build_knowledge_graph_node] KG: %d nodes, %d edges",
        kg.num_nodes, kg.num_edges,
    )
    _update_pipeline_progress(state, "build_knowledge_graph")
    return {**state, "knowledge_graph": kg}



@log_node
def search_test_plan_vector_db_node(state: PipelineState) -> PipelineState:
    """Node 6: For each PR chunk, retrieve the most relevant test case chunks via vector search."""
    config = state["config"]
    store: VectorStore = state.get("vector_store")
    if store is None or store.is_empty:
        logger.warning(
            "[search_test_plan_vector_db_node] Vector store is empty — skipping search."
        )
        return {**state, "search_results": {}}

    embedder = EmbeddingsModule(config.embeddings)
    searcher = FAISSSearch(store, embedder)
    pr_chunks: List[Document] = state.get("pr_chunks", [])

    search_results: Dict[str, List[SearchResult]] = {}
    for i, chunk in enumerate(pr_chunks):
        key = f"pr_{i}"
        # Prefer a per-request threshold override (e.g. the chat adapter lowers
        # it for conversational queries); fall back to the pipeline config default.
        threshold = state.get(
            "similarity_threshold", config.pipeline.similarity_threshold
        )
        results = searcher.search(
            chunk.page_content,
            k=config.pipeline.search_top_k,
            threshold=threshold,
        )
        search_results[key] = results

        top_score = max((r.score for r in results), default=0.0)
        logger.info(
            "[search_test_plan_vector_db_node] chunk %d/%d | %s → %d hits (top score: %.3f)",
            i + 1, len(pr_chunks),
            chunk.metadata.get("path", key),
            len(results),
            top_score,
        )
        _vector_search_logger.debug(
            "[vector] chunk %d/%d | query=%r | %d hits",
            i + 1, len(pr_chunks),
            chunk.page_content[:120],
            len(results),
        )
        for r in results:
            _vector_search_logger.debug(
                "  [rank %d] score=%.3f  tc_id=%-20s  cluster=%s",
                r.rank, r.score,
                r.metadata.get("tc_id", "?"),
                r.metadata.get("cluster_name", r.metadata.get("cluster", "?")),
            )

    logger.info("[search_test_plan_vector_db_node] Searched %d PR chunks", len(pr_chunks))
    return {**state, "search_results": search_results}



@log_node
def search_knowledge_graph_node(state: PipelineState) -> PipelineState:
    """Node 7: For each PR chunk, query the knowledge graph for related test cases.

    Uses structured change records (from ``extract_pr_changes_node``) for precise
    entity-based lookup when available, falling back to regex-based entity extraction.

    Coverage classification per chunk:
      - **direct**   — TEST_CASE nodes directly linked to an affected schema entity
      - **indirect** — TEST_CASE nodes reachable within 2 hops
      - **missing**  — no TEST_CASE nodes found → coverage gap

    Results stored in ``graph_results`` keyed by ``pr_{i}``.
    """
    kg: BaseKnowledgeGraph = state.get("knowledge_graph")
    if kg is None:
        logger.warning(
            "[search_knowledge_graph_node] No knowledge graph in state — skipping."
        )
        return {**state, "graph_results": {}}

    config = state["config"]
    top_k = config.pipeline.search_top_k
    pr_chunks: List[Document] = state.get("pr_chunks", [])
    pr_changes: List[Dict] = state.get("pr_changes", [])
    pr_requirements: List[Dict] = state.get("pr_requirements", [])

    # Build a change record lookup by chunk index
    changes_by_idx: Dict[int, Dict] = {}
    for rec in pr_changes:
        idx = rec.get("pr_chunk_index", 0)
        changes_by_idx[idx] = rec

    # Build a requirement records lookup by chunk index (multiple per chunk)
    reqs_by_idx: Dict[int, List[Dict]] = {}
    for req in pr_requirements:
        idx = req.get("source_chunk_idx", -1)
        reqs_by_idx.setdefault(idx, []).append(req)

    graph_results: Dict[str, List[GraphNode]] = {}
    coverage_notes: Dict[str, str] = {}
    _chat_intent: str = ""
    for i, chunk in enumerate(pr_chunks):
        key = f"pr_{i}"
        change_rec = changes_by_idx.get(i)

        if change_rec and hasattr(kg, "search_by_structured_change"):
            # Precise lookup via structured change
            cluster = change_rec.get("cluster", "")
            entities = change_rec.get("entities", [])
            matches: List[GraphNode] = []
            for entity in entities[:3]:  # top 3 entities
                etype = entity.get("type", "")
                ename = entity.get("name", "")
                if etype and ename:
                    hits = kg.search_by_structured_change(cluster, etype, ename,
                                                          max_results=top_k)
                    for h in hits:
                        if h.node_id not in {m.node_id for m in matches}:
                            matches.append(h)
            # Fallback if structured search found nothing
            if not matches:
                # When entities are empty but cluster is known (e.g. MODIFY_BEHAVIOR
                # changes to spec prose with no specific attribute/command), retrieve
                # all test cases for that cluster so the LLM has relevant context.
                if cluster and hasattr(kg, "get_test_cases_for_cluster"):
                    matches = kg.get_test_cases_for_cluster(cluster)[:top_k]
                if not matches:
                    matches = kg.search_by_entities(chunk.page_content, max_results=top_k,
                                                    cluster_filter=cluster)
            graph_results[key] = matches[:top_k]

            entity_names = ", ".join(
                e.get("name", "?") for e in entities[:3]
            ) or "—"
            logger.info(
                "[search_knowledge_graph_node] chunk %d/%d | %s | "
                "cluster=%r entities=[%s] → %d KG matches (structured search)",
                i + 1, len(pr_chunks),
                chunk.metadata.get("path", key),
                cluster, entity_names, len(graph_results[key]),
            )
        else:
            # Chat path: LLM query planner → plan-driven KG dispatch.
            run_ctx = state.get("run_ctx")
            is_chat = run_ctx is not None and run_ctx.client == "app_chat"

            if is_chat:
                llm = _get_run_llm(config, state.get("run_dir", ""))
                plan = _plan_chat_query(chunk.page_content, llm)
                intent = plan["intent"]
                cluster_hint = plan.get("cluster")
                entity_type = plan.get("entity_type")
                entity_name = plan.get("entity_name")
                traverse = plan.get("traverse")
                keywords = plan.get("keywords") or _extract_keywords(chunk.page_content)
                coverage_note = ""

                logger.info(
                    "[search_knowledge_graph_node] chat plan: intent=%s cluster=%s "
                    "entity_type=%s entity_name=%s traverse=%s keywords=%s",
                    intent, cluster_hint, entity_type, entity_name, traverse, keywords,
                )
                _chat_intent = intent

                # ── list_test_cases: enumerate all TCs for a cluster ────────
                if (
                    intent == "list_test_cases"
                    and cluster_hint
                    and hasattr(kg, "get_test_cases_for_cluster")
                ):
                    all_tcs = kg.get_test_cases_for_cluster(cluster_hint)
                    graph_results[key] = all_tcs
                    coverage_note = (
                        f"✓ Found {len(all_tcs)} test case(s) for cluster "
                        f"'{cluster_hint}' (complete enumeration — no top-k limit)."
                    )

                # ── list_test_cases (no cluster): keyword scan over TC content ─
                elif (
                    intent == "list_test_cases"
                    and not cluster_hint
                    and hasattr(kg, "search_tc_by_keyword")
                ):
                    # Keywords are e.g. ["factory", "reset", "test", "cases"].
                    # Try multi-word phrase first (e.g. "factory reset"), then
                    # fall back to the first meaningful non-generic keyword.
                    _stop = {"test", "cases", "case", "tc", "use", "uses", "using", "which", "what"}
                    _kw_words = [w for w in keywords if w not in _stop]
                    kw_phrase = " ".join(_kw_words[:3]) if len(_kw_words) >= 2 else (_kw_words[0] if _kw_words else (keywords[0] if keywords else "matter"))
                    all_tcs = kg.search_tc_by_keyword(kw_phrase)
                    graph_results[key] = all_tcs
                    coverage_note = (
                        f"✓ Found {len(all_tcs)} test case(s) whose content contains "
                        f"'{kw_phrase}' (keyword scan — no top-k limit)."
                    )

                # ── entity_coverage: does this attribute/command/event have coverage? ─
                elif (
                    intent == "entity_coverage"
                    and entity_name
                    and hasattr(kg, "find_entity_coverage")
                ):
                    cov = kg.find_entity_coverage(
                        cluster_hint or "",
                        entity_type or "attribute",
                        entity_name,
                    )
                    if not cov["entity_exists"]:
                        coverage_note = (
                            f"⚠ Entity '{entity_name}' ({entity_type}) in "
                            f"'{cluster_hint}' does NOT exist in the knowledge graph "
                            f"— likely a new entity with no existing test coverage."
                        )
                        graph_results[key] = []
                    elif not cov["covered"]:
                        coverage_note = (
                            f"⚠ COVERAGE GAP: '{entity_name}' ({entity_type}) in "
                            f"'{cluster_hint}' exists but NO test case directly covers it."
                        )
                        graph_results[key] = (
                            [cov["entity_node"]] if cov["entity_node"] else []
                        )
                    else:
                        coverage_note = (
                            f"✓ '{entity_name}' ({entity_type}) in '{cluster_hint}' "
                            f"is covered by {len(cov['test_cases'])} test case(s)."
                        )
                        graph_results[key] = cov["test_cases"]

                # ── requirement_lookup: spec requirements for a cluster ─────
                elif (
                    intent == "requirement_lookup"
                    and hasattr(kg, "find_requirements_and_coverage")
                ):
                    cov_result = kg.find_requirements_and_coverage(
                        keywords,
                        cluster=cluster_hint,
                        requirement_types=None,
                        max_results_reqs=20,
                        max_results_tcs=top_k,
                    )
                    seen_ids: set = set()
                    result_nodes: List[GraphNode] = []
                    for req_id, tcs in cov_result["covered"].items():
                        for tc in tcs:
                            if tc.node_id not in seen_ids:
                                result_nodes.append(tc)
                                seen_ids.add(tc.node_id)
                    for req_node in cov_result["uncovered"][:5]:
                        if req_node.node_id not in seen_ids:
                            result_nodes.append(req_node)
                            seen_ids.add(req_node.node_id)
                    graph_results[key] = result_nodes[:top_k]
                    n_cov = len(cov_result["covered"])
                    n_unc = len(cov_result["uncovered"])
                    coverage_note = (
                        f"✓ {n_cov} requirement(s) covered, {n_unc} uncovered."
                        if n_cov > 0
                        else f"⚠ {n_unc} matching requirement(s) found but NONE have linked test cases."
                    )

                # ── graph_traversal: cluster dependency relationships ────────
                elif (
                    intent == "graph_traversal"
                    and cluster_hint
                    and hasattr(kg, "get_cluster_dependencies")
                ):
                    dep_nodes = kg.get_cluster_dependencies(cluster_hint, traverse or "incoming_depends_on")
                    graph_results[key] = dep_nodes[:top_k]
                    direction_label = (
                        "that depend on" if (traverse or "") == "incoming_depends_on"
                        else "that"
                    ) + f" '{cluster_hint}'"
                    coverage_note = f"Found {len(dep_nodes)} cluster(s) {direction_label}."

                # ── general_qa: keyword search fallback ────────────────────
                else:
                    graph_results[key] = kg.search_by_keywords(
                        keywords,
                        node_types=None,
                        requirement_types=None,
                        cluster_filter=cluster_hint,
                        max_results=top_k,
                    )

                coverage_notes[key] = coverage_note
            else:
                # ── CLI path: entity regex search + behavioural requirement coverage ─
                chunk_cluster = chunk.metadata.get("cluster", "")
                entity_hits: List[GraphNode] = kg.search_by_entities(
                    chunk.page_content, max_results=top_k,
                    cluster_filter=chunk_cluster,
                )

                # ── Behavioural requirement coverage (new) ───────────────────
                # For each normative requirement sentence in this chunk, look up
                # matching KG requirements + test case coverage.  Sentences that
                # have no KG match at all → fresh coverage notes as gaps.
                chunk_reqs = reqs_by_idx.get(i, [])
                req_hits: List[GraphNode] = []
                req_coverage_parts: List[str] = []
                if chunk_reqs and hasattr(kg, "find_requirements_and_coverage"):
                    seen_req_nodes: set = set()
                    for pr_req in chunk_reqs:
                        kw = pr_req.get("keywords", [])
                        cluster = pr_req.get("inferred_cluster")
                        req_type = pr_req.get("requirement_type")
                        req_text_preview = pr_req.get("text", "")[:80]
                        if not kw:
                            continue
                        cov = kg.find_requirements_and_coverage(
                            keywords=kw,
                            cluster=cluster,
                            requirement_types=[req_type] if req_type else None,
                            max_results_reqs=10,
                            max_results_tcs=top_k,
                        )
                        # Collect unique result nodes
                        for req_id, tcs in cov["covered"].items():
                            for tc in tcs:
                                if tc.node_id not in seen_req_nodes:
                                    req_hits.append(tc)
                                    seen_req_nodes.add(tc.node_id)
                        for req_node in cov["uncovered"][:3]:
                            if req_node.node_id not in seen_req_nodes:
                                req_hits.append(req_node)
                                seen_req_nodes.add(req_node.node_id)
                        # Build coverage note for this requirement
                        n_cov = len(cov["covered"])
                        n_unc = len(cov["uncovered"])
                        rt_label = req_type or "requirement"
                        cluster_label = f" in '{cluster}'" if cluster else " (protocol-level)"
                        if n_cov == 0 and n_unc == 0:
                            req_coverage_parts.append(
                                f"⚠ No KG match for {rt_label}{cluster_label}: \"{req_text_preview}\" — "
                                f"likely NEW requirement with no existing test coverage."
                            )
                        elif n_cov == 0 and n_unc > 0:
                            req_coverage_parts.append(
                                f"⚠ COVERAGE GAP — {rt_label}{cluster_label}: \"{req_text_preview}\" "
                                f"→ {n_unc} KG requirement(s) matched but NONE have linked test cases."
                            )
                        else:
                            req_coverage_parts.append(
                                f"✓ {rt_label}{cluster_label}: \"{req_text_preview}\" "
                                f"→ {n_cov} requirement(s) covered, {n_unc} gaps."
                            )
                    if req_coverage_parts:
                        coverage_notes[key] = "\n".join(req_coverage_parts)
                    logger.info(
                        "[search_knowledge_graph_node] chunk %d/%d | %d pr_reqs → "
                        "%d req KG hits, %d coverage notes",
                        i + 1, len(pr_chunks), len(chunk_reqs),
                        len(req_hits), len(req_coverage_parts),
                    )

                # Merge entity hits + req hits, deduplicated, entity hits first
                seen_merge: set = {n.node_id for n in entity_hits}
                for n in req_hits:
                    if n.node_id not in seen_merge:
                        entity_hits.append(n)
                        seen_merge.add(n.node_id)
                graph_results[key] = entity_hits[:top_k]

                logger.info(
                    "[search_knowledge_graph_node] chunk %d/%d | %s → %d KG matches "
                    "(entity=%d req=%d)",
                    i + 1, len(pr_chunks),
                    chunk.metadata.get("path", key),
                    len(graph_results[key]),
                    len(entity_hits), len(req_hits),
                )

    total_matches = sum(len(v) for v in graph_results.values())
    logger.info(
        "[search_knowledge_graph_node] KG search over %d PR chunks → %d total matches",
        len(pr_chunks), total_matches,
    )
    # Debug-log each KG hit to search.log for traceability
    for key, hits in graph_results.items():
        if hits:
            _kg_search_logger.debug("[kg] %s | %d hits", key, len(hits))
            for hit in hits:
                _kg_search_logger.debug(
                    "  node_id=%-40s  type=%s  label=%s",
                    hit.node_id,
                    hit.node_type.value if hasattr(hit.node_type, "value") else str(hit.node_type),
                    hit.label,
                )
        else:
            _kg_search_logger.debug("[kg] %s | 0 hits", key)
    return {**state, "graph_results": graph_results, "graph_coverage_notes": coverage_notes, "chat_query_intent": _chat_intent}



def _deduplicate_missing_tc_ids(
    missing_tests: List[dict],
    existing_tc_ids: Dict[str, List[str]],
) -> List[dict]:
    """Assign deterministic TC numbers to missing_tests.

    Instead of trusting the LLM's TC-ID choices (which vary between runs),
    this function strips the LLM-assigned minor versions and re-assigns them
    sequentially based on a stable sort key (title text without the TC-ID).
    This ensures the same set of TCs always gets the same numbering regardless
    of LLM generation order or which chunk produced them.

    Strategy:
      1. Parse each item's TC-ID into (prefix, major, minor).
      2. Group by (prefix, major).
      3. Within each group, sort by a stable key (title text stripped of TC-ID).
      4. Assign sequential minor versions starting after the highest existing number.
      5. Items without a parseable TC-ID pass through unchanged.
    """
    import re as _re2
    _TC_NUM_RE = _re2.compile(r'\bTC-([A-Z][A-Z0-9]*)-(\d+)\.(\d+)\b')

    # Collect all existing TC numbers (across all clusters).
    all_existing: set = set()
    for tc_list in existing_tc_ids.values():
        all_existing.update(tc_list)

    # Parse and group items by (prefix, major)
    grouped: Dict[tuple, List[tuple]] = {}  # (prefix, major) → [(sort_key, item, old_tc_id)]
    no_tc_id: List[dict] = []

    for item in missing_tests:
        title = item.get("title", "")
        m = _TC_NUM_RE.search(title)
        if not m:
            no_tc_id.append(item)
            continue
        prefix = m.group(1)
        major = int(m.group(2))
        old_tc_id = f"TC-{prefix}-{major}.{int(m.group(3))}"
        # Stable sort key: title text without the TC-ID, lowercased
        sort_key = _TC_NUM_RE.sub("", title).strip().lower()
        key = (prefix, major)
        grouped.setdefault(key, []).append((sort_key, item, old_tc_id))

    # For each group, sort deterministically and assign sequential minor versions
    result: List[dict] = list(no_tc_id)

    for (prefix, major) in sorted(grouped.keys()):
        items = grouped[(prefix, major)]
        items.sort(key=lambda t: t[0])  # sort by title text

        # Find the highest existing minor version for this (prefix, major)
        max_existing_minor = 0
        for existing_id in all_existing:
            em = _TC_NUM_RE.search(existing_id)
            if em and em.group(1) == prefix and int(em.group(2)) == major:
                max_existing_minor = max(max_existing_minor, int(em.group(3)))

        next_minor = max_existing_minor + 1
        assigned_in_group: set = set()

        for sort_key, item, old_tc_id in items:
            new_tc_id = f"TC-{prefix}-{major}.{next_minor}"
            assigned_in_group.add(new_tc_id)

            new_item = dict(item)
            if new_tc_id != old_tc_id:
                new_item["tc_id"] = new_tc_id
                new_item["title"] = item.get("title", "").replace(old_tc_id, new_tc_id)
                if "adoc_section" in new_item and new_item["adoc_section"]:
                    new_item["adoc_section"] = new_item["adoc_section"].replace(old_tc_id, new_tc_id)
                logger.debug(
                    "[_deduplicate_missing_tc_ids] renumber: %s → %s (deterministic assignment)",
                    old_tc_id, new_tc_id,
                )
            result.append(new_item)
            next_minor += 1

    # ── PICS prefix validation (informational warning only) ──────────────
    # Check that PICS codes in adoc_section use the same prefix as the TC-ID.
    # E.g. TC-CLDIM-* should use CLDIM.S.* PICS codes, not CLCD.S.*.
    _PICS_RE = _re2.compile(r'\b([A-Z][A-Z0-9]+)\.[SC]\.[A-Z0-9]')
    for item in result:
        tc_id = item.get("tc_id") or ""
        adoc = item.get("adoc_section") or ""
        tc_m = _TC_NUM_RE.search(tc_id or item.get("title", ""))
        if not tc_m or not adoc:
            continue
        tc_prefix = tc_m.group(1)
        pics_prefixes = set(_PICS_RE.findall(adoc))
        # Filter out common non-cluster PICS prefixes (MCORE, PICS, etc.)
        pics_prefixes.discard("PICS")
        pics_prefixes.discard("MCORE")
        mismatched = {p for p in pics_prefixes if p != tc_prefix}
        if mismatched:
            logger.warning(
                "[_deduplicate_missing_tc_ids] PICS prefix mismatch for %s: "
                "TC prefix=%s but adoc_section contains PICS prefixes %s. "
                "Review generated PICS codes for correctness.",
                tc_id or item.get("title", "?"), tc_prefix,
                ", ".join(sorted(mismatched)),
            )

    return result


_CROSS_CUTTING_STOP_WORDS = frozenset({
    # English function words
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "must", "can", "not", "no",
    "nor", "so", "yet", "this", "that", "these", "those", "it", "its",
    "as", "if", "than", "too", "very", "just", "over", "under", "per", "via",
    # Matter protocol generic terms (too broad to indicate a specific feature)
    "cluster", "attribute", "command", "event", "feature", "matter",
    "section", "test", "device", "value", "type", "field", "data",
    "model", "table", "list", "update", "change", "add", "support",
    "description", "status", "response", "request", "message",
    "node", "endpoint", "fabric", "group", "scene", "binding", "report",
    "read", "write", "invoke", "subscribe", "notify", "indicate",
    "bit", "byte", "bits", "bytes", "enum", "struct", "entry", "item",
    "element", "based", "using", "used", "sets", "set", "get", "put",
    "all", "any", "each", "every", "some", "both", "other", "more",
    "less", "when", "where", "how", "what", "which", "who", "why",
    "new", "old", "first", "last", "next", "prev", "one", "two",
})


def _extract_batch_keywords(batch: List[tuple]) -> frozenset:
    """Extract meaningful (non-stop-word) keywords from the section titles in a batch."""
    words: set = set()
    for _, chunk in batch:
        title = (
            chunk.metadata.get("section_title") or
            chunk.metadata.get("section_num") or ""
        ).lower()
        for word in re.split(r"[^a-z0-9]+", title):
            if len(word) >= 3 and word not in _CROSS_CUTTING_STOP_WORDS:
                words.add(word)
    return frozenset(words)


def _find_cross_cutting_topics(
    batches: List[List[tuple]],
    min_shared: int = 2,
) -> List[List[int]]:
    """Find groups of batches from DIFFERENT clusters that share >= min_shared keywords.

    Uses BFS on a keyword-overlap graph so transitive matches are grouped together.
    Only groups containing batches from at least two distinct non-empty clusters are returned.
    Returns list of groups; each group is a list of batch indices.
    """
    n = len(batches)
    if n < 2:
        return []

    batch_meta = []
    for i, batch in enumerate(batches):
        cluster = (batch[0][1].metadata.get("cluster") or "").strip().lower()
        batch_meta.append({"cluster": cluster, "keywords": _extract_batch_keywords(batch)})

    # Build adjacency: edge if different cluster AND >= min_shared keywords
    adj: List[set] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if batch_meta[i]["cluster"] == batch_meta[j]["cluster"]:
                continue
            shared = batch_meta[i]["keywords"] & batch_meta[j]["keywords"]
            if len(shared) >= min_shared:
                adj[i].add(j)
                adj[j].add(i)

    # BFS connected components on adj graph
    from collections import deque as _deque
    visited: set = set()
    groups: List[List[int]] = []
    for start in range(n):
        if start in visited or not adj[start]:
            continue
        group: List[int] = []
        queue = _deque([start])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            group.append(node)
            queue.extend(adj[node] - visited)
        # Only include groups with batches from at least 2 distinct non-empty clusters
        group_clusters = {batch_meta[i]["cluster"] for i in group if batch_meta[i]["cluster"]}
        if len(group_clusters) >= 2:
            groups.append(group)

    return groups


# ---------------------------------------------------------------------------
# Adaptive bin-packing helpers for analyze_chunks_with_llm_node
# ---------------------------------------------------------------------------

# Estimated chars consumed by non-diff prompt sections (system + A/B/C/D/S/T/R/X + task).
# Used to compute available diff budget from max_prompt_chars.
_SECTION_OVERHEAD_CHARS = 50_000
_MIN_DIFF_BUDGET_CHARS = 4_000
_DEFAULT_DIFF_BUDGET_CHARS = 12_000


def _truncate_prompt_if_needed(prompt: str, config, label: str = "") -> str:
    """Truncate prompt tail if it exceeds max_prompt_chars. Returns (possibly truncated) prompt."""
    max_chars = getattr(config.llm, "max_prompt_chars", 0) or 0
    if max_chars <= 0 or len(prompt) <= max_chars:
        return prompt
    over = len(prompt) - max_chars
    logger.warning(
        "[%s] prompt too large (%d chars, limit %d) — truncating tail by %d chars",
        label or "truncate", len(prompt), max_chars, over,
    )
    return prompt[:max_chars] + "\n\n(... truncated to fit model context budget ...)"


def _extract_section_depth(chunk: Document) -> int:
    """Return section nesting depth from the section_title metadata.

    Counts dots in the leading section number (e.g. '11.' → depth 1;
    '11.30.5.' → depth 3) so overview sections sort before detail sections.
    """
    section_title = chunk.metadata.get("section_title") or chunk.metadata.get("section_num") or ""
    m = re.match(r"^(\d+(?:\.\d+)*\.?)\s*", section_title.strip())
    if not m:
        return 0
    return m.group(1).rstrip(".").count(".")


def _merge_search_results_for_batch(
    chunk_keys: List[str],
    search_results: Dict[str, List],
) -> List:
    """Union SearchResult lists from multiple chunk keys, keeping highest score per TC."""
    seen: Dict[str, Any] = {}
    for key in chunk_keys:
        for hit in search_results.get(key, []):
            doc = hit.document if hasattr(hit, "document") else hit
            metadata = getattr(doc, "metadata", None) or {}
            tc_id = (
                metadata.get("tc_id")
                or metadata.get("tc-id")
                or (metadata.get("path", "") + "::" + metadata.get("title", ""))
                or getattr(doc, "page_content", "")[:60]
            )
            score = hit.score if hasattr(hit, "score") else 0.0
            existing_score = seen[tc_id].score if tc_id in seen and hasattr(seen[tc_id], "score") else -1.0
            if tc_id not in seen or score > existing_score:
                seen[tc_id] = hit
    return list(seen.values())


def _merge_graph_results_for_batch(
    chunk_keys: List[str],
    graph_results: Dict[str, List],
) -> List:
    """Union GraphNode lists from multiple chunk keys, deduped by node_id."""
    seen: Dict[str, Any] = {}
    for key in chunk_keys:
        for node in graph_results.get(key, []):
            nid = node.node_id if hasattr(node, "node_id") else str(node)
            if nid not in seen:
                seen[nid] = node
    return list(seen.values())


def _pack_cluster_chunks_into_batches(
    pr_chunks: List[Document],
    budget_chars: int = _DEFAULT_DIFF_BUDGET_CHARS,
) -> List[List[tuple]]:
    """Group PR chunks by cluster, sort by section depth, greedy-pack into char-budget batches.

    Returns a list of batches. Each batch is a list of (original_index, chunk) pairs
    whose combined prepared-diff size stays within *budget_chars*.
    """
    cluster_groups: Dict[str, List[tuple]] = {}
    for i, chunk in enumerate(pr_chunks):
        cluster = (chunk.metadata.get("cluster") or "").strip()
        key = cluster.lower() if cluster else "__unknown__"
        cluster_groups.setdefault(key, []).append((i, chunk))

    batches: List[List[tuple]] = []
    for indexed_chunks in cluster_groups.values():
        sorted_chunks = sorted(indexed_chunks, key=lambda t: _extract_section_depth(t[1]))
        current_batch: List[tuple] = []
        current_used = 0
        for idx, chunk in sorted_chunks:
            prepared_len = len(_prepare_diff_content(chunk.page_content))
            if current_batch and current_used + prepared_len > budget_chars:
                batches.append(current_batch)
                current_batch = []
                current_used = 0
            current_batch.append((idx, chunk))
            current_used += prepared_len
        if current_batch:
            batches.append(current_batch)
    return batches


@log_node
def analyze_chunks_with_llm_node(state: PipelineState) -> PipelineState:
    """Node 8: Structured LLM reasoning over PR changes vs test coverage.

    For each PR chunk the LLM receives:
      - **Structured change record** (from ``extract_pr_changes_node``) describing
        what changed and which entities are affected
      - **Section A** — top-K vector similarity hits from the test plan VDB
      - **Section B** — top-K entity-matched nodes from the KG (direct + indirect)

    Output per chunk is a structured JSON record:
    {
      "change_summary": "...",
      "impacted_entities": [...],
      "coverage": {
        "direct_tests": [...],
        "indirect_tests": [...],
        "missing": true/false
      },
      "recommendation": {
        "action": "update_existing | add_new | none",
        "details": "..."
      },
      "reasoning": "..."
    }
    """
    config = state["config"]
    llm = _get_run_llm(config, state.get("run_dir", ""))

    # ------------------------------------------------------------------ #
    # Chat-client path                                                     #
    # ------------------------------------------------------------------ #
    # When the caller is the FastAPI chat app ("app_chat"), the intent is  #
    # conversational Q&A grounded in RAG results — not a PR diff analysis. #
    # We use the search results already in state to build a RAG context    #
    # string, then call the LLM with the user's question and history.      #
    # The reply is returned in state["llm_reply"] so PipelineResult can    #
    # surface it to the chat endpoint.                                     #
    run_ctx = state.get("run_ctx")
    if run_ctx is not None and run_ctx.client == "app_chat":
        return _analyze_chat_path(state, llm)

    # ------------------------------------------------------------------ #
    # CLI / PR-diff path (original logic)                                  #
    # ------------------------------------------------------------------ #
    pr_chunks: List[Document] = state.get("pr_chunks", [])
    pr_changes: List[Dict] = state.get("pr_changes", [])
    search_results = state.get("search_results", {})
    graph_results = state.get("graph_results", {})

    # Optional chunk limit for quick verification runs
    max_pr_chunks: int = state.get("max_pr_chunks", 0)
    if max_pr_chunks and max_pr_chunks > 0 and len(pr_chunks) > max_pr_chunks:
        logger.info(
            "[analyze_chunks_with_llm_node] --num-chunks=%d: processing first %d of %d PR chunks",
            max_pr_chunks, max_pr_chunks, len(pr_chunks),
        )
        pr_chunks = pr_chunks[:max_pr_chunks]

    # Change record by chunk index
    changes_by_idx: Dict[int, Dict] = {}
    for rec in pr_changes:
        changes_by_idx[rec.get("pr_chunk_index", 0)] = rec

    # System prompt is built per-chunk (protocol area may vary across chunks).
    # Cache keyed by summary_file path so each distinct area is built once.
    _system_prompt_cache: dict = {}
    _kg_for_system = state.get("knowledge_graph")
    _config_for_system = state.get("config")

    # Build cluster → sorted TC-ID list from the KG.
    # Passed into each LLM prompt so the model knows which numbers are already taken
    # and can correctly classify existing TCs as update_candidates (not missing_tests).
    existing_tc_ids: Dict[str, List[str]] = {}
    _kg_for_tcs = state.get("knowledge_graph")
    if _kg_for_tcs is not None and hasattr(_kg_for_tcs, "get_all_test_cases"):
        try:
            for _node in _kg_for_tcs.get_all_test_cases():
                _tc_id = _node.properties.get("tc_id") or ""
                _cluster = (
                    _node.properties.get("cluster_name")
                    or _node.properties.get("cluster")
                    or "unknown"
                )
                if _tc_id:
                    existing_tc_ids.setdefault(_cluster, [])
                    if _tc_id not in existing_tc_ids[_cluster]:
                        existing_tc_ids[_cluster].append(_tc_id)
            for _c in existing_tc_ids:
                existing_tc_ids[_c].sort()
            logger.debug(
                "[analyze_chunks_with_llm_node] existing_tc_ids: %d clusters, %d total TCs",
                len(existing_tc_ids),
                sum(len(v) for v in existing_tc_ids.values()),
            )
        except Exception as _exc:
            logger.warning("[analyze_chunks_with_llm_node] Could not build existing_tc_ids: %s", _exc)

    analysis_results = []
    missing_tests = []
    update_candidates = []
    negative_tests = []
    llm_failed_chunks = 0

    # Prompt blocks for optional negative-test generation
    _gen_neg = bool(state.get("generate_negative_tests"))
    _neg_task_block  = _NEGATIVE_TESTS_TASK_BLOCK  if _gen_neg else ""
    _neg_json_field  = _NEGATIVE_TESTS_JSON_FIELD  if _gen_neg else ""
    _aborted_at: Optional[int] = None

    # ── Adaptive bin-packing: group by cluster, sort by section depth,
    # greedy-pack chunks into char-budget batches so related changes from
    # the same cluster travel together in a single LLM call. ─────────────
    _max_prompt = getattr(config.llm, "max_prompt_chars", 0) or 0
    if _max_prompt > 0:
        _diff_budget = max(_MIN_DIFF_BUDGET_CHARS, _max_prompt - _SECTION_OVERHEAD_CHARS)
    else:
        _diff_budget = _DEFAULT_DIFF_BUDGET_CHARS
    batches = _pack_cluster_chunks_into_batches(pr_chunks, budget_chars=_diff_budget)
    total_batches = len(batches)
    logger.info("[Pass 1: Per-Chunk] Starting: %d LLM calls for %d PR chunks (diff_budget=%d chars)",
                total_batches, len(pr_chunks), _diff_budget)
    logger.info(
        "[analyze_chunks_with_llm_node] %d PR chunks → %d batches (avg %.1f chunks/batch)",
        len(pr_chunks), total_batches,
        len(pr_chunks) / total_batches if total_batches else 0,
    )

    batch_num = 0  # default before loop — used in fatal-error handler
    try:
        for batch_num, batch in enumerate(batches):
            if not batch:
                continue
            batch_chunk_keys = [f"pr_{idx}" for idx, _ in batch]
            primary_idx, primary_chunk = batch[0]
            batch_chunks = [chunk for _, chunk in batch]
            n_in_batch = len(batch)

            # Collect change records for all chunks in this batch
            batch_change_recs = [changes_by_idx.get(idx, {}) for idx, _ in batch]
            primary_change_rec = batch_change_recs[0]

            # System prompt: detect protocol area from primary chunk's section path.
            # matter_spec_diff chunks don't have section_path but do have cluster and
            # section_title — synthesize a breadcrumb so _detect_summary_file matches.
            _chunk_section_path = primary_chunk.metadata.get("section_path") or " > ".join(
                filter(None, [
                    primary_chunk.metadata.get("cluster", ""),
                    primary_chunk.metadata.get("section_title", ""),
                ])
            )
            _, _summary_file, _ = _detect_summary_file(_chunk_section_path, _config_for_system)
            _sp_cache_key = _summary_file or "_default_"
            if _sp_cache_key not in _system_prompt_cache:
                _system_prompt_cache[_sp_cache_key] = _build_analysis_system_prompt(
                    _kg_for_system,
                    config=_config_for_system,
                    chunk_section_path=_chunk_section_path,
                )
                logger.debug(
                    "[analyze_chunks_with_llm_node] built system prompt for key=%r (%d chars)",
                    _sp_cache_key, len(_system_prompt_cache[_sp_cache_key]),
                )
            analysis_system_prompt = _system_prompt_cache[_sp_cache_key]

            # Merge search + graph results from all chunks in this batch
            merged_vector_hits = _merge_search_results_for_batch(batch_chunk_keys, search_results)
            merged_graph_hits = _merge_graph_results_for_batch(batch_chunk_keys, graph_results)

            # Re-rank merged hits using the primary change record
            reranker_cfg = config.reranker
            if reranker_cfg.enabled and merged_vector_hits:
                weights = RerankerWeights(
                    entity_overlap           = reranker_cfg.entity_overlap,
                    cluster_match            = reranker_cfg.cluster_match,
                    condition_effect_overlap = reranker_cfg.condition_effect_overlap,
                    intent_match             = reranker_cfg.intent_match,
                    kg_direct_bonus          = reranker_cfg.kg_direct_bonus,
                    kg_indirect_bonus        = reranker_cfg.kg_indirect_bonus,
                    lexical_similarity       = reranker_cfg.lexical_similarity,
                    chunk_type_bonus         = reranker_cfg.chunk_type_bonus,
                    retrieval_score          = reranker_cfg.retrieval_score,
                )
                kg_hints = _graph_hits_to_kg_hints(merged_graph_hits) if merged_graph_hits else None
                ranked = rerank_candidates(
                    structured_change = primary_change_rec,
                    query_text        = primary_chunk.page_content,
                    candidates        = _search_results_to_candidates(merged_vector_hits),
                    kg_hits           = kg_hints,
                    top_n             = config.pipeline.search_top_k,
                    weights           = weights,
                )
                tc_text = _format_ranked_test_cases(ranked)

                # Approach 2: inject sibling cluster TCs when any alias/base cluster
                # TC appears in the ranked results — ensures the LLM sees ALL siblings
                # for cluster families (e.g., Concentration Measurement) regardless of
                # which specific sibling the reranker surfaced.
                _kg_for_siblings = state.get("knowledge_graph")
                if ranked and _kg_for_siblings and hasattr(_kg_for_siblings, "get_test_cases_for_cluster"):
                    _ranked_clusters = set()
                    _ranked_tc_ids = {r.test_case_id.lower() for r in ranked if r.test_case_id}
                    for r in ranked:
                        _rc = (r.metadata or {}).get("cluster_name") or (r.metadata or {}).get("cluster") or ""
                        if _rc:
                            _ranked_clusters.add(_rc)
                    _sibling_tcs: List[str] = []
                    _seen_sibling_clusters: set = set()
                    for _rc in list(_ranked_clusters):
                        for _sib in _find_sibling_clusters(_kg_for_siblings, _rc):
                            if _sib in _seen_sibling_clusters:
                                continue
                            _seen_sibling_clusters.add(_sib)
                            _sib_nodes = _kg_for_siblings.get_test_cases_for_cluster(_sib)
                            for _sn in _sib_nodes:
                                _sn_id = _sn.properties.get("tc_id", "")
                                if _sn_id and _sn_id.lower() not in _ranked_tc_ids:
                                    _ranked_tc_ids.add(_sn_id.lower())
                                    _purpose = _sn.properties.get("purpose", "") or ""
                                    _sibling_tcs.append(
                                        f"**[Sibling cluster TC]** — `{_sn_id}` — cluster={_sib}\n"
                                        f"  > Purpose: {_purpose[:200]}"
                                    )
                    if _sibling_tcs:
                        tc_text += "\n\n### Sibling Cluster TCs (same base schema — update these identically)\n\n"
                        tc_text += "\n\n".join(_sibling_tcs)
                        logger.debug(
                            "[analyze_chunks_with_llm_node] Batch %d: injected %d sibling TCs from %d sibling clusters",
                            batch_num + 1, len(_sibling_tcs), len(_seen_sibling_clusters),
                        )

                logger.debug(
                    "[analyze_chunks_with_llm_node] Batch %d: re-ranked %d → %d candidates",
                    batch_num + 1, len(merged_vector_hits), len(ranked),
                )
            else:
                ranked = None
                tc_text = _format_test_cases(merged_vector_hits)

            # Derive primary cluster from all change records in this batch
            _batch_clusters: List[str] = []
            for _brec in batch_change_recs:
                for _field in ("cluster", "cluster_name"):
                    _cv = _brec.get(_field)
                    if _cv:
                        _batch_clusters.append(str(_cv))
                for _ent in _brec.get("entities", []):
                    _cv = _ent.get("cluster")
                    if _cv:
                        _batch_clusters.append(str(_cv))
            _batch_clusters = list(dict.fromkeys(_batch_clusters))
            _primary_cluster = (
                _batch_clusters[0] if _batch_clusters
                else primary_chunk.metadata.get("cluster", "")
            )

            # Existing TC IDs — union across all clusters in this batch
            _relevant_tc_ids: List[str] = []
            for _cname in _batch_clusters:
                _cname_lower = _cname.lower()
                for _kg_c, _tc_list in existing_tc_ids.items():
                    if _cname_lower in _kg_c.lower() or _kg_c.lower() in _cname_lower:
                        _relevant_tc_ids.extend(_tc_list)
            if not _relevant_tc_ids:
                pass  # no cluster match — leave empty rather than dumping ALL TCs
            _relevant_tc_ids = sorted(set(_relevant_tc_ids))
            existing_tc_list = (
                ", ".join(_relevant_tc_ids)
                if _relevant_tc_ids
                else "(none — this may be a first-time build)"
            )

            # Context sections — computed from merged data
            spec_context_text = _format_spec_context(
                merged_graph_hits,
                pr_content="\n\n".join(c.page_content for c in batch_chunks),
            )
            spec_section_context_text = _format_spec_section_context(
                primary_chunk, state.get("spec_chunks", []),
                kg=state.get("knowledge_graph"),
            )
            entity_context_text = "\n\n".join(
                filter(None, [
                    _format_entity_context(state.get("knowledge_graph"), rec)
                    for rec in batch_change_recs
                ])
            ) or "(none)"
            surrounding_context_text = _format_surrounding_cluster_context(
                state.get("knowledge_graph"), primary_change_rec
            )
            graph_text = _format_graph_results(merged_graph_hits, primary_cluster=_primary_cluster)
            all_cluster_tcs_text = _format_all_cluster_tcs(
                state.get("knowledge_graph"), _primary_cluster, merged_vector_hits
            )

            # Multi-chunk diff content: label each chunk when more than one in batch
            if n_in_batch == 1:
                content_block = _prepare_diff_content(primary_chunk.page_content)
                path_str = primary_chunk.metadata.get("path", f"pr_{primary_idx}")
                change_json_str = _json.dumps(primary_change_rec, indent=2) if primary_change_rec else "{}"
            else:
                content_parts = []
                for part_num, (idx, chunk) in enumerate(batch, 1):
                    sec_title = chunk.metadata.get("section_title") or f"chunk {idx}"
                    content_parts.append(
                        f"--- Change {part_num}/{n_in_batch}: {sec_title} ---\n"
                        + _prepare_diff_content(chunk.page_content)
                    )
                content_block = "\n\n".join(content_parts)
                paths = [c.metadata.get("path", f"pr_{idx}") for idx, c in batch]
                path_str = "; ".join(dict.fromkeys(paths))
                change_json_str = _json.dumps(
                    [rec for rec in batch_change_recs if rec], indent=2
                ) if any(batch_change_recs) else "[]"

            prompt = _STRUCTURED_ANALYSIS_PROMPT.format(
                change_json=change_json_str,
                path=path_str,
                content=content_block,
                spec_context=spec_context_text,
                spec_section_context=spec_section_context_text,
                entity_context=entity_context_text,
                surrounding_cluster_context=surrounding_context_text,
                test_cases=tc_text,
                graph_context=graph_text,
                existing_tc_list=existing_tc_list,
                all_cluster_tcs=all_cluster_tcs_text,
                negative_tests_task=(
                    _neg_task_block.replace("TC-OO-", f"TC-{_cluster_to_tc_prefix(_primary_cluster)}-")
                    if _gen_neg else ""
                ),
                negative_tests_json_field=_neg_json_field,
                tc_prefix=_cluster_to_tc_prefix(_primary_cluster),
                cluster_name_example=_primary_cluster or "ClusterName",
            )

            _MAX_PROMPT_CHARS = getattr(config.llm, "max_prompt_chars", 80_000) or 0
            if _MAX_PROMPT_CHARS > 0 and len(prompt) > _MAX_PROMPT_CHARS:
                _over = len(prompt) - _MAX_PROMPT_CHARS
                logger.warning(
                    "[analyze_chunks_with_llm_node] batch %d prompt too large (%d chars, limit %d) "
                    "— truncating Section D by %d chars",
                    batch_num + 1, len(prompt), _MAX_PROMPT_CHARS, _over,
                )
                _trunc_d = all_cluster_tcs_text[:max(0, len(all_cluster_tcs_text) - _over - 500)]
                if _trunc_d:
                    _trunc_d += "\n\n(... truncated to fit prompt budget ...)"
                prompt = _STRUCTURED_ANALYSIS_PROMPT.format(
                    change_json=change_json_str,
                    path=path_str,
                    content=content_block,
                    spec_context=spec_context_text,
                    spec_section_context=spec_section_context_text,
                    entity_context=entity_context_text,
                    surrounding_cluster_context=surrounding_context_text,
                    test_cases=tc_text,
                    graph_context=graph_text,
                    existing_tc_list=existing_tc_list,
                    all_cluster_tcs=_trunc_d,
                    negative_tests_task=(
                        _neg_task_block.replace("TC-OO-", f"TC-{_cluster_to_tc_prefix(_primary_cluster)}-")
                        if _gen_neg else ""
                    ),
                    negative_tests_json_field=_neg_json_field,
                    tc_prefix=_cluster_to_tc_prefix(_primary_cluster),
                    cluster_name_example=_primary_cluster or "ClusterName",
                )

            try:
                change_summary = primary_change_rec.get("change_summary", "") or primary_change_rec.get("change_kind", "")
                entities_str = ", ".join(
                    e.get("name", "?") for e in primary_change_rec.get("entities", [])[:3]
                ) or "—"
                logger.info(
                    "[analyze_chunks_with_llm_node] LLM call %d/%d | cluster=%s | "
                    "chunks=%d change=%s entities=[%s] | tc_candidates=%d kg_hits=%d",
                    batch_num + 1, total_batches,
                    _primary_cluster or "?",
                    n_in_batch,
                    change_summary or "UNKNOWN",
                    entities_str,
                    len(ranked) if reranker_cfg.enabled and merged_vector_hits else len(merged_vector_hits),
                    len(merged_graph_hits),
                )
                cluster_hint = primary_chunk.metadata.get("cluster", "?")
                logger.info(
                    "[Pass 1: Per-Chunk] LLM call %d/%d — %s",
                    batch_num + 1, total_batches, cluster_hint,
                )
                if hasattr(llm, "set_next_label"):
                    llm.set_next_label(
                        f"Pass 1 — batch {batch_num+1}/{total_batches}"
                        + (f" ({n_in_batch} chunks)" if n_in_batch > 1 else "")
                    )
                response = llm.complete(prompt, system=analysis_system_prompt)
                # REC-2: detect prose response and retry once with an explicit JSON reminder.
                # When the model returns a paragraph of English instead of JSON (e.g. "The JSON
                # response is complete..."), _parse_structured_response silently returns empty
                # results, which is indistinguishable from a genuine "no action needed" response.
                _has_json = bool(response and re.search(r'\{', response))
                if response and not _has_json:
                    logger.warning(
                        "[analyze_chunks_with_llm_node] batch %d returned prose, not JSON — retrying",
                        batch_num + 1,
                    )
                    _retry_prompt = (
                        "IMPORTANT: Your previous response was prose, not JSON.\n"
                        "Return ONLY valid JSON — no explanation, no prose before or after.\n\n"
                        + prompt
                    )
                    response = llm.complete(_retry_prompt, system=analysis_system_prompt)
                parsed = _parse_structured_response(
                    response, primary_chunk, merged_vector_hits, merged_graph_hits, primary_change_rec
                )
                action = parsed.get("recommendation", {}).get("action", "?") if isinstance(parsed.get("recommendation"), dict) else "?"
                logger.info(
                    "[analyze_chunks_with_llm_node] call %d/%d → action=%s missing=%d updates=%d",
                    batch_num + 1, total_batches,
                    action,
                    len(parsed.get("missing_tests", [])),
                    len(parsed.get("update_candidates", [])),
                )
                analysis_results.append(parsed)
                missing_tests.extend(parsed.get("missing_tests", []))
                update_candidates.extend(parsed.get("update_candidates", []))
                if _gen_neg:
                    negative_tests.extend(parsed.get("negative_tests", []))
            except Exception as exc:
                llm_failed_chunks += 1
                logger.error("[analyze_chunks_with_llm_node] LLM error on batch %d: %s", batch_num + 1, exc)
                analysis_results.append({
                    "pr_chunk": path_str,
                    "error": str(exc),
                })

    except Exception as fatal_exc:
        _aborted_at = batch_num
        logger.error(
            "[analyze_chunks_with_llm_node] Fatal error at batch %d — generating partial report: %s",
            _aborted_at, fatal_exc,
        )
        analysis_results.append({
            "pr_chunk": "__fatal__",
            "error": f"Pipeline aborted at batch {_aborted_at}/{total_batches}: {fatal_exc}",
        })

    logger.info(
        "[analyze_chunks_with_llm_node] Missing: %d, Updates: %d, Negative: %d, Failed chunks: %d%s",
        len(missing_tests), len(update_candidates), len(negative_tests), llm_failed_chunks,
        f" (aborted at batch {_aborted_at})" if _aborted_at is not None else "",
    )

    # ── Option C: cross-cutting topic analysis ─────────────────────────────
    # After all per-domain batch calls, detect batches from different clusters
    # that share >= 2 meaningful keywords (e.g. "nfc", "encryption", "ratchet").
    # For each topic group, run one additional LLM call that sees ALL the PR
    # chunks from both sides together, generating integration TCs tagged with
    # the primary (non-empty) cluster so they route correctly to adoc files.
    _xc_topics = _find_cross_cutting_topics(batches)
    if _xc_topics:
        logger.info(
            "[analyze_chunks_with_llm_node] Found %d cross-cutting topic group(s)",
            len(_xc_topics),
        )
    for topic_idx, group_batch_indices in enumerate(_xc_topics):
        group_batches = [batches[i] for i in group_batch_indices]

        # Combine all chunks from all batches in this topic group
        all_batch_chunk_keys: List[str] = []
        all_batch_chunks: List[Document] = []
        all_change_recs: List[dict] = []
        for gbatch in group_batches:
            for idx, chunk in gbatch:
                all_batch_chunk_keys.append(f"pr_{idx}")
                all_batch_chunks.append(chunk)
                all_change_recs.append(changes_by_idx.get(idx, {}))

        if not all_batch_chunks:
            continue

        # Primary cluster: prefer non-empty clusters; first one wins
        xc_primary_cluster = ""
        for gbatch in group_batches:
            cl = (gbatch[0][1].metadata.get("cluster") or "").strip()
            if cl:
                xc_primary_cluster = cl
                break

        shared_kws: set = set()
        for i, gi in enumerate(group_batch_indices):
            kws_i = _extract_batch_keywords(batches[gi])
            if i == 0:
                shared_kws = set(kws_i)
            else:
                shared_kws &= kws_i
        topic_label = ", ".join(sorted(shared_kws)[:4]) or "cross-domain"
        logger.info(
            "[analyze_chunks_with_llm_node] Cross-cutting topic %d: clusters=%s keywords=[%s]",
            topic_idx + 1,
            [b[0][1].metadata.get("cluster", "") for b in group_batches],
            topic_label,
        )

        xc_merged_vector = _merge_search_results_for_batch(all_batch_chunk_keys, search_results)
        xc_merged_graph  = _merge_graph_results_for_batch(all_batch_chunk_keys, graph_results)

        # Build combined diff content
        xc_content_parts = []
        for part_num, (idx, chunk) in enumerate(
            [(idx, c) for gb in group_batches for idx, c in gb], 1
        ):
            sec_title = chunk.metadata.get("section_title") or f"chunk {idx}"
            cl = chunk.metadata.get("cluster") or "protocol"
            xc_content_parts.append(
                f"--- Cross-cutting change {part_num}: [{cl}] {sec_title} ---\n"
                + _prepare_diff_content(chunk.page_content)
            )
        xc_content_block = "\n\n".join(xc_content_parts)
        xc_change_json = _json.dumps(
            [r for r in all_change_recs if r], indent=2
        ) or "[]"

        # Reuse primary change record (first non-empty) for reranker
        xc_primary_change = next((r for r in all_change_recs if r), {})

        reranker_cfg = config.reranker
        if reranker_cfg.enabled and xc_merged_vector:
            weights = RerankerWeights(
                entity_overlap           = reranker_cfg.entity_overlap,
                cluster_match            = reranker_cfg.cluster_match,
                condition_effect_overlap = reranker_cfg.condition_effect_overlap,
                intent_match             = reranker_cfg.intent_match,
                kg_direct_bonus          = reranker_cfg.kg_direct_bonus,
                kg_indirect_bonus        = reranker_cfg.kg_indirect_bonus,
                lexical_similarity       = reranker_cfg.lexical_similarity,
                chunk_type_bonus         = reranker_cfg.chunk_type_bonus,
                retrieval_score          = reranker_cfg.retrieval_score,
            )
            xc_kg_hints = _graph_hits_to_kg_hints(xc_merged_graph) if xc_merged_graph else None
            xc_ranked = rerank_candidates(
                structured_change = xc_primary_change,
                query_text        = "\n".join(c.page_content for c in all_batch_chunks),
                candidates        = _search_results_to_candidates(xc_merged_vector),
                kg_hits           = xc_kg_hints,
                top_n             = config.pipeline.search_top_k,
                weights           = weights,
            )
            xc_tc_text = _format_ranked_test_cases(xc_ranked)
        else:
            xc_tc_text = _format_test_cases(xc_merged_vector)

        xc_graph_text    = _format_graph_results(xc_merged_graph, primary_cluster=xc_primary_cluster)
        xc_entity_text   = "\n\n".join(
            filter(None, [_format_entity_context(state.get("knowledge_graph"), r) for r in all_change_recs])
        ) or "(none)"
        xc_spec_ctx      = _format_spec_context(xc_merged_graph, pr_content=xc_content_block)
        xc_surround      = _format_surrounding_cluster_context(
            state.get("knowledge_graph"), xc_primary_change
        )
        xc_all_tcs       = _format_all_cluster_tcs(
            state.get("knowledge_graph"), xc_primary_cluster, xc_merged_vector
        )

        _xc_relevant_tcs: List[str] = []
        for _kg_c, _tc_list in existing_tc_ids.items():
            if xc_primary_cluster.lower() in _kg_c.lower() or _kg_c.lower() in xc_primary_cluster.lower():
                _xc_relevant_tcs.extend(_tc_list)
        if not _xc_relevant_tcs:
            for _tc_list in existing_tc_ids.values():
                _xc_relevant_tcs.extend(_tc_list)
        xc_existing_tc_list = ", ".join(sorted(set(_xc_relevant_tcs))) or "(none)"

        xc_spec_section  = _format_spec_section_context(
            all_batch_chunks[0], state.get("spec_chunks", []),
            kg=state.get("knowledge_graph"),
        )

        xc_system_prompt = _build_analysis_system_prompt(
            state.get("knowledge_graph"), config=state.get("config")
        )

        xc_tc_prefix = _cluster_to_tc_prefix(xc_primary_cluster)
        # Inject cross-cutting note by prepending to change JSON string
        xc_cross_note = (
            f"CROSS-CUTTING ANALYSIS — topic: [{topic_label}]. "
            f"This batch spans multiple protocol/cluster domains. "
            f"Focus on INTEGRATION test cases that verify behavior "
            f"spanning both the cluster-level changes and the protocol-level changes. "
            f"Tag all generated TCs with cluster='{xc_primary_cluster}'."
        )
        xc_change_json_with_note = f"// {xc_cross_note}\n{xc_change_json}"

        xc_prompt = _STRUCTURED_ANALYSIS_PROMPT.format(
            change_json=xc_change_json_with_note,
            path="; ".join(dict.fromkeys(
                c.metadata.get("path", f"pr_{i}") for i, c in
                [(idx, ch) for gb in group_batches for idx, ch in gb]
            )),
            content=xc_content_block,
            spec_context=xc_spec_ctx,
            spec_section_context=xc_spec_section,
            entity_context=xc_entity_text,
            surrounding_cluster_context=xc_surround,
            test_cases=xc_tc_text,
            graph_context=xc_graph_text,
            existing_tc_list=xc_existing_tc_list,
            all_cluster_tcs=xc_all_tcs,
            negative_tests_task=(
                _neg_task_block.replace("TC-OO-", f"TC-{xc_tc_prefix}-")
                if _gen_neg else ""
            ),
            negative_tests_json_field=_neg_json_field,
            tc_prefix=xc_tc_prefix,
            cluster_name_example=xc_primary_cluster or "ClusterName",
        )

        try:
            if hasattr(llm, "set_next_label"):
                llm.set_next_label(f"Pass 1 — cross-cutting topic {topic_idx+1} [{topic_label}]")
            xc_response = llm.complete(xc_prompt, system=xc_system_prompt)
            xc_parsed = _parse_structured_response(
                xc_response, all_batch_chunks[0], xc_merged_vector, xc_merged_graph, xc_primary_change
            )
            # Force cluster tag on all results so adoc routing works
            for tc in xc_parsed.get("missing_tests", []):
                tc.setdefault("cluster", xc_primary_cluster)
            for tc in xc_parsed.get("update_candidates", []):
                tc.setdefault("cluster", xc_primary_cluster)
            analysis_results.append(xc_parsed)
            missing_tests.extend(xc_parsed.get("missing_tests", []))
            update_candidates.extend(xc_parsed.get("update_candidates", []))
            if _gen_neg:
                negative_tests.extend(xc_parsed.get("negative_tests", []))
            logger.info(
                "[analyze_chunks_with_llm_node] Cross-cutting topic %d → missing=%d updates=%d",
                topic_idx + 1,
                len(xc_parsed.get("missing_tests", [])),
                len(xc_parsed.get("update_candidates", [])),
            )
        except Exception as xc_exc:
            llm_failed_chunks += 1
            logger.error(
                "[analyze_chunks_with_llm_node] Cross-cutting topic %d LLM error: %s",
                topic_idx + 1, xc_exc,
            )
    # ── End cross-cutting analysis ──────────────────────────────────────────

    # Post-process: deduplicate TC numbers across chunks (each chunk's LLM call is
    # independent and may suggest the same TC number).
    missing_tests = _deduplicate_missing_tc_ids(missing_tests, existing_tc_ids)
    if len(missing_tests) != sum(len(r.get("missing_tests", [])) for r in analysis_results if "error" not in r):
        logger.debug(
            "[analyze_chunks_with_llm_node] After dedup: %d unique missing-TC suggestions",
            len(missing_tests),
        )

    # Deduplicate update_candidates — keep first occurrence of each tc_id.
    # Multiple chunks can reference the same existing TC for update; keep the first.
    seen_update_ids: set = set()
    deduped_updates: List[dict] = []
    for uc in update_candidates:
        tc_id = uc.get("tc_id", "")
        if not tc_id or tc_id not in seen_update_ids:
            if tc_id:
                seen_update_ids.add(tc_id)
            deduped_updates.append(uc)
    if len(deduped_updates) != len(update_candidates):
        logger.info(
            "[analyze_chunks_with_llm_node] After dedup: %d unique update_candidates (was %d)",
            len(deduped_updates), len(update_candidates),
        )
    update_candidates = deduped_updates

    # Pass 1 snapshot — write intermediate results for debugging/resumption
    import json as _json_snap
    from datetime import datetime as _dt_snap
    _snap_path = Path(state.get("output_dir", "reports")) / f"pass1_results_{_dt_snap.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        _snap_path.parent.mkdir(parents=True, exist_ok=True)
        _snap_path.write_text(_json_snap.dumps({
            "stage": "pass1_complete",
            "timestamp": _dt_snap.now().isoformat(),
            "missing_tests_count": len(missing_tests),
            "update_candidates_count": len(update_candidates),
            "missing_tests": missing_tests,
            "update_candidates": update_candidates,
        }, indent=2), encoding="utf-8")
        logger.info("[analyze_chunks_with_llm_node] Pass 1 snapshot → %s", _snap_path)
    except Exception as _snap_exc:
        logger.warning("[analyze_chunks_with_llm_node] Pass 1 snapshot failed: %s", _snap_exc)

    _pass_stats = dict(state.get("pass_stats") or {})
    _pass_stats["pass1"] = {
        "new_tcs": len(missing_tests),
        "update_candidates": len(update_candidates),
        "chunks_analyzed": len(pr_chunks) - llm_failed_chunks,
        "chunks_total": len(pr_chunks),
    }

    _update_pipeline_progress(state, "analyze_chunks_with_llm", missing=len(missing_tests), updates=len(update_candidates), failed=llm_failed_chunks)
    return {
        **state,
        "analysis_results": analysis_results,
        "missing_tests": missing_tests,
        "update_candidates": update_candidates,
        "negative_tests": negative_tests,
        "llm_failed_chunks": llm_failed_chunks,
        "llm_aborted_at": _aborted_at,
        "llm_total_chunks": len(pr_chunks),
        "pass_stats": _pass_stats,
    }



@log_node
def write_adoc_updates_node(state: PipelineState) -> PipelineState:
    """Node 9: Write updated/new .adoc test plan files from LLM analysis results.

    For each PR chunk analysis that produced structured JSON output:
    - update_candidates → replaces the TC section in the source .adoc file
    - missing_tests     → appends new TC sections to the cluster's update file

    Output files are written to ``updated_testplans/`` under the run output directory.
    """

    from src.document_updater import create_updater
    from src.document_updater.tc_index_builder import load_tc_index

    output_dir = str(Path(state.get("output_dir", "reports")) / "llm_generated_adocs" / "new_updated_TCs")

    config = state.get("config")
    tc_index_path = (
        config.pipeline.tc_index_path
        if config and hasattr(config.pipeline, "tc_index_path")
        else "data/tc_index.json"
    )
    tc_index = load_tc_index(tc_index_path)

    # Use the deduplicated missing_tests / update_candidates from state (not the raw
    # per-chunk analysis_results which still carry pre-dedup duplicate TC IDs).
    # Also merge cluster_review_additions (new TCs from cluster_review_node) — these
    # are already in adoc_section format but were not included in the write path.
    # Also merge coverage_gap_tests so they get written to adoc files.
    # Deduplicate by tc_id then title so a TC that appears in both lists is written once.
    updater = create_updater(".adoc")
    review_additions = state.get("cluster_review_additions") or []
    coverage_gap_tests = state.get("coverage_gap_tests") or []
    _seen_tc_keys: set = set()
    _merged_missing: list = []
    for _item in list(state.get("missing_tests", [])) + list(review_additions) + list(coverage_gap_tests):
        _key = (_item.get("tc_id") or "").strip() or (_item.get("title") or "").strip().lower()
        if _key and _key in _seen_tc_keys:
            logger.debug("[write_adoc_updates_node] Dedup TC '%s' (present in both missing_tests and cluster_review_additions)", _key)
            continue
        if _key:
            _seen_tc_keys.add(_key)
        _merged_missing.append(_item)
    synthetic_results = [{
        "missing_tests": _merged_missing,
        "update_candidates": state.get("update_candidates", []),
    }]
    paths = updater.write_updates(
        synthetic_results,
        state.get("search_results", {}),
        output_dir,
        tc_index=tc_index,
    )

    logger.info("[write_adoc_updates_node] %d .adoc file(s) written to %s",
                len(paths), output_dir)
    return {**state, "adoc_output_paths": paths}


@log_node
def write_updated_testplan_node(state: PipelineState) -> PipelineState:
    """Node 9b: Write per-cluster updated .adoc test plan files.

    Requires ``test_plan_adoc_sources`` populated by a ``role='test_plans_adoc_folder'``
    source in ``sources.json``.  Skips silently when no adoc sources are present.

    For each source adoc file:
    - TC sections matching an ``update_candidate`` are replaced with the
      LLM-revised ``adoc_section``.
    - New TC sections from ``missing_tests`` are appended to the file whose
      cluster prefix matches.

    Outputs are written to ``reports/updated_testplans_<timestamp>/`` as
    ``<original_stem>_updated.adoc`` — one file per modified source.
    Paths are merged into ``adoc_output_paths`` so ``generate_report_node`` can
    list them.
    """
    from datetime import datetime
    from src.engine.adoc_updater import write_updated_adocs
    from src.document_updater.tc_index_builder import load_tc_index

    adoc_sources = state.get("test_plan_adoc_sources", [])
    if not adoc_sources:
        logger.info("[write_updated_testplan_node] No test_plan_adoc_sources — skipping")
        return state

    missing = list(state.get("missing_tests", [])) + list(state.get("coverage_gap_tests") or [])
    updates = state.get("update_candidates", [])

    if not missing and not updates:
        logger.info("[write_updated_testplan_node] No changes to apply — skipping")
        return state

    # Load the TC routing index built by fetch_documents_node
    config = state.get("config")
    tc_index_path = (
        config.pipeline.tc_index_path
        if config and hasattr(config.pipeline, "tc_index_path")
        else "data/tc_index.json"
    )
    tc_index = load_tc_index(tc_index_path)
    if tc_index:
        logger.info(
            "[write_updated_testplan_node] Loaded tc_index from %s  (tc_ids=%d  prefixes=%d)",
            tc_index_path,
            len(tc_index.get("tc_map", {})),
            len(tc_index.get("prefix_map", {})),
        )
    else:
        logger.warning(
            "[write_updated_testplan_node] tc_index not found at %s — "
            "TC routing will fall back to in-memory scan (less reliable). "
            "Check that fetch_documents_node ran with adoc sources loaded.",
            tc_index_path,
        )

    ts = datetime.now().strftime("updated_testplans_%Y%m%d_%H%M%S")
    output_dir = str(Path(state.get("output_dir", "reports")) / "llm_generated_adocs" / "updated_testplans")

    paths = write_updated_adocs(adoc_sources, missing, updates, output_dir, tc_index=tc_index)

    logger.info(
        "[write_updated_testplan_node] %d updated adoc file(s) → %s",
        len(paths), output_dir,
    )

    existing = list(state.get("adoc_output_paths", []))
    return {**state, "adoc_output_paths": existing + paths}


# ---------------------------------------------------------------------------
# Cluster-level LLM review pass prompts
# ---------------------------------------------------------------------------

_CLUSTER_REVIEW_SYSTEM = """\
You are a senior Matter protocol test engineer reviewing generated test case suggestions \
for completeness and consistency. Reply ONLY with a valid JSON object — no prose, no markdown fences.
"""

_CLUSTER_REVIEW_PROMPT = """\
## Cluster Review — {cluster}

### Changes detected in this PR for this cluster:
{change_summaries}

### New test cases generated (missing_tests) — title + first procedure steps shown:
{new_tcs}

### Existing TCs flagged for update (update_candidates):
{update_tcs}

### Existing TC IDs already in the test plan:
{existing_tc_ids}

---

## Review Tasks

1. **Symmetry gaps**: If attribute/command A got a new TC but attribute/command B had \
identical changes, flag the missing one.
2. **Missing test types**: For each changed entity, check the ADDITIONAL CONTEXT below \
(if provided) for the full checklist of test categories to verify. At minimum check: \
boundary values, error paths, subscription/reporting, persistence, conformance transitions, \
and command error handling. Flag any category missing from the generated TCs.
3. **update vs new**: Is any update_candidate actually a new distinct scenario that warrants \
a separate TC?
4. **Duplicate coverage**: Do any two entries cover the same scenario?

For every gap, choose an action:
- `"new_tc"` — the gap warrants a genuinely new standalone TC.
- `"update_existing"` — the gap closely fits an already-proposed or already-existing TC; \
  add steps to that TC instead. Set `update_tc_id` to the TC to extend. Prefer this over \
  creating yet another TC when the scenarios are closely related.

For every gap, provide `"steps"` — a concrete ordered list of TH actions + DUT expected \
responses (numbered, specific, not abstract descriptions).

Return ONLY this JSON (empty lists when nothing to report):
{{
  "cluster": "{cluster}",
  "symmetry_gaps": [
    {{
      "entity": "OnTime",
      "reason": "TC-OO-2.8 covers OffWaitTime Quieter Reporting but no equivalent TC exists for OnTime despite the same Q quality being added",
      "action": "new_tc",
      "suggested_title": "TC-OO-2.9 OnTime Quieter Reporting [DUT as Server]",
      "steps": [
        "1. TH establishes a subscription to the OnTime attribute on DUT endpoint 1.",
        "2. TH writes OnTime with value 100. Verify DUT returns SUCCESS. Verify a subscription report is received.",
        "3. TH writes OnTime with value 102 (delta = 2, below reportable-change threshold). Verify write SUCCESS. Verify no new subscription report is generated.",
        "4. TH writes OnTime with value 120 (delta = 18, meets or exceeds threshold). Verify write SUCCESS. Verify a subscription report is received with the updated value."
      ]
    }}
  ],
  "missing_test_types": [
    {{
      "entity": "OffWaitTime",
      "change": "Q (Quieter Reporting) added",
      "action": "update_existing",
      "update_tc_id": "TC-OO-2.10",
      "missing": "Threshold-suppression coverage: sub-threshold write must produce no report; super-threshold write must produce a report",
      "suggested_title": "TC-OO-2.10 OffWaitTime Quieter Reporting [DUT as Server]",
      "steps": [
        "14. TH writes OffWaitTime with value 5 (delta = 5, NOT > reportable-change threshold). Verify write SUCCESS. Verify no new subscription report is generated for this sub-threshold change.",
        "15. TH writes OffWaitTime with value 25 (delta = 20, exceeds threshold). Verify write SUCCESS. Verify a subscription report is received with the updated OffWaitTime value."
      ]
    }}
  ],
  "should_be_new_tc": [
    {{
      "tc_id": "TC-OO-2.1",
      "reason": "reportability rules are a distinct scenario from attribute reads",
      "action": "new_tc",
      "suggested_title": "TC-OO-2.11 OnTime Reportability [DUT as Server]",
      "steps": [
        "1. ...",
        "2. ..."
      ]
    }}
  ],
  "duplicates": [
    {{
      "entries": ["TC-OO-2.8", "new TC in missing_tests titled X"],
      "reason": "both test OffWaitTime null read"
    }}
  ],
  "summary": "one sentence overall assessment"
}}
"""


@log_node
def cluster_review_node(state: PipelineState) -> PipelineState:
    """Node 9c: Cluster-level LLM review pass — audit the per-chunk analysis for gaps.

    After ``analyze_chunks_with_llm_node`` produces ``missing_tests`` + ``update_candidates``,
    this node groups them by cluster and makes one small LLM call per cluster to check:
      - Symmetry gaps (entity A got a new TC, entity B had the same change but didn't)
      - Missing test types (nullable/power-cycle/conformance/access coverage)
      - Entries that should be new TCs rather than updates
      - Duplicate coverage across entries

    Output: ``cluster_review_<ts>.md`` in the output directory — a human-readable
    audit file. The HTML report and adoc files are **not** modified; this is an
    audit document only.
    """
    import json as _json
    from collections import defaultdict
    from datetime import datetime

    config      = state.get("config")
    missing     = state.get("missing_tests", [])
    updates     = state.get("update_candidates", [])
    pr_changes  = state.get("pr_changes", [])
    output_dir  = Path(state.get("output_dir", "reports"))
    output_dir.mkdir(parents=True, exist_ok=True)

    if not missing and not updates:
        logger.info("[cluster_review_node] No missing_tests or update_candidates — skipping")
        return state

    try:
        llm = _get_run_llm(config, state.get("run_dir", ""))
    except Exception as exc:
        logger.warning(
            "[cluster_review_node] LLM provider unavailable — skipping cluster review: %s", exc
        )
        return state
    # --- Build per-cluster buckets ---
    def _cluster_key(item: dict) -> str:
        return (item.get("cluster") or item.get("cluster_name") or "Unknown").strip()

    new_by_cluster: Dict[str, List[dict]]    = defaultdict(list)
    update_by_cluster: Dict[str, List[dict]] = defaultdict(list)
    changes_by_cluster: Dict[str, List[str]] = defaultdict(list)

    for t in missing:
        new_by_cluster[_cluster_key(t)].append(t)

    for u in updates:
        update_by_cluster[_cluster_key(u)].append(u)

    # Prefer analysis_results (rich per-chunk LLM output) for change context.
    # Fall back to pr_changes (rule-based extractor) if analysis_results is empty
    # or contains only error records (no "cluster" key on any item).
    analysis_results = state.get("analysis_results", [])
    valid_results = [ar for ar in analysis_results if ar.get("cluster") or ar.get("cluster_name")]
    if valid_results:
        for ar in valid_results:
            ck = (ar.get("cluster") or ar.get("cluster_name") or "Unknown").strip()
            parts = []
            cs = ar.get("change_summary", "")
            if cs:
                parts.append(cs)
            entities = ar.get("impacted_entities", [])
            if entities:
                ent_str = ", ".join(
                    f"{e.get('type','?')} {e.get('name','?')}"
                    for e in (entities if isinstance(entities, list) else [])
                )
                parts.append(f"Entities: {ent_str}")
            reasoning = ar.get("reasoning", "")
            if reasoning:
                parts.append(f"Reasoning: {reasoning[:300]}")
            if parts:
                changes_by_cluster[ck].append(" | ".join(parts))
    else:
        for ch in pr_changes:
            ck = (ch.get("cluster") or ch.get("cluster_name") or "Unknown").strip()
            summary = ch.get("change_summary", "")
            if summary:
                changes_by_cluster[ck].append(summary)

    # Merge cluster keys from all three sources
    all_clusters = sorted(set(
        list(new_by_cluster.keys()) +
        list(update_by_cluster.keys()) +
        list(changes_by_cluster.keys())
    ))
    # Drop "Unknown" if other clusters exist
    if len(all_clusters) > 1 and "Unknown" in all_clusters:
        all_clusters = [c for c in all_clusters if c != "Unknown"]

    # Build existing_tc_ids per cluster from the KG
    existing_tc_ids: Dict[str, List[str]] = {}
    kg = state.get("knowledge_graph")
    if kg is not None and hasattr(kg, "get_all_test_cases"):
        try:
            for node in kg.get_all_test_cases():
                tc_id   = node.properties.get("tc_id") or ""
                cluster = (node.properties.get("cluster_name")
                           or node.properties.get("cluster") or "Unknown")
                if tc_id:
                    existing_tc_ids.setdefault(cluster, [])
                    if tc_id not in existing_tc_ids[cluster]:
                        existing_tc_ids[cluster].append(tc_id)
        except Exception as exc:
            logger.warning("[cluster_review_node] Could not read KG TC ids: %s", exc)

    # --- Run one LLM call per cluster ---
    all_findings: List[dict] = []
    _raw_max = getattr(config.analysis, "max_llm_calls_per_run", 0) if config and hasattr(config, "analysis") else 0
    max_calls = _raw_max if _raw_max > 0 else 999_999

    if len(all_clusters) > max_calls:
        logger.warning(
            "[cluster_review_node] %d clusters to review but max_llm_calls_per_run=%d"
            " — %d clusters will be skipped. Increase analysis.max_llm_calls_per_run to review all.",
            len(all_clusters), max_calls, len(all_clusters) - max_calls,
        )

    n_review_calls = min(len(all_clusters), max_calls)
    if n_review_calls > 0:
        logger.info("[Pass 2: Cluster Review] Starting: %d cluster(s) to review", n_review_calls)

    for _ridx, cluster in enumerate(all_clusters[:max_calls], 1):
        new_tcs   = new_by_cluster.get(cluster, [])
        upd_tcs   = update_by_cluster.get(cluster, [])
        changes   = changes_by_cluster.get(cluster, [])
        if not changes:
            # Fuzzy fallback: cluster name is a subdomain of a changes key or vice versa
            cl_lower = cluster.lower()
            for k, v in changes_by_cluster.items():
                k_lower = k.lower()
                if k_lower in cl_lower or cl_lower in k_lower:
                    changes = changes + v

        # Provide full adoc for every proposed TC and update so the review LLM
        # can detect duplicates across multi-part TCs and proposed updates.
        def _fmt_new_tc_full(t: dict) -> str:
            title = t.get("title", "?")
            adoc = (t.get("adoc_section", "") or "").strip()
            return f"  ### {title}\n{adoc}" if adoc else f"  ### {title}"

        def _fmt_upd_tc_full(u: dict) -> str:
            tc_id = u.get("tc_id", "?")
            summary = u.get("change_summary", "")
            adoc = (u.get("adoc_section", "") or "").strip()
            header = f"  ### {tc_id}: {summary}"
            return f"{header}\n{adoc}" if adoc else header

        new_tcs_text = "\n\n".join(_fmt_new_tc_full(t) for t in new_tcs) or "  (none)"
        upd_tcs_text = "\n\n".join(_fmt_upd_tc_full(u) for u in upd_tcs) or "  (none)"

        changes_text = "\n".join(f"  - {c[:300]}" for c in changes[:30]) or "  (none)"

        # Cluster-matched existing TC IDs
        existing_ids: List[str] = []
        cluster_lower = cluster.lower()
        for kg_cluster, ids in existing_tc_ids.items():
            if cluster_lower in kg_cluster.lower() or kg_cluster.lower() in cluster_lower:
                existing_ids.extend(ids)
        existing_ids_text = ", ".join(sorted(set(existing_ids))) or "(none found in KG)"

        prompt = _CLUSTER_REVIEW_PROMPT.format(
            cluster=cluster,
            change_summaries=changes_text,
            new_tcs=new_tcs_text,
            update_tcs=upd_tcs_text,
            existing_tc_ids=existing_ids_text,
        )

        # Inject per-pass additional context (from llm_prompts/additional_context/pass2.md)
        _pass2_ctx = _load_additional_context(config, pass_name="pass2")
        if _pass2_ctx:
            prompt += f"\n\n--- ADDITIONAL CONTEXT ---\n{_pass2_ctx}\n--- END ADDITIONAL CONTEXT ---\n"

        try:
            logger.info("[Pass 2: Cluster Review] LLM call %d/%d — %s", _ridx, n_review_calls, cluster)
            prompt = _truncate_prompt_if_needed(prompt, config, label="cluster_review")
            if hasattr(llm, "set_next_label"):
                llm.set_next_label(f"Cluster review — {cluster}")
            response = llm.complete(prompt, system=_CLUSTER_REVIEW_SYSTEM)
            # Extract JSON — use balanced brace parser for nested objects
            _extracted = _extract_json_object(response)
            if _extracted:
                json_str = _extracted
            else:
                json_str = response.strip()
            findings = _json.loads(json_str)
            findings["cluster"] = cluster  # ensure always set
            all_findings.append(findings)
            logger.info(
                "[cluster_review_node] Cluster '%s' → symmetry_gaps=%d missing_types=%d",
                cluster,
                len(findings.get("symmetry_gaps", [])),
                len(findings.get("missing_test_types", [])),
            )
        except Exception as exc:
            logger.warning("[cluster_review_node] LLM review failed for cluster '%s': %s", cluster, exc)
            all_findings.append({"cluster": cluster, "error": str(exc)})

    # --- Write the review MD ---
    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    review_path  = output_dir / f"cluster_review_{timestamp}.md"
    review_md    = _build_cluster_review_md(all_findings, missing, updates)
    review_path.write_text(review_md, encoding="utf-8")
    logger.info("[cluster_review_node] Cluster review written → %s", review_path)

    # --- Apply duplicate removals from review findings ---
    # The review LLM flags cross-list duplicates (e.g. a new TC in missing_tests that covers
    # the same scenario as an update_candidate addition).  Remove those redundant new TCs now
    # so the HTML report and adoc writers don't emit both.
    duplicate_tc_titles: set = set()
    for f in all_findings:
        for dup in f.get("duplicates", []):
            for entry in dup.get("entries", []):
                # Entries that reference a missing_tests title (not an existing TC-ID) are
                # the ones to drop.  Heuristic: if the entry does NOT start with "TC-" it's
                # a free-form new-TC title; if it starts with "TC-" check whether it appears
                # in existing TC IDs (update_candidate tc_id) — if not, it's a new TC title.
                entry_str = str(entry).strip()
                is_existing_tc = any(
                    uc.get("tc_id", "") == entry_str for uc in updates
                )
                if not is_existing_tc:
                    duplicate_tc_titles.add(entry_str.lower())

    removed = 0
    if duplicate_tc_titles:
        before = len(missing)
        missing = [
            m for m in missing
            if m.get("title", "").lower() not in duplicate_tc_titles
        ]
        removed = before - len(missing)
        if removed:
            logger.info(
                "[cluster_review_node] Removed %d duplicate missing_tests flagged by review "
                "(titles: %s)",
                removed, ", ".join(sorted(duplicate_tc_titles)),
            )

    # --- Collect review additions for the final HTML report ---
    # Convert symmetry_gaps and missing_test_types from each cluster's findings
    # into the same shape as missing_tests items so generate_report_node can
    # render them in a separate "Review Added" section.
    review_additions: List[dict] = []
    for f in all_findings:
        if "error" in f:
            continue
        # Use the cluster name we set in Python (line 3705), not whatever
        # the LLM may have returned in its JSON response.
        review_cluster = f.get("cluster", "Unknown")
        for gap in f.get("symmetry_gaps", []):
            review_additions.append({
                "title": gap.get("suggested_title", f"Symmetry gap: {gap.get('entity', '?')}"),
                "cluster": review_cluster,
                "review_reason": gap.get("reason", ""),
                "review_type": "symmetry_gap",
                "action": gap.get("action", "new_tc"),
                "update_tc_id": gap.get("update_tc_id", ""),
                "steps": gap.get("steps", []),
            })
        for mt in f.get("missing_test_types", []):
            review_additions.append({
                "title": mt.get("suggested_title", f"Missing test type: {mt.get('entity', '?')}"),
                "cluster": review_cluster,
                "review_reason": mt.get("missing", ""),
                "review_type": "missing_test_type",
                "action": mt.get("action", "new_tc"),
                "update_tc_id": mt.get("update_tc_id", ""),
                "steps": mt.get("steps", []),
            })
        for snt in f.get("should_be_new_tc", []):
            review_additions.append({
                "title": snt.get("suggested_title", f"Should be new TC: {snt.get('tc_id', '?')}"),
                "cluster": review_cluster,
                "review_reason": snt.get("reason", ""),
                "review_type": "should_be_new_tc",
                "action": snt.get("action", "new_tc"),
                "update_tc_id": snt.get("update_tc_id", ""),
                "steps": snt.get("steps", []),
            })

    logger.info(
        "[cluster_review_node] Review additions: %d (symmetry_gaps + missing_types + new_tc suggestions)",
        len(review_additions),
    )

    _pass_stats = dict(state.get("pass_stats") or {})
    _pass_stats["pass2"] = {
        "review_additions": len(review_additions),
        "symmetry_gaps": sum(1 for r in review_additions if r.get("review_type") == "symmetry_gap"),
        "missing_test_types": sum(1 for r in review_additions if r.get("review_type") == "missing_test_type"),
        "should_be_new_tc": sum(1 for r in review_additions if r.get("review_type") == "should_be_new_tc"),
        "duplicates_removed": removed,
    }

    _update_pipeline_progress(state, "cluster_review", additions=len(review_additions))
    return {
        **state,
        "missing_tests": missing,
        "cluster_review_path": str(review_path),
        "cluster_review_additions": review_additions,
        "pass_stats": _pass_stats,
    }


# ---------------------------------------------------------------------------
# Second-pass + third-pass helpers
# ---------------------------------------------------------------------------

def _normalize_cluster_name(name: str) -> str:
    """Canonical lowercase form used for cluster name comparisons.

    Strips the word 'cluster' (trailing or embedded), collapses whitespace, and
    lowercases.  Used so that 'On/Off Cluster' and 'on/off cluster' compare equal
    but 'Mode' does NOT accidentally match 'Thermostat Mode Cluster'.
    """
    n = re.sub(r"\bcluster\b", "", name, flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


def _cluster_names_match(a: str, b: str) -> bool:
    """Exact equality after normalization — no substring match.

    Prevents 'Mode' from matching 'Thermostat Mode', 'Water Heater Mode', etc.
    """
    return _normalize_cluster_name(a) == _normalize_cluster_name(b)


def _collect_cluster_req_nodes(kg, cluster_name: str) -> List[Any]:
    """Return REQUIREMENT + BEHAVIOR_RULE nodes whose cluster property matches cluster_name."""
    from src.knowledge_graph.base_graph import NodeType
    results = []
    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None:
            continue
        if obj.node_type not in (NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE):
            continue
        node_cluster = (
            obj.properties.get("cluster_name") or
            obj.properties.get("cluster") or ""
        )
        if node_cluster and _cluster_names_match(node_cluster, cluster_name):
            results.append(obj)
    return results


def _collect_spec_section_numbers(kg, cluster_name: str, max_numbers: int = 50) -> str:
    """Extract spec section numbers from KG REQUIREMENT nodes for a cluster.

    Returns a comma-separated string of section numbers like "11.534, 11.536, 11.540".
    The numbers are embedded in requirement ``normative_text`` fields as ``[11.XXX]``
    patterns.
    """
    if kg is None or not hasattr(kg, "_graph"):
        return ""
    from src.knowledge_graph.base_graph import NodeType as _NT
    _SEC_NUM_RE = re.compile(r'\[(\d+\.\d+)\]')
    numbers: set = set()
    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None or obj.node_type not in (_NT.REQUIREMENT, _NT.BEHAVIOR_RULE):
            continue
        node_cluster = (
            obj.properties.get("cluster_name") or
            obj.properties.get("cluster") or ""
        )
        if not node_cluster or not _cluster_names_match(node_cluster, cluster_name):
            continue
        text = obj.properties.get("normative_text") or obj.properties.get("text") or ""
        for m in _SEC_NUM_RE.finditer(text):
            numbers.add(m.group(1))
        if len(numbers) >= max_numbers:
            break
    return ", ".join(sorted(numbers, key=lambda x: float(x))) if numbers else ""


def _collect_cluster_schema_nodes(kg, cluster_name: str) -> dict:
    """Return DM schema nodes (ATTRIBUTE/COMMAND/EVENT/FEATURE) for cluster_name.

    Returns a dict with keys 'attributes', 'commands', 'events', 'features',
    each a list of GraphNode objects.  Used to inject protocol vocabulary into
    the TC expand prompt so the LLM can reference real field names.
    """
    from src.knowledge_graph.base_graph import NodeType
    result: dict = {"attributes": [], "commands": [], "events": [], "features": []}
    type_map = {
        NodeType.ATTRIBUTE: "attributes",
        NodeType.COMMAND:   "commands",
        NodeType.EVENT:     "events",
        NodeType.FEATURE:   "features",
    }
    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None or obj.node_type not in type_map:
            continue
        node_cluster = (
            obj.properties.get("cluster_name") or
            obj.properties.get("cluster") or ""
        )
        if node_cluster and _cluster_names_match(node_cluster, cluster_name):
            result[type_map[obj.node_type]].append(obj)
    return result


def _format_schema_block(schema: dict) -> str:
    """Format cluster schema dict into a compact prompt block."""
    lines = []

    def _node_summary(n) -> str:
        p = n.properties
        parts = [f"0x{p.get('id', ''):04X}" if isinstance(p.get("id"), int) else str(p.get("id", "?"))]
        parts.append(n.label or n.node_id.rsplit("::", 1)[-1])
        conformance = p.get("conformance") or p.get("quality") or ""
        if conformance:
            parts.append(f"[{conformance}]")
        data_type = p.get("data_type") or p.get("type") or ""
        if data_type:
            parts.append(f"({data_type})")
        direction = p.get("direction") or ""
        if direction:
            parts.append(f"← {direction}")
        return " ".join(parts)

    if schema["attributes"]:
        lines.append("Attributes:")
        for n in sorted(schema["attributes"], key=lambda x: x.properties.get("id") or 0):
            lines.append(f"  {_node_summary(n)}")
    if schema["commands"]:
        lines.append("Commands:")
        for n in sorted(schema["commands"], key=lambda x: x.properties.get("id") or 0):
            lines.append(f"  {_node_summary(n)}")
    if schema["events"]:
        lines.append("Events:")
        for n in sorted(schema["events"], key=lambda x: x.properties.get("id") or 0):
            lines.append(f"  {_node_summary(n)}")
    if schema["features"]:
        lines.append("Features:")
        for n in sorted(schema["features"], key=lambda x: x.properties.get("bit") or 0):
            p = n.properties
            code = p.get("code") or ""
            bit = p.get("bit", "?")
            label = n.label or n.node_id.rsplit("::", 1)[-1]
            lines.append(f"  bit={bit} {code} {label}")

    return "\n".join(lines)


def _collect_cluster_section_text(
    kg,
    cluster_name: str,
    extra_section_prefixes: Optional[List[str]] = None,
    max_chars: int = 15000,
    raw_chunks: Optional[List[Any]] = None,
) -> str:
    """Collect spec section prose for the cluster's expand prompt.

    Priority:
      1a. Cluster-specific chunks from raw_chunks (pr_chunks + spec_chunks) — latest PR text.
      1b. Cross-cutting protocol chunks from raw_chunks (doc_type=pr_change, no cluster
          metadata) — IDM, CASE session, commissioning, QR code, security, etc.
          These fill the remaining char budget after cluster-specific chunks.
      2.  KG SECTION nodes (fallback when no relevant chunks found).

    All automatic — no user input required in CI.
    Cluster-specific gets up to 70 % of max_chars; cross-cutting fills the rest.
    max_chars=0 means no limit (include full section text).
    """
    cluster_lower = cluster_name.lower().replace(" cluster", "").strip()

    # Resolve VirtualCluster names to human-readable chapter keywords for chunk matching.
    if cluster_lower.startswith("virtualcluster-"):
        _kws = _VC_TO_CHAPTER_KEYWORDS.get(cluster_lower, [])
        if _kws:
            cluster_lower = _kws[0]

    unlimited = max_chars <= 0

    # ── Priority 1: live chunks from PR/spec HTML ────────────────────────────
    if raw_chunks:
        seen_content: set = set()
        parts: List[str] = []
        total = 0

        # 1a — cluster-specific: cluster name in metadata OR page_content
        cluster_budget = max_chars if unlimited else int(max_chars * 0.70)
        ordered = sorted(
            raw_chunks,
            key=lambda c: (0 if cluster_lower in (c.metadata.get("cluster") or "").lower() else 1),
        )
        for chunk in ordered:
            if not _chunk_matches_cluster(chunk, cluster_lower):
                continue
            text = chunk.page_content.strip()
            if not text or text in seen_content:
                continue
            seen_content.add(text)
            remaining = cluster_budget - total
            if not unlimited and remaining <= 0:
                break
            if not unlimited and len(text) > remaining:
                if not parts:
                    parts.append(text[:remaining])
                    total += remaining
                continue
            source = chunk.metadata.get("source", "")
            header = f"--- {source} ---\n" if source else ""
            parts.append(header + text)
            total += len(text)

        # 1b — cross-cutting protocol sections: pr_change chunks with no cluster metadata
        #       (IDM ch.8, security ch.10, CASE session, commissioning, QR code, etc.)
        cross_budget = max_chars - total if not unlimited else 0
        cross_used = 0
        if unlimited or cross_budget > 500:
            for chunk in raw_chunks:
                if chunk.metadata.get("doc_type") != "pr_change":
                    continue
                if chunk.metadata.get("cluster", "").strip():
                    continue  # cluster-assigned, not cross-cutting
                text = chunk.page_content.strip()
                if not text or text in seen_content:
                    continue
                seen_content.add(text)
                if not unlimited:
                    remaining = cross_budget - cross_used
                    if remaining <= 0:
                        break
                    if len(text) > remaining:
                        if not cross_used:
                            parts.append(text[:remaining])
                            cross_used += remaining
                        continue
                parts.append(text)
                cross_used += len(text)

        if parts:
            return "\n\n".join(parts)

    # ── Priority 2: KG SECTION nodes (built at last --build-knowledge-graph time) ──
    if kg is None or not hasattr(kg, "_graph"):
        return ""

    from src.knowledge_graph.base_graph import NodeType
    candidates: List[tuple] = []  # (section_path, full_text, label)
    seen_ids: set = set()

    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None or obj.node_type != NodeType.SECTION:
            continue
        full_text = (obj.properties.get("full_text") or "").strip()
        if not full_text:
            continue
        section_path = (obj.properties.get("section_path") or "").strip()
        label = obj.label or _nid

        is_tier1 = cluster_lower in section_path.lower()

        is_tier2 = False
        if extra_section_prefixes:
            for prefix in extra_section_prefixes:
                p = prefix.strip()
                if p and (section_path.startswith(p) or p in section_path):
                    is_tier2 = True
                    break

        if not (is_tier1 or is_tier2):
            continue
        if _nid in seen_ids:
            continue
        seen_ids.add(_nid)
        candidates.append((section_path, full_text, label))

    # Follow REFERENCES edges from matched sections (up to depth 3, no circular refs)
    _MAX_CROSSREF_DEPTH = 3
    if candidates and hasattr(kg, "_graph"):
        crossref_queue = [(nid, 1) for nid in seen_ids]
        while crossref_queue:
            src_nid, depth = crossref_queue.pop(0)
            if depth > _MAX_CROSSREF_DEPTH:
                continue
            for _, tgt, edata in kg._graph.out_edges(src_nid, data=True):
                edge_type = (edata.get("edge_type") or "").lower()
                if edge_type != "references":
                    continue
                if tgt in seen_ids:
                    continue
                tgt_data = kg._graph.nodes.get(tgt, {})
                tgt_obj = tgt_data.get("obj")
                if tgt_obj is None or tgt_obj.node_type != NodeType.SECTION:
                    continue
                tgt_text = (tgt_obj.properties.get("full_text") or "").strip()
                if not tgt_text:
                    continue
                seen_ids.add(tgt)
                tgt_path = (tgt_obj.properties.get("section_path") or "").strip()
                tgt_label = tgt_obj.label or tgt
                candidates.append((tgt_path, tgt_text, f"{tgt_label} [ref depth={depth}]"))
                crossref_queue.append((tgt, depth + 1))

    # Ancestors first (shorter paths)
    candidates.sort(key=lambda x: len(x[0]))

    parts: List[str] = []
    total = 0
    for path, text, label in candidates:
        if not unlimited and total >= max_chars:
            break
        remaining = max_chars - total
        if not unlimited and len(text) > remaining:
            if not parts:
                parts.append(f"--- {label} ---\n{text[:remaining]}")
                total += remaining
            continue
        parts.append(f"--- {label} ---\n{text}")
        total += len(text)

    return "\n\n".join(parts)


def _collect_cluster_tc_nodes(kg, cluster_name: str) -> List[Any]:
    """Return TEST_CASE nodes whose *primary* cluster matches cluster_name.

    Post-filters kg.get_test_cases_for_cluster() results by the TC's cluster
    property to exclude cross-cluster TCs that happen to have KG edges to
    entities in this cluster (e.g. TC-CC-* with OO.S.C01 PICS get a
    `tests → CLUSTER::On/Off` edge from Pass 2 edge traversal in the KG).

    Falls back to a word-level substring match (fuzzy) when no exact-match TCs
    exist — this handles protocol-level chapter names like "Commissioning" that
    do not map to a single DM cluster but whose TCs are named "General
    Commissioning Cluster", "Network Commissioning Cluster", etc.
    """
    cl_norm = _normalize_cluster_name(cluster_name)

    def _exact(tc) -> bool:
        return _cluster_names_match(
            tc.properties.get("cluster_name") or tc.properties.get("cluster") or "",
            cluster_name,
        )

    def _fuzzy(tc) -> bool:
        tc_norm = _normalize_cluster_name(
            tc.properties.get("cluster_name") or tc.properties.get("cluster") or ""
        )
        return cl_norm and cl_norm in tc_norm

    if hasattr(kg, "get_test_cases_for_cluster"):
        raw = kg.get_test_cases_for_cluster(cluster_name)
        filtered = [tc for tc in raw if _exact(tc)]
        if filtered:
            return filtered
        # Fuzzy fallback: primary cluster name appears inside TC cluster name
        return [tc for tc in raw if _fuzzy(tc)]

    from src.knowledge_graph.base_graph import NodeType
    results = []
    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None or obj.node_type != NodeType.TEST_CASE:
            continue
        node_cluster = (
            obj.properties.get("cluster_name") or
            obj.properties.get("cluster") or ""
        )
        if node_cluster and _cluster_names_match(node_cluster, cluster_name):
            results.append(obj)
    return results


_SECTION_D_MAX = 25           # cap total TCs shown in Section D
_VECTOR_SCORE_THRESHOLD = 0.40  # minimum FAISS score to include a vector-hit TC

def _format_all_cluster_tcs(
    kg,
    cluster_name: str,
    vector_hits: List[Any] = None,
) -> str:
    """Return full TC content for TCs covering *cluster_name* (Section D).

    Primary source: KG structural match (exact then fuzzy-subdomain).
    Supplement: FAISS vector hits above *_VECTOR_SCORE_THRESHOLD* when the KG
    returns fewer than *_SECTION_D_MAX* TCs (handles protocol-level cluster names
    like "Commissioning" that don't map to a single DM cluster).
    Total Section D TCs are capped at *_SECTION_D_MAX* to stay within LLM context.

    Each TC is formatted with its purpose, PICS codes, and adoc_section content
    (capped at 3 000 chars each to stay within a 200 K context budget).
    """
    all_tcs: Dict[str, Any] = {}

    if kg is not None:
        for node in _collect_cluster_tc_nodes(kg, cluster_name):
            tc_id = node.properties.get("tc_id") or node.node_id
            if tc_id and len(all_tcs) < _SECTION_D_MAX:
                all_tcs[tc_id] = node

    # Supplement with FAISS vector hits when KG structural match found few TCs.
    # This is especially useful for protocol-level cluster names (e.g. "Commissioning")
    # where the KG has no single matching cluster node but FAISS returns semantically
    # relevant TCs (General Commissioning, Network Commissioning, etc.).
    if kg is not None and vector_hits and len(all_tcs) < _SECTION_D_MAX:
        from src.knowledge_graph.base_graph import NodeType as _NT
        # Build tc_id → KG node map over ALL TC nodes (not cluster-filtered)
        tc_map: Dict[str, Any] = {}
        for _nid, data in kg._graph.nodes(data=True):
            obj = data.get("obj")
            if obj and obj.node_type == _NT.TEST_CASE:
                tid = obj.properties.get("tc_id") or obj.node_id
                if tid:
                    tc_map[tid] = obj
        # Add hits sorted by descending score, stopping at cap or threshold
        sorted_hits = sorted(
            vector_hits, key=lambda x: getattr(x, "score", 0.0), reverse=True
        )
        for sr in sorted_hits:
            if len(all_tcs) >= _SECTION_D_MAX:
                break
            if getattr(sr, "score", 0.0) < _VECTOR_SCORE_THRESHOLD:
                break  # sorted — all remaining are below threshold
            tc_id = getattr(sr, "metadata", {}).get("tc_id", "")
            if not tc_id or tc_id in all_tcs:
                continue
            node = tc_map.get(tc_id)
            if node:
                all_tcs[tc_id] = node

    if not all_tcs:
        return "_No test cases found for this cluster._"

    _TC_ADOC_CAP = 3000
    lines: List[str] = [
        f"**{len(all_tcs)} test case(s) for cluster '{cluster_name}' (KG):**\n"
    ]
    for tc_id, tc in sorted(all_tcs.items()):
        title = tc.properties.get("title", "") or tc.label or ""
        purpose = (tc.properties.get("purpose", "") or "")[:300]
        pics_raw = tc.properties.get("pics_codes") or []
        pics_str = ", ".join(pics_raw) if isinstance(pics_raw, list) else str(pics_raw)
        adoc = (tc.properties.get("adoc_section", "") or "")[:_TC_ADOC_CAP]

        lines.append(f"### {tc_id}: {title}")
        if pics_str:
            lines.append(f"PICS: {pics_str}")
        if purpose:
            lines.append(f"Purpose: {purpose}")
        if adoc:
            lines.append(adoc)
        lines.append("")

    return "\n".join(lines)


def _cluster_to_tc_prefix(cluster_name: str) -> str:
    """Derive a short TC prefix from a cluster name.

    Examples: 'Push AV Stream Transport Cluster' → 'PAVST'
              'On/Off Cluster' → 'OO'

    Overrides for clusters whose initials collide with an existing registered prefix.
    Add entries here when a new cluster produces a duplicate.
    """
    _OVERRIDES: Dict[str, str] = {
        # canonical lowercase (no 'cluster') → forced prefix
        "on/off": "OO",
        "general commissioning": "CGEN",
        "network commissioning": "CNET",
        "administrator commissioning": "CADMIN",
        "basic information": "BINFO",
        "descriptor": "DESC",
        "binding": "BIND",
        "access control": "ACE",
        "actions": "ACT",
        "ota software update requestor": "OTARQ",
        "ota software update provider": "OTAPV",
    }
    norm = _normalize_cluster_name(cluster_name)
    if norm in _OVERRIDES:
        return _OVERRIDES[norm]

    # Strip trailing "cluster" word (already done by _normalize_cluster_name, but
    # repeat here on the original so re.findall gets the right tokens)
    name = re.sub(r"\bcluster\b", "", cluster_name, flags=re.I).strip()
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not words:
        return "UNK"
    if len(words) == 1:
        return words[0][:6].upper()
    return "".join(w if w.isupper() and len(w) <= 4 else w[0].upper() for w in words)


def _derive_prefix_from_existing_tcs(tc_nodes: list, cluster_name: str) -> str:
    """Return the canonical TC prefix used by existing TCs for this cluster.

    Counts TC-ID prefixes from the existing test cases and returns the most
    common one.  Falls back to _cluster_to_tc_prefix() when no TCs exist.
    This prevents second_pass from abbreviating 'Push AV Stream Transport'
    as 'PAST' when all existing TCs use 'PAVST'.
    """
    from collections import Counter
    counts: Counter = Counter()
    for node in tc_nodes:
        tc_id = (
            node.properties.get("tc_id")
            if hasattr(node, "properties")
            else (node.get("tc_id") if isinstance(node, dict) else None)
        ) or (node.node_id if hasattr(node, "node_id") else "")
        if tc_id.startswith("TC-"):
            parts = tc_id.split("-")
            if len(parts) >= 2:
                counts[parts[1]] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return _cluster_to_tc_prefix(cluster_name)


def _build_tc_entities_map(vector_store) -> Dict[str, Dict]:
    """Build tc_id → {entity_refs, intents} from FAISS sidecar intent_summary chunks."""
    result: Dict[str, Dict] = {}
    if vector_store is None:
        return result
    entries = getattr(vector_store, "_entries", None)
    if not entries:
        return result
    for entry in entries:
        meta = entry.metadata if hasattr(entry, "metadata") else (entry.get("metadata") or {})
        tc_id = meta.get("tc_id")
        if not tc_id or meta.get("chunk_type") != "intent_summary":
            continue
        result[tc_id] = {
            "entity_refs": list(meta.get("entity_refs") or []),
            "intents": list(meta.get("intents") or []),
        }
    return result


def _format_existing_tcs_for_consolidation(
    kg,
    cluster_name: str,
    update_candidate_tc_ids: set,
    tc_entities_map: Dict,
) -> str:
    """Format existing TCs for the consolidation prompt.

    TCs in update_candidate_tc_ids get their full adoc_section (LLM needs the existing
    steps to write continuation steps). All other TCs get a compact entity-based summary
    drawn from the vector DB sidecar (entity_refs + intents) — precise and title-independent.
    """
    if kg is None:
        return "  (no KG available)"

    lines: List[str] = []
    for node in _collect_cluster_tc_nodes(kg, cluster_name):
        tc_id = node.properties.get("tc_id") or node.node_id
        title = node.properties.get("title") or node.label or tc_id
        dut_type = node.properties.get("dut_type", "")
        header = f"{tc_id} [{dut_type}]" if dut_type else tc_id

        if tc_id in update_candidate_tc_ids:
            adoc = (node.properties.get("adoc_section") or "").strip()
            if adoc:
                lines.append(f"[UPDATE CANDIDATE]\n{adoc}")
                continue
            # Fall through to compact if adoc_section is missing

        # Compact entity-based summary from vector DB
        ev = tc_entities_map.get(tc_id, {})
        refs = ev.get("entity_refs") or []
        intents = ev.get("intents") or []

        attrs  = [r.split("::")[-1] for r in refs if "::ATTRIBUTE::" in r or r.startswith("ATTRIBUTE::")]
        cmds   = [r.split("::")[-1] for r in refs if "::COMMAND::"   in r or r.startswith("COMMAND::")]
        events = [r.split("::")[-1] for r in refs if "::EVENT::"     in r or r.startswith("EVENT::")]
        protos = [r.split("::")[-1] for r in refs if r.startswith("PROTO::")]

        entry_lines = [f"{header}: {title}"]
        if cmds:
            entry_lines.append(f"  Commands: {', '.join(cmds[:8])}")
        if attrs:
            entry_lines.append(f"  Attributes: {', '.join(attrs[:8])}")
        if events:
            entry_lines.append(f"  Events: {', '.join(events[:6])}")
        if protos:
            entry_lines.append(f"  Protocol: {', '.join(protos[:6])}")
        if intents:
            entry_lines.append(f"  Operations: {', '.join(intents[:6])}")
        if not (cmds or attrs or events or protos or intents):
            # Last resort: use purpose from KG node
            purpose = (node.properties.get("purpose") or "")[:200].replace("\n", " ").strip()
            if purpose:
                entry_lines.append(f"  Purpose: {purpose}")
        lines.append("\n".join(entry_lines))

    return "\n\n".join(lines) if lines else "  (none)"


def _format_cluster_tcs_compact(kg, cluster_name: str, max_chars: int = 15_000) -> str:
    """Return a compact TC summary (TC-ID + title + purpose) for consolidation prompts.

    Unlike _format_all_cluster_tcs (which returns full adoc at 3 000 chars/TC),
    this returns ~150 chars per TC so 25 TCs fit in ~4 000 chars instead of 75 000.
    The consolidation LLM needs coverage context, not full step lists.
    """
    if kg is None:
        return "  (no KG available)"
    lines: List[str] = []
    total = 0
    for node in _collect_cluster_tc_nodes(kg, cluster_name):
        tc_id = node.properties.get("tc_id") or node.node_id
        title = node.properties.get("title") or tc_id
        purpose = (node.properties.get("purpose") or "")[:200].replace("\n", " ").strip()
        line = f"  {tc_id}: {title}"
        if purpose:
            line += f"\n    Purpose: {purpose}"
        if total + len(line) > max_chars:
            lines.append("  ... (truncated)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) if lines else "  (none)"


# ---------------------------------------------------------------------------
# Protocol chapter ↔ VirtualCluster bridging
# ---------------------------------------------------------------------------
# PR chunks use human-readable chapter headings ("Secure Channel"), while the KG
# stores protocol TCs under synthetic "VirtualCluster-SC" names.  These maps let
# second_pass_tc_gen_node recognise protocol clusters as PR-relevant and let
# _collect_cluster_section_text find matching PR chunks for virtual clusters.

_PROTOCOL_CHAPTER_TO_VC: Dict[str, str] = {
    "secure channel": "VirtualCluster-SC",
    "interaction model": "VirtualCluster-IDM",
    "interaction data model": "VirtualCluster-IDM",
    "bulk data exchange": "VirtualCluster-BDX",
    "commissioning": "VirtualCluster-DD",
    "device discovery": "VirtualCluster-DD",
    "device attestation": "VirtualCluster-DA",
    "access control": "VirtualCluster-ACE",
    "multicast": "VirtualCluster-MC",
    "joining fabric": "VirtualCluster-JF",
    "matter core": "VirtualCluster-MCORE",
}

_VC_TO_CHAPTER_KEYWORDS: Dict[str, List[str]] = {
    # Unambiguous protocol-only VCs — no real DM cluster with similar name
    "virtualcluster-sc": ["secure channel"],
    "virtualcluster-idm": ["interaction model", "interaction data model"],
    "virtualcluster-bdx": ["bulk data exchange"],
    "virtualcluster-dd": ["commissioning", "device discovery"],
    "virtualcluster-da": ["device attestation"],
    "virtualcluster-ace": ["access control enforcement"],
    "virtualcluster-mc": ["multicast"],
    "virtualcluster-jf": ["joining fabric"],
    "virtualcluster-jfadmin": ["joining fabric administrator"],
    "virtualcluster-mcore": ["matter core"],
    "virtualcluster-su": ["software update", "ota software update"],
    "virtualcluster-rr": ["minimal resource requirements"],
    "virtualcluster-gc": ["groupcast", "group communication"],
    # Ambiguous VCs — these have real DM clusters with similar names.
    # Use NARROW keywords that match ONLY the protocol test plan section,
    # NOT the cluster spec section. Include "test plan" suffix to disambiguate.
    "virtualcluster-br": ["bridged device test plan"],
    "virtualcluster-webrtc": ["webrtc transport test plan"],
    "virtualcluster-pavsti": ["push av stream transport interop", "push av stream transport test plan"],
    "virtualcluster-dt": ["device types test plan"],
    "virtualcluster-sm": ["system model test plan"],
    "virtualcluster-icdb": ["icd behavior test plan"],
}

_CONSOLIDATION_SYSTEM = (
    "You are a senior Matter protocol test engineer performing a consolidation pass over "
    "a first-pass list of proposed test case changes. Your job is to produce a clean, "
    "non-redundant, functionally complete final list.\n\n"
    "Matter test case conventions to enforce:\n"
    "- TC headings: == TC-<PREFIX>-<X>.<Y> [DUT as Server] or [DUT as Client]\n"
    "- Sub-sections: === Purpose, === PICS, === Test Environment, === Procedure, === Expected Results\n"
    "- PICS codes: use the correct server (.S.) or client (.C.) side for the DUT type; "
    "bit numbers start at 0 (LT feature = bit 0, not bit 2)\n"
    "- Procedure steps are numbered 1. 2. 3. with inline expected results: "
    "'TH reads X from DUT. Expected: <value>.'\n"
    "- Provisional features: gate with PICS (e.g. CLUSTER.S.F00) and note [PROVISIONAL] in purpose\n"
    "- Feature-gated attributes/commands: include the feature PICS alongside the entity PICS\n"
    "- TC-ID numbering: NEG infix for negative/error-path tests (TC-OO-NEG-1.1); "
    "never reuse a number already in the existing TC list\n\n"
    "Return ONLY valid JSON (no markdown fences, no prose)."
)

_CONSOLIDATION_PROMPT_TEMPLATE = """\
=== CLUSTER: {cluster_name} ==={batch_note}
COMPLETE UPDATED CLUSTER SPEC (ground truth — use this to verify functional completeness):
{cluster_spec_text}

NORMATIVE REQUIREMENTS from Knowledge Graph ({req_count} total):
{req_block}

EXISTING TEST CASES ({existing_count} TCs — update candidates shown with full steps, others as entity summary):
{existing_tcs_full}

FIRST-PASS PROPOSED CHANGES (raw, may contain duplicates):

New test cases proposed ({missing_count}):
{pass1_missing_block}

Update candidates proposed ({updates_count}):
{pass1_updates_block}

TASK — CONSOLIDATION:
{batch_task_note}
IMPORTANT: Your ONLY job is to deduplicate. Do NOT add new test cases that are not already
in the FIRST-PASS PROPOSED CHANGES above. If you think a requirement is uncovered, ignore it —
coverage gap analysis is handled by a separate pipeline stage. Return ONLY the deduplicated
subset of the proposals shown above.

Step 1 — Remove duplicates from the proposed new test cases.
  Two new TCs are duplicates if they test the same behavior (even if they have different titles
  or different TC-IDs). Keep the most complete/specific version. Remove the others.

Step 2 — Remove duplicates between new TCs and update candidates.
  If a new TC proposes the same behavior that an update_candidate already adds to an existing TC,
  remove the new TC and keep the update_candidate (prefer updating an existing TC over creating
  a new one when the scope is compatible).

Step 3 — Update candidates: for each one, confirm the referenced TC-ID exists in
  EXISTING TEST CASES. If the TC-ID does not exist, convert it to a missing_tests entry.

TC numbering rules (CRITICAL):
- PRESERVE the exact TC-IDs assigned in the FIRST-PASS PROPOSED CHANGES above.
  Do NOT renumber them unless their ID conflicts with an existing TC listed in EXISTING TEST CASES.
- For any NEW TCs, assign the next available minor version after the highest
  existing TC number for this cluster/prefix.
- Use ONLY the TC prefix shown in the cluster header above (TC prefix: {prefix}).
  Do NOT invent a new prefix or abbreviation.

Return ONLY valid JSON:
{{
  "missing_tests": [
    {{
      "title": "TC-{prefix}-2.X [DUT as Server]",
      "cluster": "{cluster_name}",
      "adoc_section": "== TC-{prefix}-2.X [DUT as Server]\\n\\n=== Purpose\\n...\\n\\n=== Specification Mapping\\n* <spec section paths>\\n\\n=== PICS\\n* {prefix}.S\\n\\n=== Precondition\\n|===\\n|**#**|*Doc. Ref.*|*Condition*|*Notes*\\n| 1 | | DUT commissioned to TH |\\n|===\\n\\n=== Test Procedure\\n[cols=\\"6%,47%,47%\\"]\\n|===\\n|# |Test Step |Expected Outcome\\n\\n| 1\\n| TH reads attribute X from DUT.\\n| DUT returns SUCCESS with value Y.\\n|===",
      "consolidation_reason": "one-sentence reason this TC is in the final list"
    }}
  ],
  "update_candidates": [
    {{
      "tc_id": "TC-{prefix}-2.1",
      "change_summary": "what to add/change and why",
      "adoc_section": "== TC-{prefix}-2.1 [DUT as Server]\\n\\n=== Purpose\\n...\\n\\n=== Test Procedure\\n[cols=\\"6%,47%,47%\\"]\\n|===\\n|# |Test Step |Expected Outcome\\n\\n| 1\\n| ...\\n| ...\\n|===",
      "consolidation_reason": "one-sentence reason this update is in the final list"
    }}
  ],
  "removed_duplicates": [
    {{
      "title": "duplicate TC title",
      "reason": "duplicate of TC-X.Y or covered by update to TC-X.Y"
    }}
  ]
}}
"""

_OUTLINE_SYSTEM = (
    "You are a senior Matter specification test engineer. "
    "Design a complete, non-redundant test plan for a Matter cluster "
    "based on its normative requirements and existing test cases. "
    "Return ONLY valid JSON (no markdown fences, no prose)."
)

_OUTLINE_PROMPT_TEMPLATE = """\
=== CLUSTER: {cluster_name} (TC prefix: {prefix}) ===

EXISTING TEST CASES ({existing_count} total):
{existing_tc_block}

NORMATIVE REQUIREMENTS ({req_count} total, grouped by section):
{req_block}

TASK — COMPLETE TC OUTLINE:
Design a complete set of test cases that covers ALL normative requirements above.
- Include existing TCs if they already cover requirements (mark is_existing=true, keep tc_id)
- Add NEW test cases for requirements not covered by any existing TC
- Each new TC should cover a coherent set of related requirements
- Prefer DUT-as-Server tests unless the requirement is client-only
- Include positive (happy-path) and negative/error tests where requirements mandate them

IMPORTANT — END-TO-END LIFECYCLE FLOW TEST CASES:
For clusters that have multi-step operations (e.g. allocate/create → configure → activate →
use/trigger → verify → cleanup/deallocate), design at least one end-to-end flow TC per major
operation lifecycle.  A lifecycle flow TC exercises the full command sequence in a single test
rather than testing each command/attribute in isolation.  These complement the feature-isolated
unit TCs and catch sequencing bugs that isolated tests miss.

When designing lifecycle flow TCs:
  - Chain ALL relevant commands in the natural order: setup → allocate/create → configure →
    activate → trigger → observe/verify → modify/update → deallocate/cleanup
  - Include verification steps between commands (read back attributes, check events)
  - Cover any conditional branches that depend on the outcome of a prior step
  - The TC title should clearly indicate it is an end-to-end flow (e.g. "Full Lifecycle",
    "End-to-End", "Allocation and Activation Flow", "Session Lifecycle")

Return ONLY valid JSON (no markdown fences):
{{
  "cluster": "{cluster_name}",
  "tc_prefix": "{prefix}",
  "test_plan": [
    {{
      "tc_id": "TC-{prefix}-2.1",
      "title": "Short descriptive title",
      "dut_type": "Server",
      "purpose": "One-sentence purpose statement",
      "test_type": "unit|lifecycle_flow|negative",
      "sections_covered": ["11.3.4. Attribute List"],
      "req_ids": ["REQ::CLUSTER::0", "REQ::CLUSTER::1"],
      "pics": ["CLUSTER.S.A0001"],
      "estimated_steps": 5,
      "is_existing": false,
      "human_notes": ""
    }}
  ]
}}

Rules:
- For new TCs: assign sequential IDs starting after the highest existing TC number
- is_existing=true for TCs already in the existing list above
- human_notes is for human editors — leave empty now
- sections_covered: use section_path strings from the requirements list
- req_ids: use node IDs from the NORMATIVE REQUIREMENTS list
- test_type: "unit" for feature-isolated tests, "lifecycle_flow" for end-to-end flows, "negative" for error-path tests\
"""

_COVERAGE_GAP_OUTLINE_SYSTEM = (
    "You are a senior Matter protocol test engineer. "
    "Design test cases to cover spec requirements that currently have NO test coverage. "
    "Return ONLY valid JSON (no markdown fences, no prose)."
)

_COVERAGE_GAP_OUTLINE_TEMPLATE = """\
=== CLUSTER: {cluster_name} (TC prefix: {prefix}) ===

EXISTING TEST CASES ({existing_count} total):
{existing_tc_block}

UNCOVERED REQUIREMENTS ({uncovered_count} — no existing TC covers these):
{uncovered_req_block}

TASK — COVERAGE GAP TC OUTLINE:
Design test cases that cover ONLY the uncovered requirements listed above.
- Include existing TCs as context (mark is_existing=true) so you avoid duplication
- Entries marked [PLANNED] are TCs already generated in earlier passes of this run — \
treat them as existing and do NOT generate duplicates for requirements they already cover
- Each new TC should cover one or more related uncovered requirements
- If ALL uncovered requirements are already addressed by existing or PLANNED TCs, \
return an empty test_plan: {{"test_plan": []}}
- Use TC prefix: {prefix}
- Assign TC numbers starting after the highest existing TC number
- Each TC purpose must reference specific spec requirements by section number (e.g. [11.534])
- Prefer functional conformance tests that exercise commands with concrete parameters
- Set dut_type: "Server" when testing server-side behavior (attribute reads/writes, command \
handling, event emission). Set dut_type: "Client" when testing client-side behavior \
(client SHALL send requests, discover services, handle responses). Set dut_type: \
"Commissioner" when testing commissioning flows (DUT commissions another device, \
discovers commissionees, performs PASE/CASE establishment).

Return ONLY valid JSON:
{{
  "test_plan": [
    {{
      "tc_id": "TC-{prefix}-X.Y",
      "title": "...",
      "test_type": "unit",
      "dut_type": "Server",
      "is_existing": false,
      "requirements_covered": ["REQ_ID_1", "REQ_ID_2"]
    }}
  ]
}}
"""


def _build_outline_prompt(
    cluster_name: str,
    req_nodes: List[Any],
    existing_tc_nodes: List[Any],
    prefix: str,
    max_total_reqs: int = 120,
    max_req_block_chars: int = 20_000,
) -> str:
    # Group requirements by section
    by_section: Dict[str, List[Any]] = {}
    for r in req_nodes:
        sec = r.properties.get("section_path") or r.properties.get("section", "Other")
        by_section.setdefault(sec, []).append(r)

    req_lines = []
    total_included = 0
    total_chars = 0
    for sec, nodes in sorted(by_section.items()):
        if total_included >= max_total_reqs or total_chars >= max_req_block_chars:
            break
        batch = nodes[:20]  # cap per-section
        remaining = max_total_reqs - total_included
        batch = batch[:remaining]
        sec_header = f"\n[{sec}]"
        req_lines.append(sec_header)
        total_chars += len(sec_header)
        for n in batch:
            if total_chars >= max_req_block_chars:
                break
            nid = n.node_id
            txt = (n.properties.get("normative_text") or n.properties.get("text") or "")[:150]
            line = f"  {nid}: {txt}"
            req_lines.append(line)
            total_chars += len(line)
        total_included += len(batch)
    if total_included < len(req_nodes):
        req_lines.append(f"\n  … ({len(req_nodes) - total_included} more requirements omitted)")

    existing_lines = []
    for tc in existing_tc_nodes:
        tc_id = tc.properties.get("tc_id") or tc.label or tc.node_id
        title = tc.properties.get("title") or tc.label or ""
        existing_lines.append(f"  {tc_id}: {title}")

    return _OUTLINE_PROMPT_TEMPLATE.format(
        cluster_name=cluster_name,
        prefix=prefix,
        existing_count=len(existing_tc_nodes),
        existing_tc_block="\n".join(existing_lines) or "  (none)",
        req_count=len(req_nodes),
        req_block="\n".join(req_lines) or "  (none)",
    )


_EXPAND_SYSTEM = (
    "You are a senior Matter specification test engineer. "
    "Write a complete, publication-quality AsciiDoc test case section for the Matter test plans. "
    "Each numbered test step must include its expected outcome inline at the end of the step line "
    "(e.g. '1. TH reads OnOff from DUT. Expected: TRUE (1).'). "
    "Do NOT add a separate === Expected Results section. "
    "Output ONLY the AsciiDoc text — no markdown fences, no prose, no commentary."
)

_EXPAND_PROMPT_TEMPLATE = """\
Write a complete AsciiDoc test case section.

TC-ID: {tc_id}
Cluster: {cluster_name}
Title: {title}
DUT Type: {dut_type}
Test Type: {test_type}
Purpose: {purpose}
PICS: {pics}
Notes: {human_notes}

CLUSTER DM SCHEMA (use these exact names in test steps — do not invent field names):
{schema_block}

RELEVANT REQUIREMENTS (normative text — use the "spec:" section paths for Specification Mapping):
{req_block}

{section_block}{additional_context_block}{lifecycle_guidance}QUALITY REQUIREMENTS:
- Include a ===== Specification Mapping section listing the spec section paths
  from the RELEVANT REQUIREMENTS above (the "spec:" values). Format as a flat
  comma-separated list: * 5.1. AttributeName Attribute, 5.2. AnotherAttribute Attribute
  Do NOT use internal IDs like REQ::cluster::N — use the human-readable section paths.
- Include a ===== Precondition section as an AsciiDoc table with columns:
  #, Doc. Ref., Condition, Notes — listing device state prerequisites
- Include a ===== Required Devices section as an AsciiDoc table with columns:
  #, Device Name, Description — listing TH and DUT with specific device archetype
- Include a ===== Device Topology section (1-2 sentences: fabric topology, connectivity)
- Use concrete test vector values (specific hex IDs, byte lengths, enum values),
  not placeholders like "a valid key" or "some value"
- Include cross-cluster PICS dependencies (e.g., AVSM.S for camera TCs, TLSCLIENT.S
  for TLS-dependent TCs)
- Minimum 8 procedure steps for functional conformance tests
- Every step must state its expected outcome inline
- Use AsciiDoc table format for Test Procedure: [cols="6%,47%,47%"] with columns
  #, Test Step, Expected Outcome

Output ONLY the AsciiDoc section using this exact structure:

== {tc_id} [{dut_type} as {dut_role}]

=== Purpose
<purpose paragraph — no spec section numbers here>

=== Specification Mapping
* <comma-separated spec section paths from RELEVANT REQUIREMENTS>

=== PICS
* {pics_line}
* <cross-cluster PICS dependencies>

=== Precondition
|===
|**#**|*Doc. Ref.*|*Condition*|*Notes*
| 1 | | DUT has been commissioned to TH |
| 2 | | <additional preconditions> |
|===

=== Required Devices
|===
|#|Device Name|Description
| 1 | TH  | Test Harness Controller
| 2 | DUT | <specific device archetype, e.g., Camera, Smart Light, Door Lock>
|===

=== Device Topology
TH and DUT are on the same fabric. <additional topology details if needed>

=== Test Procedure
[cols="6%,47%,47%"]
|===
|# |Test Step |Expected Outcome
| 1 | Commission DUT to TH. | DUT joins fabric successfully.
| 2 | <step> | <expected outcome>
|===\
"""

_LIFECYCLE_FLOW_GUIDANCE = """\
LIFECYCLE FLOW INSTRUCTIONS (test_type=lifecycle_flow):
This is an END-TO-END flow test.  Chain ALL major operations in natural order:
  1. Pre-condition checks (read current state, clear/reset if needed)
  2. Allocation/creation of the resource (e.g. AllocatePushTransport, Create, Open)
  3. Configuration steps (set parameters, verify they were stored correctly)
  4. Activation (SetStatus, Enable, Activate) and confirmation
  5. Triggering / exercising the live operation (send data, trigger event, read live attribute)
  6. Observation and verification (read back attributes, check events, verify output format)
  7. Modification mid-session (update parameters, verify change takes effect)
  8. Error/edge case in-flow (attempt invalid operation, verify correct error)
  9. Cleanup/deallocation (Deallocate, Close, Delete) and verify resource is gone
Each step must include its expected outcome inline at the end of the step line.

"""


def _expand_tc_from_outline(
    tc_entry: dict,
    cluster_name: str,
    req_nodes: List[Any],
    llm,
    kg=None,
    config=None,
    raw_chunks: Optional[List[Any]] = None,
    pass_name: str = "pass2",
) -> Optional[str]:
    """Call LLM to expand one outline entry into a full adoc section. Returns None on failure."""
    tc_id = tc_entry.get("tc_id", "")
    raw_dut_type = (tc_entry.get("dut_type") or "Server").lower()
    dut_type = "DUT"
    if "commissioner" in raw_dut_type:
        dut_role = "Commissioner"
    elif "client" in raw_dut_type:
        dut_role = "Client"
    else:
        dut_role = "Server"
    test_type = tc_entry.get("test_type", "unit")
    pics_list = tc_entry.get("pics") or []
    pics_str = ", ".join(pics_list) if pics_list else "CLUSTER.S"
    pics_line = "\n".join(pics_list) if pics_list else "CLUSTER.S"
    lifecycle_guidance = _LIFECYCLE_FLOW_GUIDANCE if test_type == "lifecycle_flow" else ""

    # Build DM schema block (attributes, commands, events, features)
    schema_block = "(schema not available)"
    if kg is not None and hasattr(kg, "_graph") and cluster_name:
        try:
            schema = _collect_cluster_schema_nodes(kg, cluster_name)
            formatted = _format_schema_block(schema)
            if formatted.strip():
                schema_block = formatted
        except Exception as _sch_exc:
            logger.debug("[_expand_tc_from_outline] Schema block formatting failed: %s", _sch_exc)

    # Tier 1 + Tier 2: spec section text (PR chunks take priority over KG SECTION nodes)
    section_block = ""
    if cluster_name:
        try:
            extra_prefixes: Optional[List[str]] = None
            section_max = 15000
            if config is not None:
                raw = getattr(config.pipeline, "spec_sections", None) or []
                extra_prefixes = [s for s in raw if s.strip()] or None
                section_max = getattr(config.pipeline, "expand_section_max_chars", 15000)
            section_text = _collect_cluster_section_text(
                kg, cluster_name, extra_prefixes, max_chars=section_max, raw_chunks=raw_chunks
            )
            if section_text.strip():
                section_block = (
                    "CLUSTER SPEC SECTIONS (full protocol prose — use for behavioral"
                    " verification steps):\n" + section_text + "\n\n"
                )
        except Exception as _sec_exc:
            logger.debug("[_expand_tc_from_outline] Section text collection failed: %s", _sec_exc)

    # Tier 3: human-supplied additional context
    additional_context_block = ""
    if config is not None:
        extra_ctx = _load_additional_context(config, pass_name=pass_name)
        if extra_ctx:
            additional_context_block = (
                "ADDITIONAL CONTEXT (human-supplied domain knowledge — follow strictly):\n"
                + extra_ctx + "\n\n"
            )

    # Collect relevant requirement texts by ID
    req_id_set = set(tc_entry.get("req_ids") or [])
    relevant_reqs = [
        r for r in req_nodes
        if r.node_id in req_id_set
    ] or req_nodes[:10]  # fallback: first 10 if no IDs matched

    req_block_lines = []
    for r in relevant_reqs[:15]:
        txt = (r.properties.get("normative_text") or r.properties.get("text") or "")[:300]
        sec_path = r.properties.get("section_path") or ""
        sec_ref = f" (spec: {sec_path})" if sec_path else ""
        req_block_lines.append(f"- [{r.node_id}]{sec_ref} {txt}")

    prompt = _EXPAND_PROMPT_TEMPLATE.format(
        tc_id=tc_id,
        cluster_name=cluster_name,
        title=tc_entry.get("title", ""),
        dut_type=dut_type,
        dut_role=dut_role,
        test_type=test_type,
        purpose=tc_entry.get("purpose", ""),
        pics=pics_str,
        pics_line=pics_line,
        human_notes=tc_entry.get("human_notes") or "",
        schema_block=schema_block,
        req_block="\n".join(req_block_lines) or "(no specific requirements identified)",
        section_block=section_block,
        additional_context_block=additional_context_block,
        lifecycle_guidance=lifecycle_guidance,
    )

    # Inject spec section numbers from KG REQUIREMENT nodes (Approach 2)
    spec_numbers = _collect_spec_section_numbers(kg, cluster_name)
    if spec_numbers:
        prompt += (
            f"\n\nSPEC SECTION NUMBERS for this cluster "
            f"(use in ===== Specification Mapping): {spec_numbers}\n"
        )

    try:
        if hasattr(llm, "set_next_label"):
            llm.set_next_label(f"Pass 2/3 — expand {tc_entry.get('tc_id', '?')}")
        adoc = llm.complete(prompt, system=_EXPAND_SYSTEM)
        adoc = adoc.strip()
        # Strip any accidental markdown fences
        if adoc.startswith("```"):
            adoc = re.sub(r"^```[a-z]*\n?", "", adoc)
            adoc = re.sub(r"\n?```$", "", adoc)
        return adoc if adoc else None
    except Exception as exc:
        logger.warning("[_expand_tc_from_outline] LLM expand failed for %s: %s", tc_id, exc)
        return None


# ---------------------------------------------------------------------------
# Second-pass: holistic TC generation from KG requirements
# ---------------------------------------------------------------------------

@log_node
def second_pass_tc_gen_node(state: PipelineState) -> PipelineState:
    """Node: Holistic TC generation for thin/gap-heavy clusters.

    Trigger conditions (per cluster):
      - PR-relevant AND pass1_missing > 0  (PR touched this cluster and gaps exist)

    For each triggered cluster:
      1. Collect all REQUIREMENT / BEHAVIOR_RULE nodes from KG
      2. Run consolidation LLM call(s) — splits into batches if prompt > 32k chars
      3. If consolidation fails, fall back to outline → expand flow
      4. Merge and deduplicate results across batches
      3. Save outline JSON to output_dir as tc_outline_<slug>_<ts>.json
      4. For each new TC in the outline, run 1 expand LLM call → full adoc section
      5. Append new TCs to missing_tests
    """
    from datetime import datetime

    kg = state.get("knowledge_graph")
    missing = list(state.get("missing_tests") or [])
    config = state["config"]
    output_dir = Path(state.get("output_dir") or "reports")
    run_dir = state.get("run_dir", "")
    raw_chunks: List[Any] = (state.get("pr_chunks") or []) + (state.get("spec_chunks") or [])

    if not kg or not hasattr(kg, "_graph"):
        logger.info("[second_pass_tc_gen_node] No KG available — skipping")
        return state

    # Count missing tests per cluster from pass 1 + cluster_review
    missing_per_cluster: Dict[str, int] = {}
    for item in missing:
        c = (item.get("cluster") or "").lower().strip()
        if c:
            missing_per_cluster[c] = missing_per_cluster.get(c, 0) + 1
    for item in (state.get("cluster_review_additions") or []):
        c = (item.get("cluster") or "").lower().strip()
        if c:
            missing_per_cluster[c] = missing_per_cluster.get(c, 0) + 1

    # Bridge protocol chapter counts to VirtualCluster names so pass1_missing
    # lookup works for VirtualCluster-* entries in all_clusters.
    _extra_mpc: Dict[str, int] = {}
    for _k, _v in missing_per_cluster.items():
        for _chap, _vc in _PROTOCOL_CHAPTER_TO_VC.items():
            if _chap in _k:
                _vc_lower = _vc.lower()
                _extra_mpc[_vc_lower] = _extra_mpc.get(_vc_lower, 0) + _v
                break
    missing_per_cluster.update(_extra_mpc)

    # Build the set of clusters that are actually relevant to the current PR.
    # Only these clusters are eligible for the "sparse TC" trigger (existing < 5).
    # Clusters with a high pass-1 missing count (> 5) can still trigger regardless.
    pr_relevant_clusters: set = set()
    for item in (state.get("pr_changes") or []):
        c = (item.get("cluster") or item.get("cluster_name") or "").lower().strip()
        if c:
            pr_relevant_clusters.add(c)
    for item in (state.get("missing_tests") or []):
        c = (item.get("cluster") or "").lower().strip()
        if c:
            pr_relevant_clusters.add(c)
    for item in (state.get("update_candidates") or []):
        c = (item.get("cluster") or "").lower().strip()
        if c:
            pr_relevant_clusters.add(c)
    for item in (state.get("cluster_review_additions") or []):
        c = (item.get("cluster") or "").lower().strip()
        if c:
            pr_relevant_clusters.add(c)
    # Also include any cluster mentioned in analysis_results
    for item in (state.get("analysis_results") or []):
        c = (item.get("cluster") or "").lower().strip()
        if c:
            pr_relevant_clusters.add(c)

    # Bridge protocol chapter names to VirtualCluster names so protocol
    # clusters become eligible for second-pass TC generation.
    for _prc in list(pr_relevant_clusters):
        for _chap, _vc in _PROTOCOL_CHAPTER_TO_VC.items():
            if _chap in _prc:
                pr_relevant_clusters.add(_vc.lower())
                break

    # Discover all clusters that have requirements or TCs in the KG
    from src.knowledge_graph.base_graph import NodeType
    all_clusters: Dict[str, str] = {}  # lower → canonical
    for _nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None:
            continue
        if obj.node_type in (NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE, NodeType.TEST_CASE):
            c = (
                obj.properties.get("cluster_name") or
                obj.properties.get("cluster") or ""
            ).strip()
            if c:
                all_clusters[c.lower()] = c

    # Apply cluster_filter
    cluster_filter = (state.get("cluster_filter") or "").lower()
    if cluster_filter:
        all_clusters = {k: v for k, v in all_clusters.items() if cluster_filter in k}

    llm = _get_run_llm(config, run_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    _tc_entities_map = _build_tc_entities_map(state.get("vector_store"))
    logger.debug("[second_pass_tc_gen_node] built entity map for %d TCs from vector store", len(_tc_entities_map))

    # LLM call budget: 1 outline call + up to _MAX_EXPAND_PER_CLUSTER expand calls per cluster.
    # Global cap prevents runaway cost on PRs that touch many sparse clusters.
    _MAX_EXPAND_PER_CLUSTER: int = getattr(config.pipeline, "second_pass_expand_cap", 20)
    _raw_global = getattr(config.analysis, "max_llm_calls_per_run", 0) if (
        config and hasattr(config, "analysis")
    ) else 0
    max_global_calls = _raw_global if _raw_global > 0 else 999_999
    global_calls_used = 0

    _pass_stats = dict(state.get("pass_stats") or {})
    _pass3_consolidation_input = 0
    _pass3_consolidation_kept = 0
    _pass3_consolidation_removed = 0
    _pass3_review_input = 0
    new_missing: List[dict] = []
    consolidated_updates_acc: List[dict] = []
    outline_paths: List[str] = []
    consolidated_clusters: set = set()  # clusters whose review items were included in consolidation
    triggered = 0

    for cluster_lower, cluster_name in sorted(all_clusters.items()):
        if global_calls_used >= max_global_calls:
            logger.warning(
                "[second_pass_tc_gen_node] Global LLM budget (%d) reached — stopping early",
                max_global_calls,
            )
            break
        existing_tc_nodes = _collect_cluster_tc_nodes(kg, cluster_name)

        # Sum missing counts using exact normalized match (not substring).
        pass1_missing = sum(
            v for k, v in missing_per_cluster.items()
            if _cluster_names_match(k, cluster_name)
        )

        # Trigger check:
        #   - PR-relevant AND at least 1 missing TC from pass-1: trigger second pass.
        #   - PR-relevant but no gaps: skip (pass-1 found nothing to fix).
        #   - Not PR-relevant: always skip — global KG sparseness is irrelevant here.
        is_pr_relevant = any(
            _cluster_names_match(cluster_name, pr_c)
            for pr_c in pr_relevant_clusters
        ) if pr_relevant_clusters else False

        if not is_pr_relevant:
            logger.debug(
                "[second_pass_tc_gen_node] '%s': not PR-relevant — skip",
                cluster_name,
            )
            continue

        if pass1_missing == 0:
            logger.debug(
                "[second_pass_tc_gen_node] '%s': PR-relevant but pass1_missing=0 — skip",
                cluster_name,
            )
            continue

        req_nodes = _collect_cluster_req_nodes(kg, cluster_name)
        if not req_nodes:
            logger.debug("[second_pass_tc_gen_node] '%s': no requirements — skip", cluster_name)
            continue

        prefix = _derive_prefix_from_existing_tcs(existing_tc_nodes, cluster_name)
        if prefix != _cluster_to_tc_prefix(cluster_name):
            logger.debug(
                "[second_pass_tc_gen_node] '%s': prefix from existing TCs=%r (fallback would be %r)",
                cluster_name, prefix, _cluster_to_tc_prefix(cluster_name),
            )

        # Skip abstract base clusters that have no existing TCs and have sibling
        # clusters with real TCs — generating TC-CMC-2.x for "Concentration Measurement
        # Clusters" is meaningless since the PICS prefix doesn't exist.
        if not existing_tc_nodes:
            _siblings = _find_sibling_clusters(kg, cluster_name)
            if _siblings:
                logger.info(
                    "[second_pass_tc_gen_node] '%s': abstract base cluster with %d siblings"
                    " and 0 existing TCs — skipping (siblings: %s)",
                    cluster_name, len(_siblings), ", ".join(_siblings[:3]),
                )
                continue

        # ---- Consolidation call (1 LLM call) — dedup + functional completeness ----
        # Collects Pass 1 proposals for this cluster, full spec text, existing TCs, and
        # normative requirements, then asks the LLM to produce a clean, non-redundant
        # final list.  If the call succeeds we skip the outline→expand path entirely
        # (the consolidation LLM already generates full adoc for each TC).
        def _cluster_match(item: dict) -> bool:
            return _cluster_names_match(
                (item.get("cluster") or "").lower().strip(), cluster_name
            )

        pass1_missing_items = [m for m in missing if _cluster_match(m)]
        pass1_update_items = [
            u for u in (state.get("update_candidates") or []) if _cluster_match(u)
        ]
        # Include cluster review additions in the consolidation scope so the LLM
        # can detect overlap between Pass 1 TCs and Pass 2 (cluster review) TCs.
        # When cluster_filter is set, include ALL review items — they belong to the
        # filtered cluster regardless of what the LLM put in the "cluster" field.
        _all_review = state.get("cluster_review_additions") or []
        if cluster_filter:
            review_items_for_cluster = list(_all_review)
        else:
            review_items_for_cluster = [r for r in _all_review if _cluster_match(r)]
        if review_items_for_cluster:
            # Convert review items to the same format as pass1_missing for the prompt
            for ri in review_items_for_cluster:
                _ri_title = ri.get("title", "")
                _ri_steps = ri.get("steps", [])
                _ri_adoc = "\n".join(f"  {s}" for s in _ri_steps) if _ri_steps else ""
                pass1_missing_items.append({
                    "title": _ri_title,
                    "cluster": ri.get("cluster", ""),
                    "adoc_section": _ri_adoc,
                    "_from_review": True,
                    "review_type": ri.get("review_type", ""),
                })

        cluster_spec_text = _collect_cluster_section_text(
            kg, cluster_name, max_chars=12000, raw_chunks=raw_chunks
        )
        _update_candidate_ids = {
            item.get("tc_id", "") for item in pass1_update_items if item.get("tc_id")
        }
        existing_tcs_full = _format_existing_tcs_for_consolidation(
            kg, cluster_name, _update_candidate_ids, _tc_entities_map
        )

        # Build req_block for consolidation.
        # Cap at 50 requirements (down from 200) — consolidation's primary job is
        # deduplication and spot-checking first-pass proposals, not comprehensive
        # coverage review (cluster_spec_text already provides the spec ground truth).
        # Priority: requirements from sections touched by first-pass proposals first,
        # then remaining requirements as filler up to the cap.
        _CONSOLIDATION_REQ_CAP = 50
        by_sec: Dict[str, List[Any]] = {}
        for r in req_nodes:
            s = r.properties.get("section_path") or r.properties.get("section", "Other")
            by_sec.setdefault(s, []).append(r)

        # Sections mentioned in first-pass adoc content (rough section-path keywords)
        _pass1_adoc_text = " ".join(
            (item.get("adoc_section", "") or "") for item in pass1_missing_items + pass1_update_items
        ).lower()

        def _sec_is_pass1_relevant(sec: str) -> bool:
            # A section is relevant if any word ≥5 chars from its path appears in pass1 adoc
            return any(
                word in _pass1_adoc_text
                for word in re.split(r"[\s>]+", sec.lower())
                if len(word) >= 5
            )

        priority_secs = sorted(
            [s for s in by_sec if _sec_is_pass1_relevant(s)],
        )
        other_secs = sorted([s for s in by_sec if s not in set(priority_secs)])

        req_lines_c: List[str] = []
        _total_r = 0
        for _sec in priority_secs + other_secs:
            if _total_r >= _CONSOLIDATION_REQ_CAP:
                break
            _rnodes = by_sec[_sec]
            _batch = _rnodes[: min(10, _CONSOLIDATION_REQ_CAP - _total_r)]
            req_lines_c.append(f"\n[{_sec}]")
            for _n in _batch:
                _txt = (
                    _n.properties.get("normative_text") or _n.properties.get("text") or ""
                )[:200]
                _sec_path = _n.properties.get("section_path") or ""
                _sec_ref = f" (spec: {_sec_path})" if _sec_path else ""
                req_lines_c.append(f"  {_n.node_id}{_sec_ref}: {_txt}")
            _total_r += len(_batch)
        if len(req_nodes) > _CONSOLIDATION_REQ_CAP:
            req_lines_c.append(
                f"\n  … ({len(req_nodes) - _CONSOLIDATION_REQ_CAP} more requirements"
                f" omitted — see cluster spec text above for complete normative content)"
            )
        req_block_c = "\n".join(req_lines_c) or "  (none)"

        def _fmt_pass1_missing(items: List[dict]) -> str:
            if not items:
                return "  (none)"
            out = []
            for i, item in enumerate(items, 1):
                title = item.get("title", "")
                adoc = (item.get("adoc_section", "") or "")[:600]
                out.append(f"{i}. {title}\n{adoc}")
            return "\n\n".join(out)

        def _fmt_pass1_updates(items: List[dict]) -> str:
            if not items:
                return "  (none)"
            out = []
            for i, item in enumerate(items, 1):
                tc_id = item.get("tc_id", "")
                summary = item.get("change_summary", "")
                adoc = (item.get("adoc_section", "") or "")[:600]
                out.append(f"{i}. {tc_id}: {summary}\n{adoc}")
            return "\n\n".join(out)

        _pass1_missing_block_text = _fmt_pass1_missing(pass1_missing_items)
        _pass1_updates_block_text = _fmt_pass1_updates(pass1_update_items)

        consolidation_done = False
        triggered += 1
        logger.info(
            "[Pass 3: Consolidation] Triggered: '%s' (proposals=%d [pass1=%d + review=%d], existing_tcs=%d)",
            cluster_name, len(pass1_missing_items),
            len(pass1_missing_items) - len(review_items_for_cluster),
            len(review_items_for_cluster), len(existing_tc_nodes),
        )

        # ── Single consolidation call (dedup-only, no batch split) ────────
        _all_batch_missing: List[dict] = []
        _all_batch_updates: List[dict] = []
        _all_batch_removed: List[dict] = []
        _batch_failed = False

        if global_calls_used >= max_global_calls:
            logger.warning(
                "[second_pass_tc_gen_node] Global LLM budget (%d) reached before consolidation"
                " for '%s'", max_global_calls, cluster_name,
            )
            _batch_failed = True

        if not _batch_failed:
            _consolidation_prompt = _CONSOLIDATION_PROMPT_TEMPLATE.format(
                cluster_name=cluster_name,
                prefix=prefix,
                batch_note="",
                batch_task_note="",
                cluster_spec_text=cluster_spec_text or "(spec text unavailable)",
                req_count=f"{_total_r} of {len(req_nodes)}",
                req_block=req_block_c,
                existing_count=len(existing_tc_nodes),
                existing_tcs_full=existing_tcs_full,
                missing_count=len(pass1_missing_items),
                pass1_missing_block=_pass1_missing_block_text,
                updates_count=len(pass1_update_items),
                pass1_updates_block=_pass1_updates_block_text,
            )

            logger.info(
                "[Pass 3: Consolidation] '%s' — prompt (%d chars)",
                cluster_name, len(_consolidation_prompt),
            )
            _consolidation_prompt = _truncate_prompt_if_needed(
                _consolidation_prompt, config, label="pass3_consolidation"
            )

            for _attempt in range(2):
                try:
                    if hasattr(llm, "set_next_label"):
                        llm.set_next_label(
                            f"Pass 2 — {cluster_name} consolidation"
                            + (f" (retry)" if _attempt > 0 else "")
                        )
                    _batch_raw = llm.complete(_consolidation_prompt, system=_CONSOLIDATION_SYSTEM)
                    global_calls_used += 1
                    _batch_raw = _batch_raw.strip()
                    # Extract JSON from response — LLM may prepend prose before
                    # the JSON block or wrap it in markdown fences.
                    _batch_json_str = _extract_json_object(_batch_raw) or _batch_raw
                    try:
                        _batch_data = _json.loads(_batch_json_str)
                    except _json.JSONDecodeError:
                        _batch_data = _json.loads(_repair_json(_batch_json_str))
                    _all_batch_missing.extend(_batch_data.get("missing_tests", []))
                    _all_batch_updates.extend(_batch_data.get("update_candidates", []))
                    _all_batch_removed.extend(_batch_data.get("removed_duplicates", []))
                    break
                except Exception as exc:
                    if _attempt == 0:
                        logger.warning(
                            "[second_pass_tc_gen_node] Consolidation attempt 1 failed"
                            " for '%s': %s — retrying in 2s",
                            cluster_name, exc,
                        )
                        time.sleep(2)
                    else:
                        logger.warning(
                            "[second_pass_tc_gen_node] Consolidation attempt 2 failed"
                            " for '%s': %s — falling back to outline",
                            cluster_name, exc,
                        )
                        _batch_failed = True

        if not _batch_failed:
            # Build lookup of original pass1 adoc_sections by title (lowercased)
            # so we can restore full content when consolidation truncated it.
            _pass1_adoc_by_title: Dict[str, str] = {}
            for _p1 in pass1_missing_items:
                _p1_title = (_p1.get("title") or "").lower().strip()
                _p1_adoc = (_p1.get("adoc_section") or "").strip()
                if _p1_title and _p1_adoc:
                    _pass1_adoc_by_title[_p1_title] = _p1_adoc

            # Merge batch results — deduplicate by title / tc_id
            _seen_titles: set = set()
            _merged_missing: List[dict] = []
            for tc in _all_batch_missing:
                if not tc.get("title") or not tc.get("adoc_section"):
                    continue
                _key = tc["title"].lower().strip()
                if _key not in _seen_titles:
                    _seen_titles.add(_key)
                    # Prefer the original pass1 adoc_section when the consolidation
                    # LLM produced a shorter version (likely due to the 600-char
                    # truncation in the consolidation prompt).
                    consolidated_adoc = tc["adoc_section"]
                    original_adoc = _pass1_adoc_by_title.get(_key, "")
                    if original_adoc and len(original_adoc) > len(consolidated_adoc):
                        logger.debug(
                            "[second_pass_tc_gen_node] Restoring original pass1 adoc_section "
                            "for '%s' (original=%d chars, consolidated=%d chars)",
                            tc["title"], len(original_adoc), len(consolidated_adoc),
                        )
                        consolidated_adoc = original_adoc
                    _merged_missing.append({
                        "title": tc["title"],
                        "cluster": cluster_name,
                        "adoc_section": consolidated_adoc,
                        "source": "first_pass_consolidated",
                        "consolidation_reason": tc.get("consolidation_reason", ""),
                    })

            # Also build lookup of pass1 update adoc_sections by tc_id
            _pass1_upd_adoc_by_id: Dict[str, str] = {}
            for _p1u in pass1_update_items:
                _p1u_id = (_p1u.get("tc_id") or "").lower().strip()
                _p1u_adoc = (_p1u.get("adoc_section") or "").strip()
                if _p1u_id and _p1u_adoc:
                    _pass1_upd_adoc_by_id[_p1u_id] = _p1u_adoc

            _seen_tc_ids: set = set()
            _merged_updates: List[dict] = []
            for upd in _all_batch_updates:
                if not upd.get("tc_id"):
                    continue
                _uid = upd["tc_id"].lower().strip()
                if _uid not in _seen_tc_ids:
                    _seen_tc_ids.add(_uid)
                    # Restore original pass1 adoc_section if consolidation truncated it
                    _upd_adoc = (upd.get("adoc_section") or "").strip()
                    _orig_upd_adoc = _pass1_upd_adoc_by_id.get(_uid, "")
                    if _orig_upd_adoc and len(_orig_upd_adoc) > len(_upd_adoc):
                        logger.debug(
                            "[second_pass_tc_gen_node] Restoring original pass1 adoc_section "
                            "for update '%s' (original=%d chars, consolidated=%d chars)",
                            upd["tc_id"], len(_orig_upd_adoc), len(_upd_adoc),
                        )
                        upd = {**upd, "adoc_section": _orig_upd_adoc}
                    _merged_updates.append({**upd, "cluster": cluster_name, "source": "second_pass_consolidation"})

            # Replace Pass 1 + review entries for this cluster with consolidated results.
            # The consolidation LLM received both Pass 1 and cluster review items, so its
            # output (_merged_missing) already includes any review items worth keeping.
            missing = [m for m in missing if not _cluster_match(m)]
            new_missing.extend(_merged_missing)
            consolidated_updates_acc.extend(_merged_updates)
            consolidated_clusters.add(cluster_lower)

            _pass3_consolidation_input += len(pass1_missing_items)
            _pass3_review_input += len(review_items_for_cluster)
            _pass3_consolidation_kept += len(_merged_missing)
            _pass3_consolidation_removed += len(_all_batch_removed)

            logger.info(
                "[second_pass_tc_gen_node] '%s': consolidation done"
                " — %d new TCs, %d updates, %d removed duplicates",
                cluster_name,
                len(_merged_missing), len(_merged_updates), len(_all_batch_removed),
            )
            consolidation_done = True

        if consolidation_done:
            continue  # Full adoc already produced by consolidation — skip outline/expand

        logger.info("[Pass 3: Consolidation] '%s' — outline fallback (consolidation failed)", cluster_name)
        _pass3_consolidation_input += len(pass1_missing_items)
        _pass3_review_input += len(review_items_for_cluster)
        outline_prompt = _build_outline_prompt(cluster_name, req_nodes, existing_tc_nodes, prefix)
        try:
            if hasattr(llm, "set_next_label"):
                llm.set_next_label(f"Pass 2 — outline for {cluster_name}")
            outline_raw = llm.complete(outline_prompt, system=_OUTLINE_SYSTEM)
            global_calls_used += 1
            outline_raw = outline_raw.strip()
            _outline_json_str = _extract_json_object(outline_raw) or outline_raw
            if not _outline_json_str:
                _outline_json_str = '{"test_plan": []}'
            try:
                outline_data = _json.loads(_outline_json_str)
            except _json.JSONDecodeError:
                outline_data = _json.loads(_repair_json(_outline_json_str))
        except Exception as exc:
            logger.warning(
                "[second_pass_tc_gen_node] Outline call failed for '%s': %s", cluster_name, exc
            )
            continue

        # Save outline JSON (human-editable; used by human_outline_expand_node)
        cluster_slug = re.sub(r"[^a-z0-9]+", "_", cluster_lower).strip("_")
        outline_file = output_dir / f"tc_outline_{cluster_slug}_{ts}.json"
        try:
            outline_file.write_text(_json.dumps(outline_data, indent=2), encoding="utf-8")
            outline_paths.append(str(outline_file))
            logger.info(
                "[second_pass_tc_gen_node] Outline saved → %s (%d TCs)",
                outline_file, len(outline_data.get("test_plan", [])),
            )
        except Exception as exc:
            logger.warning(
                "[second_pass_tc_gen_node] Could not save outline for '%s': %s", cluster_name, exc
            )

        # ---- Expand each new TC (1 LLM call each, capped at _MAX_EXPAND_PER_CLUSTER) ----
        cluster_new = 0
        cluster_expand_calls = 0
        _expandable = [e for e in outline_data.get("test_plan", [])
                       if not e.get("is_existing") and e.get("tc_id")]
        _n_expandable = min(len(_expandable), _MAX_EXPAND_PER_CLUSTER)
        for tc_entry in outline_data.get("test_plan", []):
            if tc_entry.get("is_existing"):
                continue
            tc_id = tc_entry.get("tc_id", "")
            if not tc_id:
                continue
            if cluster_expand_calls >= _MAX_EXPAND_PER_CLUSTER:
                logger.warning(
                    "[second_pass_tc_gen_node] '%s': per-cluster expand cap (%d) reached"
                    " — %d remaining TCs skipped",
                    cluster_name, _MAX_EXPAND_PER_CLUSTER,
                    sum(1 for e in outline_data.get("test_plan", [])
                        if not e.get("is_existing") and e.get("tc_id")) - cluster_expand_calls,
                )
                break
            if global_calls_used >= max_global_calls:
                logger.warning(
                    "[second_pass_tc_gen_node] Global LLM budget (%d) reached mid-expand"
                    " — stopping", max_global_calls,
                )
                break
            adoc_text = _expand_tc_from_outline(tc_entry, cluster_name, req_nodes, llm, kg=kg, config=config, raw_chunks=raw_chunks)
            global_calls_used += 1
            cluster_expand_calls += 1
            logger.info(
                "[Pass 3: Consolidation] '%s' — expand TC %d/%d (%s)",
                cluster_name, cluster_expand_calls, _n_expandable, tc_id,
            )
            if not adoc_text:
                continue
            new_missing.append({
                "title": tc_id,
                "cluster": cluster_name,
                "adoc_section": adoc_text,
                "source": "second_pass",
            })
            cluster_new += 1

        logger.info(
            "[second_pass_tc_gen_node] '%s': %d new TCs generated (%d LLM calls)",
            cluster_name, cluster_new, cluster_expand_calls,
        )

    logger.info(
        "[second_pass_tc_gen_node] Done — %d cluster(s) triggered, %d new TCs, %d outlines saved",
        triggered, len(new_missing), len(outline_paths),
    )

    if new_missing:
        missing = missing + new_missing
        # Re-run dedup so second-pass TC IDs don't collide with pass-1 IDs or
        # with each other when multiple clusters share the same TC prefix.
        existing_tc_ids: Dict[str, List[str]] = {}
        if kg is not None and hasattr(kg, "get_all_test_cases"):
            try:
                for _node in kg.get_all_test_cases():
                    _tc_id = _node.properties.get("tc_id") or ""
                    _cluster = (
                        _node.properties.get("cluster_name")
                        or _node.properties.get("cluster")
                        or "unknown"
                    )
                    if _tc_id:
                        existing_tc_ids.setdefault(_cluster, [])
                        if _tc_id not in existing_tc_ids[_cluster]:
                            existing_tc_ids[_cluster].append(_tc_id)
            except Exception as _exc:
                logger.warning("[second_pass_tc_gen_node] Could not build existing_tc_ids: %s", _exc)
        missing = _deduplicate_missing_tc_ids(missing, existing_tc_ids)

    # Merge consolidated update candidates from Pass 2 with existing update_candidates
    final_updates = list(state.get("update_candidates") or [])
    if consolidated_updates_acc:
        existing_update_ids = {u.get("tc_id") for u in final_updates if u.get("tc_id")}
        for upd in consolidated_updates_acc:
            tc_id = upd.get("tc_id")
            if tc_id and tc_id not in existing_update_ids:
                final_updates.append(upd)
                existing_update_ids.add(tc_id)
            elif tc_id:
                # Replace existing entry with the more complete consolidation version
                final_updates = [u for u in final_updates if u.get("tc_id") != tc_id]
                final_updates.append(upd)
        logger.info(
            "[second_pass_tc_gen_node] Merged %d consolidation updates into update_candidates",
            len(consolidated_updates_acc),
        )

    # ── Coverage Gap TC Generation ─────────────────────────────────────────
    # Generates TCs for spec requirements that have NO existing test coverage.
    # Runs only when include_coverage_gaps is True (default).
    _all_gap_tests: List[dict] = []
    if state.get("include_coverage_gaps", True) and kg and hasattr(kg, "_graph"):
        for cluster_lower_gap, cluster_name_gap in sorted(all_clusters.items()):
            # Only generate gap TCs for PR-relevant clusters
            if not any(_cluster_names_match(cluster_name_gap, prc) for prc in pr_relevant_clusters):
                continue
            if global_calls_used >= max_global_calls:
                logger.warning(
                    "[Pass 3: Coverage Gaps] Global LLM budget (%d) reached — stopping gap generation",
                    max_global_calls,
                )
                break

            uncovered_reqs = _collect_uncovered_reqs(kg, cluster_name_gap, max_reqs=100)
            if not uncovered_reqs:
                continue

            logger.info(
                "[Pass 3: Coverage Gaps] '%s' — %d uncovered requirements, generating outline",
                cluster_name_gap, len(uncovered_reqs),
            )

            gap_existing_tc_nodes = _collect_cluster_tc_nodes(kg, cluster_name_gap)
            gap_prefix = _derive_prefix_from_existing_tcs(gap_existing_tc_nodes, cluster_name_gap)

            # Build existing TC block for context — includes both KG TCs and
            # consolidated missing_tests from Pass 1+3 (so the coverage gap LLM
            # doesn't regenerate TCs that are already planned in this run).
            gap_existing_lines = []
            for tc in gap_existing_tc_nodes:
                tc_id = tc.properties.get("tc_id") or tc.label or tc.node_id
                title = tc.properties.get("title") or tc.label or ""
                gap_existing_lines.append(f"  {tc_id}: {title}")

            # Add consolidated missing_tests for this cluster as "already planned"
            _planned_tc_count = 0
            for m in missing:
                m_cluster = (m.get("cluster") or "").lower().strip()
                if _cluster_names_match(m_cluster, cluster_name_gap):
                    m_title = m.get("title", "")
                    gap_existing_lines.append(f"  {m_title} [PLANNED — from Pass 1+3 consolidation]")
                    _planned_tc_count += 1
            if _planned_tc_count:
                logger.debug(
                    "[Pass 3: Coverage Gaps] '%s' — injected %d planned TCs into existing TC context",
                    cluster_name_gap, _planned_tc_count,
                )

            # Build uncovered requirements block
            gap_req_lines = []
            for req in uncovered_reqs:
                _gap_sec = req.get('section_path', '')
                _gap_ref = f" (spec: {_gap_sec})" if _gap_sec else ""
                gap_req_lines.append(f"  {req['node_id']}{_gap_ref}: {req['normative_text']}")

            gap_outline_prompt = _COVERAGE_GAP_OUTLINE_TEMPLATE.format(
                cluster_name=cluster_name_gap,
                prefix=gap_prefix,
                existing_count=len(gap_existing_tc_nodes) + _planned_tc_count,
                existing_tc_block="\n".join(gap_existing_lines) or "  (none)",
                uncovered_count=len(uncovered_reqs),
                uncovered_req_block="\n".join(gap_req_lines) or "  (none)",
            )

            try:
                if hasattr(llm, "set_next_label"):
                    llm.set_next_label(f"Coverage Gap — outline for {cluster_name_gap}")
                gap_outline_raw = llm.complete(gap_outline_prompt, system=_COVERAGE_GAP_OUTLINE_SYSTEM)
                global_calls_used += 1
                gap_outline_raw = gap_outline_raw.strip()
                _gap_json_str = _extract_json_object(gap_outline_raw) or gap_outline_raw
                if not _gap_json_str:
                    _gap_json_str = "{}"
                try:
                    gap_outline_data = _json.loads(_gap_json_str)
                except _json.JSONDecodeError:
                    gap_outline_data = _json.loads(_repair_json(_gap_json_str))
            except Exception as exc:
                logger.warning(
                    "[Pass 3: Coverage Gaps] Outline call failed for '%s': %s", cluster_name_gap, exc,
                )
                continue

            # Expand each non-existing TC from the outline
            gap_req_nodes = _collect_cluster_req_nodes(kg, cluster_name_gap) if kg else []
            gap_expandable = [
                e for e in gap_outline_data.get("test_plan", [])
                if not e.get("is_existing") and e.get("tc_id")
            ]
            gap_expand_calls = 0
            _n_gap_expandable = len(gap_expandable)
            for gap_tc_entry in gap_expandable:
                if global_calls_used >= max_global_calls:
                    logger.warning(
                        "[Pass 3: Coverage Gaps] Global LLM budget (%d) reached mid-expand"
                        " — %d of %d gap TCs expanded for '%s'",
                        max_global_calls, gap_expand_calls, _n_gap_expandable, cluster_name_gap,
                    )
                    break
                gap_tc_id = gap_tc_entry.get("tc_id", "")
                gap_expand_calls += 1
                logger.info(
                    "[Pass 3: Coverage Gaps] '%s' — expanding TC %d/%d (%s)",
                    cluster_name_gap, gap_expand_calls, _n_gap_expandable, gap_tc_id,
                )
                adoc_text = _expand_tc_from_outline(
                    gap_tc_entry, cluster_name_gap, gap_req_nodes, llm,
                    kg=kg, config=config, raw_chunks=raw_chunks,
                )
                global_calls_used += 1
                if not adoc_text:
                    continue
                _all_gap_tests.append({
                    "title": gap_tc_id,
                    "cluster": cluster_name_gap,
                    "adoc_section": adoc_text,
                    "source": "coverage_gap",
                })

            logger.info(
                "[Pass 3: Coverage Gaps] '%s': %d gap TCs generated (%d LLM calls)",
                cluster_name_gap, sum(1 for t in _all_gap_tests if t.get("cluster") == cluster_name_gap),
                gap_expand_calls + 1,  # +1 for outline call
            )

    if _all_gap_tests:
        logger.info("[Pass 3: Coverage Gaps] Total: %d coverage gap TCs generated", len(_all_gap_tests))

    _final_coverage_gaps = list(state.get("coverage_gap_tests", [])) + _all_gap_tests

    # ── TC Merge Pass: consolidate overlapping TCs ─────────────────────────
    # Run a single LLM call to merge duplicates and overlapping TCs (e.g.,
    # multiple attribute-read TCs, fragmented command TCs, duplicate prefix
    # families). Always runs when there are 2+ new TCs.
    all_new_tcs = missing + _final_coverage_gaps
    if len(all_new_tcs) >= 2 and llm is not None:
        logger.info(
            "[Pass 3: TC Merge] %d total TCs — running merge check",
            len(all_new_tcs),
        )
        merged_missing, merged_gaps = _merge_overlapping_tcs(
            all_new_tcs, missing, _final_coverage_gaps, llm, config,
        )
        if merged_missing is not None:
            _pre_merge = len(missing) + len(_final_coverage_gaps)
            missing = merged_missing
            _final_coverage_gaps = merged_gaps
            _post_merge = len(missing) + len(_final_coverage_gaps)
            logger.info(
                "[Pass 3: TC Merge] Reduced %d → %d TCs (%d merged away)",
                _pre_merge, _post_merge, _pre_merge - _post_merge,
            )
            _merge_stats = {"merge_before": _pre_merge, "merge_after": _post_merge}
        else:
            _merge_stats = {}
    else:
        _merge_stats = {}

    _update_pipeline_progress(state, "second_pass_tc_gen", missing=len(missing), coverage_gaps=len(_final_coverage_gaps))

    # Filter out cluster_review_additions for clusters that went through consolidation
    # (their items are already merged into missing_tests by the consolidation LLM).
    _orig_review = state.get("cluster_review_additions") or []
    if consolidated_clusters and _orig_review:
        # If cluster_filter is set, ALL review items belong to the filtered cluster
        # and were already included in consolidation — remove them all.
        if cluster_filter:
            _filtered_review = []
        else:
            _filtered_review = [
                r for r in _orig_review
                if (r.get("cluster") or "").lower().strip() not in consolidated_clusters
            ]
        logger.info(
            "[second_pass_tc_gen_node] Filtered cluster_review_additions: %d → %d "
            "(removed %d items from consolidated clusters)",
            len(_orig_review), len(_filtered_review),
            len(_orig_review) - len(_filtered_review),
        )
    else:
        _filtered_review = _orig_review

    _pass_stats["pass3"] = {
        "consolidation_input": _pass3_consolidation_input,
        "review_input": _pass3_review_input,
        "consolidation_kept": _pass3_consolidation_kept,
        "consolidation_removed": _pass3_consolidation_removed,
        "coverage_gap_tcs": len(_all_gap_tests),
        "final_new_tcs": len(missing),
        "final_updates": len(final_updates),
        "review_after_filter": len(_filtered_review),
        **_merge_stats,
    }

    return {
        **state,
        "missing_tests": missing,
        "update_candidates": final_updates,
        "second_pass_outlines": outline_paths,
        "coverage_gap_tests": _final_coverage_gaps,
        "cluster_review_additions": _filtered_review,
        "pass_stats": _pass_stats,
    }


# ---------------------------------------------------------------------------
# Third-pass: re-expand a human-modified TC outline
# ---------------------------------------------------------------------------

@log_node
def human_outline_expand_node(state: PipelineState) -> PipelineState:
    """Node: Re-expand a human-modified TC outline into full adoc sections.

    Reads ``state["third_pass_outline_path"]``.  When the path is empty or
    not set, this node is a transparent no-op and returns state unchanged.

    Workflow:
      1. Load the JSON outline (produced by second_pass_tc_gen_node and
         optionally hand-edited by a human engineer)
      2. For each TC entry where is_existing=False, call the LLM to produce
         a fresh, fully-expanded adoc section (human_notes are injected)
      3. Append expanded TCs to missing_tests
    """
    outline_path_str = state.get("third_pass_outline_path") or ""
    if not outline_path_str:
        logger.info("[human_outline_expand_node] No outline path set — skipping")
        return state

    p = Path(outline_path_str)
    if not p.is_file():
        err = f"human_outline_expand: outline file not found: {outline_path_str}"
        logger.error("[human_outline_expand_node] %s", err)
        return {**state, "errors": [*(state.get("errors") or []), err]}

    try:
        outline_data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        err = f"human_outline_expand: JSON parse error in {p.name}: {exc}"
        logger.error("[human_outline_expand_node] %s", err)
        # Surface as fatal so the user notices rather than silently getting no TCs.
        return {**state, "errors": [*(state.get("errors") or []), err], "fatal_error": True}

    cluster_name = outline_data.get("cluster", "")
    config = state["config"]
    run_dir = state.get("run_dir", "")
    kg = state.get("knowledge_graph")
    raw_chunks: List[Any] = (state.get("pr_chunks") or []) + (state.get("spec_chunks") or [])

    req_nodes = _collect_cluster_req_nodes(kg, cluster_name) if (kg and cluster_name) else []
    llm = _get_run_llm(config, run_dir)
    missing = list(state.get("missing_tests") or [])

    new_count = 0
    for tc_entry in outline_data.get("test_plan", []):
        if tc_entry.get("is_existing"):
            continue
        tc_id = tc_entry.get("tc_id", "")
        if not tc_id:
            continue
        adoc_text = _expand_tc_from_outline(tc_entry, cluster_name, req_nodes, llm, kg=kg, config=config, raw_chunks=raw_chunks, pass_name="pass3")
        if adoc_text:
            missing.append({
                "title": tc_id,
                "cluster": cluster_name,
                "adoc_section": adoc_text,
                "source": "human_review",
            })
            new_count += 1

    logger.info(
        "[human_outline_expand_node] Expanded %d TCs from '%s'", new_count, p.name
    )
    return {**state, "missing_tests": missing}


def _build_cluster_review_md(
    findings: List[dict],
    missing_tests: List[dict],
    update_candidates: List[dict],
) -> str:
    """Format the cluster review findings as a human-readable Markdown audit document."""
    from datetime import datetime

    lines = [
        "# Matter RAG — Cluster-Level LLM Review",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "> This document is an **audit file** produced by a second-pass cluster-level LLM review.",
        "> The HTML report and generated adoc files are **not modified** by this review.",
        "> Use this document to check whether any test cases were missed by the per-chunk analysis.",
        "",
        "---",
        "",
    ]

    total_gaps    = sum(len(f.get("symmetry_gaps", [])) for f in findings)
    total_missing = sum(len(f.get("missing_test_types", [])) for f in findings)
    total_dup     = sum(len(f.get("duplicates", [])) for f in findings)
    total_new_tc  = sum(len(f.get("should_be_new_tc", [])) for f in findings)

    lines += [
        "## Summary",
        "",
        f"| Clusters reviewed | {len(findings)} |",
        "|---|---|",
        f"| Symmetry gaps found | {total_gaps} |",
        f"| Missing test types | {total_missing} |",
        f"| Should-be-new-TC flags | {total_new_tc} |",
        f"| Duplicate coverage flags | {total_dup} |",
        f"| New TCs in original report | {len(missing_tests)} |",
        f"| Update candidates in original report | {len(update_candidates)} |",
        "",
        "---",
        "",
    ]

    for f in findings:
        cluster = f.get("cluster", "Unknown")
        lines.append(f"## Cluster: {cluster}")
        lines.append("")

        if f.get("error"):
            lines.append(f"> ⚠️ Review failed: {f['error']}")
            lines.append("")
            continue

        summary = f.get("summary", "")
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        # Symmetry gaps
        gaps = f.get("symmetry_gaps", [])
        if gaps:
            lines.append("### Symmetry Gaps")
            lines.append("")
            for g in gaps:
                action = g.get("action", "new_tc")
                update_tc = g.get("update_tc_id", "")
                action_label = f"update `{update_tc}`" if action == "update_existing" and update_tc else "new TC"
                lines.append(f"- **Entity**: `{g.get('entity', '?')}` — Action: {action_label}")
                lines.append(f"  - Reason: {g.get('reason', '')}")
                if g.get("suggested_title"):
                    lines.append(f"  - Suggested: `{g['suggested_title']}`")
                for i, step in enumerate(g.get("steps", []), 1):
                    lines.append(f"  - Step {i}: {step}")
            lines.append("")

        # Missing test types
        missing_types = f.get("missing_test_types", [])
        if missing_types:
            lines.append("### Missing Test Types")
            lines.append("")
            for m in missing_types:
                action = m.get("action", "new_tc")
                update_tc = m.get("update_tc_id", "")
                action_label = f"update `{update_tc}`" if action == "update_existing" and update_tc else "new TC"
                lines.append(f"- **Entity**: `{m.get('entity', '?')}` — {m.get('change', '')} — Action: {action_label}")
                lines.append(f"  - Missing: {m.get('missing', '')}")
                if m.get("suggested_title"):
                    lines.append(f"  - Suggested: `{m['suggested_title']}`")
                for i, step in enumerate(m.get("steps", []), 1):
                    lines.append(f"  - Step {i}: {step}")
            lines.append("")

        # Should-be-new-TC
        new_tc_flags = f.get("should_be_new_tc", [])
        if new_tc_flags:
            lines.append("### Flagged for New TC (currently an update)")
            lines.append("")
            for n in new_tc_flags:
                lines.append(f"- **TC**: `{n.get('tc_id', '?')}`")
                lines.append(f"  - Reason: {n.get('reason', '')}")
                if n.get("suggested_title"):
                    lines.append(f"  - Suggested: `{n['suggested_title']}`")
                for i, step in enumerate(n.get("steps", []), 1):
                    lines.append(f"  - Step {i}: {step}")
            lines.append("")

        # Duplicates
        dups = f.get("duplicates", [])
        if dups:
            lines.append("### Duplicate Coverage")
            lines.append("")
            for d in dups:
                entries = ", ".join(f"`{e}`" for e in d.get("entries", []))
                lines.append(f"- {entries}: {d.get('reason', '')}")
            lines.append("")

        if not gaps and not missing_types and not new_tc_flags and not dups:
            lines.append("_No issues found for this cluster._")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


@log_node
def generate_report_node(state: PipelineState) -> PipelineState:
    """Node 10: Write reports to disk.

    Produces three outputs:
      llm_analysis_of_chunks_<ts>.md — per-chunk debug trace (change identified + LLM reasoning)
      test_coverage_pass1_<ts>.html  — visual report after per-chunk LLM pass only
      test_coverage_final_<ts>.html  — visual report after cluster review additions
      report_data_<ts>.json          — full structured data sidecar for this run
    """
    import json
    from datetime import datetime

    output_dir = Path(state.get("output_dir", "reports"))
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path      = output_dir / f"llm_analysis_of_chunks_{timestamp}.md"
    html_pass1_path = output_dir / f"test_coverage_pass1_{timestamp}.html"
    html_final_path = output_dir / f"test_coverage_final_{timestamp}.html"
    json_path       = output_dir / f"report_data_{timestamp}.json"

    missing    = state.get("missing_tests", [])
    updates    = state.get("update_candidates", [])
    coverage_gap_tests = state.get("coverage_gap_tests", [])
    # Also check for coverage gap items that may have been added to missing_tests
    # with source="coverage_gap" in earlier pipeline versions.
    _gap_in_missing = [m for m in missing if m.get("source") == "coverage_gap"]
    if _gap_in_missing and not coverage_gap_tests:
        logger.warning(
            "[generate_report_node] Found %d coverage_gap items in missing_tests "
            "but coverage_gap_tests is empty — migrating them to coverage_gap_tests",
            len(_gap_in_missing),
        )
        coverage_gap_tests = _gap_in_missing
        missing = [m for m in missing if m.get("source") != "coverage_gap"]
    analysis   = state.get("analysis_results", [])
    llm_failed = state.get("llm_failed_chunks", 0)
    aborted_at = state.get("llm_aborted_at")
    total_chunks = state.get("llm_total_chunks", len(analysis))
    adoc_paths = state.get("adoc_output_paths", [])
    review_additions = state.get("cluster_review_additions")  # None if review was skipped
    second_pass_outlines = state.get("second_pass_outlines") or []
    pr_url = (
        state.get("pr_url")
        or (f"local:{state['input_doc']}" if state.get("input_doc") else None)
        or "N/A"
    )
    cluster_filter = state.get("cluster_filter") or ""

    # ── 1. Analysis trace (MD) ───────────────────────────────────────────────
    trace_md = _build_analysis_trace_md(
        pr_url, analysis, adoc_paths, llm_failed, aborted_at, total_chunks,
    )
    trace_path.write_text(trace_md, encoding="utf-8")
    logger.info("[generate_report_node] Analysis trace → %s", trace_path)

    # ── 2. Test coverage HTML — pass 1 (per-chunk analysis only) ─────────────
    html_pass1 = _build_test_coverage_html(
        pr_url, analysis, missing, updates, adoc_paths, timestamp,
        llm_failed, aborted_at, total_chunks,
        review_additions=None,          # pass 1: no review section
        second_pass_outlines=[],
        coverage_gap_tests=[],
        cluster_filter=cluster_filter,
    )
    html_pass1_path.write_text(html_pass1, encoding="utf-8")
    logger.info("[generate_report_node] Pass-1 HTML → %s", html_pass1_path)

    # ── 3. Test coverage HTML — final (all passes) ────────────────────────────
    html_final = _build_test_coverage_html(
        pr_url, analysis, missing, updates, adoc_paths, timestamp,
        llm_failed, aborted_at, total_chunks,
        review_additions=review_additions or [],
        second_pass_outlines=second_pass_outlines,
        coverage_gap_tests=coverage_gap_tests,
        cluster_filter=cluster_filter,
        pass_stats=state.get("pass_stats"),
    )
    html_final_path.write_text(html_final, encoding="utf-8")
    logger.info("[generate_report_node] Final HTML → %s", html_final_path)

    # ── 4. PDF of final report (best-effort) ─────────────────────────────────
    pdf_path = output_dir / f"test_coverage_final_{timestamp}.pdf"
    if _write_pdf_report(html_final, pdf_path):
        logger.info("[generate_report_node] PDF → %s", pdf_path)
    else:
        logger.info("[generate_report_node] PDF skipped (no weasyprint / Chrome found)")

    # ── 5. JSON sidecar ──────────────────────────────────────────────────────
    _first_pass_missing = [m for m in missing if m.get("source") not in ("second_pass", "coverage_gap")]
    _second_pass_missing = [m for m in missing if m.get("source") in ("second_pass", "coverage_gap")]
    json_path.write_text(json.dumps({
        "pr_url": pr_url,
        "generated_at": timestamp,
        "cluster_filter": cluster_filter or None,
        "missing_tests_count": len(missing),
        "first_pass_missing_count": len(_first_pass_missing),
        "second_pass_missing_count": len(_second_pass_missing),
        "coverage_gap_tests_count": len(coverage_gap_tests),
        "update_candidates_count": len(updates),
        "cluster_review_additions_count": len(review_additions) if review_additions is not None else None,
        "llm_failed_chunks": llm_failed,
        "llm_total_chunks": total_chunks,
        "parse_failed_count": sum(1 for r in analysis if r.get("parse_failed")),
        "template_echo_warning_count": sum(1 for r in analysis if r.get("template_echo_warning")),
        "analysis": [
            {k: v for k, v in r.items() if k != "llm_response"}
            for r in analysis
        ],
        "missing_tests": missing,
        "coverage_gap_tests": coverage_gap_tests,
        "update_candidates": updates,
        "cluster_review_additions": review_additions or [],
    }, indent=2), encoding="utf-8")

    # ── 6. Copy llm_calls.html from log dir into the report folder ────────────
    run_dir = state.get("run_dir")
    if run_dir:
        src_html = Path(run_dir) / "llm_calls.html"
        if src_html.is_file():
            import shutil
            dst_html = output_dir / "llm_calls.html"
            shutil.copy2(src_html, dst_html)
            logger.info("[generate_report_node] llm_calls.html copied → %s", dst_html)
        else:
            logger.debug(
                "[generate_report_node] llm_calls.html not found at %s — skipping copy",
                src_html,
            )

    _update_pipeline_progress(state, "generate_report", report_path=str(trace_path))
    return {**state, "report_path": str(trace_path)}


# ---------------------------------------------------------------------------
# Structured analysis prompts (used by analyze_chunks_with_llm_node)
# ---------------------------------------------------------------------------

# Fallback static reference used when spec sections 7.3/7.6/7.7 are not in the KG.
_SPEC_COLUMN_REFERENCE_FALLBACK = """\
=== MATTER SPEC COLUMN REFERENCE ===

QUALITY FLAGS (attribute Quality column — space-separated single-letter flags):
  Q = Quieter Reporting — attribute suppresses reports for changes below a configured threshold; tests MUST cover that reports fire only when the delta exceeds the threshold and are suppressed otherwise
  N = Non-volatile   — attribute persists across power cycles; tests MUST cover power-cycle retention
  F = Fixed          — read-only; only changes on firmware update or device reconfiguration; tests MUST: (a) attempt a write → verify UNSUPPORTED_WRITE error, (b) verify value is stable across re-reads during normal operation — do NOT use a passive 'wait N seconds'; note the value MAY legitimately change after a device reconfiguration or firmware update so do not assume permanent immutability
  S = Scene          — attribute is saved/restored by scenes; tests MUST cover scene store/recall
  X = Fixed-by-manufacturer — fixed at manufacture; tests MUST verify it is read-only post-commissioning
  C = Changed omitted — not scene-sensitive; no scene coverage needed

CONFORMANCE (attribute/command/event Conformance column):
  M           = Mandatory    — MUST be implemented; tests required for ALL devices
  O           = Optional     — MAY be implemented; tests should be conditional on feature support
  P           = Provisional  — early-access feature; tests should note provisional status
  D           = Deprecated   — retained for compatibility; tests MUST verify deprecation behavior
  X           = Disallowed   — SHALL NOT be implemented; tests MUST verify absence
  [FeatureCode] = Feature-conditional (e.g. [LT] = only when Lighting feature enabled)

ACCESS (attribute Access column — space-separated tokens):
  R  = Read  (View privilege)   W  = Write (Operate privilege)   RW = Read-Write
  F  = Fabric-sensitive (per-fabric stored value)
  T  = Timed write required (write only inside timed interaction)
  VO = View + Operate   VM = View + Manage   VA = View + Admin
  Access changes (e.g. R → RW) require tests for: new write path, authorization levels, and error cases.

DATA TYPES (attribute Type column):
  uint8/16/32/64, int8/16/32/64, boolean, string, octstr, list, struct, enum8/16, bitmap8/16/32
  Type changes usually require updated range/constraint tests and null-check updates.

CONFORMANCE CHANGE IMPACT ON TESTS:
  O → M : New mandatory tests required for ALL devices (remove optional/feature-flag conditionals)
  M → O : Tests become conditional; add PICS-guard for optional support
  M → D : Add deprecation test; verify cluster still works for legacy devices
  * → X : Add test verifying attribute/command is NOT present on the device

=== END MATTER SPEC COLUMN REFERENCE ==="""


def _strip_section_numbers_simple(path: str) -> str:
    """Strip leading section numbers (e.g. '7.3. ') from each breadcrumb segment."""
    import re as _re
    _num_re = _re.compile(r"^\d+(?:\.\d+)*\.\s+")
    return " > ".join(
        _num_re.sub("", seg.strip()) for seg in path.split(" > ")
    )


def _detect_summary_file(chunk_section_path: str, config) -> tuple:
    """Return (label, summary_file_path, char_cap) for the first prompt_sections entry
    whose path_prefix (number-agnostic) matches chunk_section_path, AND which has a
    summary_file set.  Returns ('', '', 0) if no match or no summary_file.
    """
    if not chunk_section_path or config is None:
        return "", "", 0
    ps_configs = getattr(config.knowledge_graph, "prompt_sections", None) or []
    path_normalized = _strip_section_numbers_simple(chunk_section_path).lower()
    for entry in ps_configs:
        summary_file = entry.get("summary_file", "")
        if not summary_file:
            continue
        path_prefix = entry.get("path_prefix", "")
        path_prefixes = entry.get("path_prefixes") or ([path_prefix] if path_prefix else [])
        if not path_prefixes:
            continue
        for pp in path_prefixes:
            prefix_normalized = _strip_section_numbers_simple(pp).lower()
            if prefix_normalized in path_normalized:
                return entry.get("label", ""), summary_file, int(entry.get("char_cap", 0))
    return "", "", 0


def _build_spec_text_from_sections(kg, config=None) -> str:
    """Fallback: scan SECTION nodes to build spec reference text.

    Used when the KG was built before PROMPT_SECTION nodes were introduced
    (i.e. old KG JSON loaded without a rebuild).  New KG builds create
    dedicated PROMPT_SECTION nodes so this path is not normally reached.

    When config is provided, reads path_prefix entries from
    config.knowledge_graph.prompt_sections (same list that drives build_prompt_sections).
    Falls back to hardcoded defaults when config is unavailable.
    """
    _default_prefixes = [
        "7. Data Model Specification > 7.3. Conformance",
        "7. Data Model Specification > 7.6. Access",
        "7. Data Model Specification > 7.7. Other Qualities",
    ]
    if config is not None:
        ps_configs = getattr(config.knowledge_graph, "prompt_sections", None) or []
        # Skip summary-backed entries — those are injected from files, not from KG SECTION nodes
        prefixes = [
            e.get("path_prefix", "")
            for e in ps_configs
            if e.get("path_prefix") and not e.get("summary_file")
        ]
    else:
        prefixes = []
    if not prefixes:
        prefixes = _default_prefixes
    prefixes_lower = [p.lower() for p in prefixes]

    hits: list = []
    seen: set = set()
    try:
        for _nid, data in kg._graph.nodes(data=True):
            from src.knowledge_graph.base_graph import NodeType as _NT, GraphNode as _GN
            obj: Optional[_GN] = data.get("obj")
            if obj is None or obj.node_type != _NT.SECTION:
                continue
            sec_path = obj.properties.get("section_path") or obj.label or ""
            sec_path_lower = sec_path.lower()
            if not any(p in sec_path_lower for p in prefixes_lower):
                continue
            full_text = (obj.properties.get("full_text") or "").strip()
            if not full_text or sec_path in seen:
                continue
            seen.add(sec_path)
            hits.append((sec_path, full_text))
    except Exception as _sec_scan_exc:
        logger.debug("[_build_spec_text_from_sections] SECTION scan failed: %s", _sec_scan_exc)
        return ""

    if not hits:
        return ""

    hits.sort(key=lambda t: t[0])
    total = sum(len(txt) for _, txt in hits)
    logger.debug(
        "[_build_spec_text_from_sections] legacy SECTION scan: %d hits, %d chars",
        len(hits), total,
    )
    return "\n\n".join(f"--- {sp} ---\n{txt}" for sp, txt in hits)


def _load_additional_context(config, pass_name: str = "all") -> str:
    """Load per-pass additional context from --llm-additional-context.

    Supports three input formats:
      1. Directory path — reads {dir}/pass1.md, {dir}/pass2.md, {dir}/pass3.md, {dir}/all.md
         as applicable. Content from all.md is always included; pass-specific file is added on top.
      2. File path (.md or .txt) — reads entire file, applies to all passes.
      3. Inline text — applies to all passes.

    Args:
        config: AppConfig (reads config.pipeline.llm_additional_context)
        pass_name: "pass1", "pass2", "pass3", or "all"
    """
    if config is None:
        return ""
    raw_ctx = (getattr(config.pipeline, "llm_additional_context", "") or "").strip()
    if not raw_ctx:
        return ""

    from pathlib import Path as _Path

    try:
        ctx_path = _Path(raw_ctx)

        if ctx_path.is_dir():
            parts = []
            all_file = ctx_path / "all.md"
            if all_file.is_file():
                parts.append(all_file.read_text(encoding="utf-8").strip())
            pass_file = ctx_path / f"{pass_name}.md"
            if pass_file.is_file():
                parts.append(pass_file.read_text(encoding="utf-8").strip())
            result = "\n\n".join(p for p in parts if p)
            if result:
                logger.debug(
                    "[_load_additional_context] loaded from dir %s for %s (%d chars)",
                    raw_ctx, pass_name, len(result),
                )
            return result

        if ctx_path.is_file():
            result = ctx_path.read_text(encoding="utf-8").strip()
            logger.debug(
                "[_load_additional_context] loaded from file %s (%d chars)",
                raw_ctx, len(result),
            )
            return result

        return raw_ctx

    except Exception as exc:
        logger.debug("[_load_additional_context] failed: %s", exc)
        return raw_ctx


def _build_analysis_system_prompt(kg=None, config=None, chunk_section_path: str = "") -> str:
    """Build the system prompt for analyze_chunks_with_llm_node.

    Primary path: reads pre-built PROMPT_SECTION nodes from the KG.
    Protocol supplement: when chunk_section_path matches a prompt_sections entry that has
    a summary_file, reads that file and prepends it as protocol context.
    Skill file: appended as additional standing instructions.
    """
    _header = (
        "You are a senior Matter protocol test engineer. Analyze PR changes against existing "
        "test coverage and output a structured JSON response only — no prose before or after the JSON.\n\n"
    )

    # ── Step 1: spec reference text (DM sections from PROMPT_SECTION nodes) ───
    spec_text = ""
    if kg is not None:
        try:
            from src.knowledge_graph.base_graph import NodeType as _NT
            prompt_nodes = sorted(
                [
                    obj for _, data in kg._graph.nodes(data=True)
                    if (obj := data.get("obj")) and obj.node_type == _NT.PROMPT_SECTION
                ],
                key=lambda n: n.node_id,
            )
            if prompt_nodes:
                spec_text = "\n\n".join(
                    n.properties.get("full_text", "") for n in prompt_nodes
                )
                logger.debug(
                    "[_build_analysis_system_prompt] loaded %d PROMPT_SECTION nodes (%d chars)",
                    len(prompt_nodes), len(spec_text),
                )
            else:
                logger.debug(
                    "[_build_analysis_system_prompt] no PROMPT_SECTION nodes found — "
                    "falling back to legacy SECTION scan (rebuild KG to use PROMPT_SECTION nodes)"
                )
                spec_text = _build_spec_text_from_sections(kg, config=config)
        except Exception as _sp_exc:
            logger.warning("[_build_analysis_system_prompt] Spec text extraction failed: %s", _sp_exc)
            spec_text = ""

    # ── Step 2: protocol area summary (injected when chunk is from a protocol area) ──
    protocol_summary = ""
    if chunk_section_path:
        _label, _summary_file, _char_cap = _detect_summary_file(chunk_section_path, config)
        if _summary_file:
            try:
                from pathlib import Path as _Path
                _sf = _Path(_summary_file)
                if _sf.is_file():
                    raw = _sf.read_text(encoding="utf-8").strip()
                    if _char_cap and len(raw) > _char_cap:
                        raw = raw[:_char_cap]
                    protocol_summary = raw
                    logger.debug(
                        "[_build_analysis_system_prompt] injected protocol summary '%s' "
                        "from %s (%d chars)",
                        _label, _summary_file, len(protocol_summary),
                    )
            except Exception as _proto_exc:
                logger.debug("[_build_analysis_system_prompt] Protocol summary read failed: %s", _proto_exc)

    # ── Step 3: skill file (user custom instructions) ────────────────────────
    skill_text = ""
    if config is not None:
        try:
            skills_file = getattr(config.pipeline, "system_prompt_skills_file", "")
            if skills_file:
                from pathlib import Path as _Path
                _sf = _Path(skills_file)
                if _sf.is_file():
                    skill_text = _sf.read_text(encoding="utf-8").strip()
        except Exception as _skill_exc:
            logger.debug("[_build_analysis_system_prompt] Skill file read failed: %s", _skill_exc)

    # ── Assemble ─────────────────────────────────────────────────────────────
    parts: list = [_header]

    if protocol_summary:
        parts += [
            "=== PROTOCOL CONTEXT ===\n\n",
            protocol_summary,
            "\n\n=== END PROTOCOL CONTEXT ===",
        ]

    if spec_text:
        parts += [
            "\n\n=== MATTER SPEC SECTIONS (Conformance / Access / Other Qualities) ===\n\n",
            spec_text,
            "\n\n=== END MATTER SPEC SECTIONS ===",
        ]
    else:
        parts.append(_SPEC_COLUMN_REFERENCE_FALLBACK)

    if skill_text:
        parts += [
            "\n\n=== ADDITIONAL INSTRUCTIONS ===\n\n",
            skill_text,
            "\n\n=== END ADDITIONAL INSTRUCTIONS ===",
        ]

    # ── Step 4: llm_additional_context (per-run user context) ──
    additional_ctx = _load_additional_context(config, pass_name="pass1")

    if additional_ctx:
        parts += [
            "\n\n=== RUN-SPECIFIC CONTEXT ===\n\n",
            additional_ctx,
            "\n\n=== END RUN-SPECIFIC CONTEXT ===",
        ]

    return "".join(parts)



_ANN_RE = re.compile(r'\[(ADDED|REMOVED|CHANGED):.*?\]')

_CONTEXT_CHARS = 4000  # chars of surrounding context shown after annotations


_MAX_ANNOTATIONS = 30  # cap to prevent prompt overflow

def _prepare_diff_content(page_content: str) -> str:
    """Front-load diff annotations so they are never cut off by context truncation.

    For matter_spec_diff sections the annotated text can be 20 k+ chars with
    the only actual change marker buried deep in the text (e.g. [ADDED: NFC
    commissioning...] at position 5 280 in a 21 k-char section). Naive [:4000]
    slicing would drop it entirely.

    Strategy:
      1. Extract every [ADDED/REMOVED/CHANGED:] annotation.
      2. Emit them as a labelled block FIRST — they are always fully shown.
      3. Append the start of the full annotated text as context (capped at
         _CONTEXT_CHARS) so the LLM also has surrounding prose.
    """
    annotations = [m.group() for m in _ANN_RE.finditer(page_content)]
    if not annotations:
        return page_content[:_CONTEXT_CHARS]

    # Cap annotations to prevent prompt overflow
    if len(annotations) > _MAX_ANNOTATIONS:
        extra = len(annotations) - _MAX_ANNOTATIONS
        annotations = annotations[:_MAX_ANNOTATIONS]
        annotations.append(f"... and {extra} more annotations (truncated)")

    ann_block = "\n".join(f"  {a}" for a in annotations)
    return (
        "[Diff annotations — ALL changes in this section]\n"
        f"{ann_block}\n\n"
        "[Section context (truncated)]\n"
        f"{page_content[:_CONTEXT_CHARS]}"
    )


_STRUCTURED_ANALYSIS_PROMPT = """\
## Structured PR Change Record
```json
{change_json}
```

## PR Diff Content
**File**: {path}
**How to read this diff**: Lines containing `[ADDED: ...]` are new text added in this PR.
Lines containing `[REMOVED: ...]` were deleted. Lines containing `[CHANGED: old → new]` show
an in-place edit. All other surrounding text is unchanged existing spec content (context only).
```
{content}
```

---

## Section S — Spec Context (pre-change spec text for affected sections/requirements)
{spec_context}

## Section T — Spec Section Prose (full surrounding spec text for the changed section)
{spec_section_context}

## Section R — Entity Requirement Cross-Reference (spec requirements linked to changed entities)
{entity_context}

## Section X — Surrounding Cluster Context (edge-case awareness only)
{surrounding_cluster_context}

## Section A — Vector Search: Relevant Test Cases
{test_cases}

## Section B — Knowledge Graph: Entity-Linked Test Cases & Requirements
{graph_context}

## Section C — Existing TC IDs for this cluster
{existing_tc_list}

## Section D — All Existing Test Cases (Full Content)
Use this section to decide whether to UPDATE an existing TC or CREATE a new one.
Read each TC's steps before proposing changes — only update a TC if you can see it
covers the changed entity; only create a new TC if no existing TC addresses the behavior.
{all_cluster_tcs}

---

## Task
The PR diff above shows what changed in the spec. Section S shows the OLD spec text for the
affected sections — use it to understand what the tests were covering BEFORE the change.
Section T shows the full surrounding spec prose for the changed section — use it to understand
the complete behavioral context, especially for behavior-only changes with no named entity.
Section R lists spec requirements directly linked to the changed entities — use these to
understand what the spec mandates for those attributes/commands and identify missing coverage.
Section X lists clusters that behaviorally depend on the changed cluster — use this only to
spot edge cases at cluster boundaries (e.g. a Level Control interaction test that breaks when
On/Off behavior changes). Do NOT generate TCs for surrounding clusters; only flag if a
cross-cluster interaction specific to this PR change has no existing test coverage.

**REAL-WORLD TEST SCENARIO REQUIREMENTS — apply to every adoc_section you write:**
- Name the device archetype in Purpose and Test Environment (e.g. "smart light bulb", "door lock", "thermostat", "motorised window covering").
- Write test steps as real device interactions: "TH sends <Command> to DUT endpoint 1." / "TH reads <Attribute> from DUT. Expected: <VALUE>."
- For any quality flag on an entity in Section R (N, F, Q, S, C, X etc.): **read the definition of that quality in the MATTER SPEC SECTIONS above (section 7.7 Other Qualities) and derive test steps directly and completely from what the spec says — covering every behavioral aspect the spec defines for that quality.** Do NOT rely on the single-letter code alone, and do NOT omit any aspect of the quality's behavior just because it requires operator action or a conditional step. The only anti-pattern to avoid regardless of quality: "TH waits N seconds then re-reads" is a timing test — do NOT use it as a substitute for quality-specific verification unless the spec definition for that quality explicitly involves elapsed time.
- For writable attributes: verify read-back after write AND test the boundary values (min, max, and one out-of-range write returning CONSTRAINT_ERROR).
- For command/response pairs: add a step verifying the response command is received with the correct fields.
- For event-generating entities: subscribe first, trigger the event, verify the event report is received with correct fields.
- Every step must state its expected outcome **inline at the end of the same step line**, e.g. "TH reads OnOff attribute from DUT. Expected: TRUE (1)." Do NOT put expected results in a separate `=== Expected Results` section — there should be no such section.
- Minimum 4 steps per TC.
- Include `=== Test Environment` (DUT type, TH tooling, network topology) and `=== Prerequisites` (commission, reachability check) sections in every adoc_section.

**IMPORTANT CLASSIFICATION RULES — read before producing output:**
1. If a TC-ID listed in Section C already exists, it goes in `update_candidates` — NEVER in
   `missing_tests`. Existing tests are updated, not recreated.
2. For new TCs in `missing_tests`, choose only TC numbers that are NOT in Section C.
   Use the next sequential minor version after the highest existing number for this cluster
   prefix (e.g. if TC-{tc_prefix}-2.4 is the highest, the first new TC is TC-{tc_prefix}-2.5).
3. Do NOT use `.x` placeholders — always use a real numeric version.
4. Each entry in `missing_tests` must have a unique TC number — never repeat the same TC-ID.
5. **Mutual exclusivity**: If you add an entry to `update_candidates` that covers a new
   behavior (e.g. add new verification steps to TC-{tc_prefix}-2.1), do NOT also add a new TC in
   `missing_tests` for that same behavior. Choose one: update an existing TC OR create a
   new one — never both. Only add to `missing_tests` when there is NO existing TC that
   reasonably covers the new behavior even after the update.
6. **Symmetry across entities**: When `impacted_entities` lists multiple entities that all
   received the same change type (e.g., both OnTime and OffWaitTime gained Q quality), you
   MUST produce either a new TC or an update_candidate for EACH entity independently. Do
   NOT cover one entity and silently omit another with the same change. If an existing TC
   update covers entity A's new behavior, you must still produce a separate update_candidate
   or new TC entry for entity B's equivalent new behavior. Each entity gets its own explicit
   output entry — never assume entity B is "covered" just because entity A is handled.
{negative_tests_task}
Based on the change record and coverage context above, output a single JSON object:
```json
{{
  "change_summary": "one-sentence description of what changed",
  "impacted_entities": [
    {{"type": "attribute|command|event|feature", "name": "EntityName", "cluster": "ClusterName"}}
  ],
  "coverage": {{
    "direct_tests": ["TC-{tc_prefix}-2.1", "TC-{tc_prefix}-2.2"],
    "indirect_tests": ["TC-BASE-1.3"],
    "missing": true
  }},
  "recommendation": {{
    "action": "update_existing | add_new | none",
    "details": "specific description of what to add or update"
  }},
  "reasoning": "explanation of why these changes require test updates",
  "missing_tests": [
    {{
      "title": "TC-{tc_prefix}-2.5 [DUT as Server]",
      "cluster": "{cluster_name_example}",
      "adoc_section": "== TC-{tc_prefix}-2.5 [DUT as Server]\\n\\n=== Purpose\\nVerify <describe the behavior being tested for this cluster>.\\n\\n=== PICS\\n* CLUSTER.S.AXXX (<AttributeName>)\\n* CLUSTER.S.CXXX (<CommandName>)\\n\\n=== Test Environment\\n* DUT: Matter {cluster_name_example} device on endpoint 1.\\n* TH: chip-tool commissioner.\\n* Network: DUT and TH on the same Matter fabric.\\n\\n=== Prerequisites\\n1. Commission DUT to TH fabric using default setup PIN.\\n2. TH verifies DUT is reachable.\\n\\n=== Procedure\\n1. TH sends <Command> to DUT endpoint 1. Expected: command accepted with status SUCCESS.\\n2. TH reads <Attribute> from DUT endpoint 1. Expected: <value>.\\n3. <additional steps as needed. Each step on its own line with inline Expected result at the end>."
    }}
  ],
  "update_candidates": [
    {{
      "tc_id": "TC-{tc_prefix}-2.1",
      "change_summary": "what changed and why",
      "adoc_section": "== TC-{tc_prefix}-2.1 [DUT as Server]\\n\\n=== Purpose\\nVerify ...\\n\\n=== PICS\\n* CLUSTER.S.AXXX (<AttributeName>)\\n\\n=== Test Environment\\n* DUT: Matter {cluster_name_example} device on endpoint 1.\\n* TH: chip-tool.\\n\\n=== Prerequisites\\n1. Commission DUT to TH fabric.\\n\\n=== Procedure\\n1. TH reads <Attribute> from DUT. Expected: <initial value>.\\n2. ..."
    }}
  ]{negative_tests_json_field}
}}
```
Reply with ONLY the JSON object above. Do not include any text before or after it.
"""


# ---------------------------------------------------------------------------
# Negative-test prompt blocks (injected into _STRUCTURED_ANALYSIS_PROMPT
# when generate_negative_tests=True in PipelineState)
# ---------------------------------------------------------------------------

_NEGATIVE_TESTS_TASK_BLOCK = """
6. **Negative / error-path tests**: For each entity directly changed in this PR, generate
   error-path test cases that MUST produce a predictable failure response. Use the entity
   metadata in Section R (type=, access=, default=, quality=) to derive CONCRETE invalid
   inputs and the exact Matter status code the DUT must return.

   Error categories to cover (only where applicable to the changed entity):
   - **out_of_range**: write a value outside the valid numeric range for the datatype
     (e.g. type=uint8 → write 256; type=uint16 → write 0x10000). Expected: CONSTRAINT_ERROR.
   - **unsupported_write**: write to a read-only attribute (access=R). Expected: UNSUPPORTED_WRITE.
   - **access_denied**: attempt an operation without the required privilege level
     (e.g. access=administer → attempt with Operate privilege). Expected: ACCESS_DENIED.
   - **invalid_state**: send a command that is syntactically valid but illegal in the current
     device state (e.g. move before stop on a WindowCovering). Expected: FAILURE or INVALID_IN_STATE.
   - **missing_conformance**: invoke an optional feature/attribute/command not supported by
     the DUT. Expected: UNSUPPORTED_ATTRIBUTE or UNSUPPORTED_COMMAND.
   - **constraint_error**: write a value that violates a SHALL/MUST range constraint
     (e.g. min > max, enum value outside defined range). Expected: CONSTRAINT_ERROR.

   Rules:
   - Only add negative tests for entities that appear in the PR diff.
   - Each negative test must have `expected_status` set to an exact Matter status code string.
   - The `adoc_section` Expected Results section must state: DUT returns `<expected_status>`
     and no persistent state change occurs on failure.
   - Skip categories that do not apply (e.g. skip unsupported_write for writable attributes).
   - **IMPORTANT — TC numbering**: Negative tests MUST use the `NEG` infix to avoid
     conflicting with positive TC numbers.  Use the format `TC-<CLUSTER>-NEG-<N>.<M>`
     (e.g. TC-OO-NEG-1.1, TC-OO-NEG-1.2).  Do NOT reuse a number from missing_tests or
     update_candidates.  Start the NEG sequence at 1.1 and increment the minor number for
     each additional negative TC for the same cluster.
"""

_NEGATIVE_TESTS_JSON_FIELD = """,
  "negative_tests": [
    {
      "title": "TC-CLUSTER-NEG-1.1 [DUT as Server] \u2014 Negative: <scenario name>",
      "cluster": "CLUSTER",
      "negative_type": "out_of_range | unsupported_write | access_denied | invalid_state | missing_conformance | constraint_error",
      "target_entity": "EntityName",
      "expected_status": "CONSTRAINT_ERROR | UNSUPPORTED_WRITE | ACCESS_DENIED | UNSUPPORTED_ATTRIBUTE | UNSUPPORTED_COMMAND | FAILURE",
      "adoc_section": "== TC-CLUSTER-NEG-1.1 [DUT as Server]\\n\\n=== Purpose\\nVerify DUT returns <expected_status> when <invalid operation> on <EntityName>.\\n\\n=== PICS\\n[PICS.CLUSTER.S]\\n\\n=== Test Environment\\nStandard Matter test environment.\\n\\n=== Procedure\\n1. Commission DUT to TH.\\n2. <Specific invalid operation with concrete value from Section R metadata>\\n\\n=== Expected Results\\n* DUT returns status <expected_status>.\\n* No persistent state change occurs."
    }
  ]"""


# ---------------------------------------------------------------------------
# Chat-path helper
# ---------------------------------------------------------------------------

def _analyze_chat_path(state: PipelineState, llm) -> PipelineState:
    """Execute the conversational Q&A path for the ``app_chat`` client.

    Reads vector + KG results already in state (populated by the search nodes),
    formats them as a RAG context string, builds a conversational prompt using
    the session history, and returns ``{"llm_reply": <response>}``.

    This function is intentionally simple — it never touches analysis_results,
    missing_tests, or update_candidates, which belong to the CLI path.
    """
    try:
        from tests.app.services.history_builder import build_prompt_with_history
    except ImportError:
        def build_prompt_with_history(*args, **kwargs):
            return args[0] if args else ""

    search_results = state.get("search_results", {})
    graph_results = state.get("graph_results", {})
    coverage_notes: Dict[str, str] = state.get("graph_coverage_notes", {})
    chat_intent: str = state.get("chat_query_intent", "")

    # Flatten all vector hits across chunks (chat has a single synthetic chunk).
    all_vector_hits: List[SearchResult] = []
    for hits in search_results.values():
        all_vector_hits.extend(hits)

    all_graph_hits: List[GraphNode] = []
    for hits in graph_results.values():
        all_graph_hits.extend(hits)

    # ── list_test_cases: bypass LLM, format the full table directly ──────────
    # When the user explicitly wants a listing of all TCs, the LLM will only
    # mention the ones it can recall from a long context window.  We already
    # have the complete enumeration in graph_results, so render it as a Markdown
    # table without a round-trip to the LLM.
    if chat_intent == "list_test_cases" and (all_graph_hits or all_vector_hits):
        coverage_summary = "\n".join(v for v in coverage_notes.values() if v)
        tc_nodes = [
            n for n in all_graph_hits
            if (n.node_type.value if hasattr(n.node_type, "value") else str(n.node_type))
            == "TEST_CASE"
        ]
        # For list_test_cases, the KG results are authoritative — do NOT add
        # FAISS hits, which are semantic similarity matches that include TCs
        # mentioning the cluster name in cross-references (false positives).
        if tc_nodes:
            rows = []
            for n in tc_nodes:
                tc_id = n.properties.get("tc_id") or n.label
                title = n.properties.get("title") or n.properties.get("content", "")[:120]
                cluster = n.properties.get("cluster_name") or n.properties.get("cluster", "")
                rows.append((tc_id, title, cluster))
            rows.sort(key=lambda r: r[0])
            table_lines = ["| Test Case | Title |", "|-----------|-------|"]
            for tc_id, title, _ in rows:
                table_lines.append(f"| {tc_id} | {title} |")
            # Suppress the coverage_summary header in the direct listing — the
            # count is already embedded in the "N test case(s) found" line and
            # the original note may carry a stale number from before KG rebuild.
            reply = f"**{len(rows)} test case(s) found:**\n\n" + "\n".join(table_lines)
            logger.info(
                "[analyze_chunks_with_llm_node] chat path list_test_cases — returning %d TCs directly (no LLM call)",
                len(rows),
            )
            return {**state, "llm_reply": reply}

    # Build a single coverage summary string from per-chunk notes (usually 1 chunk in chat).
    coverage_summary = "\n".join(v for v in coverage_notes.values() if v)

    # Build RAG context string from search results.
    rag_context_parts: List[str] = []
    if coverage_summary:
        rag_context_parts.append("## Coverage Status\n" + coverage_summary)
    if all_vector_hits:
        rag_context_parts.append("## Relevant Test Cases\n" + _format_test_cases(all_vector_hits))
    if all_graph_hits:
        rag_context_parts.append("## Knowledge Graph Context\n" + _format_graph_results(all_graph_hits))
    rag_context = "\n\n".join(rag_context_parts)

    # Extract the user's question from the synthetic pr_chunk.
    pr_chunks: List[Document] = state.get("pr_chunks", [])
    user_message = pr_chunks[0].page_content if pr_chunks else ""

    # Build the conversational prompt with history + RAG context.
    history = state.get("chat_history", [])
    prompt = build_prompt_with_history(history, user_message, rag_context=rag_context)

    system_prompt = state.get("system_prompt", "")

    logger.info(
        "[analyze_chunks_with_llm_node] chat path | history_turns=%d vector_hits=%d kg_hits=%d",
        len(history), len(all_vector_hits), len(all_graph_hits),
    )

    try:
        if hasattr(llm, "set_next_label"):
            llm.set_next_label("Chat — response generation")
        reply = llm.complete(prompt, system=system_prompt)
        logger.info(
            "[analyze_chunks_with_llm_node] chat reply len=%d", len(reply),
        )
    except Exception as exc:
        logger.error("[analyze_chunks_with_llm_node] chat LLM error: %s", exc)
        reply = "I encountered an error while processing your question. Please try again."

    return {**state, "llm_reply": reply}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_test_cases(results: List[SearchResult]) -> str:
    if not results:
        return "_No relevant test cases found._"
    lines = []
    for r in results:
        meta = r.metadata

        tc_id = meta.get("tc_id", "")
        cluster = meta.get("cluster_name", "")
        section = meta.get("section_type", meta.get("section", ""))
        path = meta.get("path", r.doc_id)

        pics_raw = meta.get("pics_codes") or []
        pics_str = ", ".join(pics_raw) if isinstance(pics_raw, list) else str(pics_raw)

        header_parts = [f"**[Score: {r.score:.3f}]**"]
        if tc_id:
            header_parts.append(f"`{tc_id}`")
        if cluster:
            header_parts.append(f"cluster={cluster}")
        if section:
            header_parts.append(f"section={section}")
        if not tc_id:
            header_parts.append(f"`{path}`")
        header = " — ".join(header_parts)

        detail_parts = []
        if pics_str:
            detail_parts.append(f"PICS: {pics_str}")
        if path and tc_id:
            detail_parts.append(f"file: `{path}`")
        detail = " | ".join(detail_parts)

        lines.append(header)
        if detail:
            lines.append(f"  {detail}")
        lines.append(f"  > {r.page_content[:400]}")
        lines.append("")

    return "\n".join(lines)


def _format_graph_results(
    nodes: List[GraphNode],
    primary_cluster: str = "",
) -> str:
    """Format TEST_CASE nodes from KG for Section B — same-cluster TCs only.

    Filters to TCs whose primary cluster property matches *primary_cluster* to
    exclude cross-cluster TCs that reference entities here via PICS/edge traversal
    (e.g. TC-CC-* that use OO.S.C01 in setup steps and therefore have a
    `tests → CLUSTER::On/Off` edge in the KG).

    Each TC is shown as: TC-ID — title / purpose (readable, no KG-internal labels).
    """
    tc_nodes = []
    for n in nodes:
        nt = n.node_type.value if hasattr(n.node_type, "value") else str(n.node_type)
        if nt != "TEST_CASE":
            continue
        if primary_cluster:
            tc_cluster = (
                n.properties.get("cluster") or
                n.properties.get("cluster_name") or ""
            )
            if not _cluster_names_match(tc_cluster, primary_cluster):
                # Fuzzy fallback: primary cluster is a subdomain term embedded in
                # the TC cluster name (e.g. "Commissioning" in "General Commissioning")
                pc_norm = _normalize_cluster_name(primary_cluster)
                tc_norm = _normalize_cluster_name(tc_cluster)
                if not pc_norm or pc_norm not in tc_norm:
                    continue
        tc_nodes.append(n)

    if not tc_nodes:
        return "_No same-cluster test cases found in knowledge graph._"

    lines = []
    for node in tc_nodes:
        tc_id = node.properties.get("tc_id") or node.node_id
        title = node.properties.get("title") or node.label or ""
        purpose = (node.properties.get("purpose") or "")[:200]
        lines.append(f"**{tc_id}** — {title}")
        if purpose:
            lines.append(f"  Purpose: {purpose}")
        lines.append("")
    return "\n".join(lines)


def _format_spec_context(nodes: List[GraphNode], pr_content: str = "") -> str:
    """Format spec reference context for Section S.

    Primary source: ``[CHANGED: old → new]`` annotations extracted from the PR diff
    — this is the actual pre-change text, which is what Section S is meant to show.

    Secondary source: SECTION nodes from the KG — prose spec text giving surrounding
    context for the changed section.

    REQUIREMENT and BEHAVIOR_RULE nodes are intentionally excluded: they carry the
    current (post-change) spec text and are already surfaced in Section R (entity
    requirement cross-reference), so duplicating them here adds noise rather than value.
    """
    import re as _re

    parts: List[str] = []

    # ── Primary: extract pre-change text from PR diff [CHANGED: old → new] markers ──
    if pr_content:
        _CHANGED_RE = _re.compile(r'\[CHANGED:\s*(.+?)\s*→\s*(.+?)\]', _re.DOTALL)
        changed_items: List[str] = []
        for m in _CHANGED_RE.finditer(pr_content):
            old_val = m.group(1).strip()
            new_val = m.group(2).strip()
            if len(old_val) > 3:
                changed_items.append(
                    f"- **Before**: `{old_val[:300]}`  →  **After**: `{new_val[:300]}`"
                )
        if changed_items:
            parts.append(
                "_Pre-change text (extracted from PR diff annotations):_\n"
                + "\n".join(changed_items[:20])
            )

    # ── Secondary: SECTION nodes from KG (prose spec context) ───────────────────────
    section_nodes = [
        n for n in nodes
        if (n.node_type.value if hasattr(n.node_type, "value") else str(n.node_type))
        == "SECTION"
    ]
    if section_nodes:
        sec_lines: List[str] = []
        for node in section_nodes:
            section_path = node.properties.get("section_path", "")
            full_text = node.properties.get("full_text", "")
            header = f"**[SECTION]** `{node.label}`"
            if section_path:
                header += f" — {section_path}"
            sec_lines.append(header)
            if full_text:
                sec_lines.append(f"  > {full_text[:600]}")
            sec_lines.append("")
        parts.append("\n".join(sec_lines))

    if parts:
        return "\n\n".join(parts)

    return "_No spec context found (no PR diff annotations and no KG section nodes)._"


# ---------------------------------------------------------------------------
# Section T helper — spec section lookup by section-number hierarchy
# ---------------------------------------------------------------------------

_SECTION_NUM_RE = re.compile(r'^(\d+(?:\.\d+)*)\.')


def _extract_section_number(heading: str) -> str:
    """Return the dotted section number from a heading string, e.g. '1.7.5.1' from '1.7.5.1. StateChange Event'."""
    m = _SECTION_NUM_RE.match(heading.strip())
    return m.group(1) if m else ""


def _parent_section_numbers(sec_num: str) -> List[str]:
    """Return [parent, grandparent] section numbers, e.g. '1.7.5.1' → ['1.7.5', '1.7']."""
    parts = sec_num.split(".")
    parents = []
    for depth in range(len(parts) - 1, 0, -1):
        parents.append(".".join(parts[:depth]))
    return parents  # most-specific parent first


def _format_spec_section_from_kg(kg, cluster: str, section_title: str, max_chars: int) -> str:
    """Query KG SECTION nodes for full spec prose matching this cluster/section.

    Used as a fallback when spec_chunks is empty (no role='spec' source configured).
    Prioritises the most specific section that matches section_title, then falls
    back to the parent cluster section.
    """
    from src.knowledge_graph.base_graph import NodeType as _NT
    cluster_lower = cluster.lower()
    sec_num = _extract_section_number(section_title) if section_title else None
    parent_nums = _parent_section_numbers(sec_num) if sec_num else []

    hits: List[tuple] = []  # (score, section_path, full_text)
    for _, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj is None or obj.node_type != _NT.SECTION:
            continue
        sec_path = obj.properties.get("section_path") or obj.label or ""
        full_text = (obj.properties.get("full_text") or "").strip()
        if not full_text:
            continue
        if cluster_lower not in sec_path.lower():
            continue
        c_num = _extract_section_number(sec_path)
        if sec_num and c_num == sec_num:
            score = 3
        elif sec_num and parent_nums and c_num == parent_nums[0]:
            score = 2
        elif sec_num and len(parent_nums) > 1 and c_num == parent_nums[1]:
            score = 1
        else:
            score = 0
        hits.append((score, sec_path, full_text))

    if not hits:
        return "_No matching spec section found for this PR chunk._"

    hits.sort(key=lambda t: -t[0])
    best_score = hits[0][0]
    candidates = [(sp, ft) for s, sp, ft in hits if s >= max(1, best_score)]
    if not candidates:
        candidates = [(sp, ft) for _, sp, ft in hits[:3]]

    seen: set = set()
    lines: List[str] = []
    total = 0
    for sec_path, full_text in candidates:
        if sec_path in seen:
            continue
        seen.add(sec_path)
        entry = f"**{sec_path}**\n{full_text}"
        if total + len(entry) > max_chars:
            if not lines:
                lines.append(entry[:max_chars] + "…")
            break
        lines.append(entry)
        total += len(entry)

    if not lines:
        return "_No spec section content available._"

    header = f"_Spec section(s) for **{cluster}** (from KG):_\n\n"
    return header + "\n\n---\n\n".join(lines)


def _format_spec_section_context(
    pr_chunk: "Document",
    spec_chunks: List["Document"],
    max_chars: int = 2000,
    kg=None,
) -> str:
    """Return spec prose for the section being changed by this PR chunk.

    Strategy (in priority order):
    1. Exact section number match in same cluster  (e.g. chunk edits "1.7.5.1",
       find spec section "1.7.5.1. StateChange Event")
    2. Parent section(s) in same cluster            (e.g. "1.7.5. Events", "1.7.")
    3. Cluster-level keyword fallback               (heading contains entity name from PR)

    When ``spec_chunks`` is empty (no role='spec' source loaded), falls back to
    querying SECTION nodes stored in the KG (full_text property).

    All text is capped to ``max_chars`` total so the prompt stays bounded.
    """
    section_title: str = pr_chunk.metadata.get("section_title", "")
    cluster: str       = pr_chunk.metadata.get("cluster", "")

    if not spec_chunks:
        # KG fallback: query SECTION nodes whose section_path matches the cluster
        if kg is not None and cluster:
            return _format_spec_section_from_kg(kg, cluster, section_title, max_chars)
        return "_No spec source documents loaded — add a role='spec' source to sources.json._"

    if not section_title and not cluster:
        return "_No section/cluster metadata on PR chunk — cannot look up spec context._"

    sec_num = _extract_section_number(section_title)
    parent_nums = _parent_section_numbers(sec_num) if sec_num else []

    cluster_lower = cluster.lower()

    # ── 1. Filter to same cluster ───────────────────────────────────────────
    cluster_chunks = [
        c for c in spec_chunks
        if cluster_lower and (cluster_lower in c.metadata.get("heading", "").lower()
        or cluster_lower in c.page_content[:200].lower())
    ]
    if not cluster_chunks:
        # looser: any chunk from the same source doc whose path contains a cluster slug
        cluster_slug = re.sub(r"[^a-z0-9]+", "_", cluster_lower).strip("_")
        cluster_chunks = [
            c for c in spec_chunks
            if cluster_slug in c.metadata.get("path", "").lower()
        ]

    search_pool = cluster_chunks if cluster_chunks else spec_chunks

    # ── 2. Score each candidate chunk ──────────────────────────────────────
    # score: 3 = exact section match, 2 = direct parent, 1 = grandparent, 0 = cluster-only
    scored: List[tuple] = []
    for c in search_pool:
        heading = c.metadata.get("heading", "")
        c_num = _extract_section_number(heading)
        if sec_num and c_num == sec_num:
            scored.append((3, c))
        elif sec_num and parent_nums and c_num == parent_nums[0]:
            scored.append((2, c))
        elif sec_num and len(parent_nums) > 1 and c_num == parent_nums[1]:
            scored.append((1, c))
        else:
            scored.append((0, c))

    # Keep at least score ≥ 1; if nothing found, keep score 0 (cluster-only)
    best_score = max((s for s, _ in scored), default=0)
    keep_score = max(1, best_score)
    candidates = [c for s, c in scored if s >= keep_score]

    if not candidates:
        return "_No matching spec section found for this PR chunk._"

    # ── 3. Deduplicate and format, cap at max_chars ─────────────────────────
    seen: set = set()
    lines: List[str] = []
    total = 0
    for c in candidates:
        heading = c.metadata.get("heading", "(no heading)")
        text = c.page_content.strip()
        if heading in seen:
            continue
        seen.add(heading)
        entry = f"**{heading}**\n{text}"
        if total + len(entry) > max_chars:
            # Add truncated version if we have nothing yet
            if not lines:
                lines.append(entry[: max_chars] + "…")
            break
        lines.append(entry)
        total += len(entry)

    if not lines:
        return "_No spec section content available._"

    header = (
        f"_Spec source section(s) for **{section_title or cluster}** "
        f"(cluster: {cluster}):_\n\n"
    )
    return header + "\n\n---\n\n".join(lines)


def _format_surrounding_cluster_context(kg, change_rec: Dict) -> str:
    """Return a Section X block listing clusters that depend_on the changed cluster.

    Only ``depends_on`` edges are considered — ``references`` / ``related_to`` are
    excluded to keep this signal-to-noise ratio high.  Capped at 5 clusters.
    Returns an empty string when no strong dependents exist.
    """
    if kg is None or not hasattr(kg, "get_surrounding_clusters"):
        return ""

    cluster = ""
    for field in ("cluster", "cluster_name"):
        cluster = (change_rec.get(field) or "").strip()
        if cluster:
            break
    if not cluster:
        for ent in change_rec.get("entities", []):
            cluster = (ent.get("cluster") or "").strip()
            if cluster:
                break
    if not cluster:
        return ""

    surrounding = kg.get_surrounding_clusters(cluster, max_results=5)
    if not surrounding:
        return ""

    lines = [
        f"**Clusters with a `depends_on` dependency on {cluster}** "
        f"(edge-case awareness — do NOT generate new TCs for these clusters unless "
        f"the change directly breaks a cross-cluster interaction that has no existing coverage):"
    ]
    for item in surrounding:
        lines.append(f"- **{item['cluster']}** — {item['reason']}")
    return "\n".join(lines)


def _format_entity_context(kg, change_rec: Dict) -> str:
    """Query the KG for REQUIREMENT nodes linked to entities in the change record.

    Returns a Section R string with spec requirements per entity so the LLM can
    understand what behaviours the spec mandates for the changed attribute/command.

    Args:
        kg:          MatterKGBuilder (or compatible) instance.
        change_rec:  Structured change dict with ``cluster`` and ``entities`` fields.

    Returns:
        Formatted Markdown string, or a fallback message when nothing is found.
    """
    if kg is None:
        return "_KG not available — no entity requirement context._"

    cluster = change_rec.get("cluster", "") or change_rec.get("cluster_name", "")
    entities = change_rec.get("entities", [])
    if not entities:
        return "_No entities in change record._"

    lines: List[str] = []

    for ent in entities[:5]:  # cap at 5 entities to keep prompt size bounded
        ent_name = ent.get("name", "")
        ent_type = ent.get("type", "")
        if not ent_name:
            continue

        # --- Look up entity metadata from KG (datatype, access, default, quality) ---
        entity_meta_str = ""
        try:
            _etype_upper = ent_type.upper() if ent_type else "ATTRIBUTE"
            # Normalise: "attribute" → "ATTRIBUTE", etc.
            if _etype_upper not in {"ATTRIBUTE", "COMMAND", "EVENT", "FEATURE"}:
                _etype_upper = "ATTRIBUTE"
            _entity_node_id = f"{_etype_upper}::{cluster}::{ent_name}"
            _entity_data = kg._graph.nodes.get(_entity_node_id, {})
            _entity_obj = _entity_data.get("obj") if isinstance(_entity_data, dict) else None
            # Fallback: scan nodes matching type + name when direct ID lookup misses
            if _entity_obj is None:
                for _nid, _ndata in kg._graph.nodes(data=True):
                    _obj = _ndata.get("obj")
                    if _obj is None:
                        continue
                    _nt = _obj.node_type.value if hasattr(_obj.node_type, "value") else str(_obj.node_type)
                    if _nt != _etype_upper:
                        continue
                    if (_obj.label or "").lower() == ent_name.lower() or _nid.endswith(f"::{ent_name}"):
                        if not cluster or ((_obj.properties.get("cluster") or "").lower() == cluster.lower()):
                            _entity_obj = _obj
                            break
            if _entity_obj is not None:
                _p = _entity_obj.properties
                _meta_parts: List[str] = []
                if _p.get("datatype"):
                    _meta_parts.append(f"type={_p['datatype']}")
                if _p.get("access"):
                    _meta_parts.append(f"access={_p['access']}")
                if _p.get("default") not in (None, "", "null"):
                    _meta_parts.append(f"default={_p['default']}")
                if _p.get("quality"):
                    _meta_parts.append(f"quality={_p['quality']}")
                if _p.get("conformance"):
                    _meta_parts.append(f"conformance={_p['conformance']}")
                if _p.get("code"):
                    _meta_parts.append(f"id={_p['code']}")
                if _meta_parts:
                    entity_meta_str = " — " + ", ".join(_meta_parts)
        except Exception as _meta_exc:
            logger.debug("[_build_entity_context] Entity metadata extraction failed: %s", _meta_exc)

        keywords = [ent_name]
        if cluster:
            keywords.append(cluster)

        # Entity metadata header (type, access, conformance, quality, id) is what the LLM
        # needs to generate correct test step values and PICS conditions.
        if entity_meta_str:
            lines.append(f"**Entity: {ent_type} `{ent_name}`** (cluster: {cluster}){entity_meta_str}")
            lines.append("")

    if not lines:
        return "_No entity metadata found in KG for the changed entities._"

    return "\n".join(lines)


def _collect_uncovered_reqs(kg, cluster_name: str, max_reqs: int = 30) -> List[Dict]:
    """Return REQUIREMENT nodes for *cluster_name* that have no TEST_CASE coverage in the KG.

    For regular clusters, matches requirements by their ``cluster`` property.
    For VirtualCluster-* (protocol areas), matches by ``section_path`` containing
    the chapter keyword (e.g., "Secure Channel" for VirtualCluster-SC) since
    protocol requirements have empty ``cluster`` properties.

    Returns a list of dicts: [{node_id, normative_text, section_path}]
    """
    if kg is None:
        return []

    from src.knowledge_graph.base_graph import NodeType as _NT

    cluster_lower = cluster_name.lower().strip()

    # Resolve VirtualCluster names to chapter keywords for section_path matching
    _chapter_keywords: List[str] = []
    if cluster_lower.startswith("virtualcluster-"):
        _chapter_keywords = _VC_TO_CHAPTER_KEYWORDS.get(cluster_lower, [])

    results: List[Dict] = []
    seen: set = set()

    for _nid, _ndata in kg._graph.nodes(data=True):
        if len(results) >= max_reqs:
            break
        obj = _ndata.get("obj")
        if obj is None:
            continue
        nt = obj.node_type.value if hasattr(obj.node_type, "value") else str(obj.node_type)
        if nt not in ("REQUIREMENT", "BEHAVIOR_RULE"):
            continue

        node_cluster = (obj.properties.get("cluster") or obj.properties.get("cluster_name") or "").lower().strip()

        if node_cluster:
            # Regular cluster requirement — match by cluster name
            if not _cluster_names_match(node_cluster, cluster_name):
                continue
        elif _chapter_keywords:
            # Protocol requirement (empty cluster) — match by section_path
            sec_path = (obj.properties.get("section_path") or "").lower()
            if not any(kw in sec_path for kw in _chapter_keywords):
                continue
        else:
            # Empty cluster, not a VirtualCluster query — skip
            continue

        if _nid in seen:
            continue
        # KG edges run FROM test_case TO requirement (covers/tests/validates).
        # Check incoming edges on the REQUIREMENT node for any TC coverage.
        covered = False
        for _src, _, _edata in kg._graph.in_edges(_nid, data=True):
            rel = (_edata.get("edge_type") or "").lower()
            if rel in ("verifies_requirement", "covers", "tests", "validates", "implements",
                       "verifies_attribute", "tests_command", "observes_event", "verifies_rule"):
                covered = True
                break
        if covered:
            continue
        text = (
            obj.properties.get("normative_text", "")
            or obj.properties.get("full_text", "")
            or obj.label
            or ""
        )
        if not text.strip():
            continue
        seen.add(_nid)
        results.append({
            "node_id": _nid,
            "normative_text": text[:300],
            "section_path": obj.properties.get("section_path") or obj.properties.get("section") or "",
        })

    return results


def _add_chunks_with_debug(
    kg,
    chunks: List[Document],
    add_fn,
    label: str,
    run_dir: str,
) -> None:
    """Add *chunks* to *kg* one source file at a time, writing a per-source debug
    KG snapshot to ``<run_dir>/kg_debug/<label>/<source_id>.json`` after each source.

    The per-source snapshots show exactly what nodes and edges were contributed by
    each individual HTML file, making it easy to verify the KG build for each source.

    Args:
        kg:       The knowledge graph being built.
        chunks:   All chunks (spec or test plan) to be added.
        add_fn:   The graph method to call (``kg.add_spec_documents`` etc.).
        label:    Subdirectory label — ``"spec"`` or ``"test_plan"``.
        run_dir:  Per-run log directory path string.
    """
    import json as _json
    from collections import defaultdict

    # Group chunks by source_id (falls back to path stem)
    by_source: Dict[str, List[Document]] = defaultdict(list)
    for chunk in chunks:
        src = (
            chunk.metadata.get("source_id")
            or Path(chunk.metadata.get("path", "unknown")).stem
        )
        by_source[src].append(chunk)

    debug_dir = Path(run_dir) / "kg_debug" / label if run_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    for source_id, source_chunks in by_source.items():
        nodes_before = kg.num_nodes
        edges_before = kg.num_edges

        add_fn(source_chunks)

        nodes_added = kg.num_nodes - nodes_before
        edges_added = kg.num_edges - edges_before
        logger.info(
            "[build_knowledge_graph_node] %s source '%s': +%d nodes, +%d edges "
            "(total: %d nodes, %d edges)",
            label, source_id, nodes_added, edges_added, kg.num_nodes, kg.num_edges,
        )

        if debug_dir:
            try:
                snapshot = {
                    "source_id": source_id,
                    "label": label,
                    "chunks_ingested": len(source_chunks),
                    "nodes_added": nodes_added,
                    "edges_added": edges_added,
                    "total_nodes_after": kg.num_nodes,
                    "total_edges_after": kg.num_edges,
                }
                out_path = debug_dir / f"{source_id}.json"
                out_path.write_text(
                    _json.dumps(snapshot, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning(
                    "[build_knowledge_graph_node] Could not write debug snapshot for '%s': %s",
                    source_id, exc,
                )


def _chunk_matches_cluster(chunk: Document, cluster_filter: str) -> bool:
    """True if chunk belongs to the specified cluster (case-insensitive metadata match).

    Only checks chunk metadata — does NOT scan page_content, which would cause
    false positives when a chunk mentions another cluster in cross-references.

    For VirtualCluster-* names, resolves to chapter keywords (e.g., "secure channel"
    for VirtualCluster-SC) so protocol PR chunks with chapter-heading-based cluster
    metadata are correctly matched.
    """
    needle = cluster_filter.lower()
    # Resolve VirtualCluster names to chapter keywords
    _needles = [needle]
    if needle.startswith("virtualcluster-"):
        _kws = _VC_TO_CHAPTER_KEYWORDS.get(needle, [])
        if _kws:
            _needles = _kws  # use chapter keywords instead

    meta_cluster = chunk.metadata.get("cluster", "").lower()
    for n in _needles:
        if meta_cluster and (n in meta_cluster or meta_cluster in n):
            return True
    section_title = chunk.metadata.get("section_title", "").lower()
    for n in _needles:
        if section_title and n in section_title:
            return True
    return False


def _search_results_to_candidates(results: List[SearchResult]) -> List[Dict]:
    """Convert ``SearchResult`` objects to the candidate dict format the re-ranker expects."""
    candidates = []
    for i, r in enumerate(results):
        meta = r.metadata or {}
        candidates.append({
            "candidate_id": f"cand_{i}",
            "test_case_id": meta.get("tc_id") or r.doc_id,
            "chunk_type":   meta.get("section_type") or "",
            "title":        meta.get("tc_id") or "",
            "text":         r.page_content,
            "score":        r.score,
            # Pass metadata through as-is; reranker reads cluster_name OR cluster.
            "metadata":     meta,
        })
    return candidates


def _graph_hits_to_kg_hints(graph_hits: List[GraphNode]) -> Dict:
    """Convert KG search results to the ``kg_hits`` format the re-ranker expects.

    TEST_CASE nodes → ``direct_tests`` list.
    Schema entities (CLUSTER/ATTRIBUTE/COMMAND/EVENT/FEATURE) → ``matched_entities``.
    Other nodes that carry ``tc_id`` or ``related_tests`` in their properties →
    ``indirect_tests``.
    """
    direct_tests: List[str]    = []
    indirect_tests: List[str]  = []
    matched_entities: List[str] = []

    for node in graph_hits:
        ntype = node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type)
        if ntype == "TEST_CASE":
            tc_id = node.properties.get("tc_id") or node.label
            direct_tests.append(tc_id)
        elif ntype in {"CLUSTER", "ATTRIBUTE", "COMMAND", "EVENT", "FEATURE"}:
            matched_entities.append(f"{ntype}::{node.label}")
        else:
            # REQUIREMENT, BEHAVIOR_RULE, SECTION — look for linked TC IDs in properties
            for key in ("related_tests", "tc_ids", "tc_id"):
                val = node.properties.get(key)
                if isinstance(val, list):
                    indirect_tests.extend(val)
                elif isinstance(val, str) and val:
                    indirect_tests.append(val)

    return {
        "direct_tests":    direct_tests,
        "indirect_tests":  indirect_tests,
        "matched_entities": matched_entities,
    }


def _find_sibling_clusters(kg, cluster_name: str) -> List[str]:
    """Find all sibling cluster names that share the same base cluster via ALIAS_OF edges."""
    if kg is None or not hasattr(kg, "_graph"):
        return []
    from src.knowledge_graph.base_graph import NodeType as _NT, EdgeType as _ET
    cluster_lower = cluster_name.lower()

    # Find the CLUSTER node for this cluster
    target_nid = None
    for nid, data in kg._graph.nodes(data=True):
        obj = data.get("obj")
        if obj and obj.node_type == _NT.CLUSTER and obj.label.lower() == cluster_lower:
            target_nid = nid
            break
    if not target_nid:
        return []

    # Check if this cluster is an alias → follow ALIAS_OF to base
    base_nid = target_nid
    for _, tgt, edata in kg._graph.out_edges(target_nid, data=True):
        if edata.get("edge_type") in (_ET.ALIAS_OF, "alias_of"):
            base_nid = tgt
            break

    # Collect all clusters that point to the same base (including base itself)
    siblings = []
    base_obj = kg._graph.nodes.get(base_nid, {}).get("obj")
    if base_obj and base_obj.label.lower() != cluster_lower:
        siblings.append(base_obj.label)
    for nid, _, edata in kg._graph.in_edges(base_nid, data=True):
        if edata.get("edge_type") in (_ET.ALIAS_OF, "alias_of"):
            obj = kg._graph.nodes.get(nid, {}).get("obj")
            if obj and obj.label.lower() != cluster_lower:
                siblings.append(obj.label)
    return siblings

def _format_ranked_test_cases(ranked: List[RankedCandidate]) -> str:
    """Format re-ranked candidates for the LLM prompt.

    Includes the re-rank score and reason in each header so the LLM receives
    structured pre-context about *why* each test case was selected.
    """
    if not ranked:
        return "_No relevant test cases found._"
    lines = []
    for r in ranked:
        meta    = r.metadata or {}
        cluster = meta.get("cluster_name") or meta.get("cluster") or ""
        section = meta.get("section_type") or r.chunk_type or ""
        path    = meta.get("path") or ""
        pics_raw = meta.get("pics_codes") or []
        pics_str = ", ".join(pics_raw) if isinstance(pics_raw, list) else str(pics_raw)

        header_parts = [f"**[Score: {r.final_score:.3f} | {r.reason}]**"]
        if r.test_case_id:
            header_parts.append(f"`{r.test_case_id}`")
        if cluster:
            header_parts.append(f"cluster={cluster}")
        if section:
            header_parts.append(f"section={section}")
        header = " — ".join(header_parts)

        detail_parts = []
        if pics_str:
            detail_parts.append(f"PICS: {pics_str}")
        if path and r.test_case_id:
            detail_parts.append(f"file: `{path}`")
        detail = " | ".join(detail_parts)

        lines.append(header)
        if detail:
            lines.append(f"  {detail}")
        lines.append(f"  > {r.text[:400]}")
        lines.append("")
    return "\n".join(lines)


def _merge_overlapping_tcs(
    all_tcs: List[dict],
    missing_tcs: List[dict],
    gap_tcs: List[dict],
    llm,
    config,
) -> tuple:
    """Run a single LLM call to merge overlapping TCs.

    Groups TCs by category and asks the LLM to consolidate duplicates.
    Returns (merged_missing, merged_gaps) or (None, None) on failure.
    """
    import json as _json

    tc_summaries = []
    for i, tc in enumerate(all_tcs):
        title = tc.get("title", f"TC-{i}")
        purpose = ""
        adoc = tc.get("adoc_section", "")
        if "=== Purpose" in adoc:
            purpose = adoc.split("=== Purpose\n")[1].split("\n===")[0].strip()[:200]
        source = "pass1" if tc in missing_tcs else "coverage_gap"
        tc_summaries.append(f"{i}. [{source}] {title}\n   Purpose: {purpose}")

    prompt = (
        "You are a Matter test plan reviewer. Below are proposed test cases for a single "
        "cluster. Many of them overlap — multiple TCs read the same attributes, test the "
        "same commands, or cover the same events.\n\n"
        "YOUR TASK: Identify which TCs should be MERGED because they cover overlapping scope. "
        "Also identify duplicate TC prefix families (e.g., TC-AVA-* and TC-AVANALY-* for the "
        "same cluster) — keep only the shorter prefix.\n\n"
        "RULES:\n"
        "- ONE attribute-read TC per cluster (consolidate all attribute reads into one TC)\n"
        "- At most 2 TCs per command (positive + negative/boundary)\n"
        "- Consolidate event TCs into lifecycle-oriented groups\n"
        "- A 25-step TC is better than three 10-step TCs with repeated setup\n"
        "- Keep unique-scope TCs that don't overlap with anything\n\n"
        f"PROPOSED TCs ({len(all_tcs)} total):\n\n"
        + "\n".join(tc_summaries)
        + "\n\nReturn ONLY valid JSON:\n"
        '{"keep": [<list of TC indices (0-based) to KEEP as-is>],\n'
        ' "remove": [<list of TC indices to REMOVE (merged into a kept TC)>],\n'
        ' "merge_notes": "<1-2 sentence summary of what was consolidated>"}\n'
    )

    try:
        if hasattr(llm, "set_next_label"):
            llm.set_next_label("Pass 3 — TC Merge")
        response = llm.complete(prompt, system="You are a Matter test plan consolidation expert.")
    except Exception as exc:
        logger.warning("[Pass 3: TC Merge] LLM call failed: %s", exc)
        return None, None

    # Parse response
    merge_json = None
    bare = _extract_json_object(response)
    if bare:
        try:
            merge_json = _json.loads(bare)
        except _json.JSONDecodeError:
            try:
                merge_json = _json.loads(_repair_json(bare))
            except _json.JSONDecodeError:
                pass

    if not merge_json or "keep" not in merge_json:
        logger.warning("[Pass 3: TC Merge] Could not parse merge response — keeping all TCs")
        return None, None

    keep_indices = set(merge_json.get("keep", []))
    remove_indices = set(merge_json.get("remove", []))
    merge_notes = merge_json.get("merge_notes", "")

    if merge_notes:
        logger.info("[Pass 3: TC Merge] %s", merge_notes)

    if not remove_indices:
        logger.info("[Pass 3: TC Merge] LLM found no TCs to merge — keeping all")
        return None, None

    # Rebuild missing and gap lists without removed TCs
    missing_set = set(id(tc) for tc in missing_tcs)
    merged_missing = []
    merged_gaps = []
    for i, tc in enumerate(all_tcs):
        if i in remove_indices:
            continue
        if id(tc) in missing_set:
            merged_missing.append(tc)
        else:
            merged_gaps.append(tc)

    return merged_missing, merged_gaps


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the first complete top-level JSON object using brace depth tracking."""
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return None


def _repair_json(text: str) -> str:
    """Best-effort repair of common LLM JSON errors before json.loads().

    Fixes:
    - Unquoted TC-ID identifiers (e.g., TC-SC-2.1 → "TC-SC-2.1")
    - Trailing commas before ] or }
    """
    text = re.sub(r'(?<=[\[,])\s*(TC-[A-Z][A-Z0-9]*-[\d.]+)\s*(?=[,\]])', r' "\1"', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def _parse_structured_response(
    response: str,
    chunk: Document,
    vector_hits: List[SearchResult],
    graph_hits: List[GraphNode],
    change_rec: dict,
) -> dict:
    """Parse the LLM's structured JSON response for analyze_chunks_with_llm_node."""
    import json
    import re as _re

    llm_json: dict = {}

    # Try code-fenced JSON first
    m = _re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, _re.DOTALL)
    if m:
        try:
            llm_json = json.loads(m.group(1))
        except json.JSONDecodeError:
            try:
                llm_json = json.loads(_repair_json(m.group(1)))
            except json.JSONDecodeError:
                pass

    # Bare JSON fallback — use balanced-brace extractor instead of greedy regex
    if not llm_json:
        _bare = _extract_json_object(response)
        if _bare:
            try:
                llm_json = json.loads(_bare)
            except json.JSONDecodeError:
                try:
                    llm_json = json.loads(_repair_json(_bare))
                except json.JSONDecodeError:
                    pass

    # Truncation recovery: if braces are unbalanced, try appending missing '}'
    if not llm_json:
        _stripped = response.strip()
        if _stripped.startswith("```"):
            _stripped = _stripped.split("\n", 1)[1]
        if _stripped.rstrip().endswith("```"):
            _stripped = _stripped.rstrip()[:-3].rstrip()
        _open = _stripped.count("{") - _stripped.count("}")
        if _open > 0:
            _patched = _stripped + ("}" * _open)
            try:
                llm_json = json.loads(_patched)
                logger.info(
                    "[_parse_structured_response] Recovered truncated JSON "
                    "(appended %d closing braces)", _open,
                )
            except json.JSONDecodeError:
                try:
                    llm_json = json.loads(_repair_json(_patched))
                except json.JSONDecodeError:
                    pass

    # Nested format recovery: LLM sometimes puts missing_tests/update_candidates
    # inside a "recommendation" wrapper instead of at the top level
    if llm_json and "missing_tests" not in llm_json and "update_candidates" not in llm_json:
        _rec = llm_json.get("recommendation", {})
        if isinstance(_rec, dict):
            if "missing_tests" in _rec:
                llm_json["missing_tests"] = _rec["missing_tests"]
            if "update_candidates" in _rec:
                llm_json["update_candidates"] = _rec["update_candidates"]

    if not llm_json:
        logger.warning(
            "[_parse_structured_response] JSON parse failed — response_len=%d, "
            "first 200 chars: %.200s",
            len(response or ""), (response or "")[:200],
        )

    # Detect template echo: LLM may have returned the format template instead of real
    # analysis.  Flag it for the report but NEVER discard the data — false positives
    # cause silent data loss (PR 12681 incident).
    _template_echo_warning = False
    if llm_json:
        _action = llm_json.get("recommendation", {}).get("action", "") if isinstance(llm_json.get("recommendation"), dict) else ""
        _summary = llm_json.get("change_summary", "")
        _has_placeholder = (
            _summary == "one-sentence description of what changed"
            or any(
                "<describe" in (tc.get("adoc_section", "") or "")
                for tc in llm_json.get("missing_tests", [])
            )
        )
        if _has_placeholder:
            _template_echo_warning = True
            logger.warning(
                "[_parse_structured_response] Possible template echo — response contains "
                "placeholder text. Data preserved for review (not discarded)."
            )

    pr_path = chunk.metadata.get("path", "")
    pr_status = chunk.metadata.get("status", "")

    missing: List[dict] = []
    _fallback_cluster = change_rec.get("cluster") or change_rec.get("cluster_name") or ""
    for mt in llm_json.get("missing_tests", []):
        missing.append({
            "title": mt.get("title", ""),
            "cluster": mt.get("cluster", "") or _fallback_cluster,
            "adoc_section": mt.get("adoc_section", ""),
            "pr_path": pr_path,
            "pr_status": pr_status,
        })

    updates: List[dict] = []
    for uc in llm_json.get("update_candidates", []):
        updates.append({
            "tc_id": uc.get("tc_id", ""),
            "cluster": uc.get("cluster", "") or _fallback_cluster,
            "change_summary": uc.get("change_summary", ""),
            "adoc_section": uc.get("adoc_section", ""),
            "pr_path": pr_path,
            "pr_status": pr_status,
        })

    neg_tests: List[dict] = []
    for nt in llm_json.get("negative_tests", []):
        neg_tests.append({
            "title": nt.get("title", ""),
            "cluster": nt.get("cluster", "") or _fallback_cluster,
            "negative_type": nt.get("negative_type", ""),
            "target_entity": nt.get("target_entity", ""),
            "expected_status": nt.get("expected_status", ""),
            "adoc_section": nt.get("adoc_section", ""),
            "pr_path": pr_path,
            "pr_status": pr_status,
        })

    return {
        "pr_chunk": pr_path,
        "change_kind": change_rec.get("change_kind", ""),
        "cluster": change_rec.get("cluster", ""),
        "change_summary": llm_json.get("change_summary", ""),
        "impacted_entities": llm_json.get("impacted_entities", change_rec.get("entities", [])),
        "coverage": llm_json.get("coverage", {
            "direct_tests": [],
            "indirect_tests": [],
            "missing": not bool(vector_hits) and not bool(graph_hits),
        }),
        "recommendation": llm_json.get("recommendation", {"action": "none", "details": ""}),
        "reasoning": llm_json.get("reasoning", ""),
        "llm_response": response,
        "missing_tests": missing,
        "update_candidates": updates,
        "negative_tests": neg_tests,
        "vector_result_count": len(vector_hits),
        "graph_result_count": len(graph_hits),
        "parse_failed": not bool(llm_json),
        "template_echo_warning": _template_echo_warning,
    }


def _parse_llm_response(
    response: str,
    chunk: Document,
    vector_results: List[SearchResult],
    graph_nodes: List[GraphNode],
) -> dict:
    """Extract structured data from the LLM's Markdown response."""
    import json
    import re as _re

    missing = []
    updates = []
    llm_json: dict = {}

    # Primary: extract the JSON code-fence block
    json_match = _re.search(r"```json\s*(\{.*?\})\s*```", response, _re.DOTALL)
    if json_match:
        try:
            llm_json = json.loads(json_match.group(1))
            for mt in llm_json.get("missing_tests", []):
                missing.append({
                    "text": mt.get("title", ""),
                    "cluster": mt.get("cluster", ""),
                    "pr_path": chunk.metadata.get("path", ""),
                    "pr_status": chunk.metadata.get("status", ""),
                })
            for uc in llm_json.get("update_candidates", []):
                updates.append({
                    "text": uc.get("tc_id", "") + (
                        f": {uc['change_summary']}" if uc.get("change_summary") else ""
                    ),
                    "tc_id": uc.get("tc_id", ""),
                    "pr_path": chunk.metadata.get("path", ""),
                    "pr_status": chunk.metadata.get("status", ""),
                })
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[analyze_chunks_with_llm_node] JSON parse error in LLM response: %s", exc)

    # Fallback: heuristic line scanner
    if not llm_json:
        lines = response.splitlines()
        current_section = None
        for line in lines:
            lower = line.lower()
            if "missing test" in lower or "new test" in lower:
                current_section = "missing"
            elif "update" in lower and "test" in lower:
                current_section = "update"
            elif "already covered" in lower:
                current_section = "covered"
            elif line.strip().startswith(("-", "*", "1.", "2.", "3.")) and current_section:
                item = {
                    "text": line.strip().lstrip("-*0123456789. "),
                    "pr_path": chunk.metadata.get("path", ""),
                    "pr_status": chunk.metadata.get("status", ""),
                }
                if current_section == "missing":
                    missing.append(item)
                elif current_section == "update":
                    updates.append(item)

    return {
        "pr_chunk": chunk.metadata.get("path", ""),
        "llm_response": response,
        "llm_json": llm_json,
        "missing_tests": missing,
        "update_candidates": updates,
        "vector_result_count": len(vector_results),
        "graph_result_count": len(graph_nodes),
    }


def _esc_html(t: str) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_REPORT_CSS = """
*{box-sizing:border-box}
body{font-family:Arial,Helvetica,sans-serif;font-size:10pt;padding:2cm;color:#1c2833}
h1{color:#1a5276;border-bottom:2px solid #1a5276;padding-bottom:6px;font-size:1.5em}
h2{color:#1f618d;margin-top:28px;padding:4px 0 4px 10px;border-left:4px solid #1f618d;font-size:1.2em}
h3{color:#2874a6;margin-top:14px;font-size:1.05em}
.summ{background:#eaf4fb;border:1px solid #aed6f1;padding:12px 16px;border-radius:4px;margin:14px 0}
.summ table{border-collapse:collapse;width:100%}
.summ td{padding:4px 10px}
.summ td:first-child{font-weight:bold;color:#1a5276;white-space:nowrap}
.card{border:1px solid #d5d8dc;border-radius:4px;margin:12px 0;padding:12px 16px;page-break-inside:avoid}
.upd{border-left:4px solid #e67e22;background:#fef9e7}
.new{border-left:4px solid #1e8449;background:#eafaf1}
.cvr{border-left:4px solid #7f8c8d;background:#f8f9f9}
.ch{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}
.tid{font-weight:bold;font-size:1.05em;color:#1a5276}
.lbl{font-weight:bold;font-size:.85em;color:#5d6d7e;margin:8px 0 2px;text-transform:uppercase;letter-spacing:.04em}
.box{background:#fff;border:1px solid #d5d8dc;border-radius:3px;padding:7px 10px;font-size:9pt;white-space:pre-wrap;overflow-x:auto}
.badge{display:inline-block;padding:2px 8px;border-radius:3px;font-size:.78em;font-weight:bold;color:#fff}
.badge-high{background:#c0392b}
.badge-medium{background:#d68910}
.badge-low{background:#1e8449}
.badge-none{background:#7f8c8d}
.cluster-tag{background:#2874a6;color:#fff;padding:1px 6px;border-radius:3px;font-size:.8em}
table.analysis{width:100%;border-collapse:collapse;font-size:9pt;margin-top:6px}
table.analysis th{background:#2874a6;color:#fff;padding:5px 8px;text-align:left}
table.analysis td{border:1px solid #d0d3d4;padding:4px 7px;vertical-align:top}
table.analysis tr:nth-child(even) td{background:#f2f3f4}
hr{border:none;border-top:1px solid #d5d8dc;margin:24px 0}
.ft{font-size:8pt;color:#888;margin-top:20px}
@page{size:A4;margin:2cm}
"""


def _pri_badge(priority: str) -> str:
    cls_map = {"High": "badge-high", "Medium": "badge-medium", "Low": "badge-low"}
    cls = cls_map.get(priority, "badge-none")
    return f'<span class="badge {cls}">{_esc_html(priority or "—")}</span>'


def _partial_banner_html(llm_failed: int, aborted_at: Optional[int], total: int) -> str:
    if aborted_at is not None:
        return (
            f'<div style="background:#fde8e8;border:2px solid #c0392b;border-radius:4px;'
            f'padding:12px 16px;margin:14px 0;">'
            f'<strong style="color:#c0392b">&#9888; PARTIAL REPORT — Pipeline aborted at chunk '
            f'{aborted_at + 1}/{total}</strong><br>'
            f'The LLM connection was lost mid-run. Results cover only the first {aborted_at} chunk(s). '
            f'Re-run with <code>--compare-only</code> to complete the analysis.</div>'
        )
    if llm_failed > 0:
        return (
            f'<div style="background:#fef9e7;border:2px solid #e67e22;border-radius:4px;'
            f'padding:12px 16px;margin:14px 0;">'
            f'<strong style="color:#e67e22">&#9888; PARTIAL REPORT — {llm_failed} of {total} '
            f'chunk(s) failed due to LLM errors.</strong><br>'
            f'Results may be incomplete. Re-run with <code>--compare-only</code> to retry.</div>'
        )
    return ""


def _build_pass_funnel_html(pass_stats: Optional[Dict[str, Any]]) -> str:
    """Build a collapsible pipeline pass funnel summary. Never raises."""
    try:
        return _build_pass_funnel_html_inner(pass_stats)
    except Exception:
        return ""


def _build_pass_funnel_html_inner(pass_stats: Optional[Dict[str, Any]]) -> str:
    """Build a collapsible pipeline pass funnel summary."""
    if not pass_stats:
        return ""

    p1 = pass_stats.get("pass1", {})
    p2 = pass_stats.get("pass2", {})
    p3 = pass_stats.get("pass3", {})

    rows = ""

    # Pass 1
    if p1:
        rows += (
            f'<tr style="background:#eafaf1">'
            f'<td><strong>Pass 1: Per-Chunk Analysis</strong></td>'
            f'<td>{p1.get("chunks_analyzed", 0)}/{p1.get("chunks_total", 0)} chunks</td>'
            f'<td style="color:#1e8449"><strong>+{p1.get("new_tcs", 0)}</strong> new TCs</td>'
            f'<td>{p1.get("update_candidates", 0)} updates</td>'
            f'<td></td></tr>'
        )

    # Pass 2
    if p2:
        rows += (
            f'<tr style="background:#f5eef8">'
            f'<td><strong>Pass 2: Cluster Review</strong></td>'
            f'<td>{p2.get("symmetry_gaps", 0)} symmetry gaps, '
            f'{p2.get("missing_test_types", 0)} missing types</td>'
            f'<td style="color:#6c3483"><strong>+{p2.get("review_additions", 0)}</strong> findings</td>'
            f'<td></td>'
            f'<td></td></tr>'
        )

    # Pass 3
    if p3:
        _input = p3.get("consolidation_input", 0)
        _review_in = p3.get("review_input", 0)
        _kept = p3.get("consolidation_kept", 0)
        _removed = p3.get("consolidation_removed", 0)
        _gaps = p3.get("coverage_gap_tcs", 0)
        _final = p3.get("final_new_tcs", 0)
        _review_after = p3.get("review_after_filter", 0)

        rows += (
            f'<tr style="background:#eaf4fb">'
            f'<td><strong>Pass 3: Consolidation</strong></td>'
            f'<td>Input: {_input} TCs (pass1) + {_review_in} (review)</td>'
            f'<td style="color:#1a5276"><strong>{_kept}</strong> kept</td>'
            f'<td style="color:#c0392b">{_removed} duplicates removed</td>'
            f'<td>{_review_after} review items remaining</td></tr>'
        )
        if _gaps > 0:
            rows += (
                f'<tr style="background:#f3e5f5">'
                f'<td><strong>Pass 3: Coverage Gaps</strong></td>'
                f'<td>Uncovered spec requirements</td>'
                f'<td style="color:#5b2c6f"><strong>+{_gaps}</strong> gap TCs</td>'
                f'<td></td>'
                f'<td></td></tr>'
            )

        # Final total
        rows += (
            f'<tr style="background:#d5f5e3;font-weight:bold">'
            f'<td>Final Output</td>'
            f'<td></td>'
            f'<td style="color:#1e8449">{_final} new TCs</td>'
            f'<td>{p3.get("final_updates", 0)} updates</td>'
            f'<td>{_gaps} coverage gap TCs</td></tr>'
        )

    if not rows:
        return ""

    return (
        '<details style="margin:20px 0;border:1px solid #aed6f1;border-radius:4px">'
        '<summary style="background:#eaf4fb;padding:10px 14px;cursor:pointer;'
        'font-weight:bold;color:#1a5276;font-size:1.05em">'
        'Pipeline Pass Summary (click to expand)</summary>'
        '<div style="padding:10px 14px">'
        '<table style="width:100%;border-collapse:collapse;font-size:9pt">'
        '<tr style="background:#2874a6;color:#fff">'
        '<th style="padding:5px 8px;text-align:left">Pass</th>'
        '<th style="padding:5px 8px;text-align:left">Input</th>'
        '<th style="padding:5px 8px;text-align:left">Added</th>'
        '<th style="padding:5px 8px;text-align:left">Removed</th>'
        '<th style="padding:5px 8px;text-align:left">Notes</th></tr>'
        f'{rows}'
        '</table></div></details>'
    )


def _build_test_coverage_html(
    pr_url: str,
    analysis: List[dict],
    missing_tests: List[dict],
    update_candidates: List[dict],
    adoc_paths: List[str],
    timestamp: str,
    llm_failed: int = 0,
    aborted_at: Optional[int] = None,
    total_chunks: int = 0,
    review_additions: Optional[List[dict]] = None,
    second_pass_outlines: Optional[List[str]] = None,
    coverage_gap_tests: Optional[List[dict]] = None,
    cluster_filter: str = "",
    pass_stats: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the traceability HTML report: each change → its new/updated TCs.

    When ``review_additions`` is provided (non-None), a Section 4 is appended
    showing what the cluster-level review pass added on top of pass 1.
    When it is None (pass-1-only report), Section 4 is omitted entirely.

    TCs with ``source=="second_pass"`` (holistic KG analysis) are rendered in
    Section 5 with outline-file links.  TCs with ``source=="human_review"``
    (human-edited outline re-expansion) are rendered in Section 6.
    Coverage gap TCs are rendered in Section 7 with distinct blue/purple styling.
    Pass-1 TCs (no ``source`` field) continue to appear in Section 1.
    """
    from datetime import datetime
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Split missing_tests by origin so each pass gets its own section.
    pass1_missing = [t for t in missing_tests if t.get("source") not in ("second_pass", "human_review")]
    pass2_missing = [t for t in missing_tests if t.get("source") == "second_pass"]
    pass3_missing = [t for t in missing_tests if t.get("source") == "human_review"]

    # Build lookup from pr_chunk key → analysis result (for cluster/change metadata).
    analysis_by_chunk = {r.get("pr_chunk", ""): r for r in analysis if "error" not in r}

    # Group pass-1 (PR-chunk-linked) missing TCs by pr_path for Section 1.
    new_by_chunk: Dict[str, List[dict]] = {}
    for tc in pass1_missing:
        key = tc.get("pr_path", "")
        new_by_chunk.setdefault(key, []).append(tc)

    upd_by_chunk: Dict[str, List[dict]] = {}
    for tc in update_candidates:
        key = tc.get("pr_path", "")
        upd_by_chunk.setdefault(key, []).append(tc)

    # "No action" = analysis entries with no new or updated TCs.
    no_action: List[dict] = []
    for r in analysis:
        if "error" in r:
            continue
        key = r.get("pr_chunk", "")
        action = (r.get("recommendation") or {}).get("action", "none")
        if action == "none" and not new_by_chunk.get(key) and not upd_by_chunk.get(key):
            no_action.append(r)

    partial_banner = _partial_banner_html(llm_failed, aborted_at, total_chunks)

    # No-chunks banner: shown when PR/input-doc was given but yielded zero PR chunks
    # (e.g. cluster filter excluded all content, or input doc had no parseable diff).
    no_chunks_banner = ""
    if not analysis and not missing_tests and not update_candidates:
        cluster_note = ""
        if cluster_filter:
            cluster_note = (
                f'<li><strong>Active cluster filter: <code>{_esc_html(cluster_filter)}</code></strong> &mdash; '
                f'the input document may not contain sections matching this cluster name. '
                f'Check the section headings in the input document and verify the cluster name spelling.</li>'
            )
        no_chunks_banner = f"""
<div style="background:#fef9e7;border:1px solid #f39c12;border-radius:4px;padding:14px 18px;margin:16px 0;">
  <strong style="color:#d68910">&#9888; No test cases recommended by the LLM</strong>
  <p style="margin:6px 0 0;color:#555">The pipeline ran successfully but produced no analysis results.
  Possible reasons:</p>
  <ul style="margin:6px 0 0;color:#555">
    {cluster_note}
    <li>The <strong>cluster filter</strong> (<code>--cluster</code>) excluded all PR chunks &mdash;
        try running without <code>--cluster</code> or with a broader filter.</li>
    <li>The <strong>chunk limit</strong> (<code>--num-chunks</code>) was set to 0 or the
        input document contained no recognisable diff sections.</li>
    <li>The input document was parsed but all sections were filtered out as non-normative
        or too short (check <code>pr_chunks_ignored_or_rejected.txt</code> in the run log dir).</li>
    <li>All changes were already fully covered by existing test cases
        (recommendation = <em>none</em> for every chunk).</li>
  </ul>
</div>"""

    # Summary
    failed_cell = (
        f'<strong style="color:#c0392b">{llm_failed}</strong>' if llm_failed else "0"
    )
    cluster_filter_row = (
        f'<tr><td>Cluster filter</td><td><code>{_esc_html(cluster_filter)}</code></td></tr>'
        if cluster_filter else ""
    )
    summ_rows = (
        f"<tr><td>PR / Source</td><td>{_esc_html(pr_url)}</td></tr>"
        f"<tr><td>Generated at</td><td>{_esc_html(generated_at)}</td></tr>"
        f"{cluster_filter_row}"
        f"<tr><td>New TCs (pass 1 — per chunk)</td><td><strong>{len(pass1_missing)}</strong></td></tr>"
        f"<tr><td>New TCs (pass 2 — holistic KG)</td><td><strong>{len(pass2_missing)}</strong></td></tr>"
        f"<tr><td>New TCs (pass 3 — human review outline)</td><td><strong>{len(pass3_missing)}</strong></td></tr>"
        f"<tr><td>New TCs total</td><td><strong>{len(missing_tests)}</strong></td></tr>"
        f"<tr><td>Coverage gap TCs</td><td><strong>{len(coverage_gap_tests or [])}</strong></td></tr>"
        f"<tr><td>Test cases needing updates</td><td><strong>{len(update_candidates)}</strong></td></tr>"
        f"<tr><td>PR chunks with no action needed</td><td>{len(no_action)}</td></tr>"
        f"<tr><td>Chunks analysed / total</td><td>{len(analysis) - llm_failed} / {total_chunks or len(analysis)}</td></tr>"
        f"<tr><td>Chunks failed (LLM error)</td><td>{failed_cell}</td></tr>"
        f"<tr><td>Updated .adoc files</td><td>{len(adoc_paths or [])}</td></tr>"
    )

    # Adoc files list
    adoc_html = ""
    if adoc_paths:
        items = "".join(f"<li><code>{_esc_html(p)}</code></li>" for p in adoc_paths)
        adoc_html = f'<h2>Generated AsciiDoc Files</h2><ul>{items}</ul>'

    # ── Section 1: New test cases (grouped under their triggering change) ──
    new_tc_num = 0
    new_html = ""
    for chunk_key, tcs in new_by_chunk.items():
        r = analysis_by_chunk.get(chunk_key, {})
        cluster = _esc_html(r.get("cluster", ""))
        change_summary = _esc_html(r.get("change_summary", ""))
        change_kind = _esc_html(r.get("change_kind", ""))
        cluster_tag = f'<span class="cluster-tag">{cluster}</span>' if cluster else ""
        entities = r.get("impacted_entities", [])
        entity_tags = "".join(
            f'<span class="cluster-tag" style="background:#8e44ad">{_esc_html(e.get("name",""))}</span>'
            for e in entities[:4]
        )
        new_html += (
            f'<div style="border:1px solid #1e8449;border-radius:4px;margin:16px 0;">'
            f'<div style="background:#1e8449;color:#fff;padding:8px 14px;border-radius:3px 3px 0 0;">'
            f'<strong>PR Change:</strong> <code style="color:#aef5c0">{_esc_html(chunk_key)}</code>'
            f' {cluster_tag} {entity_tags}'
            f'<span style="margin-left:8px;font-size:.85em;opacity:.9">{change_kind}</span></div>'
            f'<div style="padding:8px 14px;background:#f0fdf4;font-size:9pt">{change_summary}</div>'
        )
        for tc in tcs:
            new_tc_num += 1
            title = _esc_html(tc.get("title") or tc.get("text", f"NEW-TC-{new_tc_num}"))
            tc_cluster = _esc_html(tc.get("cluster", ""))
            adoc_section = _esc_html(tc.get("adoc_section", ""))
            tc_cluster_tag = f'<span class="cluster-tag">{tc_cluster}</span>' if tc_cluster else ""
            new_html += (
                f'<div class="card new" style="margin:8px 12px 12px;">'
                f'<div class="ch"><span class="tid">NEW-TC-{new_tc_num}</span>{tc_cluster_tag}</div>'
                f'<div style="font-weight:bold;margin-bottom:6px">{title}</div>'
                f'{"<div class=lbl>AsciiDoc</div><div class=box>" + adoc_section + "</div>" if adoc_section else ""}'
                f'</div>'
            )
        new_html += '</div>'

    if not new_html:
        new_html = "<p><em>No new test cases required.</em></p>"

    # ── Section 2: TC updates (grouped under their triggering change) ──
    upd_html = ""
    for chunk_key, tcs in upd_by_chunk.items():
        r = analysis_by_chunk.get(chunk_key, {})
        cluster = _esc_html(r.get("cluster", ""))
        change_summary = _esc_html(r.get("change_summary", ""))
        change_kind = _esc_html(r.get("change_kind", ""))
        cluster_tag = f'<span class="cluster-tag">{cluster}</span>' if cluster else ""
        upd_html += (
            f'<div style="border:1px solid #e67e22;border-radius:4px;margin:16px 0;">'
            f'<div style="background:#e67e22;color:#fff;padding:8px 14px;border-radius:3px 3px 0 0;">'
            f'<strong>PR Change:</strong> <code style="color:#fff3cd">{_esc_html(chunk_key)}</code>'
            f' {cluster_tag}'
            f'<span style="margin-left:8px;font-size:.85em;opacity:.9">{change_kind}</span></div>'
            f'<div style="padding:8px 14px;background:#fffbf0;font-size:9pt">{change_summary}</div>'
        )
        for tc in tcs:
            tc_id = _esc_html(tc.get("tc_id") or tc.get("text", ""))
            tc_change = _esc_html(tc.get("change_summary", ""))
            adoc_section = _esc_html(tc.get("adoc_section", ""))
            upd_html += (
                f'<div class="card upd" style="margin:8px 12px 12px;">'
                f'<div class="ch"><span class="tid">{tc_id}</span></div>'
                f'{"<div class=lbl>What Changed</div><div class=box>" + tc_change + "</div>" if tc_change else ""}'
                f'{"<div class=lbl>Updated AsciiDoc</div><div class=box>" + adoc_section + "</div>" if adoc_section else ""}'
                f'</div>'
            )
        upd_html += '</div>'

    if not upd_html:
        upd_html = "<p><em>No existing test cases require updates.</em></p>"

    # ── Section 3: No-action changes summary table ──
    no_action_rows = "".join(
        f"<tr><td><code>{_esc_html(r.get('pr_chunk',''))}</code></td>"
        f"<td><span class='cluster-tag'>{_esc_html(r.get('cluster',''))}</span></td>"
        f"<td>{_esc_html(r.get('change_summary',''))}</td>"
        f"<td>{', '.join(r.get('coverage',{}).get('direct_tests',[])[:3]) or '—'}</td></tr>"
        for r in no_action
    )
    no_action_html = (
        '<table class="analysis"><thead><tr>'
        '<th>PR File</th><th>Cluster</th><th>Change Summary</th><th>Covered By</th>'
        f'</tr></thead><tbody>{no_action_rows}</tbody></table>'
    ) if no_action_rows else "<p><em>All changes required action.</em></p>"

    # ── Section 4: Cluster review additions (final report only) ──────────────
    review_section_html = ""
    review_badge = ""
    if review_additions is not None:
        _type_labels = {
            "symmetry_gap":     ("Symmetry Gap",       "#6c3483", "#f5eef8"),
            "missing_test_type":("Missing Test Type",  "#1a5276", "#eaf4fb"),
            "should_be_new_tc": ("Should Be New TC",   "#145a32", "#eafaf1"),
        }
        review_cards = ""
        for item in review_additions:
            rtype  = item.get("review_type", "")
            label, col, bg = _type_labels.get(rtype, ("Review Finding", "#555", "#f9f9f9"))
            title  = _esc_html(item.get("title", ""))
            cluster = _esc_html(item.get("cluster", ""))
            reason = _esc_html(item.get("review_reason", ""))
            action = item.get("action", "new_tc")
            update_tc_id = _esc_html(item.get("update_tc_id", ""))
            steps = item.get("steps", [])

            action_badge = (
                f'<span style="background:#c0392b;color:#fff;padding:1px 6px;'
                f'border-radius:3px;font-size:.78em;margin-left:6px">New TC</span>'
                if action == "new_tc" else
                f'<span style="background:#117a65;color:#fff;padding:1px 6px;'
                f'border-radius:3px;font-size:.78em;margin-left:6px">'
                f'Add to {update_tc_id or "existing TC"}</span>'
            )
            steps_html = ""
            if steps:
                step_items = "".join(
                    f'<li style="margin:3px 0">{_esc_html(str(s))}</li>'
                    for s in steps
                )
                steps_html = (
                    f'<div class=lbl>Steps to Add</div>'
                    f'<ol style="margin:4px 0 0 18px;padding:0;font-size:9pt">'
                    f'{step_items}</ol>'
                )

            review_cards += (
                f'<div class="card" style="border-left:4px solid {col};background:{bg};'
                f'margin:8px 0;padding:10px 14px;">'
                f'<div class="ch">'
                f'<span class="tid" style="background:{col};color:#fff;padding:2px 8px;border-radius:3px">{label}</span>'
                f'<span class="cluster-tag">{cluster}</span>'
                f'{action_badge}'
                f'</div>'
                f'<div style="font-weight:bold;margin:4px 0">{title}</div>'
                f'{"<div class=lbl>Why</div><div class=box>" + reason + "</div>" if reason else ""}'
                f'{steps_html}'
                f'</div>'
            )
        if not review_cards:
            review_cards = "<p><em>Cluster review found no additional gaps beyond pass 1.</em></p>"

        n_review = len(review_additions)
        review_badge = (
            f' &nbsp;<span style="background:#6c3483;color:#fff;border-radius:3px;'
            f'padding:1px 8px;font-size:.8em">+{n_review} from review</span>'
            if n_review else ""
        )
        review_section_html = (
            f'<h2>4. Added by Cluster Review Pass ({n_review})</h2>'
            f'<p>These gaps were <strong>not found by pass 1</strong> (per-chunk analysis) '
            f'but were identified by the cluster-level review LLM looking at all chunks together.</p>'
            f'{review_cards}'
        )

        # Augment summary table with review row
        summ_rows += (
            f"<tr><td>Added by cluster review</td>"
            f"<td><strong style='color:#6c3483'>{n_review}</strong></td></tr>"
        )

    pass1_note = (
        '<p style="background:#fef9e7;border:1px solid #f0b429;border-radius:4px;'
        'padding:8px 14px;font-size:.9em;color:#7d6608">'
        '&#9888; <strong>Pass 1 only</strong> — cluster review pass not yet run. '
        'See <code>test_coverage_final_*.html</code> for the complete report.</p>'
    ) if review_additions is None else ""

    # ── Section 5: Holistic KG analysis TCs (second_pass) ───────────────────
    pass2_section_html = ""
    if pass2_missing:
        outline_links = ""
        if second_pass_outlines:
            items = "".join(
                f'<li><code>{_esc_html(p)}</code> — edit this file and re-run with '
                f'<code>--third-pass-expand {_esc_html(p)}</code></li>'
                for p in second_pass_outlines
            )
            outline_links = (
                f'<div style="background:#fff8e1;border:1px solid #f9a825;border-radius:4px;'
                f'padding:10px 14px;margin:0 0 14px;font-size:.9em">'
                f'<strong>&#9998; Human Review:</strong> Outline JSON files saved — '
                f'edit them and re-run with <code>--third-pass-expand &lt;file&gt;</code> '
                f'to regenerate with your changes.<ul style="margin:6px 0 0">{items}</ul></div>'
            )
        pass2_cards = ""
        for i, tc in enumerate(pass2_missing, 1):
            title = _esc_html(tc.get("title") or f"KG-TC-{i}")
            tc_cluster = _esc_html(tc.get("cluster", ""))
            adoc_section = _esc_html(tc.get("adoc_section", ""))
            tc_cluster_tag = f'<span class="cluster-tag">{tc_cluster}</span>' if tc_cluster else ""
            pass2_cards += (
                f'<div class="card new" style="margin:8px 0 12px;border-left:4px solid #1565c0">'
                f'<div class="ch">'
                f'<span class="tid" style="background:#1565c0">KG-AUTO-{i}</span>'
                f'{tc_cluster_tag}'
                f'<span style="background:#0d47a1;color:#fff;border-radius:3px;padding:1px 7px;'
                f'font-size:.75em;margin-left:6px">Holistic KG</span>'
                f'</div>'
                f'<div style="font-weight:bold;margin-bottom:6px">{title}</div>'
                f'{"<div class=lbl>AsciiDoc</div><div class=box>" + adoc_section + "</div>" if adoc_section else ""}'
                f'</div>'
            )
        n2 = len(pass2_missing)
        pass2_section_html = (
            f'<h2>5. Added by Holistic KG Analysis — Pass 2 ({n2})</h2>'
            f'<p>These test cases were generated by the <strong>second-pass holistic analysis</strong> — '
            f'the LLM looked at all KG REQUIREMENT nodes for thin/gap-heavy clusters '
            f'rather than just the PR chunks. These represent systematic coverage gaps '
            f'independent of the current PR.</p>'
            f'{outline_links}'
            f'{pass2_cards}'
        )
        summ_rows += (
            f"<tr><td>Holistic KG analysis TCs (pass 2)</td>"
            f"<td><strong style='color:#1565c0'>{n2}</strong></td></tr>"
        )

    # ── Section 6: Human review outline TCs (human_review) ──────────────────
    pass3_section_html = ""
    if pass3_missing:
        pass3_cards = ""
        for i, tc in enumerate(pass3_missing, 1):
            title = _esc_html(tc.get("title") or f"HR-TC-{i}")
            tc_cluster = _esc_html(tc.get("cluster", ""))
            adoc_section = _esc_html(tc.get("adoc_section", ""))
            tc_cluster_tag = f'<span class="cluster-tag">{tc_cluster}</span>' if tc_cluster else ""
            pass3_cards += (
                f'<div class="card new" style="margin:8px 0 12px;border-left:4px solid #6a1b9a">'
                f'<div class="ch">'
                f'<span class="tid" style="background:#6a1b9a">HR-TC-{i}</span>'
                f'{tc_cluster_tag}'
                f'<span style="background:#4a148c;color:#fff;border-radius:3px;padding:1px 7px;'
                f'font-size:.75em;margin-left:6px">Human Review</span>'
                f'</div>'
                f'<div style="font-weight:bold;margin-bottom:6px">{title}</div>'
                f'{"<div class=lbl>AsciiDoc</div><div class=box>" + adoc_section + "</div>" if adoc_section else ""}'
                f'</div>'
            )
        n3 = len(pass3_missing)
        pass3_section_html = (
            f'<h2>6. Added by Human Review Outline — Pass 3 ({n3})</h2>'
            f'<p>These test cases were generated from a <strong>human-reviewed outline JSON</strong> '
            f'(passed via <code>--third-pass-expand</code>). A human engineer reviewed and edited '
            f'the outline before the LLM expanded each TC into full AsciiDoc.</p>'
            f'{pass3_cards}'
        )
        summ_rows += (
            f"<tr><td>Human review outline TCs</td>"
            f"<td><strong style='color:#6a1b9a'>{n3}</strong></td></tr>"
        )

    # ── Section 7: Coverage Gap TCs ────────────────────────────────────────
    _gap_tests = coverage_gap_tests or []
    coverage_gap_section_html = ""
    if _gap_tests:
        gap_cards = ""
        for i, tc in enumerate(_gap_tests, 1):
            title = _esc_html(tc.get("title") or f"GAP-TC-{i}")
            tc_cluster = _esc_html(tc.get("cluster", ""))
            adoc_section = _esc_html(tc.get("adoc_section", ""))
            tc_cluster_tag = f'<span class="cluster-tag">{tc_cluster}</span>' if tc_cluster else ""
            gap_cards += (
                f'<div class="card new" style="margin:8px 0 12px;border-left:4px solid #5b2c6f">'
                f'<div class="ch">'
                f'<span class="tid" style="background:#5b2c6f;color:#fff;padding:2px 8px;border-radius:3px">GAP-TC-{i}</span>'
                f'{tc_cluster_tag}'
                f'<span style="background:#4a148c;color:#fff;border-radius:3px;padding:1px 7px;'
                f'font-size:.75em;margin-left:6px">Coverage Gap</span>'
                f'</div>'
                f'<div style="font-weight:bold;margin-bottom:6px">{title}</div>'
                f'{"<div class=lbl>AsciiDoc</div><div class=box>" + adoc_section + "</div>" if adoc_section else ""}'
                f'</div>'
            )
        n_gap = len(_gap_tests)
        coverage_gap_section_html = (
            f'<h2 style="color:#5b2c6f">7. Coverage Gap Test Cases ({n_gap})</h2>'
            f'<p>These test cases cover spec requirements that have <strong>no existing test coverage</strong> '
            f'in the knowledge graph. They are independent of the current PR and address '
            f'systematic gaps in the test plan.</p>'
            f'{gap_cards}'
        )
        summ_rows += (
            f"<tr><td>Coverage gap TCs</td>"
            f"<td><strong style='color:#5b2c6f'>{n_gap}</strong></td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Matter Test Coverage Report</title>
<style>{_REPORT_CSS}</style></head><body>
<h1>Matter Test Coverage Report</h1>
<p style="color:#555;margin-top:-10px">AI-assisted analysis &middot; {_esc_html(generated_at)}</p>
{partial_banner}
{no_chunks_banner}
{pass1_note}
<div class="summ"><h3 style="margin:0 0 8px;color:#1a5276">Executive Summary</h3>
<table>{summ_rows}</table></div>
{adoc_html}
<h2>1. New Test Cases Required — Pass 1: Per-Chunk Analysis ({len(pass1_missing)}){review_badge}</h2>
<p>Each block shows the spec change that triggered the gap, then the suggested new test case(s) with AsciiDoc.</p>
{new_html}
<h2>2. Existing Test Cases Needing Updates ({len(update_candidates)})</h2>
<p>Each block shows the spec change, then which existing TC needs revision and the updated AsciiDoc.</p>
{upd_html}
<h2>3. Changes With No Action Required ({len(no_action)})</h2>
<p>These spec changes are already covered by existing test cases.</p>
{no_action_html}
{review_section_html}
{pass2_section_html}
{pass3_section_html}
{coverage_gap_section_html}
{_build_pass_funnel_html(pass_stats)}
<hr>
<div class="ft">Generated by Matter RAG pipeline &middot; {_esc_html(generated_at)}.
Review all suggestions with a test engineer before acting on them.</div>
</body></html>"""


def _write_pdf_report(html_content: str, out_path: "Path") -> bool:  # type: ignore[name-defined]
    """Attempt to write an HTML string to PDF. Returns True on success."""
    import shutil
    import subprocess

    # Try weasyprint
    try:
        from weasyprint import HTML as _WP  # type: ignore[import]
        _WP(string=html_content).write_pdf(str(out_path))
        return True
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("[_write_pdf_report] weasyprint failed: %s", exc)

    # Try Chrome / Chromium headless
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if chrome:
        import os
        import tempfile
        tmp_html = None
        tmp_profile = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
                f.write(html_content)
                tmp_html = f.name
            tmp_profile = tempfile.mkdtemp(prefix="chrome_pdf_")
            subprocess.run(
                [
                    chrome,
                    "--headless", "--disable-gpu", "--no-sandbox",
                    "--no-first-run", "--disable-extensions",
                    f"--user-data-dir={tmp_profile}",
                    "--run-all-compositor-stages-before-draw",
                    f"--print-to-pdf={out_path}",
                    f"file://{tmp_html}",
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
            return out_path.exists()
        except Exception as exc:
            logger.debug("[_write_pdf_report] Chrome headless failed: %s", exc)
        finally:
            try:
                if tmp_html:
                    os.unlink(tmp_html)
            except Exception as _unlink_exc:
                logger.debug("[_write_pdf_report] Temp file cleanup failed: %s", _unlink_exc)
            if tmp_profile and Path(tmp_profile).exists():
                shutil.rmtree(tmp_profile, ignore_errors=True)

    return False


def _build_analysis_trace_md(
    pr_url: str,
    analysis: List[dict],
    adoc_paths: List[str] = None,
    llm_failed_chunks: int = 0,
    llm_aborted_at: Optional[int] = None,
    llm_total_chunks: int = 0,
) -> str:
    """Build per-chunk debug trace Markdown.

    One section per analysis_results entry showing: file path, cluster, change kind,
    change summary, impacted entities, coverage found, LLM reasoning, action, TC titles.
    """
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_new: List[dict] = []
    all_upd: List[dict] = []
    for r in analysis:
        if "error" not in r:
            all_new.extend(r.get("missing_tests", []))
            all_upd.extend(r.get("update_candidates", []))

    lines: List[str] = [
        "# Matter RAG — Analysis Trace",
        f"\n**Generated**: {ts}",
        f"**Source**: {pr_url}",
        "",
    ]

    # Partial-report warning
    if llm_aborted_at is not None:
        lines += [
            f"> ⚠️ **PARTIAL REPORT** — Pipeline aborted at chunk {llm_aborted_at + 1}/{llm_total_chunks}.",
            f"> Results cover only the first {llm_aborted_at} chunk(s).",
            f"> Re-run with `--compare-only` to complete.\n",
        ]
    elif llm_failed_chunks > 0:
        lines += [
            f"> ⚠️ **PARTIAL REPORT** — {llm_failed_chunks}/{llm_total_chunks} chunks failed (LLM error).",
            f"> Results may be incomplete. Re-run with `--compare-only` to retry.\n",
        ]

    # Summary table
    ok_chunks = (llm_total_chunks or len(analysis)) - llm_failed_chunks
    lines += [
        "## Summary",
        "| Metric | Value |",
        "|---|---|",
        f"| PR chunks analysed | {ok_chunks} / {llm_total_chunks or len(analysis)} |",
        f"| Chunks failed | {llm_failed_chunks} |",
        f"| New test cases suggested | {len(all_new)} |",
        f"| Existing TCs flagged for update | {len(all_upd)} |",
        f"| Updated .adoc files generated | {len(adoc_paths or [])} |",
        "",
    ]

    if adoc_paths:
        lines += ["## Generated AsciiDoc Files", ""]
        for p in adoc_paths:
            lines.append(f"- `{p}`")
        lines.append("")

    lines += ["---", "", "## Per-Chunk Trace", ""]

    for idx, result in enumerate(analysis, 1):
        pr_chunk = result.get("pr_chunk", "unknown")
        lines.append(f"### Chunk {idx}: `{pr_chunk}`")
        lines.append("")

        if "error" in result:
            err = result["error"]
            lines.append(f"**Status**: ❌ Error")
            lines.append(f"> {err}")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue

        cluster      = result.get("cluster", "")
        change_kind  = result.get("change_kind", "")
        change_sum   = result.get("change_summary", "")
        entities     = result.get("impacted_entities", result.get("entities", []))
        coverage     = result.get("coverage", {})
        reasoning    = result.get("reasoning", "")
        rec          = result.get("recommendation", {})
        action       = rec.get("action", "") if isinstance(rec, dict) else ""
        new_tcs      = result.get("missing_tests", [])
        upd_tcs      = result.get("update_candidates", [])

        if cluster:
            lines.append(f"**Cluster**: {cluster}")
        if change_kind:
            lines.append(f"**Change kind**: `{change_kind}`")
        if change_sum:
            lines.append(f"**Change summary**: {change_sum}")
        lines.append("")

        if entities:
            ent_list = entities if isinstance(entities, list) else [str(entities)]
            lines.append(f"**Impacted entities**: {', '.join(str(e) for e in ent_list[:10])}")
            lines.append("")

        if coverage:
            direct   = coverage.get("direct_tests", [])
            indirect = coverage.get("indirect_tests", [])
            missing  = coverage.get("missing", False)
            lines.append("**Existing coverage**:")
            lines.append(f"- Direct tests: {', '.join(f'`{t}`' for t in direct[:5]) or 'none'}")
            lines.append(f"- Indirect tests: {', '.join(f'`{t}`' for t in indirect[:5]) or 'none'}")
            lines.append(f"- Gap detected: `{missing}`")
            lines.append("")

        if reasoning:
            lines.append("**LLM reasoning**:")
            lines.append(f"> {reasoning.strip()}")
            lines.append("")

        if action:
            lines.append(f"**Action taken**: `{action}`")
            if isinstance(rec, dict) and rec.get("details"):
                lines.append(f"> {rec['details']}")
            lines.append("")

        if new_tcs:
            lines.append(f"**New TCs suggested** ({len(new_tcs)}):")
            for tc in new_tcs:
                title = tc.get("title") or tc.get("text", "N/A")
                tc_cluster = tc.get("cluster", cluster)
                lines.append(f"- `{title}`" + (f" [{tc_cluster}]" if tc_cluster else ""))
            lines.append("")

        if upd_tcs:
            lines.append(f"**Existing TCs to update** ({len(upd_tcs)}):")
            for tc in upd_tcs:
                tc_id = tc.get("tc_id") or tc.get("text", "N/A")
                summ  = tc.get("change_summary", "")
                lines.append(f"- `{tc_id}`" + (f" — {summ}" if summ else ""))
            lines.append("")

        # Fallback: no structured fields extracted — show raw LLM text
        if not change_sum and not reasoning and not new_tcs and not upd_tcs:
            raw = result.get("llm_response", "")
            if raw:
                lines.append("**Raw LLM response**:")
                lines.append(f"```\n{raw[:800]}\n```")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GraphBundle → MatterKGBuilder bridge
# ---------------------------------------------------------------------------

def _import_graph_bundle(kg, bundle) -> None:
    """Import a typed GraphBundle (from new KB pipeline) into MatterKGBuilder._graph.

    Maps new ``GraphNodeType`` / ``GraphEdgeType`` (SCREAMING_SNAKE values) to the
    old ``NodeType`` enum (matched by ``.name`` attribute — also SCREAMING_SNAKE).
    """
    from src.knowledge_graph.base_graph import GraphNode, NodeType

    _name_to_old_ntype = {nt.name: nt for nt in NodeType}

    imported_nodes = 0
    for node_rec in bundle.nodes:
        ntype_name = node_rec.node_type.name   # "CLUSTER", "TEST_CASE", etc.
        old_ntype = _name_to_old_ntype.get(ntype_name, NodeType.SECTION)
        gn = GraphNode(
            node_id=node_rec.node_id,
            node_type=old_ntype,
            label=node_rec.label,
            properties=node_rec.properties,
        )
        kg._graph.add_node(node_rec.node_id, obj=gn)
        imported_nodes += 1

    # Edge type priority for conflict resolution: higher = more specific / preferred.
    # When GraphBundle contains duplicate (source, target) with different edge_types
    # (e.g. 'tests' from _add_test_case_layer AND 'in_context' from infer_graph_edges
    # for CLUSTER:: refs that aren't in entity_lookup), the more specific edge wins.
    _EDGE_PRIORITY = {
        "tests": 10,
        "tests_command": 9,
        "verifies_attribute": 8,
        "reads": 8,
        "writes": 8,
        "observes_event": 7,
        "verifies_requirement": 6,
        "verifies_rule": 5,
        "in_context": 1,
    }

    imported_edges = 0
    _dropped = 0
    for edge_rec in bundle.edges:
        edge_type_str = (
            edge_rec.edge_type.value
            if hasattr(edge_rec.edge_type, "value")
            else str(edge_rec.edge_type)
        )
        if kg._graph.has_node(edge_rec.source) and kg._graph.has_node(edge_rec.target):
            existing = kg._graph.get_edge_data(edge_rec.source, edge_rec.target)
            if existing is not None:
                existing_type = existing.get("edge_type", "")
                if _EDGE_PRIORITY.get(edge_type_str, 0) <= _EDGE_PRIORITY.get(existing_type, 0):
                    continue  # don't overwrite a more specific edge
            kg._graph.add_edge(
                edge_rec.source, edge_rec.target,
                edge_type=edge_type_str,
                **{k: v for k, v in edge_rec.properties.items() if k != "edge_type"},
            )
            imported_edges += 1
        else:
            _dropped += 1

    if _dropped:
        logger.warning("[_import_graph_bundle] Dropped %d edges with missing endpoints", _dropped)

    logger.info(
        "[_import_graph_bundle] Imported %d nodes, %d edges into MatterKGBuilder",
        imported_nodes, imported_edges,
    )


# ---------------------------------------------------------------------------
# Cleanup node — always the last node in both pipeline paths
# ---------------------------------------------------------------------------

@log_node
def cleanup_node(state: PipelineState) -> PipelineState:
    """Final node: release GPU/process resources and summarise run.

    Runs on BOTH terminal paths:
      - Full pipeline: after ``generate_report_node``
      - Build-only:    after ``build_knowledge_graph_node`` (no PR chunks)

    What it does:
      1. Releases MPS/CUDA tensor memory if torch is loaded — prevents GPU
         OOM on repeated CI runs where the process stays alive between invocations.
      2. Runs Python garbage collection to reclaim embedder model memory.
      3. Logs a one-line pipeline summary for easy CI triage.

    Note on LLM context: every LLM call in this pipeline is stateless — a fresh
    message list is constructed per call and the subprocess exits immediately
    (ClaudeSubprocessProvider) or returns a single-turn response (ClaudeProvider /
    OllamaProvider).  There is NO accumulated conversation history to clear.
    """
    import gc

    # ── GPU memory release ───────────────────────────────────────────────
    try:
        import torch  # type: ignore
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
            logger.debug("[cleanup_node] MPS cache cleared")
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.debug("[cleanup_node] CUDA cache cleared")
    except Exception as _torch_exc:
        logger.debug("[cleanup_node] GPU cache clear skipped: %s", _torch_exc)

    gc.collect()

    # ── Run summary ──────────────────────────────────────────────────────
    errors = state.get("errors", [])
    fatal = state.get("fatal_error", False)
    report = state.get("report_path", "—")
    missing = len(state.get("missing_tests", []))
    updates = len(state.get("update_candidates", []))
    pr_chunks = len(state.get("pr_chunks", []))
    kg = state.get("knowledge_graph")
    kg_nodes = kg.num_nodes if kg else 0
    kg_edges = kg.num_edges if kg else 0

    logger.info(
        "[cleanup_node] Pipeline complete — "
        "fatal=%s  errors=%d  pr_chunks=%d  kg=%d/%d  "
        "missing_TCs=%d  update_TCs=%d  report=%s",
        fatal, len(errors), pr_chunks, kg_nodes, kg_edges,
        missing, updates, report,
    )
    if errors:
        for err in errors[:5]:
            logger.warning("[cleanup_node] Error recorded during run: %s", err)

    return state  # pass through unchanged