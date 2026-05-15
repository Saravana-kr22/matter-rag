"""Knowledge Graph builder — assembles a typed NetworkX graph from all sources.

Input:  CanonicalSchema + List[SpecRecord] + List[TestCaseRecord]
Output: GraphBundle (flat lists of GraphNodeRecord + GraphEdgeRecord)

Node ID conventions  (same as in matter_kg_builder canonical ID scheme):
    Cluster:     ``CLUSTER::On/Off``
    Attribute:   ``ATTRIBUTE::On/Off::OnOff``
    Command:     ``COMMAND::On/Off::Off``
    Event:       ``EVENT::On/Off::SwitchLatched``
    Feature:     ``FEATURE::On/Off::Lighting``
    Requirement: ``REQ::On/Off::0``
    TestCase:    ``TC-OO-2.1``

Edge types follow the GraphEdgeType enum:
    CLUSTER →[HAS_ATTRIBUTE]→ ATTRIBUTE
    CLUSTER →[HAS_COMMAND]→   COMMAND
    CLUSTER →[HAS_EVENT]→     EVENT
    CLUSTER →[HAS_FEATURE]→   FEATURE
    CLUSTER →[DEPENDS_ON]→    CLUSTER   (cross-cluster dependency, from DEP* FEATURE nodes)
    REQUIREMENT →[IMPLEMENTS]→ entity
    TEST_CASE   →[TESTS]→      cluster/entity
    TEST_CASE   →[COVERS]→     requirement
    TEST_CASE   →[VALIDATES]→  requirement
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Set

from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    ClusterRecord,
    EntityType,
    GraphBundle,
    GraphEdgeRecord,
    GraphEdgeType,
    GraphNodeRecord,
    GraphNodeType,
    RejectedCandidate,
    RequirementType,
    SectionRecord,
    SpecRecord,
    TestCaseRecord,
    ValidationReport,
)
from src.knowledge_graph.rule_engine import infer_graph_edges, extract_protocol_areas, extract_behavior_hints, _is_protocol_prefix
from src.knowledge_graph.vector_chunk_gen import _extract_step_actions as _extract_step_keywords

logger = logging.getLogger(__name__)

# Maps TC-ID prefix → PROTOCOL_AREA slug for protocol-family test cases.
# These TCs span protocol layers, not individual clusters.
_PROTOCOL_TC_TO_AREA_SLUG: Dict[str, str] = {
    "IDM":     "Interaction_Data_Model",
    "SC":      "Secure_Channel",
    "BDX":     "Bulk_Data_Exchange",
    "DD":      "Device_Discovery",
    "DA":      "Device_Attestation",
    "ACE":     "Access_Control",
    "MC":      "Multicast",
    "JFADMIN": "Joining_Fabric_Administrator",
    "JF":      "Joining_Fabric_Administrator",
    "MCORE":   "Matter_Core",
    # Additional protocol-adjacent prefixes from _DEFINITELY_PROTOCOL_TC_PREFIXES
    "DT":      "Descriptor",
    "SU":      "Software_Update",
    # Prefixes that _is_protocol_prefix() may classify as protocol at runtime
    # (not in DM XML _known_cluster_prefixes).  Adding known ones here gives
    # them human-readable area slugs; any remaining are handled by the deferred
    # VirtualCluster creation pass in build_graph().
    "BR":      "Bridged_Device",
    "SM":      "Scene_Management",
    "RR":      "Reachability",
    "ICDB":    "ICD_Management",
    "WEBRTC":  "WebRTC_Transport",
    "PAVSTI":  "Push_AV_Stream_Transport_Interop",
}



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_graph(
    canonical_schema: CanonicalSchema,
    spec_records: List[SpecRecord],
    test_case_records: List[TestCaseRecord],
    section_records: Optional[List[SectionRecord]] = None,
    rejected_candidates: Optional[List[RejectedCandidate]] = None,
) -> GraphBundle:
    """Build a complete GraphBundle from all KB sources."""
    import time
    nodes: Dict[str, GraphNodeRecord] = {}
    edges: List[GraphEdgeRecord] = []
    seen_edges: Set[tuple] = set()

    def add_node(rec: GraphNodeRecord) -> None:
        if rec.node_id not in nodes:
            nodes[rec.node_id] = rec

    def add_edge(src: str, tgt: str, edge_type: GraphEdgeType, **props) -> None:
        key = (src, tgt, edge_type)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append(GraphEdgeRecord(source=src, target=tgt, edge_type=edge_type, properties=props))

    t0 = time.time()
    logger.info(
        "[kb_graph_builder] ── Stage: Build Graph ──  clusters=%d  sections=%d  spec_records=%d  test_cases=%d",
        len(canonical_schema.clusters),
        len(section_records or []),
        len(spec_records),
        len(test_case_records),
    )

    # ── Layer 0: canonical schema nodes ──────────────────────────────────────
    logger.info("[kb_graph_builder] Layer 0 — DM XML schema nodes (%d clusters)…", len(canonical_schema.clusters))
    t1 = time.time()
    _add_schema_layer(canonical_schema, add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 0 done — %d nodes, %d edges  (%.1fs)",
        len(nodes), len(edges), time.time() - t1,
    )

    # ── Layer 1a: spec section nodes ─────────────────────────────────────────
    _sections = section_records or []
    logger.info("[kb_graph_builder] Layer 1a — Spec section nodes (%d sections)…", len(_sections))
    t1 = time.time()
    nodes_before, edges_before = len(nodes), len(edges)
    _add_section_layer(_sections, add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 1a done — +%d nodes, +%d edges  (%.1fs)",
        len(nodes) - nodes_before, len(edges) - edges_before, time.time() - t1,
    )

    # ── Layer 1b: spec requirement / behavior-rule nodes ─────────────────────
    logger.info("[kb_graph_builder] Layer 1b — Spec requirement nodes (%d records)…", len(spec_records))
    t1 = time.time()
    nodes_before, edges_before = len(nodes), len(edges)
    _add_spec_layer(spec_records, canonical_schema, add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 1b done — +%d nodes, +%d edges  (%.1fs)",
        len(nodes) - nodes_before, len(edges) - edges_before, time.time() - t1,
    )

    # ── Layer 1c: protocol-area backbone ─────────────────────────────────────
    logger.info("[kb_graph_builder] Layer 1c — Protocol-area backbone…")
    t1 = time.time()
    nodes_before, edges_before = len(nodes), len(edges)
    _add_protocol_area_layer(_sections, spec_records, canonical_schema, add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 1c done — +%d nodes, +%d edges  (%.1fs)",
        len(nodes) - nodes_before, len(edges) - edges_before, time.time() - t1,
    )

    # ── Layer 1d: behavior nodes ──────────────────────────────────────────────
    logger.info("[kb_graph_builder] Layer 1d — Behavior nodes…")
    t1 = time.time()
    nodes_before, edges_before = len(nodes), len(edges)
    _add_behavior_layer(spec_records, add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 1d done — +%d nodes, +%d edges  (%.1fs)",
        len(nodes) - nodes_before, len(edges) - edges_before, time.time() - t1,
    )

    # ── Layer 1e: virtual cluster nodes for protocol TC families ─────────────
    # Must run after Layer 1c so PROTOCOL_AREA target nodes exist.
    logger.info("[kb_graph_builder] Layer 1e — VirtualCluster nodes for protocol TC families…")
    t1 = time.time()
    nodes_before = len(nodes)
    _add_virtual_cluster_layer(add_node, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 1e done — +%d VirtualCluster nodes  (%.1fs)",
        len(nodes) - nodes_before, time.time() - t1,
    )

    # ── Layer 2: test case nodes ──────────────────────────────────────────────
    logger.info("[kb_graph_builder] Layer 2 — Test case nodes (%d TCs)…", len(test_case_records))
    t1 = time.time()
    nodes_before, edges_before = len(nodes), len(edges)
    pics_prefix_to_cluster = {c.pics_code: c.name for c in canonical_schema.clusters if c.pics_code}
    # Build sorted cluster name list for prose-based cluster detection in TC step text.
    # Sorted longest-first so specific names (e.g. "On/Off Switch Configuration") are
    # matched before shorter overlapping names (e.g. "On/Off").
    _sorted_cluster_names = sorted(
        [(c.name, f"CLUSTER::{c.name}") for c in canonical_schema.clusters],
        key=lambda x: len(x[0]),
        reverse=True,
    )
    _add_test_case_layer(
        test_case_records, spec_records, canonical_schema.entity_lookup,
        add_node, add_edge, pics_prefix_to_cluster,
        cluster_name_list=_sorted_cluster_names,
        cluster_lookup=canonical_schema.cluster_lookup,
        all_nodes=nodes,
    )
    logger.info(
        "[kb_graph_builder] Layer 2 done — +%d nodes, +%d edges  (%.1fs)",
        len(nodes) - nodes_before, len(edges) - edges_before, time.time() - t1,
    )

    # ── Layer 2b: deferred VirtualCluster node creation ─────────────────────
    # _add_test_case_layer may assign primary_cluster = "VirtualCluster-{prefix}"
    # for ANY prefix where _is_protocol_prefix() returns True.  Layer 1e only
    # creates VirtualCluster nodes for prefixes listed in _PROTOCOL_TC_TO_AREA_SLUG.
    # This pass finds VirtualCluster edge targets that don't have a corresponding
    # node and creates them, so no TESTS edge has a dangling target.
    t1 = time.time()
    _vc_prefix = "CLUSTER::VirtualCluster-"
    missing_vc: Set[str] = set()
    for edge_rec in edges:
        if edge_rec.target.startswith(_vc_prefix) and edge_rec.target not in nodes:
            missing_vc.add(edge_rec.target)
    for vc_id in sorted(missing_vc):
        prefix = vc_id[len(_vc_prefix):]
        area_slug = _PROTOCOL_TC_TO_AREA_SLUG.get(prefix, prefix)
        add_node(GraphNodeRecord(
            node_id=vc_id,
            node_type=GraphNodeType.CLUSTER,
            label=f"VirtualCluster-{prefix}",
            properties={
                "name": f"VirtualCluster-{prefix}",
                "virtual": True,
                "protocol_area": area_slug,
                "source": "virtual",
                "doc_type": "virtual",
            },
        ))
        # Link to PROTOCOL_AREA if one exists for this slug.
        area_id = f"PROTOCOL_AREA::{area_slug}"
        add_edge(vc_id, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)
        logger.info(
            "[kb_graph_builder] Deferred VirtualCluster created: %s (area=%s)",
            vc_id, area_slug,
        )
    if missing_vc:
        logger.info(
            "[kb_graph_builder] Layer 2b — created %d deferred VirtualCluster nodes  (%.1fs)",
            len(missing_vc), time.time() - t1,
        )

    # ── Layer 3: cross-reference edges between SECTION nodes ────────────────
    logger.info("[kb_graph_builder] Layer 3 — Section cross-reference edges…")
    t1 = time.time()
    edges_before = len(edges)
    _add_section_crossref_edges(nodes, add_edge)
    logger.info(
        "[kb_graph_builder] Layer 3 done — +%d cross-reference edges  (%.1fs)",
        len(edges) - edges_before, time.time() - t1,
    )

    bundle = GraphBundle(nodes=list(nodes.values()), edges=edges)
    logger.info(
        "[kb_graph_builder] ── Done ── %d total nodes, %d total edges  (%.1fs)",
        len(bundle.nodes), len(bundle.edges), time.time() - t0,
    )
    return bundle


# ---------------------------------------------------------------------------
# Layer builders
# ---------------------------------------------------------------------------

_SECTION_REF_RE = re.compile(
    r'(?:(?:[Ss]ee|[Ss]ection|[Cc]lause|[Rr]efer(?:\s+to)?)\s+)'
    r'(\d+(?:\.\d+)+)',
)


def _add_section_crossref_edges(
    nodes: Dict[str, "GraphNodeRecord"],
    add_edge,
) -> None:
    """Detect cross-references in SECTION full_text and create REFERENCES edges.

    Parses patterns like "See Section 4.3.7", "refer to 7.3.2", "clause 11.7.1.8".
    Matches references to existing SECTION nodes by section_path number prefix.
    Skips self-references and limits to avoid noise from heavily-referenced sections.
    """
    section_nodes = {
        nid: rec for nid, rec in nodes.items()
        if rec.node_type == GraphNodeType.SECTION
    }
    if not section_nodes:
        return

    # Build index: section number prefix → node_id
    # e.g., "4.3.7" → "SECTION::4.3. Thermostat Cluster > 4.3.7. Attributes"
    num_to_id: Dict[str, str] = {}
    for nid, rec in section_nodes.items():
        path = rec.properties.get("section_path", "") or ""
        # Extract leading number from last segment: "4.3.7. Attributes" → "4.3.7"
        parts = path.split(">")
        for part in parts:
            part = part.strip()
            m = re.match(r'^(\d+(?:\.\d+)*)', part)
            if m:
                num_to_id.setdefault(m.group(1), nid)

    # For each section, find references to other sections
    _MAX_REFS_PER_SECTION = 10
    total_added = 0
    for nid, rec in section_nodes.items():
        text = rec.properties.get("full_text", "") or ""
        if not text:
            continue

        refs_added = 0
        seen_targets: set = set()
        for m in _SECTION_REF_RE.finditer(text):
            if refs_added >= _MAX_REFS_PER_SECTION:
                break
            ref_num = m.group(1)
            target_id = num_to_id.get(ref_num)
            if not target_id or target_id == nid or target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            add_edge(nid, target_id, GraphEdgeType.REFERENCES)
            refs_added += 1
            total_added += 1

    logger.debug("[_add_section_crossref_edges] %d cross-reference edges created", total_added)


def _add_section_layer(
    section_records: List[SectionRecord],
    add_node,
    add_edge,
) -> None:
    """Add SECTION nodes and link cluster sections to their CLUSTER node."""
    for sec in section_records:
        add_node(GraphNodeRecord(
            node_id=sec.id,
            node_type=GraphNodeType.SECTION,
            label=sec.title or sec.id,
            properties={
                "title": sec.title,
                "cluster": sec.cluster,
                "section_path": sec.section_path,
                "source_doc": sec.source_doc,
                "doc_type": "spec",
                "full_text": sec.full_text[:6000] if sec.full_text else "",
            },
        ))
        # If this section corresponds to a cluster → link via REFERENCES
        if sec.cluster_id:
            add_edge(sec.id, sec.cluster_id, GraphEdgeType.REFERENCES)


# ---------------------------------------------------------------------------
# Cross-cluster dependency helpers
# ---------------------------------------------------------------------------

# Matches "Dependency with the On/Off cluster" or "Dependency with OnOff"
_DEP_SUMMARY_RE = re.compile(
    r'[Dd]ependency with (?:the )?(.+?)(?:\s+[Cc]luster)?\s*\.?\s*$'
)


def _extract_dep_cluster_hint(entity: CanonicalEntityRef) -> Optional[str]:
    """Return a target-cluster name hint for a cross-cluster dependency FEATURE, or None.

    Checks ``entity.summary`` first (e.g. "Dependency with the On/Off cluster" → "On/Off"),
    then falls back to ``entity.code_short`` (e.g. "DEPONOFF" → "ONOFF").
    Returns None when the feature does not describe a cross-cluster dependency.
    """
    if entity.summary:
        m = _DEP_SUMMARY_RE.search(entity.summary.strip())
        if m:
            return m.group(1).strip()
    cs = (entity.code_short or "").upper()
    if cs.startswith("DEP") and len(cs) > 3:
        return cs[3:]  # e.g. "ONOFF", "ONOFFLIT"
    return None


def _norm_name(name: str) -> str:
    """Strip all non-alphanumeric chars and lowercase — used for fuzzy cluster matching."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _find_dep_cluster_id(hint: str, schema: CanonicalSchema) -> Optional[str]:
    """Return the canonical cluster ID whose name best matches ``hint``.

    Normalises both hint and candidate cluster names by stripping punctuation
    (spaces, slashes, hyphens) so "On/Off" == "OnOff" == "on/off cluster" after
    stripping the trailing "cluster" word.

    Preference order:
    1. Exact match after normalisation (e.g. "onoff" == "onoff")
    2. Either string starts with the other (e.g. "onoff" in "onoffcluster")
    3. No match → None
    """
    hint_norm = _norm_name(hint)
    hint_stripped = re.sub(r'cluster$', '', hint_norm)

    best_id: Optional[str] = None
    best_score = 0
    for cluster in schema.clusters:
        cname_norm = _norm_name(cluster.name)
        cname_stripped = re.sub(r'cluster$', '', cname_norm)

        # Exact match after normalisation
        if hint_stripped == cname_stripped or hint_norm == cname_norm:
            return cluster.id  # perfect — return immediately

        # Prefix / containment match
        if cname_stripped.startswith(hint_stripped) or hint_stripped.startswith(cname_stripped):
            score = len(hint_stripped)
            if score > best_score:
                best_score = score
                best_id = cluster.id

    return best_id


