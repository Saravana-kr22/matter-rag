"""LangGraph nodes for the PICS code validation analysis pipeline.

Loads the knowledge graph from disk, builds a PICS schema map from DM XML,
batches test cases by cluster, sends each cluster to the LLM for PICS validation,
and generates an HTML + JSON report.

State flow:
    load_pics_stores_node
        → build_pics_map_node
            → prepare_cluster_batches_node
                → run_llm_pics_analysis_node
                    → aggregate_pics_findings_node
                        → generate_pics_report_node → END
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
from src.knowledge_graph.dm_pics_validator import build_pics_map, ClusterPicsSchema, PICS_CODE_RE
from src.knowledge_graph.graph_factory import create_knowledge_graph
from src.llm.llm_provider import get_llm
from src.logging_config import log_node, PipelineFatalError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class PicsAnalysisState(TypedDict, total=False):
    config: AppConfig
    run_ctx: RunContext
    run_dir: str
    output_dir: str
    cluster_filter: str           # "" = all clusters
    max_llm_calls: int

    knowledge_graph: Any          # MatterKGBuilder loaded from disk
    pics_map: Dict[str, Any]      # ClusterPicsSchema map keyed by PICS prefix
    cluster_batches: List[Dict]   # [{cluster_name, pics_code, schema_text, tcs}]

    cluster_findings: List[Dict]  # [{cluster, pics_code, pics_issues}]
    total_issues: int

    report_path: str
    errors: List[str]
    fatal_error: bool


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@log_node
def load_pics_stores_node(state: PicsAnalysisState) -> PicsAnalysisState:
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
        "[load_pics_stores_node] KG loaded: %d nodes, %d edges",
        kg.num_nodes, kg.num_edges,
    )
    return {"knowledge_graph": kg}


@log_node
def build_pics_map_node(state: PicsAnalysisState) -> PicsAnalysisState:
    """Parse DM XML files into a PICS prefix → ClusterPicsSchema map."""
    config = state["config"]
    dm_dir = Path(config.analysis.dm_dir)

    if not dm_dir.is_dir():
        raise PipelineFatalError(
            f"DM XML directory not found: {dm_dir}. "
            "Check config.analysis.dm_dir or pass --dm-dir."
        )

    pics_map = build_pics_map(dm_dir)
    logger.info(
        "[build_pics_map_node] Built PICS map: %d cluster schemas from %s",
        len(pics_map), dm_dir,
    )
    return {"pics_map": pics_map}


@log_node
def prepare_cluster_batches_node(state: PicsAnalysisState) -> PicsAnalysisState:
    """Group TEST_CASE nodes by cluster and attach PICS schema info."""
    kg = state["knowledge_graph"]
    pics_map: Dict[str, ClusterPicsSchema] = state.get("pics_map", {})
    cluster_filter = (state.get("cluster_filter") or "").lower()

    # Build reverse lookup: cluster_name_lower → ClusterPicsSchema
    cluster_to_pics: Dict[str, ClusterPicsSchema] = {}
    for schema in pics_map.values():
        cluster_to_pics[schema.cluster_name.lower()] = schema
        cluster_to_pics[schema.pics_code.lower()] = schema

    # Collect TEST_CASE nodes by cluster
    tc_by_cluster: Dict[str, List[GraphNode]] = {}
    for _nid, data in kg._graph.nodes(data=True):
        obj: Optional[GraphNode] = data.get("obj")
        if obj is None or obj.node_type != NodeType.TEST_CASE:
            continue
        cluster = (obj.properties.get("cluster") or "").strip()
        if not cluster:
            continue
        if cluster_filter and cluster_filter not in cluster.lower():
            continue
        tc_by_cluster.setdefault(cluster, []).append(obj)

    if not tc_by_cluster:
        logger.warning(
            "[prepare_cluster_batches_node] No TEST_CASE nodes found in KG "
            "(cluster_filter=%r). Rebuild KG with --build-knowledge-graph.",
            cluster_filter or "all",
        )

    batches: List[Dict] = []
    for cluster_name in sorted(tc_by_cluster.keys()):
        tcs = tc_by_cluster[cluster_name]

        # Find PICS schema: exact match first, then partial
        schema: Optional[ClusterPicsSchema] = None
        for key in [
            cluster_name.lower(),
            cluster_name.lower().replace("/", "").strip(),
            cluster_name.lower().split()[0],   # first word
        ]:
            schema = cluster_to_pics.get(key)
            if schema:
                break
        if schema is None:
            for k, s in cluster_to_pics.items():
                if k in cluster_name.lower() or cluster_name.lower() in k:
                    schema = s
                    break

        pics_code = schema.pics_code if schema else "unknown"
        schema_text = (
            schema.format_schema_text()
            if schema
            else "(not in DM XML — may be a protocol-level cluster)"
        )
        batches.append({
            "cluster_name": cluster_name,
            "pics_code": pics_code,
            "schema_text": schema_text,
            "tcs": tcs,
        })

    logger.info(
        "[prepare_cluster_batches_node] %d clusters with TCs (cluster_filter=%r)",
        len(batches), cluster_filter or "all",
    )
    return {"cluster_batches": batches}


@log_node
def run_llm_pics_analysis_node(state: PicsAnalysisState) -> PicsAnalysisState:
    """Send each cluster batch to the LLM for PICS code validation.

    Supports parallel execution via ``config.analysis.parallel_workers``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    config = state["config"]
    batches: List[Dict] = state.get("cluster_batches", [])
    _raw = state.get("max_llm_calls")
    if _raw is None:
        _raw = getattr(config.analysis, "max_llm_calls_per_run", 0)
    max_calls = _raw if _raw > 0 else 999_999
    errors: List[str] = list(state.get("errors") or [])
    workers = getattr(config.analysis, "parallel_workers", 1) or 1

    if not batches:
        logger.warning("[run_llm_pics_analysis_node] No cluster batches to analyze.")
        return {"cluster_findings": [], "errors": errors}

    batches_to_run = batches[:max_calls]
    llm = get_llm(config.llm, log_dir=state.get("run_dir"))
    pics_map = state.get("pics_map", {})

    logger.info(
        "[run_llm_pics_analysis_node] %d clusters to analyze, workers=%d",
        len(batches_to_run), workers,
    )

    def _process_one_cluster(batch: Dict) -> Dict:
        """Process a single cluster — called from thread pool."""
        cluster_name = batch["cluster_name"]
        pics_code = batch["pics_code"]
        tcs: List[GraphNode] = batch["tcs"]

        pics_schema: Optional[ClusterPicsSchema] = (
            pics_map.get(pics_code) if pics_code != "unknown" else None
        )
        determ_issues: List[Dict] = []
        for tc in tcs:
            meta_dut = _infer_dut_type_from_metadata(tc)
            determ_issues.extend(_check_dut_type_mismatch(tc, pics_schema, meta_dut))
            determ_issues.extend(_check_pics_deterministic(tc, pics_schema, meta_dut))

        prompt = _build_pics_prompt(batch, pics_schema, state)

        try:
            if hasattr(llm, "set_next_label"):
                llm.set_next_label(f"PICS validation — {cluster_name}")
            response = llm.complete(prompt)
            parsed = _parse_llm_json(response, context=cluster_name)
            llm_issues = parsed.get("pics_issues", [])
            seen: set = {(i["tc_id"], i["issue_type"], i.get("pics_code", "")) for i in determ_issues}
            merged = determ_issues + [
                i for i in llm_issues
                if (i.get("tc_id"), i.get("issue_type"), i.get("pics_code", "")) not in seen
            ]
            logger.info(
                "[run_llm_pics_analysis_node] cluster=%r  determ=%d  llm=%d  total=%d",
                cluster_name, len(determ_issues), len(llm_issues), len(merged),
            )
            return {"cluster": cluster_name, "pics_code": pics_code, "pics_issues": merged, "error": None}
        except Exception as exc:
            msg = f"LLM call failed for cluster {cluster_name!r}: {exc}"
            logger.error("[run_llm_pics_analysis_node] %s", msg)
            return {"cluster": cluster_name, "pics_code": pics_code, "pics_issues": determ_issues, "error": msg}

    findings: List[Dict] = []

    if workers <= 1:
        for i, batch in enumerate(batches_to_run):
            print(
                f"[pics_analysis] LLM call {i+1}/{len(batches_to_run)}"
                f" — {len(batches_to_run)-i-1} remaining: cluster={batch['cluster_name']!r}",
                flush=True,
            )
            result = _process_one_cluster(batch)
            if result["error"]:
                errors.append(result["error"])
            findings.append({"cluster": result["cluster"], "pics_code": result["pics_code"], "pics_issues": result["pics_issues"]})
    else:
        print(
            f"[pics_analysis] Running {len(batches_to_run)} clusters with {workers} parallel workers",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_batch = {
                executor.submit(_process_one_cluster, batch): batch
                for batch in batches_to_run
            }
            completed = 0
            for future in as_completed(future_to_batch):
                completed += 1
                result = future.result()
                if result["error"]:
                    errors.append(result["error"])
                findings.append({"cluster": result["cluster"], "pics_code": result["pics_code"], "pics_issues": result["pics_issues"]})
                if completed % 10 == 0 or completed == len(batches_to_run):
                    print(
                        f"[pics_analysis] {completed}/{len(batches_to_run)} clusters done",
                        flush=True,
                    )

    logger.info(
        "[run_llm_pics_analysis_node] done: %d clusters analyzed",
        len(findings),
    )
    return {"cluster_findings": findings, "errors": errors}


def _build_pics_prompt(batch: Dict, pics_schema, state) -> str:
    """Build the LLM prompt for a single cluster's PICS validation."""
    cluster_name = batch["cluster_name"]
    pics_code = batch["pics_code"]
    schema_text = batch["schema_text"]
    tcs = batch["tcs"]

    _STRICT_PICS_RE = re.compile(
        r'^[A-Z][A-Z0-9]{0,15}\.[SCM](?:\.[ACEF][0-9A-Fa-f]{1,8}(?:\.Rsp)?)?$'
    )
    tc_lines = []
    for tc in tcs:
        props = tc.properties
        tc_id = props.get("tc_id", tc.node_id)
        meta_dut = _infer_dut_type_from_metadata(tc)
        intents = ", ".join(props.get("intents", []))
        raw_pics = props.get("pics_codes", [])
        valid_pics: List[str] = []
        for code in raw_pics:
            bare = re.sub(r'\(.*?\)', '', code).strip()
            bare = re.sub(r'\.Rsp$', '', bare, flags=re.I)
            if _STRICT_PICS_RE.match(bare):
                valid_pics.append(code)
        purpose = (props.get("purpose") or "")[:200]
        dut_label = meta_dut if meta_dut else "unknown (infer from PICS distribution)"
        tc_lines.append(
            f"TC: {tc_id}  [dut_type={dut_label}]\n"
            f"  purpose: {purpose!r}\n"
            f"  intents: [{intents}]\n"
            f"  PICS declared: {valid_pics}"
        )
    tc_block = "\n\n".join(tc_lines) if tc_lines else "(no test cases)"

    return (
        f"You are a Matter specification expert reviewing PICS code usage in test cases.\n\n"
        f"=== CLUSTER: {cluster_name} (PICS prefix: {pics_code}) ===\n\n"
        f"DM SCHEMA:\n{schema_text}\n\n"
        "=== CONFORMANCE RULES — apply these exactly ===\n"
        "Entities in the DM schema above fall into four conformance categories:\n\n"
        "  1. UNCONDITIONALLY MANDATORY (marked (M) in schema):\n"
        "     Every DUT implementing this cluster MUST support these entities.\n"
        "     Test cases do NOT need to declare PICS for them — they are always present.\n"
        "     Do NOT flag any issue for absent PICS of (M) entities.\n\n"
        "  2. UNCONDITIONALLY OPTIONAL (no (M) mark, no feature dependency):\n"
        "     Test cases MUST declare the entity PICS before exercising this entity.\n"
        "     Flag step_pics_mismatch if the test exercises the entity without PICS.\n\n"
        "  3. FEATURE-DEPENDENT MANDATORY (entity is mandatory IF a feature is enabled):\n"
        "     Test cases only need the FEATURE PICS (.F.bit) — NOT the entity PICS.\n"
        "     Only flag missing_feature_pics when the feature PICS itself is absent.\n\n"
        "  4. FEATURE-DEPENDENT OPTIONAL (entity is optional even when the feature is on):\n"
        "     Test cases need BOTH the feature PICS AND the entity PICS.\n\n"
        "  5. DISALLOWED / DEPRECATED (marked (X) in schema):\n"
        "     Do NOT flag missing PICS for disallowed/deprecated entities.\n\n"
        "The (M) annotation marks unconditionally mandatory entities (case 1).\n"
        "The (O) annotation marks unconditionally optional entities (case 2) — "
        "entity PICS required but NO feature PICS required.\n"
        "The (X) annotation marks disallowed/deprecated entities (case 5).\n"
        "=== END CONFORMANCE RULES ===\n\n"
        "NOTE: Protocol-level PICS (BLE, Thread, WiFi, TCP, QR commissioning, NFC, etc.) "
        "are NOT in the schema above. Use your Matter protocol knowledge to detect missing "
        "protocol PICS in test steps.\n\n"
        "NOTE: The dut_type shown for each TC is inferred from the TC title and purpose text.\n"
        "Use it as ground truth for wrong_side checks.\n\n"
        "NOTE: Attributes with revision-conditional conformance (e.g., 'M, Rev >= v5') are "
        "mandatory — do NOT flag missing PICS for these.\n\n"
        "NOTE: Before flagging missing PICS, check if the PICS code appears in any individual "
        "test step. Step-level PICS gating is valid.\n\n"
        f"TEST CASES:\n{tc_block}\n\n"
        "TASK — PICS VALIDATION:\n"
        "Identify issues of types: wrong_side, non_existent, missing_feature_pics, "
        "missing_protocol_pics, step_pics_mismatch.\n"
        "Do NOT return dut_type_mismatch — handled outside the LLM.\n\n"
        "Return ONLY valid JSON:\n"
        '{"pics_issues": [{"tc_id": "<TC-ID>", "issue_type": "<type>", '
        '"pics_code": "<code>", "description": "<one sentence>"}]}\n\n'
        'If no issues: {"pics_issues": []}'
    )


@log_node
def aggregate_pics_findings_node(state: PicsAnalysisState) -> PicsAnalysisState:
    """Count total PICS issues across all clusters."""
    findings: List[Dict] = state.get("cluster_findings", [])
    total = sum(len(f.get("pics_issues", [])) for f in findings)
    logger.info("[aggregate_pics_findings_node] total PICS issues: %d", total)
    return {"total_issues": total}


@log_node
def generate_pics_report_node(state: PicsAnalysisState) -> PicsAnalysisState:
    """Write HTML and JSON reports for PICS analysis results."""
    config = state["config"]
    findings: List[Dict] = state.get("cluster_findings", [])
    total_issues = state.get("total_issues", 0)
    output_dir = (
        state.get("output_dir")
        or getattr(config.analysis, "output_dir", "reports")
    )

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"matter_rag_reports_{ts}_pics_analysis"
    out_path.mkdir(parents=True, exist_ok=True)

    # JSON sidecar
    json_path = out_path / f"pics_analysis_{ts}.json"
    json_data = {
        "timestamp": ts,
        "total_issues": total_issues,
        "clusters_analyzed": len(findings),
        "findings": findings,
    }
    json_path.write_text(json.dumps(json_data, indent=2))

    # HTML report
    html_path = out_path / f"pics_analysis_{ts}.html"
    html_path.write_text(_build_pics_html(findings, total_issues, ts))

    logger.info(
        "[generate_pics_report_node] Report written: %s (%d issues)",
        html_path, total_issues,
    )

    # Copy llm_calls.html from the run log dir into the reports folder so
    # all run artefacts are in one place and parallel runs don't collide.
    import shutil as _shutil
    run_dir = state.get("run_dir", "")
    if run_dir:
        _src = Path(run_dir) / "llm_calls.html"
        if _src.is_file():
            _shutil.copy2(str(_src), str(out_path / "llm_calls.html"))
            logger.info("[generate_pics_report_node] Copied llm_calls.html → %s", out_path)

    return {"report_path": str(html_path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json_balanced(text: str) -> str:
    """Extract the first balanced JSON object using brace depth tracking."""
    start = text.find('{')
    if start < 0:
        return ""
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
    return ""


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
        balanced = _extract_json_balanced(text)
        if balanced:
            try:
                return json.loads(balanced)
            except json.JSONDecodeError:
                pass
    logger.warning("[_parse_llm_json] Failed to parse LLM JSON response: %r", response[:300])
    try:
        from src.llm.call_logger import log_parse_error as _log_pe
        _log_pe(f"PICS analysis{' — ' + context if context else ''}", response[:500])
    except Exception:
        pass
    return {}


def _entity_type_label(entity_type: str) -> str:
    return {"A": "attribute", "C": "command", "E": "event", "F": "feature"}.get(entity_type, entity_type)


# Keywords in TC titles/purposes that strongly signal Server DUT
_SERVER_KW = re.compile(
    r'\b(dut\s+as\s+server|server\s+dut|as\s+a\s+server|acting\s+as\s+server'
    r'|with\s+server|device\s+under\s+test.*server)\b',
    re.I,
)
# Keywords that strongly signal Client / Commissioner DUT
_CLIENT_KW = re.compile(
    r'\b(dut\s+as\s+(client|commissioner|initiator)'
    r'|client\s+dut|commissioner\s+dut|initiator\s+dut'
    r'|(client|commissioner|initiator)\s+as\s+(?:a\s+)?dut'  # "client as DUT"
    r'|as\s+a\s+(client|commissioner|initiator)'
    r'|acting\s+as\s+(client|commissioner|initiator)'
    r'|cluster\s+client\b)',                                   # "Cluster client" in purpose
    re.I,
)
# Intents that imply the DUT is a server (responding to reads/writes/commands)
_SERVER_INTENTS = frozenset({
    "read_attribute", "write_attribute", "subscribe_attribute",
    "receive_command", "send_response", "verify_attribute",
    "functional", "performance", "robustness",
})
# Intents that imply the DUT is a client / initiator
_CLIENT_INTENTS = frozenset({
    "send_command", "initiate_commissioning", "commission",
    "discover", "onboard", "pair",
})


def _infer_dut_type_from_metadata(tc: "GraphNode") -> str:
    """Infer DUT type from TC title, purpose, and intents — NOT from PICS codes.

    Returns one of: "Server", "Client", "Commissioner", or "" (unknown).
    This is the ground-truth signal used to detect wrong PICS side declarations.
    """
    props = tc.properties
    stored = (props.get("dut_type") or "").strip()
    if stored:
        return stored

    tc_id   = (props.get("tc_id") or tc.node_id or "")
    title   = (tc.label or tc_id or "").lower()
    purpose = (props.get("purpose") or "").lower()
    intents: List[str] = [i.lower() for i in (props.get("intents") or [])]
    text    = f"{title} {purpose}"

    if _CLIENT_KW.search(text):
        if "commissioner" in text:
            return "Commissioner"
        return "Client"
    if _SERVER_KW.search(text):
        return "Server"

    # TC-XX-3.x convention: major version 3 = client DUT
    _tc_major_m = re.match(r'TC-[A-Z]+-(\d+)\.', tc_id)
    if _tc_major_m and int(_tc_major_m.group(1)) == 3:
        return "Client"

    # Secondary signal: "client" mentioned in purpose text
    if re.search(r'\bclient\b', purpose):
        return "Client"

    # Intent voting
    srv_score = sum(1 for i in intents if i in _SERVER_INTENTS)
    cli_score = sum(1 for i in intents if i in _CLIENT_INTENTS)
    if cli_score > srv_score:
        return "Client"
    if srv_score > 0:
        return "Server"

    return ""   # genuinely unknown


def _check_dut_type_mismatch(
    tc: "GraphNode",
    pics_schema: Optional[ClusterPicsSchema],
    meta_dut_type: str,
) -> List[Dict]:
    """Check whether the PICS side distribution contradicts the metadata-inferred DUT type.

    This catches cases where test case authors wrote the correct title/purpose (Server DUT)
    but accidentally used .C. PICS codes (or vice versa).  Unlike _check_pics_deterministic,
    the ground-truth here comes from TC *metadata* — not from the PICS codes themselves,
    so the check is not circular.

    Only fires when meta_dut_type is non-empty and the PICS codes for the cluster prefix
    all agree on the opposite side.
    """
    issues: List[Dict] = []
    if not meta_dut_type:
        return issues

    props = tc.properties
    tc_id = props.get("tc_id", tc.node_id)
    pics_codes: List[str] = props.get("pics_codes", [])
    cluster_prefix = pics_schema.pics_code if pics_schema else ""
    if not cluster_prefix or not pics_codes:
        return issues

    # Collect the side (.S. or .C.) for every PICS code that belongs to this cluster prefix
    cluster_sides: set = set()
    for code in pics_codes:
        raw = re.sub(r'\(.*?\)', '', code).strip()
        raw = re.sub(r'\.Rsp$', '', raw, flags=re.I)
        m = PICS_CODE_RE.match(raw)
        if m and m.group(1) == cluster_prefix:
            cluster_sides.add(m.group(2))

    if not cluster_sides or cluster_sides == {"S", "C"}:
        return issues   # mixed or absent — no clear contradiction

    pics_side = next(iter(cluster_sides))   # "S" or "C"

    is_client_meta = any(
        kw in meta_dut_type.lower()
        for kw in ("client", "commissioner", "initiator")
    )
    is_server_meta = not is_client_meta and "server" in meta_dut_type.lower()

    if is_server_meta and pics_side == "C":
        issues.append({
            "tc_id": tc_id,
            "issue_type": "dut_type_mismatch",
            "pics_code": "",
            "description": (
                f"TC title/purpose identifies a Server DUT but all {cluster_prefix} PICS codes "
                f"use client-side (.C.) — PICS side and DUT role are inconsistent. "
                f"Either the DUT type description or the PICS codes are wrong."
            ),
        })
    elif is_client_meta and pics_side == "S":
        issues.append({
            "tc_id": tc_id,
            "issue_type": "dut_type_mismatch",
            "pics_code": "",
            "description": (
                f"TC title/purpose identifies a {meta_dut_type} DUT but all {cluster_prefix} PICS codes "
                f"use server-side (.S.) — PICS side and DUT role are inconsistent. "
                f"Either the DUT type description or the PICS codes are wrong."
            ),
        })

    return issues


def _check_pics_deterministic(
    tc: GraphNode,
    pics_schema: Optional[ClusterPicsSchema],
    meta_dut_type: str = "",
) -> List[Dict]:
    """Deterministic wrong_side and non_existent checks using the DM schema.

    Uses *meta_dut_type* (inferred from TC title/purpose/intents by _infer_dut_type_from_metadata)
    as the ground truth for wrong_side detection.  Falls back to PICS-side inference only when
    meta_dut_type is empty, to avoid leaving wrong_side completely unchecked.

    Only checks PICS codes whose prefix matches the cluster under test.
    Cross-cluster client PICS on server DUTs are never flagged (dual-role pattern).
    """
    issues: List[Dict] = []
    props = tc.properties
    tc_id = props.get("tc_id", tc.node_id)
    pics_codes: List[str] = props.get("pics_codes", [])

    if not pics_codes:
        return issues

    cluster_prefix = pics_schema.pics_code if pics_schema else ""

    # Pre-parse all PICS codes once
    parsed: list = []  # (original_code, prefix, side, entity_type, hex_id_str)
    for code in pics_codes:
        raw = re.sub(r'\(.*?\)', '', code).strip()
        raw = re.sub(r'\.Rsp$', '', raw, flags=re.I)
        m = PICS_CODE_RE.match(raw)
        if m:
            parsed.append((code, m.group(1), m.group(2), m.group(3), m.group(4)))

    # Determine effective dut_type for wrong_side check:
    # 1. Use metadata-inferred type (from title/purpose/intents) — not circular
    # 2. Fall back to PICS-side inference only if metadata gives nothing
    dut_type = meta_dut_type
    skip_wrong_side = False
    if not dut_type and cluster_prefix:
        same_prefix_sides = {side for _, pfx, side, _, _ in parsed if pfx == cluster_prefix}
        if same_prefix_sides == {"C"}:
            dut_type = "Client"
        elif same_prefix_sides == {"S"}:
            dut_type = "Server"
        else:
            skip_wrong_side = True  # mixed or no cluster-prefix codes → can't determine role

    if not dut_type:
        dut_type = "Server"  # final fallback — only affects non_existent check path

    is_client_dut = any(
        kw in dut_type.lower()
        for kw in ("client", "commissioner", "initiator")
    )

    for code, prefix, side, entity_type, hex_id_str in parsed:
        # Only validate codes for the cluster under test
        if prefix != cluster_prefix:
            continue

        # Skip disallowed/deprecated entities — these are intentionally not
        # available in derived clusters and should not be flagged for any issue.
        if pics_schema:
            try:
                _eid = int(hex_id_str, 16)
            except ValueError:
                _eid = None
            if _eid is not None:
                if entity_type == "A" and _eid in (
                    pics_schema.disallowed_server_attrs | pics_schema.disallowed_client_attrs
                ):
                    continue
                if entity_type == "C" and _eid in (
                    pics_schema.disallowed_server_cmds | pics_schema.disallowed_client_cmds
                ):
                    continue

        # wrong_side check
        if cluster_prefix and not skip_wrong_side:
            etype = _entity_type_label(entity_type)
            if not is_client_dut and side == "C":
                issues.append({
                    "tc_id": tc_id,
                    "issue_type": "wrong_side",
                    "pics_code": code,
                    "description": (
                        f"Server DUT test declares client-side {etype} PICS "
                        f"{code}; expected {cluster_prefix}.S.{entity_type}{hex_id_str}"
                    ),
                })
            elif is_client_dut and side == "S":
                issues.append({
                    "tc_id": tc_id,
                    "issue_type": "wrong_side",
                    "pics_code": code,
                    "description": (
                        f"Client/Commissioner DUT test declares server-side {etype} PICS "
                        f"{code}; expected {cluster_prefix}.C.{entity_type}{hex_id_str}"
                    ),
                })

        # non_existent check
        if not pics_schema:
            continue
        try:
            entity_id = int(hex_id_str, 16)
        except ValueError:
            continue

        if entity_type == "A":
            all_attrs = {**pics_schema.server_attrs, **pics_schema.client_attrs}
            if entity_id not in all_attrs:
                issues.append({
                    "tc_id": tc_id,
                    "issue_type": "non_existent",
                    "pics_code": code,
                    "description": (
                        f"Attribute 0x{entity_id:04X} does not exist in {cluster_prefix} DM schema"
                    ),
                })
        elif entity_type == "C":
            all_cmds = {**pics_schema.server_cmds, **pics_schema.client_cmds}
            if entity_id not in all_cmds:
                issues.append({
                    "tc_id": tc_id,
                    "issue_type": "non_existent",
                    "pics_code": code,
                    "description": (
                        f"Command 0x{entity_id:02X} does not exist in {cluster_prefix} DM schema"
                    ),
                })
        elif entity_type == "F":
            if entity_id not in pics_schema.features:
                issues.append({
                    "tc_id": tc_id,
                    "issue_type": "non_existent",
                    "pics_code": code,
                    "description": (
                        f"Feature bit {entity_id} does not exist in {cluster_prefix} DM schema"
                    ),
                })

    return issues


def _build_pics_html(findings: List[Dict], total_issues: int, ts: str) -> str:
    """Render an HTML report for PICS analysis."""
    rows = []
    for finding in findings:
        cluster = finding["cluster"]
        pics_code = finding["pics_code"]
        for issue in finding.get("pics_issues", []):
            rows.append({
                "cluster": cluster,
                "pics_prefix": pics_code,
                "tc_id": issue.get("tc_id", ""),
                "issue_type": issue.get("issue_type", ""),
                "affected_pics": issue.get("pics_code", ""),
                "description": issue.get("description", ""),
            })

    issue_type_counts: Dict[str, int] = {}
    for r in rows:
        issue_type_counts[r["issue_type"]] = issue_type_counts.get(r["issue_type"], 0) + 1

    row_html = "".join(
        f"<tr data-issue-type=\"{r['issue_type']}\" data-pics-code=\"{r['affected_pics'].upper()}\">"
        f"<td>{r['tc_id']}</td>"
        f"<td>{r['cluster']}</td>"
        f"<td><code class='it-{r['issue_type']}'>{r['issue_type']}</code></td>"
        f"<td><code>{r['affected_pics']}</code></td>"
        f"<td>{r['description']}</td>"
        f"</tr>\n"
        for r in rows
    ) or "<tr id='no-results-row'><td colspan='5' style='color:#6a9955'>No PICS issues found.</td></tr>\n"

    summary_rows = "".join(
        f"<tr><td><code>{k}</code></td><td>{v}</td></tr>\n"
        for k, v in sorted(issue_type_counts.items(), key=lambda x: -x[1])
    ) or "<tr><td colspan='2' style='color:#6a9955'>No issues.</td></tr>\n"

    # Build issue_type options for the dropdown
    issue_type_options = "<option value=''>All Issue Types</option>\n" + "".join(
        f"<option value='{k}'>{k} ({v})</option>\n"
        for k, v in sorted(issue_type_counts.items(), key=lambda x: -x[1])
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PICS Analysis — {ts}</title>
<style>
  body {{ font-family: monospace; background:#1e1e1e; color:#d4d4d4; padding:20px; }}
  h1, h2 {{ color:#4ec9b0; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:30px; }}
  th {{ background:#264f78; color:#d4d4d4; text-align:left; padding:8px; }}
  td {{ border-bottom:1px solid #3c3c3c; padding:8px; vertical-align:top; }}
  tr:hover {{ background:#2d2d2d; }}
  code {{ color:#ce9178; }}
  .summary {{ background:#264f78; padding:10px; margin-bottom:20px; border-radius:4px; }}
  .it-wrong_side {{ color:#ef5350; }}
  .it-dut_type_mismatch {{ color:#ff7043; }}
  .it-non_existent {{ color:#ffa726; }}
  .it-invalid_format {{ color:#f44336; }}
  .it-missing_feature_pics {{ color:#ab47bc; }}
  .it-missing_protocol_pics {{ color:#26c6da; }}
  .it-step_pics_mismatch {{ color:#66bb6a; }}
  .filter-bar {{ display:flex; gap:16px; align-items:center; flex-wrap:wrap;
                 background:#252526; border:1px solid #3c3c3c; border-radius:4px;
                 padding:10px 14px; margin-bottom:14px; }}
  .filter-bar label {{ color:#9cdcfe; font-size:0.85em; white-space:nowrap; }}
  .filter-bar select, .filter-bar input {{
    background:#1e1e1e; color:#d4d4d4; border:1px solid #555; border-radius:3px;
    padding:4px 8px; font-family:monospace; font-size:0.9em; min-width:180px; }}
  .filter-bar button {{
    background:#264f78; color:#d4d4d4; border:none; border-radius:3px;
    padding:4px 12px; font-family:monospace; cursor:pointer; }}
  .filter-bar button:hover {{ background:#1f3f5e; }}
  #match-count {{ color:#6a9955; font-size:0.85em; }}
  tr.hidden-row {{ display:none; }}
</style>
</head>
<body>
<h1>PICS Code Validation Report</h1>
<p>Generated: {ts} &nbsp;|&nbsp; Pipeline: pics_analysis</p>

<div class="summary">
  <strong>Total PICS issues: {total_issues}</strong>
  &nbsp;|&nbsp; Clusters analyzed: {len(findings)}
</div>

<h2>Issue Type Summary</h2>
<table>
<tr><th>Issue Type</th><th>Count</th></tr>
{summary_rows}
</table>

<h2>All PICS Issues</h2>

<div class="filter-bar">
  <label for="filter-issue-type">Issue Type:</label>
  <select id="filter-issue-type" onchange="filterRows()">
    {issue_type_options}
  </select>
  <label for="filter-pics-code">PICS Code:</label>
  <input id="filter-pics-code" type="text" placeholder="e.g. OO.S.A0000"
         oninput="filterRows()" autocomplete="off" spellcheck="false">
  <button onclick="clearFilters()">Clear</button>
  <span id="match-count"></span>
</div>

<table id="issues-table">
<tr>
  <th>TC ID</th>
  <th>Cluster</th>
  <th>Issue Type</th>
  <th>PICS Code</th>
  <th>Description</th>
</tr>
{row_html}
</table>

<script>
function filterRows() {{
  var selType  = document.getElementById('filter-issue-type').value;
  var selPics  = document.getElementById('filter-pics-code').value.trim().toUpperCase();
  var rows     = document.querySelectorAll('#issues-table tr[data-issue-type]');
  var visible  = 0;
  rows.forEach(function(row) {{
    var typeMatch = !selType || row.dataset.issueType === selType;
    var picsMatch = !selPics || row.dataset.picsCode.includes(selPics);
    if (typeMatch && picsMatch) {{
      row.classList.remove('hidden-row');
      visible++;
    }} else {{
      row.classList.add('hidden-row');
    }}
  }});
  var noResultsRow = document.getElementById('no-results-row');
  if (noResultsRow) {{ noResultsRow.style.display = visible === 0 ? '' : 'none'; }}
  var countEl = document.getElementById('match-count');
  if (rows.length > 0) {{
    countEl.textContent = visible + ' / ' + rows.length + ' shown';
  }}
}}

function clearFilters() {{
  document.getElementById('filter-issue-type').value = '';
  document.getElementById('filter-pics-code').value  = '';
  filterRows();
}}

// Show initial count
filterRows();
</script>
</body>
</html>"""
