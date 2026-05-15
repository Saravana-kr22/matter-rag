"""LangGraph nodes for the test plan coverage gap analysis pipeline.

Loads the knowledge graph from disk, identifies REQUIREMENT / BEHAVIOR_RULE nodes
that have no TEST_CASE coverage edge, groups them by cluster, sends each cluster to
the LLM for confirmation, and generates an HTML + JSON report.

State flow:
    load_coverage_stores_node
        → build_cluster_coverage_map_node
            → run_llm_coverage_analysis_node
                → aggregate_coverage_findings_node
                    → generate_coverage_report_node → END
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict

from src.config.models import AppConfig
from src.engine.run_context import RunContext
from src.knowledge_graph.base_graph import NodeType, GraphNode
from src.knowledge_graph.graph_factory import create_knowledge_graph
from src.llm.llm_provider import get_llm
from src.logging_config import log_node, PipelineFatalError

logger = logging.getLogger(__name__)

# Edge types that count as "TC covers this requirement"
_TC_COVER_EDGE_TYPES = frozenset({
    "covers", "tests", "validates", "implements",
    "verifies_requirement", "tests_command", "verifies_attribute",
    "observes_event", "verifies_rule",
})

def _req_has_tc_coverage(kg, req: "GraphNode", cluster_name: str) -> bool:
    """Return True if any TC in the same cluster covers this requirement.

    Two checks:
      1. Direct edge: TC --covers/validates/...--> REQ
      2. Two-hop entity path: REQ --implements--> ATTRIBUTE/CMD/EVENT
         <--verifies_attribute/tests_command/...--> TC (same cluster)

    The two-hop check handles field-level requirements (e.g. a requirement about
    the SimultaneousInvocationsSupported field of CapabilityMinima) where the KG
    has no direct TC→REQ edge but TC-BINFO-2.1 does have a verifies_attribute
    edge to the CapabilityMinima attribute node that the requirement also links to.
    """
    # --- Check 1: direct TC→REQ edge ---
    for src, _, edata in kg._graph.in_edges(req.node_id, data=True):
        et = edata.get("edge_type")
        et_val = et.value if hasattr(et, "value") else str(et)
        if et_val in _TC_COVER_EDGE_TYPES:
            src_obj = kg._graph.nodes.get(src, {}).get("obj")
            if src_obj and src_obj.node_type == NodeType.TEST_CASE:
                return True

    # --- Check 2: 2-hop via entity node ---
    # Follow REQ --implements/references--> entity
    for _, tgt, edata in kg._graph.out_edges(req.node_id, data=True):
        et = edata.get("edge_type")
        et_val = et.value if hasattr(et, "value") else str(et)
        if et_val not in {"implements", "references"}:
            continue
        # Skip cluster-node fallback targets — too coarse (every TC tests the cluster)
        tgt_obj = kg._graph.nodes.get(tgt, {}).get("obj")
        if tgt_obj is None or tgt_obj.node_type == NodeType.CLUSTER:
            continue
        # Check: does any TC in the same cluster have a coverage edge to this entity?
        for src, _, edata2 in kg._graph.in_edges(tgt, data=True):
            et2 = edata2.get("edge_type")
            et2_val = et2.value if hasattr(et2, "value") else str(et2)
            if et2_val not in _TC_COVER_EDGE_TYPES:
                continue
            src_obj = kg._graph.nodes.get(src, {}).get("obj")
            if src_obj and src_obj.node_type == NodeType.TEST_CASE:
                tc_cluster = (src_obj.properties.get("cluster") or "").strip()
                if tc_cluster.lower().replace(" cluster", "").strip() == cluster_name.lower().replace(" cluster", "").strip():
                    return True

    return False


def _find_covering_tcs(kg, req: "GraphNode", cluster_name: str, max_tcs: int = 3) -> List[str]:
    """Return TC-IDs that cover this requirement (up to max_tcs).

    Uses the same two-check logic as _req_has_tc_coverage but collects IDs
    instead of returning a boolean.
    """
    result: List[str] = []
    seen: set = set()

    # --- Check 1: direct TC->REQ edge ---
    for src, _, edata in kg._graph.in_edges(req.node_id, data=True):
        et = edata.get("edge_type")
        et_val = et.value if hasattr(et, "value") else str(et)
        if et_val in _TC_COVER_EDGE_TYPES:
            src_obj = kg._graph.nodes.get(src, {}).get("obj")
            if src_obj and src_obj.node_type == NodeType.TEST_CASE:
                tc_id = src_obj.properties.get("tc_id") or src_obj.label
                if tc_id not in seen:
                    seen.add(tc_id)
                    result.append(tc_id)
                    if len(result) >= max_tcs:
                        return result

    # --- Check 2: 2-hop via entity node ---
    for _, tgt, edata in kg._graph.out_edges(req.node_id, data=True):
        et = edata.get("edge_type")
        et_val = et.value if hasattr(et, "value") else str(et)
        if et_val not in {"implements", "references"}:
            continue
        tgt_obj = kg._graph.nodes.get(tgt, {}).get("obj")
        if tgt_obj is None or tgt_obj.node_type == NodeType.CLUSTER:
            continue
        for src, _, edata2 in kg._graph.in_edges(tgt, data=True):
            et2 = edata2.get("edge_type")
            et2_val = et2.value if hasattr(et2, "value") else str(et2)
            if et2_val not in _TC_COVER_EDGE_TYPES:
                continue
            src_obj = kg._graph.nodes.get(src, {}).get("obj")
            if src_obj and src_obj.node_type == NodeType.TEST_CASE:
                tc_cluster = (src_obj.properties.get("cluster") or "").strip()
                if tc_cluster.lower().replace(" cluster", "").strip() == cluster_name.lower().replace(" cluster", "").strip():
                    tc_id = src_obj.properties.get("tc_id") or src_obj.label
                    if tc_id not in seen:
                        seen.add(tc_id)
                        result.append(tc_id)
                        if len(result) >= max_tcs:
                            return result

    return result


# Regex for sentence-boundary periods.
#
# A period is a sentence boundary when:
#   - Followed by whitespace + uppercase letter, OR end of string/line
#   - NOT part of a section-number chain like "11.5.6." (digit.digit pattern before)
#   - NOT a decimal number continuation like "3.14" (digit after)
#
# Fixed-width negative lookbehinds check for digit.digit and digit.dd and digit.ddd
# immediately before the candidate period, which covers chains of any length:
#   "11.5.6." → at the final "." the 3 chars behind are "5.6" → matches (?<!\d\.\d\d) → skip
#   "set to 0." → 3 chars behind are "t 0" → no match → sentence boundary
#
_SENTENCE_END_RE = re.compile(
    r"(?<!\d\.\d)"       # not preceded by digit.digit  (e.g. 11.5 in 11.5.6)
    r"(?<!\d\.\d\d)"     # not preceded by digit.dd     (e.g. 5.67)
    r"(?<!\d\.\d\d\d)"   # not preceded by digit.ddd    (e.g. 5.678)
    r"\."                # the period itself
    r"(?!\d)"            # not followed by a digit       (rules out decimals and x.y chains)
    r"(?:\s+(?=[A-Z\n])|\s*$)",  # sentence boundary: space+uppercase, newline, or EOS
    re.MULTILINE,
)


def _truncate_to_sentence(text: str, max_chars: int = 800) -> str:
    """Return text up to the end of its first complete sentence, capped at max_chars.

    Avoids cutting at section-number periods (e.g. '11.5.6.') or decimal
    numbers (e.g. '3.14').  A sentence ends at a period that is:
      - followed by whitespace + uppercase letter, or by end of string
      - NOT preceded by the pattern digit.digit (multi-part section refs)
      - NOT followed by a digit (single-pair decimal or section ref)

    If no sentence boundary is found within max_chars, the text is hard-capped
    with a trailing '…' so the display never runs away.
    """
    text = text.strip()
    if not text:
        return text
    for m in _SENTENCE_END_RE.finditer(text):
        end = m.end()
        if end <= max_chars:
            return text[:end].rstrip()
        # First boundary found but it's beyond max_chars — fall through to hard cap
        break
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class CoverageAnalysisState(TypedDict, total=False):
    config: "AppConfig"
    run_ctx: "RunContext"
    run_dir: str
    output_dir: str
    cluster_filter: str           # "" = all clusters
    max_llm_calls: int

    knowledge_graph: Any          # MatterKGBuilder loaded from disk
    coverage_map: Dict[str, Any]  # per-cluster {requirements, test_cases, uncovered_reqs, covered_reqs}

    cluster_findings: List[Dict]  # [{cluster, confirmed_gaps, num_requirements, num_tcs}]
    total_gaps: int

    report_path: str
    errors: List[str]
    fatal_error: bool


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@log_node
def load_coverage_stores_node(state: CoverageAnalysisState) -> CoverageAnalysisState:
    """Load the knowledge graph from disk."""
    config = state["config"]
    graph_path = config.knowledge_graph.graph_store_path

    if not Path(graph_path).is_file():
        raise PipelineFatalError(
            f"Knowledge graph not found: {graph_path}. "
            "Run 'python scripts/run_ghpr_analysis.py --build-knowledge-graph' first."
        )

    kg = create_knowledge_graph(config.knowledge_graph)
    kg.load_from_json(graph_path)
    logger.info(
        "[load_coverage_stores_node] KG loaded: %d nodes, %d edges",
        kg.num_nodes, kg.num_edges,
    )
    return {"knowledge_graph": kg}


@log_node
def build_cluster_coverage_map_node(state: CoverageAnalysisState) -> CoverageAnalysisState:
    """Build per-cluster coverage maps: which REQs have TC coverage and which don't."""
    kg = state["knowledge_graph"]
    cluster_filter = (state.get("cluster_filter") or "").lower()

    # NOTE: DM XML hierarchy="base" means "standalone primary cluster" (e.g. Door Lock,
    # Access Control) — NOT abstract/template.  hierarchy="derived" means "derives from a
    # base cluster" (e.g. RVC Operational State).  We intentionally do NOT filter on
    # hierarchy here so all clusters with REQUIREMENT nodes are analysed.

    tc_by_cluster: Dict[str, List[GraphNode]] = {}
    req_by_cluster: Dict[str, List[GraphNode]] = {}

    for _nid, data in kg._graph.nodes(data=True):
        obj: Optional[GraphNode] = data.get("obj")
        if obj is None:
            continue
        cluster = (obj.properties.get("cluster") or "").strip()
        if not cluster:
            continue
        if cluster_filter and cluster_filter not in cluster.lower():
            continue

        if obj.node_type == NodeType.TEST_CASE:
            tc_by_cluster.setdefault(cluster, []).append(obj)
        elif obj.node_type in {NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE}:
            req_by_cluster.setdefault(cluster, []).append(obj)

    # For each cluster with requirements, find covered vs uncovered
    coverage_map: Dict[str, Dict] = {}
    for cluster_name in sorted(req_by_cluster.keys()):
        reqs = req_by_cluster[cluster_name]
        tcs = tc_by_cluster.get(cluster_name, [])

        uncovered: List[GraphNode] = []
        covered: List[GraphNode] = []
        covered_by: Dict[str, List[str]] = {}  # req_node_id -> [tc_ids]

        for req in reqs:
            has_tc_coverage = _req_has_tc_coverage(kg, req, cluster_name)
            if has_tc_coverage:
                covered.append(req)
                # Find covering TCs (cap at 3 for prompt size)
                covering_tcs = _find_covering_tcs(kg, req, cluster_name, max_tcs=3)
                covered_by[req.node_id] = covering_tcs
            else:
                uncovered.append(req)

        # Deduplicate uncovered reqs by normalized normative text to avoid sending
        # near-identical requirements (e.g. spec text repeated verbatim in multiple
        # sub-sections) as separate LLM calls.
        seen_norm: set = set()
        deduped_uncovered: List[GraphNode] = []
        for req in uncovered:
            norm_key = " ".join(
                (req.properties.get("normative_text") or req.label or "").lower().split()
            )[:160]
            if norm_key not in seen_norm:
                seen_norm.add(norm_key)
                deduped_uncovered.append(req)
        if len(deduped_uncovered) < len(uncovered):
            logger.debug(
                "[build_cluster_coverage_map_node] %s: dropped %d duplicate reqs "
                "(same normative text), %d unique remain",
                cluster_name, len(uncovered) - len(deduped_uncovered), len(deduped_uncovered),
            )
        uncovered = deduped_uncovered

        coverage_map[cluster_name] = {
            "requirements": reqs,
            "test_cases": tcs,
            "covered_reqs": covered,
            "uncovered_reqs": uncovered,
            "covered_by": covered_by,
        }

    total_reqs = sum(len(v["requirements"]) for v in coverage_map.values())
    total_uncovered = sum(len(v["uncovered_reqs"]) for v in coverage_map.values())
    logger.info(
        "[build_cluster_coverage_map_node] %d clusters, %d reqs, %d uncovered (pre-LLM)",
        len(coverage_map), total_reqs, total_uncovered,
    )
    return {"coverage_map": coverage_map}