def _add_virtual_cluster_layer(add_node, add_edge) -> None:
    """Create virtual CLUSTER nodes for protocol TC families (IDM, SC, DD, etc.).

    These nodes get `VirtualCluster-` prefix names so they are easily distinguishable
    from real DM cluster nodes and can be queried together (e.g. all virtual clusters).
    Protocol TCs receive TESTS edges pointing to these nodes, making them findable
    via the same KG search path used for regular cluster TCs.
    """
    created: set = set()
    for prefix, area_slug in _PROTOCOL_TC_TO_AREA_SLUG.items():
        vc_id = f"CLUSTER::VirtualCluster-{prefix}"
        if vc_id in created:
            continue
        created.add(vc_id)
        add_node(GraphNodeRecord(
            node_id=vc_id,
            node_type=GraphNodeType.CLUSTER,
            label=f"VirtualCluster-{prefix}",
            properties={
                "name": f"VirtualCluster-{prefix}",
                "virtual": True,
                "protocol_area": area_slug,
                "source": "virtual",
                "doc_type": "virtual",
            },
        ))
        # Link virtual cluster to its PROTOCOL_AREA node (created by Layer 1c).
        area_id = f"PROTOCOL_AREA::{area_slug}"
        add_edge(vc_id, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)
        logger.debug("[kb_graph_builder] VirtualCluster created: %s → %s", vc_id, area_id)


