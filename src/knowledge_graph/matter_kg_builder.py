"""Matter protocol knowledge graph builder — NetworkX implementation.

Builds and queries a directed knowledge graph specifically for the Matter
(formerly Project CHIP) connectivity standard.  Documents from the Matter
specification, test plans, and PR changes are ingested as typed nodes;
Matter-specific entities (clusters, commands, attributes) are extracted
and linked.

In the future, analogous builders (``bluetooth_kg_builder.py``,
``thread_kg_builder.py``, ``mdns_kg_builder.py`` …) will implement the
same ``BaseKnowledgeGraph`` interface and can be merged into a single
unified multi-protocol knowledge graph.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from src.config.config_loader import KnowledgeGraphConfig
from src.knowledge_graph.base_graph import (
    BaseKnowledgeGraph,
    ChangeKind,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from src.loader.base_loader import Document

logger = logging.getLogger(__name__)

__all__ = [
    "MatterKGBuilder",
    # Re-export shared types so callers of this module get them too
    "ChangeKind",
    "NodeType",
    "EdgeType",
    "GraphNode",
    "GraphEdge",
]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class MatterKGBuilder(BaseKnowledgeGraph):
    """Build and query a directed knowledge graph over Matter spec documents.

    Nodes: Requirements, TestCases, Sections, PRChanges, Clusters, Attributes, Commands
    Edges: covers, references, derived_from, conflicts_with, belongs_to, tests

    Implements ``BaseKnowledgeGraph`` so it can be swapped for other protocol
    builders (Bluetooth, Thread, mDNS …) or the Docker HTTP client without
    changing the pipeline nodes.
    """

    def __init__(self, config: KnowledgeGraphConfig) -> None:
        self.config = config
        try:
            import networkx as nx  # type: ignore
            self._graph: nx.DiGraph = nx.DiGraph()
        except ImportError:
            raise ImportError("Install networkx: pip install networkx")
        # O(1) lookup index: lowercased cluster name → node_id
        self._cluster_index: Dict[str, str] = {}

    def _rebuild_cluster_index(self) -> None:
        """Scan all CLUSTER nodes once and populate ``_cluster_index``.

        Maps ``lowercased_label → node_id`` for every CLUSTER node so that
        ``_resolve_cluster_to_base()`` and ``search_by_structured_change()``
        can do O(1) lookups instead of O(N) full-graph scans.
        """
        self._cluster_index.clear()
        for nid, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.CLUSTER:
                self._cluster_index[obj.label.lower()] = nid

    # ------------------------------------------------------------------
    # Building the graph
    # ------------------------------------------------------------------

    def add_data_model_documents(self, documents: List[Document]) -> None:
        """Ingest Matter data-model XML documents — canonical schema nodes.

        For each document that carries ``doc.metadata['schema']``, creates:
          - CLUSTER node
          - ATTRIBUTE nodes linked to the cluster via BELONGS_TO
          - COMMAND nodes linked to the cluster via BELONGS_TO
          - EVENT nodes linked to the cluster via BELONGS_TO
          - FEATURE nodes linked to the cluster via BELONGS_TO

        Falls back to regex-based entity extraction when ``schema`` metadata is absent.
        """
        for doc in documents:
            schema = doc.metadata.get("schema")
            if schema:
                self._ingest_schema(schema)
            else:
                # Fallback: treat like any document and run regex extraction
                self.extract_matter_entities([doc])

    def _ingest_schema(self, schema: dict) -> None:
        """Build the data-model backbone from a structured schema dict.

        Graph model
        -----------
        Canonical node IDs use ``NodeType::Name`` (cluster) and
        ``NodeType::ClusterName::EntityName`` (all others), e.g.::

            CLUSTER::OccupancySensing
            ATTRIBUTE::OccupancySensing::OccupancySensorType
            COMMAND::OccupancySensing::TriggerOccupancySensing
            EVENT::OccupancySensing::OccupancyChanged
            FEATURE::OccupancySensing::PassiveInfrared

        Edges are directed **from the cluster outward**::

            CLUSTER::X  --[HAS_ATTRIBUTE]-->  ATTRIBUTE::X::Y
            CLUSTER::X  --[HAS_COMMAND]-->    COMMAND::X::Y
            CLUSTER::X  --[HAS_EVENT]-->      EVENT::X::Y
            CLUSTER::X  --[HAS_FEATURE]-->    FEATURE::X::Y

        All nodes carry a ``name`` property (the canonical human-readable name)
        so higher layers (REQUIREMENT, TEST_CASE, BEHAVIOR_RULE …) can attach
        to the backbone by ``name``-lookup without needing to reconstruct IDs.
        """
        cluster_name = schema.get("cluster_name", "")
        cluster_id   = schema.get("cluster_id", "")
        if not cluster_name:
            return

        # ── CLUSTER node ─────────────────────────────────────────────────────
        cid = f"CLUSTER::{cluster_name}"
        self._add_node(GraphNode(
            node_id=cid,
            node_type=NodeType.CLUSTER,
            label=cluster_name,
            properties={
                "name":       cluster_name,
                "cluster_id": cluster_id,
                "revision":   schema.get("revision", ""),
                "source":     "data_model",
            },
        ))

        # ── Alias CLUSTER nodes (for base-cluster XML files) ─────────────────
        # E.g. "Water Content Measurement Clusters" has alias "Relative Humidity Measurement"
        # (id=0x0405, picsCode=RH).  Each alias gets its own CLUSTER node linked back
        # to the base via ALIAS_OF so test cases that mention the alias name resolve correctly.
        for alias in schema.get("cluster_aliases", []):
            alias_name = alias.get("name", "").strip()
            if not alias_name or alias_name == cluster_name:
                continue
            alias_node_id = f"CLUSTER::{alias_name}"
            self._add_node(GraphNode(
                node_id=alias_node_id,
                node_type=NodeType.CLUSTER,
                label=alias_name,
                properties={
                    "name":           alias_name,
                    "cluster_id":     alias.get("id", ""),
                    "pics_code":      alias.get("picsCode", ""),
                    "revision":       schema.get("revision", ""),
                    "source":         "data_model",
                    "base_cluster":   cluster_name,
                },
            ))
            self._add_edge(GraphEdge(
                source=alias_node_id,
                target=cid,
                edge_type=EdgeType.ALIAS_OF,
            ))

        # ── ATTRIBUTE nodes ───────────────────────────────────────────────────
        for attr in schema.get("attributes", []):
            name = attr.get("name", "")
            if not name:
                continue
            aid = f"ATTRIBUTE::{cluster_name}::{name}"
            self._add_node(GraphNode(
                node_id=aid,
                node_type=NodeType.ATTRIBUTE,
                label=name,
                properties={
                    "name":         name,
                    "attribute_id": attr.get("id", ""),
                    "datatype":     attr.get("type", ""),
                    "access":       attr.get("access", ""),
                    "conformance":  attr.get("conformance", ""),
                    "quality":      attr.get("quality", ""),
                    "default":      attr.get("default", ""),
                    "cluster":      cluster_name,
                    "source":       "data_model",
                },
            ))
            self._add_edge(GraphEdge(source=cid, target=aid, edge_type=EdgeType.HAS_ATTRIBUTE))

        # ── COMMAND nodes ─────────────────────────────────────────────────────
        for cmd in schema.get("commands", []):
            name = cmd.get("name", "")
            if not name:
                continue
            cmdid = f"COMMAND::{cluster_name}::{name}"
            self._add_node(GraphNode(
                node_id=cmdid,
                node_type=NodeType.COMMAND,
                label=name,
                properties={
                    "name":        name,
                    "command_id":  cmd.get("id", ""),
                    "direction":   cmd.get("direction", ""),
                    "response":    cmd.get("response", ""),
                    "conformance": cmd.get("conformance", ""),
                    "cluster":     cluster_name,
                    "source":      "data_model",
                },
            ))
            self._add_edge(GraphEdge(source=cid, target=cmdid, edge_type=EdgeType.HAS_COMMAND))

        # ── EVENT nodes ───────────────────────────────────────────────────────
        for evt in schema.get("events", []):
            name = evt.get("name", "")
            if not name:
                continue
            evid = f"EVENT::{cluster_name}::{name}"
            self._add_node(GraphNode(
                node_id=evid,
                node_type=NodeType.EVENT,
                label=name,
                properties={
                    "name":        name,
                    "event_id":    evt.get("id", ""),
                    "priority":    evt.get("priority", ""),
                    "conformance": evt.get("conformance", ""),
                    "cluster":     cluster_name,
                    "source":      "data_model",
                },
            ))
            self._add_edge(GraphEdge(source=cid, target=evid, edge_type=EdgeType.HAS_EVENT))

        # ── FEATURE nodes ─────────────────────────────────────────────────────
        for feat in schema.get("features", []):
            name = feat.get("name", "")
            if not name:
                continue
            fid = f"FEATURE::{cluster_name}::{name}"
            self._add_node(GraphNode(
                node_id=fid,
                node_type=NodeType.FEATURE,
                label=name,
                properties={
                    "name":        name,
                    "bit":         feat.get("bit", ""),
                    "code":        feat.get("code", ""),
                    "conformance": feat.get("conformance", ""),
                    "cluster":     cluster_name,
                    "source":      "data_model",
                },
            ))
            self._add_edge(GraphEdge(source=cid, target=fid, edge_type=EdgeType.HAS_FEATURE))

    def _resolve_cluster_to_base(self, cluster_name: str) -> str:
        """If *cluster_name* is an alias cluster, return the base cluster name.

        Alias CLUSTER nodes carry an outgoing ALIAS_OF edge to their base cluster node.
        Entity nodes (ATTRIBUTE / COMMAND / EVENT / FEATURE) are stored under the base
        cluster name, so callers that need to look up entities must resolve aliases first.

        Returns the original *cluster_name* unchanged when it is not an alias.
        """
        # O(1) lookup via cluster index
        nid = self._cluster_index.get(cluster_name.lower())
        if nid is None:
            return cluster_name  # not found — return unchanged
        # Check for an ALIAS_OF outgoing edge
        for _, tgt, edata in self._graph.out_edges(nid, data=True):
            if edata.get("edge_type") in (EdgeType.ALIAS_OF, "alias_of"):
                tgt_obj: GraphNode = self._graph.nodes.get(tgt, {}).get("obj")
                if tgt_obj:
                    return tgt_obj.label
        return cluster_name  # found but not an alias

    def search_by_structured_change(
        self,
        cluster: str,
        entity_type: str,
        entity_name: str,
        max_results: int = 10,
    ) -> List[GraphNode]:
        """Find TEST_CASE / REQUIREMENT nodes related to a specific schema entity.

        Performs a two-hop lookup:
          1. Find schema node (CLUSTER/ATTRIBUTE/COMMAND/EVENT/FEATURE) by name.
          2. Return all TEST_CASE / REQUIREMENT nodes that reference it directly
             (via BELONGS_TO / COVERS / TESTS / REFERENCES edges), plus any in
             the surrounding cluster sub-graph (depth 2).

        More precise than ``search_by_entities()`` because it uses structured
        node ids rather than regex over free text.
        """
        import networkx as nx

        # Resolve alias cluster names to their base cluster so entity node IDs resolve correctly.
        # E.g. "Relative Humidity Measurement" → "Water Content Measurement Clusters"
        cluster = self._resolve_cluster_to_base(cluster)

        # Map entity_type string to NodeType
        type_map = {
            "attribute": NodeType.ATTRIBUTE,
            "command":   NodeType.COMMAND,
            "event":     NodeType.EVENT,
            "feature":   NodeType.FEATURE,
            "cluster":   NodeType.CLUSTER,
        }
        target_type = type_map.get(entity_type.lower())

        candidate_node_ids: List[str] = []
        search_name    = entity_name.lower()
        search_cluster = cluster.lower()

        # Skip overly generic entity names that would match thousands of nodes
        _GENERIC_NAMES = {"matter", "end", "start", "new", "old", "value", "type",
                          "data", "field", "state", "mode", "status", "list", "set",
                          "the", "and", "for", "with", "from", "this", "that"}
        if search_name in _GENERIC_NAMES or len(search_name) < 3:
            return self.search_by_entities(f"{cluster} {entity_type} {entity_name}",
                                           max_results=max_results)

        # Fast path: try canonical ID lookup first
        # e.g. ATTRIBUTE::OccupancySensing::OccupancySensorType
        if target_type and entity_name:
            type_prefix = {
                NodeType.ATTRIBUTE: "ATTRIBUTE",
                NodeType.COMMAND:   "COMMAND",
                NodeType.EVENT:     "EVENT",
                NodeType.FEATURE:   "FEATURE",
                NodeType.CLUSTER:   "CLUSTER",
            }.get(target_type, "")

            if type_prefix and cluster:
                # O(1) lookup via cluster index for canonical cluster name
                for idx_name, idx_nid in self._cluster_index.items():
                    if search_cluster in idx_name:
                        idx_obj: GraphNode = self._graph.nodes.get(idx_nid, {}).get("obj")
                        if idx_obj:
                            canonical_cluster = idx_obj.label
                            probe_id = f"{type_prefix}::{canonical_cluster}::{entity_name}"
                            if self._graph.has_node(probe_id):
                                candidate_node_ids.append(probe_id)
                            break

        # Fallback: label substring scan across all nodes
        if not candidate_node_ids:
            search_name = entity_name.lower()
            search_cluster = cluster.lower()
            for node_id, data in self._graph.nodes(data=True):
                obj: GraphNode = data.get("obj")
                if not obj:
                    continue
                if target_type and obj.node_type != target_type:
                    continue
                label_low = obj.label.lower()
                if search_name and search_name in label_low:
                    props_cluster = obj.properties.get("cluster", "").lower()
                    if not search_cluster or not props_cluster or search_cluster in props_cluster:
                        candidate_node_ids.append(node_id)
                        if len(candidate_node_ids) >= 20:
                            break

        # Also include the CLUSTER node itself so ego_graph picks up its children
        if search_cluster:
            for idx_name, idx_nid in self._cluster_index.items():
                if search_cluster in idx_name:
                    candidate_node_ids.append(idx_nid)

        if not candidate_node_ids:
            # Fallback to text search
            return self.search_by_entities(f"{cluster} {entity_type} {entity_name}",
                                           max_results=max_results)

        # Collect all reachable TEST_CASE / REQUIREMENT nodes within depth 2
        target_types = {NodeType.TEST_CASE, NodeType.REQUIREMENT}
        found: Dict[str, GraphNode] = {}
        for start_id in candidate_node_ids:
            try:
                subgraph = nx.ego_graph(self._graph, start_id, radius=2, undirected=True)
            except (nx.NetworkXError, KeyError):
                continue
            for nid in subgraph.nodes():
                node_data = self._graph.nodes.get(nid, {}).get("obj")
                if node_data and node_data.node_type in target_types:
                    # Enforce cluster match on REQUIREMENT nodes to avoid cross-cluster
                    # contamination (e.g. Mode Select or Color Control requirements that
                    # REFERENCE the On/Off Cluster appear in the 2-hop neighbourhood).
                    if node_data.node_type == NodeType.REQUIREMENT and search_cluster:
                        req_cluster = node_data.properties.get("cluster", "").lower()
                        if req_cluster and search_cluster not in req_cluster:
                            continue
                    # Enforce cluster match on TEST_CASE nodes to prevent bleed:
                    # 2-hop traversal can reach TCs from adjacent clusters that share
                    # entity refs (e.g. Mode cluster TCs linked to On/Off attributes).
                    if node_data.node_type == NodeType.TEST_CASE and search_cluster:
                        tc_cluster = node_data.properties.get("cluster", "").lower()
                        if tc_cluster and search_cluster not in tc_cluster and tc_cluster not in search_cluster:
                            continue
                    found[nid] = node_data

        # Also collect SECTION nodes for the matching cluster (spec context for the LLM).
        # These carry the old spec text that the LLM needs alongside the PR diff.
        if search_cluster:
            section_limit = max(3, max_results // 3)
            section_count = 0
            for nid, data in self._graph.nodes(data=True):
                if section_count >= section_limit:
                    break
                obj: GraphNode = data.get("obj")
                if obj and obj.node_type == NodeType.SECTION:
                    node_cluster = obj.properties.get("cluster", "").lower()
                    if node_cluster and search_cluster in node_cluster:
                        found[nid] = obj
                        section_count += 1

        results = sorted(found.values(), key=lambda n: n.node_id)[:max_results]
        logger.debug(
            "search_by_structured_change(%s, %s, %s) → %d results",
            cluster, entity_type, entity_name, len(results),
        )
        return results

    def add_test_plan_documents(self, documents: List[Document]) -> None:
        """Ingest test plan Documents (adoc/pdf) into the graph."""
        for doc in documents:
            node_id = self._make_id(doc)
            node_type = self._infer_node_type(doc)
            label = self._extract_label(doc)

            self._add_node(GraphNode(
                node_id=node_id,
                node_type=node_type,
                label=label,
                properties={
                    "content": doc.page_content[:500],
                    "source": doc.metadata.get("source", ""),
                    "path": doc.metadata.get("absolute_path", doc.metadata.get("path", "")),
                    "section": doc.metadata.get("section", ""),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "doc_type": "test_plan",
                },
            ))

            # Link section nodes to their parent file
            parent_id = self._file_node_id(doc)
            if parent_id != node_id:
                self._ensure_file_node(doc)
                self._add_edge(GraphEdge(
                    source=node_id,
                    target=parent_id,
                    edge_type=EdgeType.BELONGS_TO,
                ))

    def add_pr_documents(self, documents: List[Document]) -> None:
        """Ingest PR change Documents into the graph."""
        for doc in documents:
            node_id = "pr_" + self._make_id(doc)
            label = doc.metadata.get("path", "PR Change")

            self._add_node(GraphNode(
                node_id=node_id,
                node_type=NodeType.PR_CHANGE,
                label=label,
                properties={
                    "content": doc.page_content[:500],
                    "pr_url": doc.metadata.get("pr_url", ""),
                    "status": doc.metadata.get("status", "modified"),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                },
            ))

    def add_spec_documents(self, documents: List[Document]) -> None:
        """Ingest Matter specification Documents into the graph.

        Spec docs are normative text (shall/must), so they default to
        ``NodeType.REQUIREMENT`` rather than the ``SECTION`` fallback used by
        ``add_test_plan_documents``.
        """
        for doc in documents:
            node_id = "spec_" + self._make_id(doc)
            node_type = self._infer_node_type(doc)
            if node_type == NodeType.SECTION:
                node_type = NodeType.REQUIREMENT
            label = self._extract_label(doc)

            self._add_node(GraphNode(
                node_id=node_id,
                node_type=node_type,
                label=label,
                properties={
                    "content": doc.page_content[:500],
                    "source": doc.metadata.get("source", ""),
                    "path": doc.metadata.get("absolute_path", doc.metadata.get("path", "")),
                    "section": doc.metadata.get("section", ""),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "doc_type": "spec",
                },
            ))

            parent_id = "spec_file_" + self._file_node_id(doc)
            if parent_id != node_id:
                if not self._graph.has_node(parent_id):
                    path = doc.metadata.get("absolute_path", doc.metadata.get("path", ""))
                    self._add_node(GraphNode(
                        node_id=parent_id,
                        node_type=NodeType.SECTION,
                        label=Path(path).name if path else "Unknown",
                        properties={"path": path, "doc_type": "spec"},
                    ))
                self._add_edge(GraphEdge(
                    source=node_id,
                    target=parent_id,
                    edge_type=EdgeType.BELONGS_TO,
                ))

    def link_pr_to_test_cases(self, pr_node_id: str, test_node_ids: List[str],
                               edge_type: EdgeType = EdgeType.COVERS) -> None:
        """Link a PR change node to related test case nodes."""
        for tc_id in test_node_ids:
            if self._graph.has_node(tc_id):
                self._add_edge(GraphEdge(
                    source=pr_node_id,
                    target=tc_id,
                    edge_type=edge_type,
                ))

    def extract_matter_entities(self, documents: List[Document]) -> None:
        """Extract Matter-specific entities (clusters, commands, attributes) from documents."""
        cluster_re = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\s+[Cc]luster\b")
        command_re = re.compile(r"\b(?:command|Command)\s+([A-Z][a-zA-Z]+)\b")
        attr_re = re.compile(r"\b(?:attribute|Attribute)\s+([a-z][a-zA-Z]+)\b")

        for doc in documents:
            text = doc.page_content

            for m in cluster_re.finditer(text):
                cid = f"cluster_{m.group(1).lower()}"
                self._add_node(GraphNode(cid, NodeType.CLUSTER, m.group(1)))

            for m in command_re.finditer(text):
                cid = f"cmd_{m.group(1).lower()}"
                self._add_node(GraphNode(cid, NodeType.COMMAND, m.group(1)))

            for m in attr_re.finditer(text):
                aid = f"attr_{m.group(1).lower()}"
                self._add_node(GraphNode(aid, NodeType.ATTRIBUTE, m.group(1)))

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def find_related(self, node_id: str, depth: int = 1) -> List[GraphNode]:
        """Return nodes reachable from node_id within `depth` hops."""
        import networkx as nx

        if not self._graph.has_node(node_id):
            return []

        subgraph_nodes = nx.ego_graph(
            self._graph, node_id, radius=min(depth, self.config.max_depth)
        ).nodes()

        return sorted(
            [self._graph.nodes[n]["obj"] for n in subgraph_nodes if n != node_id],
            key=lambda n: n.node_id,
        )

    def get_test_cases_for_pr(self, pr_node_id: str) -> List[GraphNode]:
        """Return all test case nodes linked to a PR change node."""
        if not self._graph.has_node(pr_node_id):
            return []

        results = []
        for _, target, data in self._graph.out_edges(pr_node_id, data=True):
            if data.get("edge_type") in (EdgeType.COVERS, EdgeType.REFERENCES, EdgeType.TESTS):
                node_data = self._graph.nodes.get(target, {}).get("obj")
                if node_data and node_data.node_type == NodeType.TEST_CASE:
                    results.append(node_data)
        results.sort(key=lambda n: n.node_id)
        return results

    def get_coverage_gaps(self) -> List[GraphNode]:
        """Return PR change nodes that have no linked test case nodes."""
        gaps = []
        for node_id, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.PR_CHANGE:
                linked = self.get_test_cases_for_pr(node_id)
                if not linked:
                    gaps.append(obj)
        gaps.sort(key=lambda n: n.node_id)
        return gaps

    def search_by_keywords(
        self,
        keywords: List[str],
        node_types: Optional[List[NodeType]] = None,
        requirement_types: Optional[List[str]] = None,
        cluster_filter: Optional[str] = None,
        max_results: int = 10,
    ) -> List[GraphNode]:
        """Full-text keyword search over node labels, content and section fields.

        Designed for natural-language chat queries (e.g. ``["timer", "protocol"]``).
        Scores each matching node by the number of distinct keywords found in its
        text fields so more-relevant nodes rank higher.

        When ``requirement_types`` is given (e.g. ``["timing_requirement"]``),
        only REQUIREMENT / BEHAVIOR_RULE nodes whose
        ``properties["requirement_type"]`` value matches are returned — other
        content-bearing node types (TEST_CASE) are unaffected by this filter.
        Nodes whose ``requirement_type`` matches get a **2× score bonus** so
        they rank above keyword-only matches.

        When ``cluster_filter`` is given, only nodes whose
        ``properties["cluster"]`` contains the cluster string (case-insensitive
        substring match) are included.  Useful when the user asks about a
        specific cluster (e.g. "On/Off Cluster timer requirements").

        Args:
            keywords:          Lowercase plain-text keywords (stop words already stripped).
            node_types:        NodeTypes to include.  None → REQUIREMENT, BEHAVIOR_RULE,
                               TEST_CASE (the content-bearing types).
            requirement_types: RequirementType string values to filter spec nodes
                               (e.g. ``["timing_requirement", "protocol_behavior_requirement"]``).
                               None → no filtering by requirement type.
            cluster_filter:    Case-insensitive cluster name substring filter.
                               None → all clusters.
            max_results:       Maximum results.

        Returns:
            GraphNode list sorted descending by (type-boosted) keyword hit count.
        """
        if not keywords:
            return []

        if node_types is None:
            target_types: set = {NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE, NodeType.TEST_CASE}
        else:
            target_types = set(node_types)

        # Build a frozenset for O(1) requirement_type membership test
        req_type_filter: Optional[frozenset] = (
            frozenset(requirement_types) if requirement_types else None
        )
        # NodeTypes that carry a requirement_type property
        _spec_types = {NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE}
        cluster_filter_lower = cluster_filter.lower() if cluster_filter else None

        scored: List[Tuple[int, GraphNode]] = []
        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj is None or obj.node_type not in target_types:
                continue

            # Apply cluster filter (substring match on properties["cluster"])
            if cluster_filter_lower:
                node_cluster = obj.properties.get("cluster", "").lower()
                if not node_cluster or cluster_filter_lower not in node_cluster:
                    continue

            # Apply requirement_type filter to spec nodes
            if req_type_filter is not None and obj.node_type in _spec_types:
                node_req_type = obj.properties.get("requirement_type", "")
                if node_req_type not in req_type_filter:
                    continue

            haystack = " ".join([
                obj.label,
                obj.properties.get("content", ""),
                obj.properties.get("purpose", ""),    # TC nodes store description here
                obj.properties.get("section", ""),
                obj.properties.get("cluster", ""),
                obj.properties.get("normative_text", ""),
                " ".join(obj.properties.get("intents", [])),       # TC intents e.g. "reset"
                " ".join(obj.properties.get("step_keywords", [])), # extracted step action keywords
            ]).lower()
            score = sum(1 for kw in keywords if kw in haystack)
            if score > 0:
                # 2× boost when node's requirement_type is in the requested set
                if req_type_filter is not None and obj.properties.get("requirement_type") in req_type_filter:
                    score *= 2
                scored.append((score, obj))

        scored.sort(key=lambda x: (-x[0], x[1].node_id))
        logger.debug(
            "keyword search: %d keywords, req_types=%s, cluster=%s → %d matches (node_types=%s)",
            len(keywords),
            list(requirement_types) if requirement_types else "all",
            cluster_filter or "any",
            len(scored),
            [t.value for t in target_types],
        )
        return [node for _, node in scored[:max_results]]

    def find_entity_coverage(
        self,
        cluster: str,
        entity_type: str,
        entity_name: str,
    ) -> Dict[str, Any]:
        """Check whether any test case directly covers a schema entity.

        Looks for TEST_CASE nodes that have a direct edge pointing to the
        entity node (via ``tests_command``, ``verifies_attribute``, ``tests``,
        ``observes_event``, or ``covers``).

        Unlike ``search_by_structured_change``, this method returns a structured
        result so callers can distinguish "entity exists but no TC coverage"
        (coverage gap) from "entity doesn't exist in KG" (new entity added by PR).

        Args:
            cluster:     Cluster name as stored in the DM XML (e.g. ``"On/Off Cluster"``).
                         A case-insensitive substring match is used when an exact
                         match fails.
            entity_type: One of ``"command"``, ``"attribute"``, ``"event"``,
                         ``"feature"``, ``"cluster"``.
            entity_name: Entity name exactly as stored (e.g. ``"OnWithTimedOff"``).

        Returns:
            dict with keys:
            - ``entity_exists`` (bool): True if the node is in the graph.
            - ``entity_node`` (GraphNode | None): the schema node if found.
            - ``test_cases`` (List[GraphNode]): TEST_CASE nodes with direct edges.
            - ``covered`` (bool): True when at least one test case was found.
        """
        type_prefix_map = {
            "command":   "COMMAND",
            "attribute": "ATTRIBUTE",
            "event":     "EVENT",
            "feature":   "FEATURE",
            "cluster":   "CLUSTER",
        }
        prefix = type_prefix_map.get(entity_type.lower(), entity_type.upper())

        # Resolve alias cluster names so entity node IDs (ATTRIBUTE::Base::Name) are found.
        cluster = self._resolve_cluster_to_base(cluster)

        # Fast path: canonical ID
        node_id: Optional[str] = None
        probe_id = f"{prefix}::{cluster}::{entity_name}"
        if self._graph.has_node(probe_id):
            node_id = probe_id
        else:
            # Fuzzy: find the cluster node by substring, then build the ID
            cluster_lower = cluster.lower()
            for nid, data in self._graph.nodes(data=True):
                obj: GraphNode = data.get("obj")
                if obj and obj.node_type == NodeType.CLUSTER and cluster_lower in obj.label.lower():
                    candidate = f"{prefix}::{obj.label}::{entity_name}"
                    if self._graph.has_node(candidate):
                        node_id = candidate
                        break
            # Final fallback: label match
            if node_id is None:
                target_nt = {
                    "COMMAND": NodeType.COMMAND, "ATTRIBUTE": NodeType.ATTRIBUTE,
                    "EVENT": NodeType.EVENT, "FEATURE": NodeType.FEATURE,
                    "CLUSTER": NodeType.CLUSTER,
                }.get(prefix)
                name_lower = entity_name.lower()
                for nid, data in self._graph.nodes(data=True):
                    obj = data.get("obj")
                    if obj and (target_nt is None or obj.node_type == target_nt):
                        if name_lower == obj.label.lower():
                            node_id = nid
                            break

        if node_id is None:
            logger.debug("find_entity_coverage: entity not found: %s::%s::%s", prefix, cluster, entity_name)
            return {"entity_exists": False, "entity_node": None, "test_cases": [], "covered": False}

        entity_obj: Optional[GraphNode] = self._graph.nodes[node_id].get("obj")

        _direct_tc_edges = {
            EdgeType.TESTS_COMMAND,
            EdgeType.VERIFIES_ATTRIBUTE,
            EdgeType.OBSERVES_EVENT,
            EdgeType.TESTS,
            EdgeType.COVERS,
            EdgeType.VALIDATES,
            # String fallbacks for edges stored as raw strings (pre-enum)
            "tests_command", "verifies_attribute", "observes_event",
            "tests", "covers", "validates",
        }
        test_cases: List[GraphNode] = []
        seen_tcs: Set[str] = set()
        for src, _, edata in self._graph.in_edges(node_id, data=True):
            if edata.get("edge_type") in _direct_tc_edges:
                src_obj: Optional[GraphNode] = self._graph.nodes.get(src, {}).get("obj")
                if src_obj and src_obj.node_type == NodeType.TEST_CASE and src not in seen_tcs:
                    test_cases.append(src_obj)
                    seen_tcs.add(src)

        test_cases.sort(key=lambda n: n.node_id)

        logger.debug(
            "find_entity_coverage: %s::%s::%s → entity_exists=True, %d direct TC(s)",
            prefix, cluster, entity_name, len(test_cases),
        )
        return {
            "entity_exists": True,
            "entity_node": entity_obj,
            "test_cases": test_cases,
            "covered": len(test_cases) > 0,
        }

    def find_requirements_and_coverage(
        self,
        keywords: List[str],
        cluster: Optional[str] = None,
        requirement_types: Optional[List[str]] = None,
        max_results_reqs: int = 20,
        max_results_tcs: int = 5,
    ) -> Dict[str, Any]:
        """Find REQUIREMENT nodes matching keywords, then their linked TEST_CASEs.

        This is the reverse-lookup path for chat queries like:
        "tell me test cases that verify BLE advertisement shall terminate after X seconds".

        Steps:
          1. Score all REQUIREMENT / BEHAVIOR_RULE nodes by keyword hit count
             (+ optional ``cluster`` and ``requirement_types`` filters).
          2. For each matched requirement, walk ``verifies_requirement`` in-edges
             to find TEST_CASE nodes that verify it.
          3. Partition results into ``covered`` (requirement has at least one TC)
             and ``uncovered`` (requirement has no TC → coverage gap).

        Args:
            keywords:          Plain-text keywords (stop words stripped).
            cluster:           Optional cluster name substring filter.
            requirement_types: Optional requirement type string filter.
            max_results_reqs:  Max requirement nodes to score (default 20).
            max_results_tcs:   Max TCs per requirement to return (default 5).

        Returns:
            dict with keys:
            - ``requirements`` (List[GraphNode]): all matched requirement nodes.
            - ``covered`` (Dict[str, List[GraphNode]]): req_node_id → [TC nodes].
            - ``uncovered`` (List[GraphNode]): requirements with no TC coverage.
        """
        if not keywords:
            return {"requirements": [], "covered": {}, "uncovered": []}

        target_types = {NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE}
        req_type_filter: Optional[frozenset] = (
            frozenset(requirement_types) if requirement_types else None
        )
        cluster_lower = cluster.lower() if cluster else None

        scored: List[Tuple[int, GraphNode]] = []
        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj is None or obj.node_type not in target_types:
                continue
            if cluster_lower:
                node_cluster = obj.properties.get("cluster", "").lower()
                if not node_cluster or cluster_lower not in node_cluster:
                    continue
            if req_type_filter:
                if obj.properties.get("requirement_type") not in req_type_filter:
                    continue
            haystack = " ".join([
                obj.label,
                obj.properties.get("normative_text", ""),
                obj.properties.get("section_path", ""),
                obj.properties.get("cluster", ""),
            ]).lower()
            score = sum(1 for kw in keywords if kw.lower() in haystack)
            if score > 0:
                scored.append((score, obj))

        scored.sort(key=lambda x: (-x[0], x[1].node_id))
        matched_reqs = [node for _, node in scored[:max_results_reqs]]

        covered: Dict[str, List[GraphNode]] = {}
        uncovered: List[GraphNode] = []

        for req_node in matched_reqs:
            tc_nodes: List[GraphNode] = []
            seen_tcs: Set[str] = set()
            for src, _, edata in self._graph.in_edges(req_node.node_id, data=True):
                et = edata.get("edge_type")
                if et in (EdgeType.VERIFIES_REQUIREMENT, "verifies_requirement"):
                    src_obj = self._graph.nodes.get(src, {}).get("obj")
                    if src_obj and src_obj.node_type == NodeType.TEST_CASE and src not in seen_tcs:
                        tc_nodes.append(src_obj)
                        seen_tcs.add(src)
            if tc_nodes:
                tc_nodes.sort(key=lambda n: n.node_id)
                covered[req_node.node_id] = tc_nodes[:max_results_tcs]
            else:
                uncovered.append(req_node)

        uncovered.sort(key=lambda n: n.node_id)

        logger.debug(
            "find_requirements_and_coverage: keywords=%s cluster=%s req_types=%s → "
            "%d matched reqs, %d covered, %d uncovered",
            keywords, cluster, requirement_types,
            len(matched_reqs), len(covered), len(uncovered),
        )
        return {"requirements": matched_reqs, "covered": covered, "uncovered": uncovered}

    # ── ChangeKind → edge types that signal "this TC is impacted" ────────────
    _CHANGE_KIND_TO_EDGES: Dict[ChangeKind, List[EdgeType]] = {
        # ENTITY_ADDED: no existing tests by definition — caller checks coverage_gap flag
        ChangeKind.ENTITY_ADDED:          [],
        # ENTITY_REMOVED: any TC that interacts with the entity at all is broken
        ChangeKind.ENTITY_REMOVED:        [
            EdgeType.READS, EdgeType.WRITES,
            EdgeType.TESTS_COMMAND, EdgeType.OBSERVES_EVENT,
            EdgeType.VERIFIES_ATTRIBUTE, EdgeType.NEGATIVE_TESTS,
            EdgeType.TESTS, EdgeType.COVERS,
        ],
        # Attribute-property changes
        ChangeKind.DATATYPE_CHANGED:      [EdgeType.READS, EdgeType.WRITES, EdgeType.VALIDATES_TYPE, EdgeType.VERIFIES_ATTRIBUTE],
        ChangeKind.CONSTRAINT_CHANGED:    [EdgeType.VALIDATES_RANGE, EdgeType.READS, EdgeType.NEGATIVE_TESTS],
        ChangeKind.DEFAULT_CHANGED:       [EdgeType.VALIDATES_DEFAULT, EdgeType.READS],
        ChangeKind.QUIETER_REPORTING_CHANGED: [EdgeType.VALIDATES_QUIETER_REPORTING, EdgeType.READS, EdgeType.NEGATIVE_TESTS],
        ChangeKind.ENUM_CHANGED:          [EdgeType.VALIDATES_ENUM, EdgeType.READS, EdgeType.WRITES, EdgeType.NEGATIVE_TESTS],
        ChangeKind.ACCESS_CHANGED:        [EdgeType.VALIDATES_ACCESS, EdgeType.NEGATIVE_TESTS],
        ChangeKind.CONFORMANCE_CHANGED:   [EdgeType.VALIDATES_CONFORMANCE, EdgeType.VERIFIES_RULE],
        # Behavioural / structural changes
        ChangeKind.BEHAVIOR_CHANGED:      [EdgeType.VERIFIES_RULE, EdgeType.VERIFIES_REQUIREMENT, EdgeType.TESTS_COMMAND],
        ChangeKind.DEPENDENCY_CHANGED:    [EdgeType.DEPENDS_ON, EdgeType.IN_CONTEXT],
        ChangeKind.STATE_MACHINE_CHANGED: [EdgeType.VERIFIES_RULE, EdgeType.VERIFIES_REQUIREMENT],
    }

    def analyze_impact_for_change(
        self,
        cluster: str,
        entity_type: str,
        entity_name: str,
        change_kind: ChangeKind,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        """Find all test cases impacted by a specific kind of change to an entity.

        Given a changed entity (e.g. ATTRIBUTE ``OnOff`` in cluster ``On/Off``)
        and a change category (e.g. ``ChangeKind.DATATYPE_CHANGED``), this method:

        1. Locates the entity node in the graph by canonical ID.
        2. Retrieves the ``ChangeKind → List[EdgeType]`` mapping from
           ``_CHANGE_KIND_TO_EDGES``.
        3. Walks *in-edges* on the entity node to find TEST_CASE nodes whose
           edge type appears in the mapping (``directly_impacted``).
        4. Also collects TCs reachable via broader edges like ``TESTS`` / ``COVERS``
           (``possibly_impacted``) — these may still need review even if their
           edge is not a precise match.
        5. Sets ``coverage_gap=True`` when the entity exists but has **no**
           TC edges of any kind.

        Args:
            cluster:     Cluster name (e.g. ``"On/Off"``).
            entity_type: One of ``"attribute"``, ``"command"``, ``"event"``,
                         ``"feature"``, ``"cluster"``.
            entity_name: Entity name (e.g. ``"OnOff"``).
            change_kind: The category of change (``ChangeKind`` enum).
            max_results: Cap on nodes returned in each list (default 20).

        Returns:
            dict with keys:

            - ``entity_node``       (``GraphNode | None``) — the changed entity, if found.
            - ``directly_impacted`` (``List[GraphNode]``) — TCs with a precise matching edge.
            - ``possibly_impacted`` (``List[GraphNode]``) — TCs with broader TESTS/COVERS edges.
            - ``coverage_gap``      (``bool``) — entity exists but no TC covers it at all.
            - ``change_kind``       (``str``) — echoed for logging.
            - ``relevant_edge_types`` (``List[str]``) — edge types queried for direct impact.
        """
        # ── 1. Locate the entity node ─────────────────────────────────────────
        entity_node = self._find_entity_node(cluster, entity_type, entity_name)

        relevant_edges: List[EdgeType] = self._CHANGE_KIND_TO_EDGES.get(change_kind, [])
        relevant_edge_values: Set[Any] = {et.value for et in relevant_edges} | {et for et in relevant_edges}

        # Broad edges that always signal some interaction (used for possibly_impacted)
        _broad_edges: Set[Any] = {
            EdgeType.TESTS, EdgeType.COVERS, EdgeType.VALIDATES, EdgeType.IMPLEMENTS,
            "tests", "covers", "validates", "implements",
        }

        directly_impacted: List[GraphNode] = []
        possibly_impacted: List[GraphNode] = []
        any_tc_edge = False
        seen_direct: Set[str] = set()
        seen_possible: Set[str] = set()

        if entity_node is not None and self._graph.has_node(entity_node.node_id):
            for src, _, edata in self._graph.in_edges(entity_node.node_id, data=True):
                et = edata.get("edge_type")
                src_obj = self._graph.nodes.get(src, {}).get("obj")
                if src_obj is None or src_obj.node_type != NodeType.TEST_CASE:
                    continue
                any_tc_edge = True
                if et in relevant_edge_values:
                    if src_obj.node_id not in seen_direct:
                        directly_impacted.append(src_obj)
                        seen_direct.add(src_obj.node_id)
                elif et in _broad_edges:
                    if src_obj.node_id not in seen_possible and src_obj.node_id not in seen_direct:
                        possibly_impacted.append(src_obj)
                        seen_possible.add(src_obj.node_id)

        directly_impacted.sort(key=lambda n: n.node_id)
        possibly_impacted.sort(key=lambda n: n.node_id)

        coverage_gap = (entity_node is not None) and (not any_tc_edge)

        logger.debug(
            "analyze_impact_for_change: cluster=%r entity_type=%r entity=%r change=%s → "
            "entity_found=%s direct=%d possible=%d coverage_gap=%s",
            cluster, entity_type, entity_name, change_kind,
            entity_node is not None,
            len(directly_impacted), len(possibly_impacted), coverage_gap,
        )

        return {
            "entity_node":        entity_node,
            "directly_impacted":  directly_impacted[:max_results],
            "possibly_impacted":  possibly_impacted[:max_results],
            "coverage_gap":       coverage_gap,
            "change_kind":        change_kind.value if hasattr(change_kind, "value") else str(change_kind),
            "relevant_edge_types": [et.value for et in relevant_edges],
        }

    def _find_entity_node(
        self,
        cluster: str,
        entity_type: str,
        entity_name: str,
    ) -> Optional[GraphNode]:
        """Resolve (cluster, entity_type, entity_name) → a GraphNode, or None."""
        type_upper = entity_type.upper()
        # Try canonical ID formats
        for node_id in [
            f"{type_upper}::{cluster}::{entity_name}",
            f"{type_upper}::{cluster.lower()}::{entity_name}",
            f"{type_upper}::{cluster.lower()}::{entity_name.lower()}",
        ]:
            if self._graph.has_node(node_id):
                return self._graph.nodes[node_id].get("obj")

        # Fuzzy: scan all nodes of matching type with cluster + name substring
        cluster_lower = cluster.lower()
        name_lower = entity_name.lower()
        node_type_val = NodeType[type_upper] if type_upper in NodeType.__members__ else None

        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj is None:
                continue
            if node_type_val and obj.node_type != node_type_val:
                continue
            label_lower = obj.label.lower()
            node_cluster = obj.properties.get("cluster", "").lower()
            if name_lower in label_lower and cluster_lower in node_cluster:
                return obj
        return None

    def search_by_entities(self, text: str, max_results: int = 10, cluster_filter: str = "") -> List[GraphNode]:
        """Find TEST_CASE and REQUIREMENT nodes whose content mentions Matter entities.

        Args:
            text:           Free-text to extract entity names from (PR chunk content).
            max_results:    Maximum number of nodes to return.
            cluster_filter: If non-empty, restrict results to nodes whose ``cluster``
                            property contains this string (case-insensitive).  Applied
                            as a first-pass filter before scoring so noise from
                            unrelated clusters is excluded even when entities are generic.
        """
        cluster_re = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\s+[Cc]luster\b")
        command_re = re.compile(r"\b(?:command|Command)\s+([A-Z][a-zA-Z]+)\b")
        attr_re = re.compile(r"\b(?:attribute|Attribute)\s+([a-z][a-zA-Z]+)\b")

        entities: Set[str] = set()
        for m in cluster_re.finditer(text):
            entities.add(m.group(1).lower())
        for m in command_re.finditer(text):
            entities.add(m.group(1).lower())
        for m in attr_re.finditer(text):
            entities.add(m.group(1).lower())

        if not entities:
            return []

        scored: List[Tuple[int, GraphNode]] = []
        target_types = {NodeType.TEST_CASE, NodeType.REQUIREMENT}
        cluster_filter_lower = cluster_filter.lower() if cluster_filter else ""

        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj is None or obj.node_type not in target_types:
                continue
            # Apply cluster filter before scoring to exclude noise from other clusters.
            if cluster_filter_lower:
                node_cluster = obj.properties.get("cluster", "").lower()
                if node_cluster and cluster_filter_lower not in node_cluster:
                    continue
            haystack = (obj.label + " " + obj.properties.get("content", "")).lower()
            score = sum(1 for e in entities if e in haystack)
            if score > 0:
                scored.append((score, obj))

        scored.sort(key=lambda x: (-x[0], x[1].node_id))
        logger.debug(
            "graph search: %d entities extracted, %d matches found",
            len(entities), len(scored),
        )
        return [node for _, node in scored[:max_results]]

    def get_all_test_cases(self) -> List[GraphNode]:
        """Return all test case nodes in the graph."""
        result = [
            data["obj"]
            for _, data in self._graph.nodes(data=True)
            if data.get("obj") and data["obj"].node_type == NodeType.TEST_CASE
        ]
        result.sort(key=lambda n: n.node_id)
        return result

    def get_test_cases_for_cluster(self, cluster_name: str) -> List[GraphNode]:
        """Return all TEST_CASE nodes whose cluster property matches *cluster_name*.

        Matching is case-insensitive substring so "on/off" matches "On/Off Cluster".
        Falls back to graph-edge traversal (TESTS / IN_CONTEXT edges to CLUSTER node)
        for TCs that don't have the cluster property set.
        """
        cluster_lower = cluster_name.lower()
        found: Dict[str, GraphNode] = {}

        # Normalize for matching
        _query_norm = cluster_lower.replace(" cluster", "").strip()

        # Pass 1: cluster property on TEST_CASE node — exact normalized match first,
        # then substring containment as fallback.  This avoids false positives from
        # token intersection (e.g. "Mode Select" matching "Thermostat Mode" via the
        # shared "mode" token).
        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.TEST_CASE:
                tc_cluster = (obj.properties.get("cluster") or obj.properties.get("cluster_name") or "").lower()
                if not tc_cluster:
                    continue
                tc_norm = tc_cluster.replace(" cluster", "").strip()
                # Exact match or substring containment (both directions)
                if tc_norm == _query_norm or _query_norm in tc_norm or tc_norm in _query_norm:
                    found[obj.node_id] = obj

        # Pass 2: edge traversal — TCs that TESTS / IN_CONTEXT a matching CLUSTER node
        cluster_node_ids: set = set()
        for nid, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.CLUSTER:
                cluster_label_norm = obj.label.lower().replace(" cluster", "").strip()
                if cluster_label_norm == _query_norm or _query_norm in cluster_label_norm or cluster_label_norm in _query_norm:
                    cluster_node_ids.add(nid)

        for src, tgt, attrs in self._graph.edges(data=True):
            et = attrs.get("edge_type", "")
            et_str = et.value if hasattr(et, "value") else str(et)
            if et_str in ("tests", "in_context") and tgt in cluster_node_ids:
                src_data = self._graph.nodes.get(src, {}).get("obj")
                if src_data and src_data.node_type == NodeType.TEST_CASE:
                    found[src] = src_data

        return sorted(found.values(), key=lambda n: n.node_id)

    def search_tc_by_keyword(self, keyword: str) -> List[GraphNode]:
        """Return TEST_CASE nodes whose title, purpose, or step keywords contain *keyword*.

        Case-insensitive substring search across:
        - node label (TC title)
        - ``purpose`` property
        - ``step_keywords`` list joined as a space-separated string

        Designed for chat queries like "what test cases use factory reset" where the
        user wants exact phrase matching rather than semantic similarity.
        """
        kw = keyword.lower().strip()
        if not kw:
            return []
        results: List[GraphNode] = []
        for _, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj is None or obj.node_type != NodeType.TEST_CASE:
                continue
            if kw in obj.label.lower():
                results.append(obj)
                continue
            props = obj.properties
            if kw in (props.get("purpose") or "").lower():
                results.append(obj)
                continue
            step_kws = props.get("step_keywords") or []
            if kw in " ".join(step_kws).lower():
                results.append(obj)
        results.sort(key=lambda n: n.node_id)
        return results

    def get_cluster_dependencies(
        self,
        cluster_name: str,
        direction: str = "incoming_depends_on",
    ) -> List[GraphNode]:
        """Return CLUSTER nodes related to *cluster_name* via dependency edges.

        Parameters
        ----------
        cluster_name:
            Target cluster (e.g. ``"On/Off Cluster"``).  Case-insensitive
            substring match against cluster node labels.
        direction:
            ``"incoming_depends_on"``  — clusters that depend ON the named cluster
            (callers of this cluster, i.e. nodes with an edge pointing TO it).
            ``"outgoing_depends_on"``  — clusters that the named cluster depends on
            (dependencies of this cluster, i.e. nodes this cluster has an edge TO).

        Returns a list of CLUSTER ``GraphNode`` objects (deduplicated).
        """
        cluster_lower = cluster_name.lower()

        # Find matching target CLUSTER node IDs
        target_ids: set = set()
        for nid, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.CLUSTER:
                if cluster_lower in obj.label.lower():
                    target_ids.add(nid)

        if not target_ids:
            return []

        # Dependency edge type strings to consider
        _DEP_EDGES = {"depends_on", "requires", "references", "related_to"}

        found: Dict[str, GraphNode] = {}
        for src, tgt, attrs in self._graph.edges(data=True):
            et = attrs.get("edge_type", "")
            et_str = (et.value if hasattr(et, "value") else str(et)).lower()
            if et_str not in _DEP_EDGES:
                continue
            if direction == "incoming_depends_on":
                # clusters that depend ON our target (src → tgt where tgt is target)
                if tgt in target_ids and src not in target_ids:
                    obj = self._graph.nodes.get(src, {}).get("obj")
                    if obj and obj.node_type == NodeType.CLUSTER:
                        found[src] = obj
            else:  # outgoing_depends_on
                # clusters that our target depends on (src → tgt where src is target)
                if src in target_ids and tgt not in target_ids:
                    obj = self._graph.nodes.get(tgt, {}).get("obj")
                    if obj and obj.node_type == NodeType.CLUSTER:
                        found[tgt] = obj

        return sorted(found.values(), key=lambda n: n.node_id)

    def get_all_pr_changes(self) -> List[GraphNode]:
        """Return all PR change nodes."""
        return [
            data["obj"]
            for _, data in self._graph.nodes(data=True)
            if data.get("obj") and data["obj"].node_type == NodeType.PR_CHANGE
        ]

    def get_surrounding_clusters(
        self,
        cluster_name: str,
        max_results: int = 5,
    ) -> List[Dict]:
        """Return clusters that have a ``depends_on`` edge TO *cluster_name*.

        These are clusters whose behaviour explicitly depends on the target cluster
        (e.g. Level Control depends_on On/Off, Color Control depends_on On/Off).
        Only ``depends_on`` edges are considered — ``references`` and ``related_to``
        edges are excluded to avoid noise from textual citations.

        Returns a list of dicts::

            [{"cluster": "Level Control Cluster",
              "reason": "depends_on edge via LLM spec-refinement"}]

        Capped at *max_results* (default 5).
        """
        _STRONG_EDGES = {"depends_on"}

        cluster_lower = cluster_name.lower()

        # Resolve target cluster node IDs
        target_ids: set = set()
        for nid, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj and obj.node_type == NodeType.CLUSTER:
                if cluster_lower in obj.label.lower():
                    target_ids.add(nid)

        if not target_ids:
            return []

        found: Dict[str, Dict] = {}
        for src, tgt, attrs in self._graph.edges(data=True):
            if tgt not in target_ids:
                continue
            et = attrs.get("edge_type", "")
            et_str = (et.value if hasattr(et, "value") else str(et)).lower()
            if et_str not in _STRONG_EDGES:
                continue
            if src in target_ids:
                continue  # skip self-loops / same cluster
            obj = self._graph.nodes.get(src, {}).get("obj")
            if obj and obj.node_type == NodeType.CLUSTER:
                if obj.label not in found:
                    found[obj.label] = {
                        "cluster": obj.label,
                        "reason": f"{et_str} edge → {cluster_name}",
                    }
                if len(found) >= max_results:
                    break

        return sorted(found.values(), key=lambda d: d["cluster"])[:max_results]
    # Persistence
    # ------------------------------------------------------------------

    # Default prompt-section config — used when build_prompt_sections() is called
    # without a section_configs argument (e.g. direct API calls / tests).
    # Production builds pass config.knowledge_graph.prompt_sections from config.yaml.
    # Section numbers are intentionally omitted — matching is number-agnostic so the
    # config survives spec chapter renumbering without any edits.
    _DEFAULT_PROMPT_SECTIONS: list = [
        {"path_prefix": "Data Model Specification > Conformance",    "label": "Conformance"},
        {"path_prefix": "Data Model Specification > Access",         "label": "Access"},
        {"path_prefix": "Data Model Specification > Other Qualities","label": "Other Qualities"},
    ]

    # Strip leading section numbers (e.g. "7.3. " or "11. ") from a breadcrumb segment
    # so matching works regardless of where the spec numbers land after renumbering.
    _SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*\.\s+")

    @classmethod
    def _strip_section_numbers(cls, path: str) -> str:
        """Return *path* with leading section numbers removed from every breadcrumb segment.

        ``"7. Data Model Specification > 7.3. Conformance"``
        becomes ``"Data Model Specification > Conformance"``.

        This makes prefix matching robust to spec chapter renumbering — "7.3" today
        may become "8.3" in the next spec revision, but the title stays stable.
        """
        return " > ".join(
            cls._SECTION_NUM_RE.sub("", seg.strip())
            for seg in path.split(" > ")
        )

    def build_prompt_sections(self, section_configs: list | None = None) -> int:
        """Consolidate spec sections into PROMPT_SECTION nodes for LLM system prompts.

        ``section_configs`` is a list of dicts, each with:
          - ``path_prefix`` — substring matched (case-insensitive, number-agnostic)
            against each SECTION node's ``section_path`` property.  Section numbers
            are stripped before comparing, so ``"Data Model Specification > Conformance"``
            and ``"7. Data Model Specification > 7.3. Conformance"`` are equivalent.
            All SECTION nodes whose normalized path contains the normalized prefix are
            merged into one PROMPT_SECTION node.
          - ``label`` — human-readable name; also used as the node ID suffix so
            it must be unique across entries.

        When ``section_configs`` is None the class-level ``_DEFAULT_PROMPT_SECTIONS``
        are used (backward-compatible for direct / test calls).

        Returns the number of PROMPT_SECTION nodes created or updated.
        Idempotent — re-running replaces existing nodes.
        """
        from src.knowledge_graph.base_graph import GraphNode, NodeType

        configs = section_configs if section_configs is not None else self._DEFAULT_PROMPT_SECTIONS
        created = 0

        for entry in configs:
            path_prefix: str = entry.get("path_prefix", "")
            label: str = entry.get("label", path_prefix)
            if not path_prefix:
                continue

            # Entries with summary_file are loaded directly from disk at prompt time —
            # skip them here so protocol chapters don't get baked into KG nodes and
            # injected into every system prompt regardless of chunk area.
            if entry.get("summary_file"):
                continue

            # Normalize: strip section numbers so "7.3. Conformance" == "Conformance"
            prefix_normalized = self._strip_section_numbers(path_prefix).lower()
            section_hits: list = []
            seen: set = set()

            for _nid, data in self._graph.nodes(data=True):
                obj = data.get("obj")
                if obj is None or obj.node_type != NodeType.SECTION:
                    continue
                sec_path = obj.properties.get("section_path") or obj.label or ""
                sec_normalized = self._strip_section_numbers(sec_path).lower()
                if prefix_normalized not in sec_normalized:
                    continue
                full_text = (obj.properties.get("full_text") or "").strip()
                if not full_text or sec_path in seen:
                    continue
                seen.add(sec_path)
                section_hits.append((sec_path, full_text))

            section_hits.sort(key=lambda t: t[0])
            parts = [f"--- {sp} ---\n{txt}" for sp, txt in section_hits]
            if not parts:
                continue

            total = sum(len(txt) for _, txt in section_hits)
            node_id = f"PROMPT_SECTION::{label}"
            gn = GraphNode(
                node_id=node_id,
                node_type=NodeType.PROMPT_SECTION,
                label=label,
                properties={
                    "full_text": "\n\n".join(parts),
                    "path_prefix": path_prefix,
                    "label": label,
                    "subsections_included": len(parts),
                    "chars": total,
                },
            )
            self._graph.add_node(node_id, obj=gn)
            created += 1
            logger.debug(
                "[build_prompt_sections] created PROMPT_SECTION::%s  "
                "(%d subsections, %d chars)",
                label, len(parts), total,
            )

        return created

    def export_json(self, path: str | Path) -> None:
        """Export the graph to a JSON file."""
        import networkx as nx

        data = nx.node_link_data(self._graph)

        for node in data.get("nodes", []):
            if "obj" in node:
                obj: GraphNode = node.pop("obj")
                node.update({
                    "node_type": obj.node_type,
                    "label": obj.label,
                    "properties": obj.properties,
                })
        # NetworkX 3.x uses "edges" key; 2.x used "links". Handle both.
        edges_key = "edges" if "edges" in data else "links"
        for link in data.get(edges_key, []):
            if "edge_type" in link:
                link["edge_type"] = str(link["edge_type"])

        Path(path).write_text(json.dumps(data, indent=2))
        logger.info("Graph exported to %s (%d nodes, %d edges)",
                    path, self._graph.number_of_nodes(), self._graph.number_of_edges())

    def export_graphviz(self, path: str | Path) -> None:
        """Export the graph to a Graphviz DOT file."""
        lines = ["digraph MatterRAG {", "  rankdir=LR;"]
        for node_id, data in self._graph.nodes(data=True):
            obj: GraphNode = data.get("obj")
            if obj:
                label = obj.label.replace('"', '\\"')
                lines.append(f'  "{node_id}" [label="{label}" shape=box];')
        for src, tgt, data in self._graph.edges(data=True):
            et = data.get("edge_type", "")
            lines.append(f'  "{src}" -> "{tgt}" [label="{et}"];')
        lines.append("}")
        Path(path).write_text("\n".join(lines))
        logger.info("Graph exported to DOT: %s", path)

    def export_subgraph(self, path: str | Path, source_filter: str) -> None:
        """Export a filtered sub-graph containing only nodes from one source.

        Parameters
        ----------
        path:
            Destination JSON file path.
        source_filter:
            ``"data_model"`` — nodes where ``properties["source"] == "data_model"``
            ``"spec"``        — nodes where ``properties["doc_type"] == "spec"``
            ``"test_plan"``   — nodes where ``properties["doc_type"] == "test_plan"``

        Note: This legacy method uses ``nx.subgraph`` which drops cross-component edges.
        Use ``export_component()`` for the composable architecture where cross-graph
        edges (e.g. TEST_CASE→CLUSTER) must be preserved.
        """
        def _matches(ndata: dict) -> bool:
            obj = ndata.get("obj")
            if obj is None:
                return False
            props = obj.properties
            if source_filter == "data_model":
                return props.get("source") == "data_model"
            return props.get("doc_type") == source_filter

        keep = {nid for nid, ndata in self._graph.nodes(data=True) if _matches(ndata)}
        if not keep:
            logger.warning(
                "export_subgraph: no nodes matched source_filter=%r — skipping %s",
                source_filter, path,
            )
            return

        # nx.subgraph returns a view; .copy() materialises it so export_json can mutate it
        subg = self._graph.subgraph(keep).copy()
        tmp = MatterKGBuilder(self.config)
        tmp._graph = subg
        tmp.export_json(path)
        logger.info(
            "Subgraph '%s' exported to %s (%d nodes, %d edges)",
            source_filter, path, subg.number_of_nodes(), subg.number_of_edges(),
        )

    def export_component(self, path: str | Path, source_filter: str) -> None:
        """Export a KG component — component nodes plus ALL their outbound edges.

        Unlike ``export_subgraph()`` which uses ``nx.subgraph`` and silently drops
        edges that cross component boundaries, this method preserves every outbound
        edge from the component nodes even when the edge target belongs to a different
        component (e.g. TEST_CASE→CLUSTER, REQUIREMENT→ATTRIBUTE).

        The resulting file can be loaded with ``load_from_json()`` and combined with
        other component files via ``load_from_components()`` to reconstruct the full
        merged graph.

        Parameters
        ----------
        source_filter:
            Same semantics as ``export_subgraph``:
            ``"data_model"`` / ``"spec"`` / ``"test_plan"``
        """
        def _matches(ndata: dict) -> bool:
            obj = ndata.get("obj")
            if obj is None:
                return False
            props = obj.properties
            if source_filter == "data_model":
                return props.get("source") == "data_model"
            return props.get("doc_type") == source_filter

        keep = {nid for nid, ndata in self._graph.nodes(data=True) if _matches(ndata)}
        if not keep:
            logger.warning(
                "export_component: no nodes matched source_filter=%r — skipping %s",
                source_filter, path,
            )
            return

        # Serialize manually so we control exactly which nodes appear in the file.
        # Only `keep` nodes are written to the nodes array — foreign targets referenced
        # by edges are intentionally omitted here.  When load_from_json() reconstructs
        # the file, NetworkX may auto-create stub nodes for those foreign targets;
        # load_from_json() skips stubs (nodes with no attributes) so the caller must
        # load components in canonical order (data_model → spec → test_plan) or use
        # load_from_components() which handles ordering automatically.
        nodes_list = []
        for nid in keep:
            ndata = self._graph.nodes[nid]
            obj: GraphNode = ndata.get("obj")
            if obj:
                nodes_list.append({
                    "id": nid,
                    "node_type": obj.node_type.value,
                    "label": obj.label,
                    "properties": obj.properties,
                })

        edges_list = []
        for src, tgt, attrs in self._graph.edges(data=True):
            if src in keep:
                edge = {"source": src, "target": tgt}
                edge.update({k: str(v) if k == "edge_type" else v for k, v in attrs.items()})
                edges_list.append(edge)

        data = {
            "directed": True,
            "multigraph": False,
            "graph": {"component": source_filter},
            "nodes": nodes_list,
            "links": edges_list,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2))
        logger.info(
            "Component '%s' exported to %s (%d nodes, %d edges, %d cross-component edges)",
            source_filter, path, len(nodes_list), len(edges_list),
            sum(1 for e in edges_list if e["target"] not in keep),
        )

    def export_llm_component(self, path: str | Path) -> None:
        """Export LLM-enriched edges as a standalone component file.

        Only edges whose NetworkX attrs contain ``source='spec_llm'`` are exported,
        together with both endpoint nodes (so the file is self-contained and can be
        loaded independently with ``load_from_json()``).

        When merged via ``load_from_components()`` after the other three components,
        the ``load_from_json()`` ``has_node`` guard ensures the endpoint nodes are not
        overwritten — only the new edges are added to the graph.
        """
        llm_edges = []
        endpoint_ids: set = set()
        for src, tgt, attrs in self._graph.edges(data=True):
            if attrs.get("source") == "spec_llm":
                edge = {"source": src, "target": tgt}
                edge.update({k: str(v) if k == "edge_type" else v for k, v in attrs.items()})
                llm_edges.append(edge)
                endpoint_ids.add(src)
                endpoint_ids.add(tgt)

        if not llm_edges:
            logger.warning(
                "export_llm_component: no source='spec_llm' edges found — skipping %s", path
            )
            return

        nodes_list = []
        for nid in endpoint_ids:
            if not self._graph.has_node(nid):
                continue
            ndata = self._graph.nodes[nid]
            obj: GraphNode = ndata.get("obj")
            if obj:
                nodes_list.append({
                    "id": nid,
                    "node_type": obj.node_type.value,
                    "label": obj.label,
                    "properties": obj.properties,
                })

        data = {
            "directed": True,
            "multigraph": False,
            "graph": {"component": "spec_llm"},
            "nodes": nodes_list,
            "links": llm_edges,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2))
        logger.info(
            "LLM component exported to %s (%d edge-endpoint nodes, %d llm edges)",
            path, len(nodes_list), len(llm_edges),
        )

    def load_from_components(
        self,
        component_paths: "list[str | Path]",
    ) -> None:
        """Merge multiple component KG files into this graph.

        Loads components in the order given.  The canonical order is:

            [data_model_kg.json, spec_kg.json, test_plan_kg.json]

        This ensures that schema nodes (CLUSTER, ATTRIBUTE, …) are present before
        spec and test-plan edges that reference them are processed.

        Each ``load_from_json()`` call accumulates nodes and edges on top of whatever
        is already in ``self._graph``, so later components fill in nodes that were
        referenced-but-not-yet-defined by earlier components.

        Parameters
        ----------
        component_paths:
            Ordered list of JSON file paths to merge.  Missing files are skipped
            with a warning so a partial set of components still produces a useful graph.
        """
        loaded = 0
        for p in component_paths:
            p = Path(p)
            if not p.exists():
                logger.warning(
                    "[load_from_components] Component file not found — skipping: %s", p
                )
                continue
            before_nodes = self._graph.number_of_nodes()
            before_edges = self._graph.number_of_edges()
            self.load_from_json(str(p))
            logger.info(
                "[load_from_components] Loaded %s  +%d nodes  +%d edges",
                p.name,
                self._graph.number_of_nodes() - before_nodes,
                self._graph.number_of_edges() - before_edges,
            )
            loaded += 1
        logger.info(
            "[load_from_components] Merged %d components → %d nodes, %d edges total",
            loaded, self._graph.number_of_nodes(), self._graph.number_of_edges(),
        )

    def load_from_json(self, path: str) -> None:
        """Restore a previously exported graph from a JSON file."""
        import networkx as nx

        raw_data = json.loads(Path(path).read_text())

        # NetworkX 3.x node_link_graph defaults to reading the "links" key for edges,
        # but node_link_data in 3.x writes them under "edges". Normalise so loading
        # always works regardless of which NetworkX version wrote the file.
        if "links" in raw_data and "edges" not in raw_data:
            raw_data["edges"] = raw_data["links"]
        elif "edges" in raw_data and "links" not in raw_data:
            raw_data["links"] = raw_data["edges"]

        raw_graph = nx.node_link_graph(raw_data)

        for node_id, attrs in raw_graph.nodes(data=True):
            if not attrs:
                # Stub node auto-created by NetworkX when an edge references a node
                # that belongs to another component file — skip it here; the real node
                # will be populated when that component file is loaded.
                continue
            if self._graph.has_node(node_id):
                # Node already loaded from an earlier component — do not overwrite.
                continue
            raw_nt = attrs.get("node_type", "SECTION")
            # Handle "NodeType.TEST_CASE" repr written by older export_component()
            if isinstance(raw_nt, str) and raw_nt.startswith("NodeType."):
                raw_nt = raw_nt[len("NodeType."):]
            # Migrate legacy lowercase values ("Section" → "SECTION" etc.)
            raw_nt = raw_nt.upper()
            try:
                node_type = NodeType(raw_nt)
            except ValueError:
                node_type = NodeType.SECTION
            node = GraphNode(
                node_id=node_id,
                node_type=node_type,
                label=attrs.get("label", ""),
                properties=attrs.get("properties", {}),
            )
            self._graph.add_node(node_id, obj=node)

        for src, tgt, attrs in raw_graph.edges(data=True):
            edge_type_raw = attrs.get("edge_type", "")
            try:
                edge_type = EdgeType(edge_type_raw)
            except ValueError:
                logger.warning("[load_from_json] Unknown edge type %r for edge %s->%s, defaulting to REFERENCES", edge_type_raw, src, tgt)
                edge_type = EdgeType.REFERENCES
            edge_attrs = {k: v for k, v in attrs.items() if k != "edge_type"}
            self._graph.add_edge(src, tgt, edge_type=edge_type, **edge_attrs)

        logger.info(
            "Graph loaded from %s (%d nodes, %d edges)",
            path, self._graph.number_of_nodes(), self._graph.number_of_edges(),
        )
        self._rebuild_cluster_index()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def num_nodes(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def num_edges(self) -> int:
        return self._graph.number_of_edges()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_node(self, node: GraphNode) -> None:
        if not self._graph.has_node(node.node_id):
            self._graph.add_node(node.node_id, obj=node)
            if node.node_type == NodeType.CLUSTER:
                self._cluster_index[node.label.lower()] = node.node_id

    def _add_edge(self, edge: GraphEdge) -> None:
        self._graph.add_edge(
            edge.source, edge.target,
            edge_type=edge.edge_type,
            **edge.properties,
        )

    def _ensure_file_node(self, doc: Document) -> None:
        file_id = self._file_node_id(doc)
        if not self._graph.has_node(file_id):
            path = doc.metadata.get("absolute_path", doc.metadata.get("path", ""))
            self._add_node(GraphNode(
                node_id=file_id,
                node_type=NodeType.SECTION,
                label=Path(path).name if path else "Unknown",
                properties={"path": path},
            ))

    @staticmethod
    def _make_id(doc: Document) -> str:
        path = doc.metadata.get("path", "unknown")
        chunk = doc.metadata.get("chunk_index", 0)
        section = doc.metadata.get("section", "")
        base = re.sub(r"[^a-zA-Z0-9_]", "_", path)
        return f"{base}_{section}_{chunk}".strip("_")[:80]

    @staticmethod
    def _file_node_id(doc: Document) -> str:
        path = doc.metadata.get("path", "unknown")
        return "file_" + re.sub(r"[^a-zA-Z0-9_]", "_", path)[:60]

    @staticmethod
    def _infer_node_type(doc: Document) -> NodeType:
        section = doc.metadata.get("section", "").lower()
        content = doc.page_content.lower()
        content_head = content[:200]
        if any(kw in section or kw in content_head
               for kw in ("test case", "test step", "test procedure", "tc-")):
            return NodeType.TEST_CASE
        # Behavioral rules: conditional or procedural text describing HOW a device acts.
        # Check section headings first (most reliable), then content triggers.
        if any(kw in section for kw in ("behavior", "behaviour", "conformance rule",
                                         "state machine", "transition")):
            return NodeType.BEHAVIOR_RULE
        if any(kw in content_head for kw in ("upon receipt", "upon receiving",
                                              "when the device", "when a device",
                                              "in response to", "if the device",
                                              "if the node")):
            return NodeType.BEHAVIOR_RULE
        if any(kw in section or kw in content_head
               for kw in ("requirement", "shall", "must", "normative")):
            return NodeType.REQUIREMENT
        return NodeType.SECTION

    @staticmethod
    def _extract_label(doc: Document) -> str:
        section = doc.metadata.get("section", "")
        if section and section != "preamble":
            return section[:80]
        return doc.page_content[:80].strip()


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------

#: ``KnowledgeGraphBuilder`` is kept as an alias so any code written before
#: the rename continues to work without changes.
KnowledgeGraphBuilder = MatterKGBuilder