_TC_ENTITY_EDGE_TYPES = frozenset({
    "reads", "writes", "verifies_attribute", "tests_command",
    "observes_event", "validates_range", "validates_type",
    "validates_default", "validates_access", "validates_conformance",
    "validates_enum", "negative_tests",
})


def _build_compact_tc_summaries(tcs: List[GraphNode], kg=None) -> str:
    """Build compact TC summaries using TC-ID + Purpose + tested entities.

    When ``kg`` is provided, entity names are extracted from KG edges
    (reads, verifies_attribute, tests_command, etc.) which are far richer
    than the ``entity_refs`` property (often empty for protocol TCs).
    """
    lines: List[str] = []
    for tc in tcs:
        tc_id = tc.properties.get("tc_id") or tc.label
        purpose = tc.properties.get("purpose", "") or ""
        if not purpose:
            purpose = (tc.properties.get("content", "") or "")[:150]

        entity_names: List[str] = []
        if kg is not None:
            seen: set = set()
            for _, tgt, edata in kg._graph.out_edges(tc.node_id, data=True):
                et = edata.get("edge_type")
                et_val = et.value if hasattr(et, "value") else str(et)
                if et_val not in _TC_ENTITY_EDGE_TYPES:
                    continue
                tgt_obj = kg._graph.nodes.get(tgt, {}).get("obj")
                if tgt_obj is None:
                    continue
                name = tgt_obj.properties.get("name", "") or tgt_obj.label
                if name and name not in seen:
                    seen.add(name)
                    entity_names.append(name)

        if not entity_names:
            for ref in (tc.properties.get("entity_refs", []) or []):
                parts = ref.split("::")
                if len(parts) >= 3:
                    entity_names.append(parts[2])
                elif len(parts) == 2:
                    entity_names.append(parts[1])

        intents = tc.properties.get("intents", []) or []

        parts_list = [f"{tc_id}: {purpose[:200]}"]
        if entity_names:
            parts_list.append(f"  entities tested: {', '.join(entity_names[:20])}")
        if intents:
            parts_list.append(f"  intents: {', '.join(intents[:8])}")
        lines.append("\n".join(parts_list))
    return "\n".join(lines)