def _add_schema_layer(
    schema: CanonicalSchema,
    add_node,
    add_edge,
) -> None:
    """Add CLUSTER + entity nodes and HAS_* edges."""
    for cluster in schema.clusters:
        add_node(GraphNodeRecord(
            node_id=cluster.id,
            node_type=GraphNodeType.CLUSTER,
            label=cluster.name,
            properties={
                "name": cluster.name,
                "code": cluster.code,
                "revision": cluster.revision,
                "pics_code": cluster.pics_code,
                "hierarchy": cluster.hierarchy,
                "base_cluster": cluster.base_cluster,
                "source": "data_model",
                "doc_type": "data_model",
            },
        ))
        for entity in cluster.entities:
            _add_entity_node(entity, add_node)
            edge_type = _entity_to_has_edge(entity.entity_type)
            add_edge(cluster.id, entity.id, edge_type)

            # Emit CLUSTER →[DEPENDS_ON]→ CLUSTER for cross-cluster dependency features.
            # These are FEATURE nodes whose summary reads "Dependency with the On/Off cluster"
            # (or whose code_short starts with "DEP", e.g. "DEPONOFF").
            if entity.entity_type == EntityType.FEATURE:
                hint = _extract_dep_cluster_hint(entity)
                if hint:
                    dep_id = _find_dep_cluster_id(hint, schema)
                    if dep_id and dep_id != cluster.id:
                        add_edge(cluster.id, dep_id, GraphEdgeType.DEPENDS_ON)
                        logger.debug(
                            "cross-cluster DEPENDS_ON: %s → %s  (feature=%s, hint=%r)",
                            cluster.id, dep_id, entity.id, hint,
                        )

        if cluster.hierarchy == "alias" and cluster.base_cluster:
            base_id = f"CLUSTER::{cluster.base_cluster}"
            add_edge(cluster.id, base_id, GraphEdgeType.ALIAS_OF)
            logger.debug("ALIAS_OF: %s → %s", cluster.id, base_id)


def _add_entity_node(entity: CanonicalEntityRef, add_node) -> None:
    gtype = _entity_type_to_graph_node_type(entity.entity_type)
    props = {
        "name": entity.name,
        "cluster": entity.cluster,
        "code": entity.code,
        "conformance": entity.conformance,
        "source": "data_model",
        "doc_type": "data_model",
    }
    if entity.entity_type == EntityType.ATTRIBUTE:
        props["datatype"] = entity.datatype
        props["access"] = entity.access
        props["default"] = entity.default
    elif entity.entity_type == EntityType.COMMAND:
        props["direction"] = entity.direction
        props["response"] = entity.response
        props["access"] = entity.access
    elif entity.entity_type == EntityType.EVENT:
        props["priority"] = entity.priority
    elif entity.entity_type == EntityType.FEATURE:
        props["bit"] = entity.bit
        props["code_short"] = entity.code_short
        props["summary"] = entity.summary

    add_node(GraphNodeRecord(
        node_id=entity.id,
        node_type=gtype,
        label=entity.name,
        properties=props,
    ))


def _add_spec_layer(
    spec_records: List[SpecRecord],
    schema: CanonicalSchema,
    add_node,
    add_edge,
) -> None:
    """Add REQUIREMENT or BEHAVIOR_RULE nodes linked to schema entities and parent sections."""
    for rec in spec_records:
        req_type = rec.requirement_type.value
        # conditional_behavior_rule → BEHAVIOR_RULE node; all others → REQUIREMENT
        if rec.requirement_type == RequirementType.CONDITIONAL_BEHAVIOR_RULE:
            node_type = GraphNodeType.BEHAVIOR_RULE
        else:
            node_type = GraphNodeType.REQUIREMENT
        add_node(GraphNodeRecord(
            node_id=rec.id,
            node_type=node_type,
            label=rec.normative_text[:80],
            properties={
                "requirement_type": req_type,
                "cluster": rec.cluster,
                "normative_text": rec.normative_text,
                "context_text": rec.context_text,
                "section_path": rec.section_path,
                "source_doc": rec.source_doc,
                "confidence": rec.confidence,
                "ambiguous": rec.ambiguous,
                "signals": rec.signals,
                "alternatives": rec.alternatives,
                "doc_type": "spec",
            },
        ))
        # REQUIREMENT → parent SECTION
        if rec.section_id:
            add_edge(rec.id, rec.section_id, GraphEdgeType.BELONGS_TO)
        # REQUIREMENT → entity (implements)
        for entity_ref in rec.entity_refs:
            add_edge(rec.id, entity_ref, GraphEdgeType.IMPLEMENTS)
        # Fallback: when no fine-grained entity refs were matched but the cluster
        # is known from section context, link to the cluster node.  This ensures
        # requirements that describe cluster-level behaviour (e.g. protocol
        # semantics sentences that don't name a specific attribute) are still
        # reachable from their cluster in a 1-hop query.
        if not rec.entity_refs and rec.cluster:
            cluster_node_id = f"CLUSTER::{rec.cluster}"
            add_edge(rec.id, cluster_node_id, GraphEdgeType.IMPLEMENTS)