# Maximum estimated prompt size (chars) before we paginate requirements.
_MAX_CLASSIFICATION_PROMPT = 60000


def _estimate_classification_pages(
    uncovered_reqs: List[GraphNode],
    tc_summary_chars: int,
) -> List[List[GraphNode]]:
    """Split requirements into pages so each classification prompt fits under the char limit."""
    per_req_chars = 250  # estimated chars per requirement line
    template_overhead = 3000  # chars for the prompt template text
    available_for_reqs = _MAX_CLASSIFICATION_PROMPT - tc_summary_chars - template_overhead
    reqs_per_page = max(10, available_for_reqs // per_req_chars)
    pages = [
        uncovered_reqs[i : i + reqs_per_page]
        for i in range(0, len(uncovered_reqs), reqs_per_page)
    ]
    return pages


def _build_req_lines_for_classification(
    reqs: List[GraphNode],
    covered_by: Optional[Dict[str, List[str]]] = None,
) -> str:
    """Format requirements compactly for the Phase 1 classification prompt.

    When covered_by is provided, includes KG coverage context showing which
    TCs the KG thinks cover each requirement. This helps the LLM determine
    whether the coverage is full or only partial.
    """
    if covered_by is None:
        covered_by = {}
    lines: List[str] = []
    for req in reqs:
        normative = req.properties.get("normative_text") or req.label or ""
        req_type = req.properties.get("requirement_type", "")
        section_path = (req.properties.get("section_path") or "")[:200]
        covering = covered_by.get(req.node_id, [])
        if covering:
            tc_info = ", ".join(covering)
            kg_line = f"\n  KG coverage: {tc_info}"
        else:
            kg_line = "\n  KG coverage: (none)"
        lines.append(
            f"REQ: {req.node_id}\n"
            f"  type: {req_type}\n"
            f"  section: {section_path!r}\n"
            f"  normative_text: {_truncate_to_sentence(normative, max_chars=800)!r}"
            f"{kg_line}"
        )
    return "\n\n".join(lines)


def _run_classification_call(
    llm,
    cluster_name: str,
    req_block: str,
    tc_summary_block: str,
    spec_reference_section: str,
) -> Dict:
    """Phase 1: Classify each requirement as uncovered / covered / partial."""
    prompt = (
        "You are a Matter specification expert reviewing test coverage gaps.\n\n"
        f"{spec_reference_section}"
        f"=== CLUSTER: {cluster_name} ===\n\n"
        f"EXISTING TEST CASES (compact summaries):\n{tc_summary_block}\n\n"
        f"REQUIREMENTS TO CLASSIFY:\n{req_block}\n\n"
        "TASK — COVERAGE CLASSIFICATION:\n"
        "For each requirement above, classify it as one of:\n"
        '  - "uncovered" — no TC tests this requirement\'s normative behavior\n'
        '  - "covered" — an existing TC adequately tests this requirement\n'
        '  - "partial" — a TC references this requirement but doesn\'t fully exercise '
        "the normative behavior (e.g., reads the attribute but doesn't test reboot-reset)\n\n"
        "IMPORTANT — ENTITY CROSS-CHECK BEFORE CLASSIFYING AS UNCOVERED:\n"
        "Before classifying a requirement as 'uncovered', extract the entity name from "
        "its 'section' field (e.g., 'OverrunCount' from '...OverrunCount Attribute'). "
        "Then check if ANY TC above lists that entity in its 'entities tested' line. "
        "If a TC tests that entity, classify as 'partial' (not 'uncovered') and set "
        "covering_tc to that TC. Only classify as 'uncovered' if NO TC mentions the entity.\n\n"
        "For requirements with KG coverage listed (i.e. KG coverage is not '(none)'), "
        "determine if the listed TCs FULLY test the normative behavior described. "
        "Classify as 'covered' only if the TC purpose confirms it tests this specific "
        "behavior. Classify as 'partial' if the TC references the entity but does not "
        "test the specific normative behavior (e.g., reads an attribute but doesn't "
        "test reboot-reset). For requirements with KG coverage '(none)', rely on your "
        "knowledge of the existing test cases listed above.\n\n"
        "For each requirement also check the 'section' field. If the section path clearly "
        "belongs to a different area of the spec (e.g. PKI/certificate formats, commissioning, "
        "device attestation, or a different cluster entirely) and NOT to "
        f"'{cluster_name}', classify as \"covered\" with covering_tc=\"MISATTRIBUTED\".\n\n"
        "Return ONLY valid JSON (no markdown fences, no explanation outside the JSON):\n"
        '{"classifications": [\n'
        '  {"req_id": "<node_id>", "status": "partial", "covering_tc": "TC-XX-1.1", '
        '"gap": "reads attribute but does not test reboot-reset behavior"},\n'
        '  {"req_id": "<node_id>", "status": "uncovered", "covering_tc": null, '
        '"gap": "no TC triggers AssociationFailure events"},\n'
        '  {"req_id": "<node_id>", "status": "covered", "covering_tc": "TC-XX-2.3", '
        '"gap": null}\n'
        "]}\n\n"
        "Include ALL requirements from the list above — one entry per requirement."
    )
    if hasattr(llm, "set_next_label"):
        llm.set_next_label(f"Coverage classify — {cluster_name}")
    response = llm.complete(prompt)
    return _parse_llm_json(response, context=f"classify-{cluster_name}")


def _run_gap_description_call(
    llm,
    cluster_name: str,
    gap_reqs: List[Dict],
    tc_summary_block: str,
    sections_block: str,
    spec_reference_section: str,
    kg,
) -> List[Dict]:
    """Phase 2: Generate gap descriptions for confirmed uncovered/partial requirements."""
    req_lines: List[str] = []
    for item in gap_reqs:
        req_id = item["req_id"]
        status = item["status"]
        covering_tc = item.get("covering_tc") or ""
        phase1_gap = item.get("gap") or ""
        # Look up the original normative text from the node
        node_data = kg._graph.nodes.get(req_id, {}).get("obj")
        normative = ""
        section_path = ""
        req_type = ""
        if node_data:
            normative = node_data.properties.get("normative_text") or node_data.label or ""
            section_path = (node_data.properties.get("section_path") or "")[:200]
            req_type = node_data.properties.get("requirement_type", "")
        entry = (
            f"REQ: {req_id}\n"
            f"  type: {req_type}\n"
            f"  section: {section_path!r}\n"
            f"  normative_text: {_truncate_to_sentence(normative, max_chars=800)!r}\n"
            f"  classification: {status}"
        )
        if covering_tc:
            entry += f"\n  partial_tc: {covering_tc}"
        if phase1_gap:
            entry += f"\n  phase1_gap_note: {phase1_gap!r}"
        req_lines.append(entry)
    req_block = "\n\n".join(req_lines)

    prompt = (
        "You are a Matter specification expert generating actionable test gap descriptions.\n\n"
        f"{spec_reference_section}"
        f"=== CLUSTER: {cluster_name} ===\n\n"
        f"SPEC SECTIONS (full text for requirements below — use for context):\n{sections_block}\n\n"
        f"EXISTING TEST CASES (compact summaries):\n{tc_summary_block}\n\n"
        f"CONFIRMED UNCOVERED / PARTIAL REQUIREMENTS:\n{req_block}\n\n"
        "TASK — GAP DESCRIPTION GENERATION:\n"
        "For each requirement above, generate a concise (1-2 sentences) description of "
        "what test is needed.\n\n"
        "For 'partial' requirements: describe what the existing TC is missing and what "
        "additional test step or scenario is needed. Be specific and actionable "
        "(e.g., 'TC-DGWIFI-2.1 reads OverrunCount but does not exercise the reboot-reset "
        "behavior. Add a reboot test step.').\n\n"
        "For 'uncovered' requirements: describe the test type or scenario that is missing.\n\n"
        "Return ONLY valid JSON (no markdown fences, no explanation outside the JSON):\n"
        '{"confirmed_gaps": [\n'
        '  {"req_id": "<node_id>", '
        '"normative_text": "<copy verbatim from normative_text field above>", '
        '"requirement_type": "<type>", '
        '"section_path": "<section breadcrumb from the req>", '
        '"truly_uncovered": true or false, '
        '"covers_tc": "<TC-ID or null>", '
        '"missing_test_description": "<1-2 actionable sentences>"}]}\n\n'
        "Set truly_uncovered=true for fully uncovered requirements, false for partial.\n"
        "For 'partial' requirements, set covers_tc to the TC that partially covers it."
    )
    if hasattr(llm, "set_next_label"):
        llm.set_next_label(f"Coverage gaps — {cluster_name}")
    response = llm.complete(prompt)
    parsed = _parse_llm_json(response, context=f"gaps-{cluster_name}")
    return parsed.get("confirmed_gaps", [])


@log_node
def run_llm_coverage_analysis_node(state: CoverageAnalysisState) -> CoverageAnalysisState:
    """Two-phase coverage analysis: classify requirements, then generate gap descriptions.

    Phase 1 — Classification: Uses compact TC summaries (~200 chars/TC) and asks the
    LLM to classify each requirement as uncovered / covered / partial.  Large clusters
    are paginated so each prompt stays under ~60k chars.

    Phase 2 — Gap Description: Only for confirmed uncovered and partial requirements,
    generates actionable 1-2 sentence descriptions of what test is needed.
    """
    config = state["config"]
    coverage_map: Dict[str, Dict] = state.get("coverage_map", {})
    _raw = state.get("max_llm_calls")
    if _raw is None:
        _raw = getattr(config.analysis, "max_llm_calls_per_run", 0)
    max_calls = _raw if _raw > 0 else 999_999
    errors: List[str] = list(state.get("errors") or [])

    if not coverage_map:
        logger.warning("[run_llm_coverage_analysis_node] No coverage data to analyze.")
        return {"cluster_findings": [], "errors": errors}

    llm = get_llm(config.llm, log_dir=state.get("run_dir"))
    findings: List[Dict] = []
    calls_made = 0

    # Build once — Matter core spec reference (conformance / access / qualities)
    kg = state["knowledge_graph"]
    spec_reference_block = _build_spec_reference_block(kg)
    spec_reference_section = (
        f"=== MATTER SPEC REFERENCE (sections 7.3 Conformance / 7.6 Access / 7.7 Other Qualities) ===\n"
        f"{spec_reference_block}\n\n"
        if spec_reference_block
        else ""
    )
    if spec_reference_block:
        logger.info(
            "[run_llm_coverage_analysis_node] spec_reference_block loaded (%d chars)",
            len(spec_reference_block),
        )
    else:
        logger.warning(
            "[run_llm_coverage_analysis_node] spec_reference_block empty — "
            "sections 7.3/7.6/7.7 not found in KG (rebuild KG with spec docs to enable)"
        )

    # Pre-compute how many LLM calls will be needed (accounting for pagination).
    clusters_needing_llm: List[tuple] = []
    clusters_fully_covered: List[str] = []
    total_llm_needed = 0

    for name, cmap in coverage_map.items():
        all_reqs = cmap.get("covered_reqs", []) + cmap.get("uncovered_reqs", [])
        if not all_reqs:
            clusters_fully_covered.append(name)
            continue
        tcs = cmap.get("test_cases", [])
        tc_summary_block = _build_compact_tc_summaries(tcs, kg=kg)
        pages = _estimate_classification_pages(all_reqs, len(tc_summary_block))
        # Each cluster needs: len(pages) classification calls + 1 gap description call
        cluster_calls = len(pages) + 1
        clusters_needing_llm.append((name, cmap, cluster_calls))
        total_llm_needed += cluster_calls

    total_llm_needed = min(total_llm_needed, max_calls)
    print(
        f"[coverage_analysis] {len(coverage_map)} clusters total — "
        f"{len(clusters_fully_covered)} with no requirements (no LLM call), "
        f"{len(clusters_needing_llm)} have requirements to classify — "
        f"will make up to {total_llm_needed} LLM calls (max_llm_calls={max_calls})",
        flush=True,
    )

    # Record clusters with no requirements without LLM calls
    for cluster_name in clusters_fully_covered:
        cmap = coverage_map[cluster_name]
        findings.append({
            "cluster": cluster_name,
            "confirmed_gaps": [],
            "num_requirements": len(cmap.get("requirements", [])),
            "num_tcs": len(cmap.get("test_cases", [])),
        })

    for cluster_name, cmap, _est_calls in clusters_needing_llm:
        covered_reqs: List[GraphNode] = cmap.get("covered_reqs", [])
        uncovered_reqs: List[GraphNode] = cmap.get("uncovered_reqs", [])
        covered_by: Dict[str, List[str]] = cmap.get("covered_by", {})
        all_reqs: List[GraphNode] = covered_reqs + uncovered_reqs
        tcs: List[GraphNode] = cmap.get("test_cases", [])

        if calls_made >= max_calls:
            logger.info(
                "[run_llm_coverage_analysis_node] max_llm_calls=%d reached — stopping.",
                max_calls,
            )
            print(
                f"[coverage_analysis] max_llm_calls={max_calls} reached — stopping early.",
                flush=True,
            )
            break

        # Build compact TC summaries (shared across all pages and both phases)
        tc_summary_block = _build_compact_tc_summaries(tcs, kg=kg)

        # Collect unique section full-texts via BELONGS_TO edges (req -> section)
        # Used only in Phase 2 for gap description context
        seen_section_ids: set = set()
        section_texts: List[str] = []
        for req in all_reqs[:50]:
            for _, tgt, edata in kg._graph.out_edges(req.node_id, data=True):
                et = edata.get("edge_type")
                et_val = et.value if hasattr(et, "value") else str(et)
                if et_val == "belongs_to" and tgt not in seen_section_ids:
                    seen_section_ids.add(tgt)
                    sec_obj = kg._graph.nodes.get(tgt, {}).get("obj")
                    if sec_obj:
                        full_text = (sec_obj.properties.get("full_text") or "")[:5000]
                        sec_path = (
                            sec_obj.properties.get("section_path")
                            or sec_obj.label
                            or tgt
                        )
                        if full_text:
                            section_texts.append(f"--- {sec_path} ---\n{full_text}")
                    if len(section_texts) >= 8:
                        break
            if len(section_texts) >= 8:
                break
        sections_block = (
            "\n\n".join(section_texts)
            if section_texts
            else "(no section text available)"
        )

        # ---- Phase 1: Classification with compact TC summaries ----
        # Send ALL requirements (covered + uncovered) with KG coverage context
        pages = _estimate_classification_pages(all_reqs, len(tc_summary_block))
        total_pages = len(pages)
        all_classifications: List[Dict] = []

        for page_idx, page_reqs in enumerate(pages):
            if calls_made >= max_calls:
                logger.info(
                    "[run_llm_coverage_analysis_node] max_llm_calls=%d reached mid-classification.",
                    max_calls,
                )
                break

            page_num = page_idx + 1
            remaining = total_llm_needed - (calls_made + 1)
            print(
                f"[coverage_analysis] LLM call {calls_made + 1}/{total_llm_needed}"
                f" — {remaining} remaining: cluster={cluster_name!r}"
                + (f" (page {page_num}/{total_pages})" if total_pages > 1 else "")
                + f"  classifying {len(page_reqs)} reqs (all: covered+uncovered)",
                flush=True,
            )

            req_block = _build_req_lines_for_classification(page_reqs, covered_by)
            try:
                parsed = _run_classification_call(
                    llm, cluster_name, req_block, tc_summary_block,
                    spec_reference_section,
                )
                calls_made += 1
                page_classifications = parsed.get("classifications", [])
                all_classifications.extend(page_classifications)
                logger.info(
                    "[run_llm_coverage_analysis_node] Phase 1 classify cluster=%r page=%d/%d "
                    "classified=%d",
                    cluster_name, page_num, total_pages, len(page_classifications),
                )
            except Exception as exc:
                calls_made += 1  # count the failed call against the budget
                msg = f"Phase 1 classification failed for cluster {cluster_name!r} page {page_num}: {exc}"
                logger.error("[run_llm_coverage_analysis_node] %s", msg)
                errors.append(msg)

        # Filter to uncovered + partial only
        gap_reqs = [
            c for c in all_classifications
            if c.get("status") in {"uncovered", "partial"}
        ]
        covered_count = len(all_classifications) - len(gap_reqs)
        logger.info(
            "[run_llm_coverage_analysis_node] Phase 1 result cluster=%r: "
            "%d classified, %d covered/misattributed, %d uncovered+partial for Phase 2",
            cluster_name, len(all_classifications), covered_count, len(gap_reqs),
        )

        if not gap_reqs:
            # All requirements were covered or misattributed — no Phase 2 needed
            findings.append({
                "cluster": cluster_name,
                "confirmed_gaps": [],
                "num_requirements": len(cmap.get("requirements", [])),
                "num_tcs": len(tcs),
            })
            print(
                f"[coverage_analysis]   Phase 1 complete — 0 gaps in {cluster_name!r} "
                f"({covered_count} covered/misattributed), skipping Phase 2",
                flush=True,
            )
            continue

        # ---- Phase 2: Gap description generation ----
        if calls_made >= max_calls:
            # Budget exhausted before Phase 2 — record Phase 1 results with
            # gap notes from classification as the description
            confirmed_gaps = []
            for item in gap_reqs:
                req_id = item.get("req_id", "")
                confirmed_gaps.append({
                    "req_id": req_id,
                    "normative_text": "",
                    "requirement_type": "",
                    "section_path": "",
                    "truly_uncovered": True,
                    "covers_tc": item.get("covering_tc"),
                    "covering_tcs": covered_by.get(req_id, []),
                    "status": item.get("status", "uncovered"),
                    "missing_test_description": item.get("gap", "(Phase 2 skipped — budget exhausted)"),
                })
            findings.append({
                "cluster": cluster_name,
                "confirmed_gaps": confirmed_gaps,
                "num_requirements": len(cmap.get("requirements", [])),
                "num_tcs": len(tcs),
            })
            print(
                f"[coverage_analysis] max_llm_calls={max_calls} reached — "
                f"using Phase 1 gap notes for {cluster_name!r} ({len(gap_reqs)} gaps)",
                flush=True,
            )
            break

        remaining = total_llm_needed - (calls_made + 1)
        print(
            f"[coverage_analysis] LLM call {calls_made + 1}/{total_llm_needed}"
            f" — {remaining} remaining: cluster={cluster_name!r}"
            f"  Phase 2 gap descriptions for {len(gap_reqs)} reqs",
            flush=True,
        )

        try:
            confirmed_gaps = _run_gap_description_call(
                llm, cluster_name, gap_reqs, tc_summary_block,
                sections_block, spec_reference_section, kg,
            )
            calls_made += 1
            # Enrich gap dicts with covering_tcs and status from Phase 1
            phase1_by_req = {c.get("req_id"): c for c in gap_reqs}
            for gap in confirmed_gaps:
                req_id = gap.get("req_id", "")
                p1 = phase1_by_req.get(req_id, {})
                gap.setdefault("covering_tcs", covered_by.get(req_id, []))
                gap.setdefault("status", p1.get("status", "uncovered"))
            findings.append({
                "cluster": cluster_name,
                "confirmed_gaps": confirmed_gaps,
                "num_requirements": len(cmap.get("requirements", [])),
                "num_tcs": len(tcs),
            })
            print(
                f"[coverage_analysis]   done — "
                f"{len(confirmed_gaps)} confirmed gap(s) in {cluster_name!r}",
                flush=True,
            )
            logger.info(
                "[run_llm_coverage_analysis_node] Phase 2 cluster=%r  confirmed_gaps=%d",
                cluster_name, len(confirmed_gaps),
            )
        except Exception as exc:
            calls_made += 1  # count the failed call against the budget
            # Fallback: use Phase 1 gap notes as descriptions
            confirmed_gaps = []
            for item in gap_reqs:
                req_id = item.get("req_id", "")
                confirmed_gaps.append({
                    "req_id": req_id,
                    "normative_text": "",
                    "requirement_type": "",
                    "section_path": "",
                    "truly_uncovered": True,
                    "covers_tc": item.get("covering_tc"),
                    "covering_tcs": covered_by.get(req_id, []),
                    "status": item.get("status", "uncovered"),
                    "missing_test_description": item.get("gap", "(Phase 2 failed)"),
                })
            findings.append({
                "cluster": cluster_name,
                "confirmed_gaps": confirmed_gaps,
                "num_requirements": len(cmap.get("requirements", [])),
                "num_tcs": len(tcs),
            })
            msg = f"Phase 2 gap description failed for cluster {cluster_name!r}: {exc}"
            logger.error("[run_llm_coverage_analysis_node] %s", msg)
            errors.append(msg)

    logger.info(
        "[run_llm_coverage_analysis_node] done: %d clusters, %d LLM calls made (2-phase)",
        len(findings), calls_made,
    )
    return {"cluster_findings": findings, "errors": errors}


@log_node
def aggregate_coverage_findings_node(state: CoverageAnalysisState) -> CoverageAnalysisState:
    """Count total confirmed coverage gaps."""
    findings: List[Dict] = state.get("cluster_findings", [])
    total = sum(len(f.get("confirmed_gaps", [])) for f in findings)
    logger.info("[aggregate_coverage_findings_node] total confirmed gaps: %d", total)
    return {"total_gaps": total}


@log_node
def generate_coverage_report_node(state: CoverageAnalysisState) -> CoverageAnalysisState:
    """Write HTML and JSON reports for coverage gap analysis results."""
    config = state["config"]
    findings: List[Dict] = state.get("cluster_findings", [])
    total_gaps = state.get("total_gaps", 0)
    output_dir = (
        state.get("output_dir")
        or getattr(config.analysis, "output_dir", "reports")
    )

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"matter_rag_reports_{ts}_coverage_analysis"
    out_path.mkdir(parents=True, exist_ok=True)

    # JSON sidecar
    json_path = out_path / f"coverage_analysis_{ts}.json"
    json_data = {
        "timestamp": ts,
        "total_confirmed_gaps": total_gaps,
        "clusters_analyzed": len(findings),
        "findings": findings,
    }
    json_path.write_text(json.dumps(json_data, indent=2))

    # HTML report
    html_path = out_path / f"coverage_analysis_{ts}.html"
    html_path.write_text(_build_coverage_html(findings, ts))

    logger.info(
        "[generate_coverage_report_node] Report written: %s (%d gaps)",
        html_path, total_gaps,
    )

    import shutil as _shutil
    run_dir = state.get("run_dir", "")
    if run_dir:
        _src = Path(run_dir) / "llm_calls.html"
        if _src.is_file():
            _shutil.copy2(str(_src), str(out_path / "llm_calls.html"))
            logger.info("[generate_coverage_report_node] Copied llm_calls.html → %s", out_path)

    return {"report_path": str(html_path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_spec_reference_block(kg, max_chars: int = 12_000) -> str:
    """Return Matter core spec sections 7.3 / 7.6 / 7.7 for LLM prompts.

    Prefers PROMPT_SECTION nodes (pre-built during KG construction with a char
    cap) over the legacy full-text regex scan.  Falls back to the regex scan for
    KG files that predate PROMPT_SECTION nodes.  The result is capped at
    max_chars so a single cluster prompt stays well under the subprocess token
    limit.
    """
    from src.knowledge_graph.base_graph import NodeType as _NT

    # Prefer pre-built PROMPT_SECTION nodes (already char-capped per section)
    prompt_nodes = sorted(
        [obj for _, data in kg._graph.nodes(data=True)
         if (obj := data.get("obj")) and obj.node_type == _NT.PROMPT_SECTION],
        key=lambda n: n.node_id,
    )
    if prompt_nodes:
        result = "\n\n".join(n.properties.get("full_text", "") for n in prompt_nodes)
        return result[:max_chars]

    # Legacy fallback: regex scan over SECTION nodes (no char cap in source)
    _TARGET_PATTERNS = re.compile(
        r"(?:"
        r"7\.3[\s.–—]|conformance"
        r"|7\.6[\s.–—]|access\s+(privilege|control\s+level|level)"
        r"|7\.7[\s.–—]|other\s+qualit"
        r")",
        re.I,
    )

    hits: List[tuple] = []  # (section_path, full_text)
    seen: set = set()

    for _nid, data in kg._graph.nodes(data=True):
        obj: Optional[GraphNode] = data.get("obj")
        if obj is None or obj.node_type != NodeType.SECTION:
            continue
        sec_path = obj.properties.get("section_path") or obj.label or ""
        label = obj.label or ""
        if not _TARGET_PATTERNS.search(sec_path) and not _TARGET_PATTERNS.search(label):
            continue
        full_text = (obj.properties.get("full_text") or "").strip()
        if not full_text or sec_path in seen:
            continue
        seen.add(sec_path)
        hits.append((sec_path, full_text))

    if not hits:
        return ""

    # Sort so 7.3 < 7.6 < 7.7 order is preserved
    hits.sort(key=lambda t: t[0])
    parts = [f"--- {sp} ---\n{txt}" for sp, txt in hits]
    return "\n\n".join(parts)


def _parse_llm_json(response: str, context: str = "") -> Dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Balanced brace extraction — find first complete JSON object
        start = text.find('{')
        if start >= 0:
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
                        try:
                            return json.loads(text[start:i+1])
                        except json.JSONDecodeError:
                            break
    logger.warning("[_parse_llm_json] Failed to parse LLM JSON response: %r", response[:300])
    try:
        from src.llm.call_logger import log_parse_error as _log_pe
        _log_pe(f"Coverage analysis{' — ' + context if context else ''}", response[:500])
    except Exception:
        pass
    return {}


def _build_coverage_html(findings: List[Dict], ts: str) -> str:
    """Render an HTML report for coverage gap analysis."""
    rows = []
    # key -> index in rows list; used to merge duplicates instead of dropping them
    seen_gap_keys: Dict[tuple, int] = {}
    for finding in findings:
        cluster = finding["cluster"]
        for gap in finding.get("confirmed_gaps", []):
            full_norm = (gap.get("normative_text") or "").strip()
            norm_key = " ".join(full_norm.lower().split())[:160]
            gap_key = (cluster, norm_key)
            new_test_desc = gap.get("missing_test_description", "")
            covering_tcs = gap.get("covering_tcs", [])
            # Prefer the LLM's identified covering TC over the KG edges
            llm_covering_tc = gap.get("covers_tc", "")
            if llm_covering_tc:
                covering_tcs = [llm_covering_tc]
            status = gap.get("status", "uncovered")
            if gap_key in seen_gap_keys:
                # Merge: append req_id and add test description only if it's distinct
                existing = rows[seen_gap_keys[gap_key]]
                req_id = gap.get("req_id", "")
                if req_id and req_id not in existing["req_id"]:
                    existing["req_id"] += f", {req_id}"
                if new_test_desc and new_test_desc != existing["missing_test_raw"]:
                    existing["missing_test_parts"].append(new_test_desc)
                # Merge covering TCs
                for tc in covering_tcs:
                    if tc not in existing["covering_tcs"]:
                        existing["covering_tcs"].append(tc)
            else:
                # Display: show the full first sentence; hard-cap only if unusually long
                display_norm = _truncate_to_sentence(full_norm, max_chars=800)
                row = {
                    "cluster": cluster,
                    "req_id": gap.get("req_id", ""),
                    "req_type": gap.get("requirement_type", ""),
                    "section_path": gap.get("section_path", ""),
                    "normative_text_display": display_norm,
                    "normative_text_full": full_norm,
                    "missing_test_raw": new_test_desc,
                    "missing_test_parts": [new_test_desc] if new_test_desc else [],
                    "covering_tcs": list(covering_tcs),
                    "status": status,
                }
                seen_gap_keys[gap_key] = len(rows)
                rows.append(row)

    # Collect unique cluster names (sorted) for the filter dropdown
    all_clusters = sorted({r["cluster"] for r in rows})

    # Collect unique TC-IDs across all rows for the TC filter dropdown
    all_tcs: set = set()
    for r in rows:
        for tc in r.get("covering_tcs", []):
            all_tcs.add(tc)
    all_tcs_sorted = sorted(all_tcs)

    def _escape_attr(s: str) -> str:
        """Escape string for use in HTML attributes."""
        return s.replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    # Cluster summary rows (include data-cluster for filter sync)
    cluster_rows = "".join(
        f"<tr data-cluster='{_escape_attr(f['cluster'])}'>"
        f"<td>{_escape_attr(f['cluster'])}</td>"
        f"<td>{f['num_requirements']}</td>"
        f"<td>{f['num_tcs']}</td>"
        f"<td>{len(f.get('confirmed_gaps', []))}</td>"
        f"</tr>\n"
        for f in sorted(findings, key=lambda x: -len(x.get("confirmed_gaps", [])))
        if len(f.get("confirmed_gaps", [])) > 0
    ) or "<tr><td colspan='4' style='color:#6a9955'>No gaps found.</td></tr>\n"

    def _render_test_cell(parts: List[str]) -> str:
        if len(parts) <= 1:
            return parts[0] if parts else ""
        items = "".join(f"<li>{p}</li>" for p in parts)
        return f"<ol style='margin:0;padding-left:1.2em'>{items}</ol>"

    def _render_covering_tcs(tcs: List[str], status: str) -> str:
        if not tcs:
            return "<span style='color:#6a9955'>(none)</span>"
        tc_links = ", ".join(f"<code>{tc}</code>" for tc in tcs)
        badge = ""
        if status == "partial":
            badge = " <span style='color:#e8a317;font-size:0.8em'>[partial]</span>"
        return f"{tc_links}{badge}"

    def _render_section_cell(section_path: str) -> str:
        """Render section path as a clickable cell with popup."""
        if not section_path:
            return "<td style='color:#9cdcfe;font-size:0.85em'></td>"
        escaped = _escape_attr(section_path)
        return (
            f"<td class='section-link' style='color:#9cdcfe;font-size:0.85em;cursor:pointer;text-decoration:underline dotted #555' "
            f"onclick='showSectionInfo(this)' "
            f"data-section-path='{escaped}' "
            f"title='Click for section anchor'>"
            f"{section_path}</td>"
        )

    gap_rows_parts: List[str] = []
    for r in rows:
        tc_data = " ".join(r.get("covering_tcs", []))
        gap_rows_parts.append(
            f"<tr data-cluster='{r['cluster']}' data-tcs='{_escape_attr(tc_data)}'>"
            f"<td>{r['cluster']}</td>"
            f"<td><code>{r['req_id']}</code></td>"
            f"<td><code>{r['req_type']}</code></td>"
            f"{_render_section_cell(r['section_path'])}"
            f"<td title='{_escape_attr(r['normative_text_full'])}'>{r['normative_text_display']}</td>"
            f"<td>{_render_covering_tcs(r.get('covering_tcs', []), r.get('status', 'uncovered'))}</td>"
            f"<td>{_render_test_cell(r['missing_test_parts'])}</td>"
            f"</tr>\n"
        )
    gap_rows = "".join(gap_rows_parts) or "<tr><td colspan='8' style='color:#6a9955'>No coverage gaps found.</td></tr>\n"

    cluster_options = "\n".join(
        f"<option value='{c}'>{c}</option>" for c in all_clusters
    )
    tc_options = "\n".join(
        f"<option value='{tc}'>{tc}</option>" for tc in all_tcs_sorted
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Coverage Gap Analysis — {ts}</title>
<style>
  body {{ font-family: monospace; background:#1e1e1e; color:#d4d4d4; padding:20px; }}
  h1, h2 {{ color:#4ec9b0; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:30px; }}
  th {{ background:#264f78; color:#d4d4d4; text-align:left; padding:8px; }}
  td {{ border-bottom:1px solid #3c3c3c; padding:8px; vertical-align:top; }}
  tr:hover {{ background:#2d2d2d; }}
  code {{ color:#ce9178; }}
  .summary {{ background:#264f78; padding:10px; margin-bottom:20px; border-radius:4px; }}
  .filter-bar {{ display:flex; align-items:center; gap:12px; margin-bottom:16px;
    background:#252526; padding:10px 14px; border-radius:4px; flex-wrap:wrap; }}
  .filter-bar label {{ color:#9cdcfe; font-size:0.9em; white-space:nowrap; }}
  .filter-bar select {{ background:#1e1e1e; color:#d4d4d4; border:1px solid #3c3c3c;
    padding:4px 8px; font-family:monospace; font-size:0.9em; min-width:260px; }}
  .filter-bar button {{ background:#264f78; color:#d4d4d4; border:none; padding:4px 10px;
    cursor:pointer; font-family:monospace; font-size:0.9em; }}
  .filter-bar button:hover {{ background:#37589a; }}
  #gap-count {{ color:#4ec9b0; font-size:0.85em; }}
  .section-link:hover {{ color:#4ec9b0 !important; }}
  .section-popup-overlay {{ position:fixed; top:0; left:0; width:100%; height:100%;
    background:rgba(0,0,0,0.4); z-index:99; }}
  .section-popup {{ position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
    background:#1a1a1a; border:1px solid #333; padding:16px; border-radius:6px;
    max-width:550px; font-size:12px; z-index:100; box-shadow:0 4px 16px rgba(0,0,0,0.6); }}
  .section-popup .sp-label {{ color:#4caf50; margin-bottom:6px; font-weight:bold; }}
  .section-popup .sp-value {{ color:#e0e0e0; word-break:break-all; margin-bottom:10px; }}
  .section-popup .sp-anchor {{ color:#00bcd4; font-family:monospace; }}
  .section-popup button {{ background:#333; color:#e0e0e0; border:1px solid #555;
    padding:5px 14px; border-radius:3px; cursor:pointer; font-family:monospace; font-size:12px; }}
  .section-popup button:hover {{ background:#444; }}
</style>
</head>
<body>
<h1>Test Plan Coverage Gap Analysis Report</h1>
<p>Generated: {ts} &nbsp;|&nbsp; Pipeline: coverage_analysis</p>

<div class="summary">
  <strong>Total confirmed coverage gaps: {len(rows)}</strong>
  &nbsp;|&nbsp; Clusters analyzed: {len(findings)}
  &nbsp;|&nbsp; Total requirements: {sum(f.get('num_requirements', 0) for f in findings)}
  &nbsp;|&nbsp; Total TCs in scope: {sum(f.get('num_tcs', 0) for f in findings)}
</div>

<div style="background:#1e2a1e;border:1px solid #2a3a2a;padding:12px 18px;border-radius:6px;margin:12px 0;font-size:13px;color:#aaa;line-height:1.7">
  <strong style="color:#80cbc4">How to read this report:</strong><br>
  <b>Partial ({sum(1 for r in rows if r.get('status')=='partial')})</b> — An existing TC tests the entity but does not fully exercise the specific normative behavior. The "Covering TC" column shows which TC partially covers it.<br>
  <b>Uncovered ({sum(1 for r in rows if r.get('status')=='uncovered')})</b> — No existing TC tests this requirement at all. A new TC is needed.<br>
  <b>TCs NOT shown ({sum(f.get('num_tcs',0) for f in findings) - len(all_tcs_sorted)})</b> — TCs that fully cover all their requirements have no gaps and do not appear in this report.<br>
  <b>Not in scope</b> — Protocol TCs (SC, IDM, DD, DA, BDX) and sub-cluster TCs with no own requirements are excluded from this analysis.
</div>

<div class="filter-bar">
  <label for="cluster-filter">Filter by Cluster:</label>
  <select id="cluster-filter" onchange="applyFilters()">
    <option value="">— All clusters ({len(all_clusters)}) —</option>
    {cluster_options}
  </select>
  <label for="tc-filter">Filter by TC:</label>
  <select id="tc-filter" onchange="applyFilters()">
    <option value="">— All TCs ({len(all_tcs_sorted)}) —</option>
    {tc_options}
  </select>
  <button onclick="clearFilters()">Clear</button>
  <span id="gap-count"></span>
</div>

<h2>Clusters with Gaps</h2>
<table id="summary-table">
<tr>
  <th>Cluster</th>
  <th>Requirements</th>
  <th>Test Cases</th>
  <th>Confirmed Gaps</th>
</tr>
{cluster_rows}
</table>

<h2>Confirmed Coverage Gaps</h2>
<table id="gap-table">
<tr>
  <th>Cluster</th>
  <th>Requirement ID</th>
  <th>Type</th>
  <th>Spec Section</th>
  <th>Normative Text</th>
  <th>Covering TCs</th>
  <th>Missing Test Description</th>
</tr>
{gap_rows}
</table>

<script>
function applyFilters() {{
  var clSel = document.getElementById('cluster-filter').value;
  var tcSel = document.getElementById('tc-filter').value;
  var gapRows = document.querySelectorAll('#gap-table tr[data-cluster]');
  var sumRows = document.querySelectorAll('#summary-table tr[data-cluster]');
  var visible = 0;
  gapRows.forEach(function(tr) {{
    var clMatch = !clSel || tr.getAttribute('data-cluster') === clSel;
    var tcMatch = !tcSel || (tr.getAttribute('data-tcs') || '').indexOf(tcSel) !== -1;
    var show = clMatch && tcMatch;
    tr.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  sumRows.forEach(function(tr) {{
    tr.style.display = (!clSel || tr.getAttribute('data-cluster') === clSel) ? '' : 'none';
  }});
  var countEl = document.getElementById('gap-count');
  var parts = [];
  if (clSel || tcSel) parts.push(visible + ' gap(s) shown');
  countEl.textContent = parts.join(' ');
}}
function clearFilters() {{
  document.getElementById('cluster-filter').value = '';
  document.getElementById('tc-filter').value = '';
  applyFilters();
}}
function showSectionInfo(el) {{
  // Remove any existing popup
  var existing = document.querySelector('.section-popup-overlay');
  if (existing) existing.remove();
  existing = document.querySelector('.section-popup');
  if (existing) existing.remove();

  var path = el.getAttribute('data-section-path');
  var parts = path.split(' > ');
  var deepest = parts[parts.length - 1];
  var anchor = deepest.toLowerCase()
    .replace(/[^a-z0-9\\s-]/g, '')
    .replace(/\\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-+|-+$/g, '');

  var overlay = document.createElement('div');
  overlay.className = 'section-popup-overlay';
  overlay.onclick = function() {{
    overlay.remove();
    popup.remove();
  }};
  document.body.appendChild(overlay);

  var popup = document.createElement('div');
  popup.className = 'section-popup';
  popup.innerHTML =
    '<div class="sp-label">Section Path</div>' +
    '<div class="sp-value">' + path + '</div>' +
    '<div class="sp-label">Anchor ID</div>' +
    '<div class="sp-anchor">#' + anchor + '</div>' +
    '<div style="margin-top:12px">' +
    '<button onclick="navigator.clipboard.writeText(\\'#' + anchor + '\\').then(function(){{this.textContent=\\'Copied!\\'}}.bind(this))">Copy anchor</button>' +
    ' <button onclick="this.closest(\\'.section-popup\\').remove();document.querySelector(\\'.section-popup-overlay\\').remove()">Close</button>' +
    '</div>';
  document.body.appendChild(popup);
}}
</script>
</body>
</html>"""