def _build_adoc_section(tc) -> str:
    """Construct a minimal adoc section text from a TestCaseRecord.

    Used to populate ``adoc_section`` in KG TC node properties so that
    Section D of the LLM analysis prompt can show real test steps.
    """
    dut_suffix = f" [DUT as {tc.dut_type}]" if getattr(tc, "dut_type", "") else ""
    lines = [f"== {tc.id}{dut_suffix}"]
    if tc.purpose:
        lines += ["\n=== Purpose", tc.purpose]
    pics = getattr(tc, "pics_codes", [])
    if pics:
        lines.append("\n=== PICS")
        lines.extend(f"* {p}" for p in pics[:20])
    prereqs = getattr(tc, "prerequisites", "") or getattr(tc, "setup", "")
    if prereqs:
        lines += ["\n=== Prerequisites", prereqs[:400]]
    steps = getattr(tc, "procedure_steps", [])
    if steps:
        lines.append("\n=== Test Steps")
        lines.extend(f"{i + 1}. {s}" for i, s in enumerate(steps[:20]))
    return "\n".join(lines)[:2500]


def _add_test_case_layer(
    test_case_records: List[TestCaseRecord],
    spec_records: List[SpecRecord],
    entity_lookup: dict,
    add_node,
    add_edge,
    pics_prefix_to_cluster: dict = None,
    cluster_name_list: List[tuple] = None,
    cluster_lookup: dict = None,
    all_nodes: Dict[str, GraphNodeRecord] = None,
) -> None:
    """Add TEST_CASE nodes and link them to clusters, entities, and spec refs (typed edges)."""
    pics_prefix_to_cluster = pics_prefix_to_cluster or {}
    for tc in test_case_records:
        # ── Cross-cluster detection via PICS codes ────────────────────────────
        # PICS prefix (e.g. "OO" from "OO.S.A0000") maps unambiguously to a cluster.
        related_clusters: List[str] = []
        seen_clusters: set = set()
        for pics in tc.pics_codes:
            prefix = pics.split(".")[0].upper()
            if prefix and prefix not in seen_clusters:
                cluster_name = pics_prefix_to_cluster.get(prefix)
                if cluster_name:
                    seen_clusters.add(prefix)
                    related_clusters.append(cluster_name)

        # Override primary cluster using TC-ID prefix → PICS code → cluster name.
        # The TC-ID prefix (e.g. "BRBINFO" from "TC-BRBINFO-2.1") is authoritative:
        # it matches the cluster's picsCode in the DM XML, making this a more reliable
        # signal than section-heading substring matching which can pick a wrong neighbor
        # (e.g. "Thread Network Diagnostics" leaking into TC-CHANNEL or TC-CNET).
        # Guard: skip protocol-family TCs (ACE, BDX, DA, etc.) — their prefix appears in
        # the DM XML PICS map by coincidence (ACE → Access Control Cluster) but these TCs
        # are protocol-level and intentionally have no primary cluster.
        tc_id_prefix = tc.id.split("-")[1].upper() if tc.id.startswith("TC-") else ""
        primary_cluster = tc.cluster
        # Hard-clear cluster for protocol-family TCs regardless of what the extractor inferred.
        # (The extractor's _infer_cluster may assign a wrong cluster from incidental entity refs
        # when the regex guard fails on bare TC IDs.)
        if tc_id_prefix and _is_protocol_prefix(tc_id_prefix):
            if primary_cluster and not primary_cluster.startswith("VirtualCluster-"):
                logger.debug(
                    "[kb_graph_builder] %s stripping cluster %r — protocol TC prefix %s",
                    tc.id, primary_cluster, tc_id_prefix,
                )
            # Assign a virtual cluster so the TC gets a TESTS edge and is findable in the KG.
            primary_cluster = f"VirtualCluster-{tc_id_prefix}"
        elif tc_id_prefix and tc_id_prefix in pics_prefix_to_cluster:
            pics_derived = pics_prefix_to_cluster[tc_id_prefix]
            if pics_derived != tc.cluster:
                logger.debug(
                    "[kb_graph_builder] %s cluster corrected: %r → %r (PICS prefix %s)",
                    tc.id, tc.cluster, pics_derived, tc_id_prefix,
                )
            primary_cluster = pics_derived

        # ── Cross-cluster detection via entity refs ───────────────────────────
        # tc.entity_refs contains canonical IDs like "ATTRIBUTE::Scenes::SceneCount"
        # matched from TC text by test_plan_extractor.  Looking up each ref in
        # entity_lookup gives its owning cluster — far more accurate than substring
        # matching cluster names in prose (which causes "actions" to match every TC
        # that says "perform the following actions").
        #
        # Guard 1: if the entity's simple name (last segment of the ID, e.g. "Mode")
        # also exists in the primary cluster's own schema, the match is ambiguous —
        # skip it to prevent phantom TESTS edges (e.g. Mode-cluster TCs → Window Covering).
        #
        # Guard 2: when TC has PICS codes (seen_clusters is non-empty), only accept
        # entity-ref derived cross-cluster additions that are PICS-confirmed. This
        # prevents spec-template-shared entity refs (e.g. shared Mode base cluster
        # referencing Window Covering) from creating spurious TESTS edges.
        primary_entity_names: set = set()
        if primary_cluster and cluster_lookup:
            pcluster_rec = cluster_lookup.get(primary_cluster.lower())
            if pcluster_rec:
                for e in pcluster_rec.entities:
                    primary_entity_names.add(e.id.rsplit("::", 1)[-1].lower())

        # Build reverse map: cluster_name (lower) → PICS prefix for PICS validation guard.
        pics_confirmed_names: set = {c.lower() for c in related_clusters}  # already PICS-derived

        already_claimed: set = {c.lower() for c in related_clusters}
        if primary_cluster:
            already_claimed.add(primary_cluster.lower())
        # Reverse map used by Guard 2: cluster_name → pics_prefix
        _cluster_name_to_pics = {v.lower(): k for k, v in pics_prefix_to_cluster.items()}

        entity_added = 0
        for ref in tc.entity_refs:
            if ref.startswith("CLUSTER::"):
                # Direct cluster reference resolved by entity_name_map
                cname = ref[len("CLUSTER::"):]
            else:
                entity = entity_lookup.get(ref)
                cname = entity.cluster if entity else ""
                # Guard 1: skip if this entity's simple name is shared with primary cluster
                if entity and primary_entity_names:
                    simple = entity.id.rsplit("::", 1)[-1].lower()
                    if simple in primary_entity_names:
                        logger.debug(
                            "[kb_graph_builder] %s skipping ambiguous entity ref %s "
                            "(simple name %r shared with primary cluster %r)",
                            tc.id, ref, simple, primary_cluster,
                        )
                        continue
            if cname and cname.lower() not in already_claimed:
                # Guard 2: when TC has PICS codes, require the cross-cluster ref to be
                # PICS-confirmed (its prefix must appear in seen_clusters).
                if seen_clusters and cname.lower() not in pics_confirmed_names:
                    cname_pics_prefix = _cluster_name_to_pics.get(cname.lower(), "").upper()
                    if not cname_pics_prefix or cname_pics_prefix not in seen_clusters:
                        logger.debug(
                            "[kb_graph_builder] %s skipping entity-ref cluster %r "
                            "— not confirmed by PICS codes (seen_clusters=%s)",
                            tc.id, cname, seen_clusters,
                        )
                        continue
                already_claimed.add(cname.lower())
                related_clusters.append(cname)
                entity_added += 1
        if entity_added:
            logger.debug(
                "[kb_graph_builder] %s entity-detected %d cross-cluster refs: %s",
                tc.id, entity_added,
                related_clusters[-entity_added:],
            )

        add_node(GraphNodeRecord(
            node_id=tc.id,
            node_type=GraphNodeType.TEST_CASE,
            label=tc.title or tc.id,
            properties={
                "tc_id": tc.id,
                "cluster": primary_cluster,
                "related_clusters": related_clusters,
                "mode": tc.mode.value,
                "intents": [i.value for i in tc.intents],
                "purpose": tc.purpose,
                "source_doc": tc.source_doc,
                "doc_type": "test_plan",
                "pics_codes": tc.pics_codes,
                "entity_refs": tc.entity_refs,
                "step_keywords": _extract_step_keywords(tc.procedure_steps),
                "adoc_section": _build_adoc_section(tc),
            },
        ))
        # Create TESTS edges to every cluster this TC exercises (primary + cross-cluster)
        all_tc_clusters = list(dict.fromkeys(
            [primary_cluster] + related_clusters if primary_cluster else related_clusters
        ))
        for cname in all_tc_clusters:
            if cname:
                add_edge(tc.id, f"CLUSTER::{cname}", GraphEdgeType.TESTS)

        # Typed edges via rule engine — also collects any PICS:: virtual node refs
        pics_ids: set = set()
        for edge in infer_graph_edges(tc, spec_records, entity_lookup):
            et = edge.edge_type
            if et == GraphEdgeType.VERIFIES_REQUIREMENT and all_nodes:
                tgt_node = all_nodes.get(edge.target)
                if tgt_node and tgt_node.node_type == GraphNodeType.BEHAVIOR_RULE:
                    et = GraphEdgeType.VERIFIES_RULE
            add_edge(edge.source, edge.target, et, **edge.properties)
            if edge.target.startswith("PICS::"):
                pics_ids.add(edge.target)

        # Create virtual PICS nodes so no edge has a dangling target
        for pics_id in pics_ids:
            pics_code = pics_id.split("::", 1)[1] if "::" in pics_id else pics_id
            add_node(GraphNodeRecord(
                node_id=pics_id,
                node_type=GraphNodeType.PICS_ITEM,   # PICS conditions are test-gating items
                label=pics_code,
                properties={"pics_code": pics_code, "doc_type": "pics"},
            ))

        # For protocol-family TCs: link to PROTOCOL_AREA instead of any cluster.
        # PROTOCOL_AREA nodes are created by _add_protocol_area_layer (Layer 1c) before
        # this layer runs, so add_node here is only a safety net when spec is absent.
        if tc_id_prefix and tc_id_prefix not in pics_prefix_to_cluster:
            area_slug = _PROTOCOL_TC_TO_AREA_SLUG.get(tc_id_prefix)
            if area_slug:
                area_id = f"PROTOCOL_AREA::{area_slug}"
                add_node(GraphNodeRecord(
                    node_id=area_id,
                    node_type=GraphNodeType.PROTOCOL_AREA,
                    label=area_slug.replace("_", " "),
                    properties={"doc_type": "spec"},
                ))
                add_edge(tc.id, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)
                logger.debug(
                    "[kb_graph_builder] %s linked to %s (prefix %s)",
                    tc.id, area_id, tc_id_prefix,
                )


# ---------------------------------------------------------------------------
# Enum mapping helpers
# ---------------------------------------------------------------------------

def _entity_type_to_graph_node_type(et: EntityType) -> GraphNodeType:
    return {
        EntityType.CLUSTER:   GraphNodeType.CLUSTER,
        EntityType.ATTRIBUTE: GraphNodeType.ATTRIBUTE,
        EntityType.COMMAND:   GraphNodeType.COMMAND,
        EntityType.EVENT:     GraphNodeType.EVENT,
        EntityType.FEATURE:   GraphNodeType.FEATURE,
    }.get(et, GraphNodeType.SECTION)


def _entity_to_has_edge(et: EntityType) -> GraphEdgeType:
    return {
        EntityType.ATTRIBUTE: GraphEdgeType.HAS_ATTRIBUTE,
        EntityType.COMMAND:   GraphEdgeType.HAS_COMMAND,
        EntityType.EVENT:     GraphEdgeType.HAS_EVENT,
        EntityType.FEATURE:   GraphEdgeType.HAS_FEATURE,
    }.get(et, GraphEdgeType.RELATED_TO)


# ---------------------------------------------------------------------------
# Protocol-area backbone
# ---------------------------------------------------------------------------

def _add_protocol_area_layer(
    section_records: List[SectionRecord],
    spec_records: List[SpecRecord],
    schema: CanonicalSchema,
    add_node,
    add_edge,
) -> None:
    """Create PROTOCOL_AREA nodes from section breadcrumbs and link requirements to them.

    Each unique PROTOCOL_AREA id extracted from a section_path or spec_record
    section_path becomes one PROTOCOL_AREA node. Requirements and cluster-section
    nodes are then linked via ``BELONGS_TO_PROTOCOL_AREA`` edges.
    """
    area_labels: Dict[str, str] = {}

    def _ensure_area(area_id: str) -> None:
        if area_id not in area_labels:
            label = area_id.replace("PROTOCOL_AREA::", "").replace("_", " ").title()
            area_labels[area_id] = label
            add_node(GraphNodeRecord(
                node_id=area_id,
                node_type=GraphNodeType.PROTOCOL_AREA,
                label=label,
                properties={"doc_type": "spec"},
            ))

    # From sections — each section path part becomes a protocol area
    for sec in section_records:
        for area_id in extract_protocol_areas(sec.section_path):
            _ensure_area(area_id)
            add_edge(sec.id, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)

    # From spec records — link requirements to their protocol areas
    for rec in spec_records:
        for area_id in extract_protocol_areas(rec.section_path):
            _ensure_area(area_id)
            add_edge(rec.id, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)

    # From schema clusters — link cluster nodes to protocol areas inferred from
    # any section that names the cluster
    cluster_names_lower = {c.name.lower(): c.id for c in schema.clusters}
    linked_clusters: set = set()
    for sec in section_records:
        for area_id in extract_protocol_areas(sec.section_path):
            for cname_lower, cid in cluster_names_lower.items():
                if cname_lower in sec.section_path.lower() and cid not in linked_clusters:
                    linked_clusters.add(cid)
                    add_edge(cid, area_id, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA)


# ---------------------------------------------------------------------------
# Behavior backbone
# ---------------------------------------------------------------------------

def _add_behavior_layer(
    spec_records: List[SpecRecord],
    add_node,
    add_edge,
) -> None:
    """Create BEHAVIOR nodes from hint extraction and link requirements to them.

    ``extract_behavior_hints()`` returns canonical behavior-name strings
    (e.g. ``"Power_Cycling"``, ``"Commissioning_Mode_Entry"``).  Each unique
    hint becomes one ``BEHAVIOR`` node; requirements that exhibit the hint are
    linked via ``HAS_BEHAVIOR_RULE`` edges.
    """
    behavior_reqs: Dict[str, List[str]] = {}

    for rec in spec_records:
        for hint in extract_behavior_hints(rec.normative_text):
            behavior_reqs.setdefault(hint, []).append(rec.id)

    for hint, req_ids in behavior_reqs.items():
        behavior_id = f"BEHAVIOR::{hint}"
        add_node(GraphNodeRecord(
            node_id=behavior_id,
            node_type=GraphNodeType.BEHAVIOR,
            label=hint.replace("_", " "),
            properties={"doc_type": "spec", "hint": hint},
        ))
        for req_id in req_ids:
            add_edge(behavior_id, req_id, GraphEdgeType.HAS_BEHAVIOR_RULE)


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------

def validate_graph(
    bundle: GraphBundle,
    rejected_candidates: Optional[List[RejectedCandidate]] = None,
) -> ValidationReport:
    """Validate the graph bundle and return a quality report.

    Checks performed:
    1. Orphan requirements   — REQUIREMENT/BEHAVIOR_RULE with no outgoing IMPLEMENTS edge
    2. Orphan test cases     — TEST_CASE with no TESTS/COVERS/VALIDATES/VERIFIES_* edges
    3. Dangling edges        — edges whose source or target node is absent
    4. Requirements without protocol area — nodes missing BELONGS_TO_PROTOCOL_AREA
    5. Test cases with no spec links    — TEST_CASE with no COVERS/VALIDATES/VERIFIES_REQUIREMENT edge
    """
    report = ValidationReport(
        total_nodes=len(bundle.nodes),
        total_edges=len(bundle.edges),
        rejected_candidates=rejected_candidates or [],
    )

    node_ids: Set[str] = {n.node_id for n in bundle.nodes}
    node_type_map: Dict[str, GraphNodeType] = {n.node_id: n.node_type for n in bundle.nodes}

    # Index edges by source
    edges_from: Dict[str, List[GraphEdgeRecord]] = {}
    for e in bundle.edges:
        edges_from.setdefault(e.source, []).append(e)

    # 1. Orphan requirements / behavior rules
    req_types = {GraphNodeType.REQUIREMENT, GraphNodeType.BEHAVIOR_RULE}
    implements_targets: Set[str] = {
        e.target for e in bundle.edges if e.edge_type == GraphEdgeType.IMPLEMENTS
    }
    for n in bundle.nodes:
        if n.node_type in req_types:
            # Orphan if: no IMPLEMENTS edge AND not linked to a SECTION
            outgoing = edges_from.get(n.node_id, [])
            has_implements = any(e.edge_type == GraphEdgeType.IMPLEMENTS for e in outgoing)
            has_belongs = any(e.edge_type in (
                GraphEdgeType.BELONGS_TO, GraphEdgeType.BELONGS_TO_PROTOCOL_AREA
            ) for e in outgoing)
            if not has_implements and not has_belongs:
                report.orphan_requirements.append(n.node_id)

    # 2. Orphan test cases
    _tc_link_types = {
        GraphEdgeType.TESTS, GraphEdgeType.COVERS, GraphEdgeType.VALIDATES,
        GraphEdgeType.VERIFIES_ATTRIBUTE, GraphEdgeType.TESTS_COMMAND,
        GraphEdgeType.OBSERVES_EVENT, GraphEdgeType.VERIFIES_REQUIREMENT,
        GraphEdgeType.VERIFIES_RULE,
    }
    for n in bundle.nodes:
        if n.node_type == GraphNodeType.TEST_CASE:
            outgoing = edges_from.get(n.node_id, [])
            if not any(e.edge_type in _tc_link_types for e in outgoing):
                report.orphan_test_cases.append(n.node_id)

    # 3. Dangling edges
    for e in bundle.edges:
        if e.source not in node_ids:
            report.invalid_edges.append(f"dangling source: {e.source} -[{e.edge_type}]-> {e.target}")
        if e.target not in node_ids:
            report.invalid_edges.append(f"dangling target: {e.source} -[{e.edge_type}]-> {e.target}")

    # 4. Requirements without protocol area link
    has_protocol_area: Set[str] = {
        e.source for e in bundle.edges
        if e.edge_type == GraphEdgeType.BELONGS_TO_PROTOCOL_AREA
    }
    for n in bundle.nodes:
        if n.node_type in req_types and n.node_id not in has_protocol_area:
            report.requirements_without_protocol_area.append(n.node_id)

    # 5. Test cases with no spec links
    _spec_link_types = {
        GraphEdgeType.COVERS, GraphEdgeType.VALIDATES,
        GraphEdgeType.VERIFIES_REQUIREMENT, GraphEdgeType.VERIFIES_RULE,
    }
    for n in bundle.nodes:
        if n.node_type == GraphNodeType.TEST_CASE:
            outgoing = edges_from.get(n.node_id, [])
            if not any(e.edge_type in _spec_link_types for e in outgoing):
                report.test_cases_with_no_links.append(n.node_id)

    # Summary warnings
    if report.orphan_requirements:
        report.warnings.append(
            f"{len(report.orphan_requirements)} orphan requirements (no entity/section link)"
        )
    if report.orphan_test_cases:
        report.warnings.append(
            f"{len(report.orphan_test_cases)} orphan test cases (no entity link)"
        )
    if report.invalid_edges:
        report.warnings.append(
            f"{len(report.invalid_edges)} dangling edge endpoints"
        )
    if report.requirements_without_protocol_area:
        report.warnings.append(
            f"{len(report.requirements_without_protocol_area)} requirements lack a protocol-area link"
        )
    if report.test_cases_with_no_links:
        report.warnings.append(
            f"{len(report.test_cases_with_no_links)} test cases have no spec requirement links"
        )

    logger.info(
        "[kb_graph_builder] validate_graph — nodes=%d  edges=%d  orphan_reqs=%d  orphan_tcs=%d  "
        "dangling_edges=%d  reqs_no_area=%d  tcs_no_spec=%d  rejected_candidates=%d",
        report.total_nodes, report.total_edges,
        len(report.orphan_requirements), len(report.orphan_test_cases),
        len(report.invalid_edges), len(report.requirements_without_protocol_area),
        len(report.test_cases_with_no_links), len(report.rejected_candidates),
    )
    return report
