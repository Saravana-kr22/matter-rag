"""Knowledge Base orchestrator — assembles the full KnowledgeBase from all sources.

This module is the top-level entry point for the new KB pipeline.  It wires
together all five components:

    1.  ``dm_xml_parser``      → CanonicalSchema
    2.  ``spec_extractor``     → List[SpecRecord]
    3.  ``test_plan_extractor``→ List[TestCaseRecord]
    4.  ``kb_graph_builder``   → GraphBundle
    5.  ``vector_chunk_gen``   → List[VectorChunkRecord]

Usage::

    from src.knowledge_graph.knowledge_base import KnowledgeBaseBuilder

    kb_builder = KnowledgeBaseBuilder()
    kb = kb_builder.build(
        data_model_docs=data_model_fetched,   # FetchedDocument list, role="data_model"
        spec_docs=spec_chunks,                # Document list, role="spec"
        test_plan_docs=test_plan_fetched,     # FetchedDocument list, role="test_plan"
    )

    # Persist
    kb_builder.export_json(kb, "data/knowledge_graph/kb.json")

    # Load
    kb2 = kb_builder.load_json("data/knowledge_graph/kb.json")

The exported JSON has four top-level keys::

    {
      "canonical_schema": {...},
      "spec_records": [...],
      "test_case_records": [...],
      "graph": {"nodes": [...], "edges": [...]},
      "vector_chunks": [...]
    }
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.fetcher.base_fetcher import FetchedDocument
from src.knowledge_graph.dm_xml_parser import parse_data_model_documents
from src.knowledge_graph.kb_graph_builder import build_graph, validate_graph
from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    ClusterRecord,
    ConditionRecord,
    ConstraintRecord,
    EffectRecord,
    EntityType,
    GraphBundle,
    GraphEdgeRecord,
    GraphEdgeType,
    GraphNodeRecord,
    GraphNodeType,
    KnowledgeBase,
    RequirementType,
    SectionRecord,
    SpecRecord,
    TestCaseRecord,
    TestIntent,
    TestMode,
    VectorChunkRecord,
    VectorChunkType,
)
from src.knowledge_graph.spec_extractor import extract_spec_sections_and_records
from src.knowledge_graph.test_plan_extractor import extract_test_cases
from src.knowledge_graph.vector_chunk_gen import generate_vector_chunks

logger = logging.getLogger(__name__)


class KnowledgeBaseBuilder:
    """Orchestrator: builds and persists the full KnowledgeBase."""

    def build(
        self,
        data_model_docs: Optional[List[FetchedDocument]] = None,
        spec_docs: Optional[list] = None,         # List[Document] from loader
        test_plan_docs: Optional[List[FetchedDocument]] = None,
        output_dir: str = "",                     # write per-stage rejected logs here
        max_workers: int = 0,                     # parallel workers for spec HTML parsing
    ) -> KnowledgeBase:
        """Build a fresh KnowledgeBase from source documents."""
        import time
        t_total = time.time()
        stage_times: dict = {}

        logger.info(
            "[kb] ════ KnowledgeBase Build Start ════  "
            "data_model=%d  spec=%d  test_plan=%d",
            len(data_model_docs or []),
            len(spec_docs or []),
            len(test_plan_docs or []),
        )

        # ── 1. DM XML → CanonicalSchema ──────────────────────────────────────
        t0 = time.time()
        if data_model_docs:
            canonical_schema = parse_data_model_documents(data_model_docs)
        else:
            logger.info("[kb] Stage 1/5 — No data model documents — empty CanonicalSchema")
            canonical_schema = CanonicalSchema()
        stage_times["1_dm_xml"] = time.time() - t0

        # Inform rule_engine which prefixes are real clusters (from DM XML) so
        # that _is_protocol_prefix() correctly classifies TC families without a
        # hardcoded frozenset.  Must run before test_plan_extractor and kb_graph_builder.
        from src.knowledge_graph.rule_engine import configure_known_cluster_prefixes
        _cluster_pics_prefixes = {c.pics_code.upper() for c in canonical_schema.clusters if c.pics_code}
        configure_known_cluster_prefixes(_cluster_pics_prefixes)
        logger.debug(
            "[kb] configured %d known cluster PICS prefixes for protocol detection",
            len(_cluster_pics_prefixes),
        )

        # ── 2. Spec docs → SectionRecords + SpecRecords ──────────────────────
        t0 = time.time()
        section_records: List[SectionRecord] = []
        spec_records: List[SpecRecord] = []
        rejected_candidates = []
        if spec_docs:
            section_records, spec_records, rejected_candidates = extract_spec_sections_and_records(
                spec_docs, canonical_schema=canonical_schema,
                max_workers=max_workers, output_dir=output_dir,
            )
        else:
            logger.info("[kb] Stage 2/5 — No spec documents — skipping requirement extraction")
        stage_times["2_spec"] = time.time() - t0

        # ── 3. Test plan docs → TestCaseRecords ───────────────────────────────
        t0 = time.time()
        tc_records: List[TestCaseRecord] = []
        if test_plan_docs:
            tc_records = extract_test_cases(
                test_plan_docs, canonical_schema=canonical_schema, spec_records=spec_records,
                max_workers=max_workers,
            )
        else:
            logger.info("[kb] Stage 3/5 — No test plan documents — skipping TC extraction")
        stage_times["3_test_plan"] = time.time() - t0

        # ── 4. Build graph ────────────────────────────────────────────────────
        t0 = time.time()
        graph = build_graph(
            canonical_schema, spec_records, tc_records,
            section_records=section_records,
            rejected_candidates=rejected_candidates,
        )
        stage_times["4_graph"] = time.time() - t0

        # ── 4b. Validate graph ────────────────────────────────────────────────
        validation_report = validate_graph(graph, rejected_candidates=rejected_candidates)

        # ── 5. Generate vector chunks ─────────────────────────────────────────
        t0 = time.time()
        chunks = generate_vector_chunks(tc_records, output_dir=output_dir)
        stage_times["5_chunks"] = time.time() - t0

        total_elapsed = time.time() - t_total
        kb = KnowledgeBase(
            canonical_schema=canonical_schema,
            section_records=section_records,
            spec_records=spec_records,
            test_case_records=tc_records,
            graph=graph,
            vector_chunks=chunks,
            rejected_candidates=rejected_candidates,
            validation_report=validation_report,
        )
        logger.info(
            "[kb] ════ KnowledgeBase Build Complete ════  total=%.1fs\n"
            "       Stage timings:  dm_xml=%.1fs  spec=%.1fs  test_plan=%.1fs  graph=%.1fs  chunks=%.1fs\n"
            "       Results:  clusters=%d  entities=%d  sections=%d  spec_records=%d  "
            "test_cases=%d  graph_nodes=%d  graph_edges=%d  vector_chunks=%d  rejected=%d",
            total_elapsed,
            stage_times["1_dm_xml"], stage_times["2_spec"],
            stage_times["3_test_plan"], stage_times["4_graph"], stage_times["5_chunks"],
            len(kb.canonical_schema.clusters),
            len(kb.canonical_schema.entity_lookup),
            len(kb.section_records),
            len(kb.spec_records),
            len(kb.test_case_records),
            len(kb.graph.nodes),
            len(kb.graph.edges),
            len(kb.vector_chunks),
            len(kb.rejected_candidates),
        )
        return kb

    # ──────────────────────────────────────────────────────────────────────────
    # Serialisation
    # ──────────────────────────────────────────────────────────────────────────

    def export_json(self, kb: KnowledgeBase, path: str) -> None:
        """Persist the KnowledgeBase to a JSON file."""
        out = {
            "canonical_schema": _schema_to_dict(kb.canonical_schema),
            "section_records": [dataclasses.asdict(r) for r in kb.section_records],
            "spec_records": [dataclasses.asdict(r) for r in kb.spec_records],
            "test_case_records": [dataclasses.asdict(r) for r in kb.test_case_records],
            "graph": {
                "nodes": [dataclasses.asdict(n) for n in kb.graph.nodes],
                "edges": [dataclasses.asdict(e) for e in kb.graph.edges],
            },
            "vector_chunks": [dataclasses.asdict(c) for c in kb.vector_chunks],
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        logger.info("[kb] KnowledgeBase exported to %s", path)

    def load_json(self, path: str) -> KnowledgeBase:
        """Restore a KnowledgeBase from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        canonical_schema = _schema_from_dict(data.get("canonical_schema", {}))
        section_records = [_section_record_from_dict(d) for d in data.get("section_records", [])]
        spec_records = [_spec_record_from_dict(d) for d in data.get("spec_records", [])]
        tc_records = [_tc_record_from_dict(d) for d in data.get("test_case_records", [])]
        graph_data = data.get("graph", {})
        graph = GraphBundle(
            nodes=[_graph_node_from_dict(n) for n in graph_data.get("nodes", [])],
            edges=[_graph_edge_from_dict(e) for e in graph_data.get("edges", [])],
        )
        chunks = [_chunk_from_dict(c) for c in data.get("vector_chunks", [])]
        kb = KnowledgeBase(
            canonical_schema=canonical_schema,
            section_records=section_records,
            spec_records=spec_records,
            test_case_records=tc_records,
            graph=graph,
            vector_chunks=chunks,
        )
        logger.info("[kb] KnowledgeBase loaded from %s", path)
        return kb


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _schema_to_dict(schema: CanonicalSchema) -> dict:
    d: dict = {
        "clusters": [],
        # entity_lookup and cluster_lookup are redundant — rebuild on load
    }
    for cluster in schema.clusters:
        d["clusters"].append({
            "id": cluster.id,
            "name": cluster.name,
            "code": cluster.code,
            "revision": cluster.revision,
            "source_file": cluster.source_file,
            "entities": [dataclasses.asdict(e) for e in cluster.entities],
        })
    return d


def _schema_from_dict(d: dict) -> CanonicalSchema:
    schema = CanonicalSchema()
    for cd in d.get("clusters", []):
        entities = [_entity_from_dict(e) for e in cd.get("entities", [])]
        cluster = ClusterRecord(
            id=cd["id"],
            name=cd["name"],
            code=cd.get("code", ""),
            revision=cd.get("revision", ""),
            source_file=cd.get("source_file", ""),
            entities=entities,
        )
        schema.clusters.append(cluster)
        schema.cluster_lookup[cluster.name.lower()] = cluster
        for entity in entities:
            schema.entity_lookup[entity.id] = entity
    return schema


def _entity_from_dict(d: dict) -> CanonicalEntityRef:
    return CanonicalEntityRef(
        id=d["id"],
        entity_type=EntityType(d["entity_type"]),
        name=d["name"],
        cluster=d.get("cluster", ""),
        code=d.get("code", ""),
        datatype=d.get("datatype", ""),
        access=d.get("access", ""),
        conformance=d.get("conformance", ""),
        quality=d.get("quality", ""),
        default=d.get("default", ""),
        direction=d.get("direction", ""),
        response=d.get("response", ""),
        priority=d.get("priority", ""),
        bit=d.get("bit", ""),
        code_short=d.get("code_short", ""),
        summary=d.get("summary", ""),
    )


def _section_record_from_dict(d: dict) -> SectionRecord:
    return SectionRecord(
        id=d["id"],
        title=d.get("title", ""),
        cluster=d.get("cluster", ""),
        cluster_id=d.get("cluster_id", ""),
        full_text=d.get("full_text", ""),
        section_path=d.get("section_path", ""),
        source_doc=d.get("source_doc", ""),
    )


def _spec_record_from_dict(d: dict) -> SpecRecord:
    from src.knowledge_graph.schema import ConditionRecord, EffectRecord, ConstraintRecord
    return SpecRecord(
        id=d["id"],
        requirement_type=RequirementType(d["requirement_type"]),
        cluster=d.get("cluster", ""),
        section_id=d.get("section_id", ""),
        entity_refs=d.get("entity_refs", []),
        normative_text=d.get("normative_text", ""),
        conditions=[ConditionRecord(**c) for c in d.get("conditions", [])],
        effects=[EffectRecord(**e) for e in d.get("effects", [])],
        constraints=[ConstraintRecord(**c) for c in d.get("constraints", [])],
        section_path=d.get("section_path", ""),
        source_doc=d.get("source_doc", ""),
    )


def _tc_record_from_dict(d: dict) -> TestCaseRecord:
    return TestCaseRecord(
        id=d["id"],
        title=d.get("title", ""),
        cluster=d.get("cluster", ""),
        mode=TestMode(d.get("mode", TestMode.AMBIGUOUS)),
        intents=[TestIntent(i) for i in d.get("intents", [])],
        entity_refs=d.get("entity_refs", []),
        spec_refs=d.get("spec_refs", []),
        purpose=d.get("purpose", ""),
        dut_type=d.get("dut_type", ""),
        default_dut=d.get("default_dut", ""),
        prerequisites=d.get("prerequisites", ""),
        setup=d.get("setup", ""),
        procedure_steps=d.get("procedure_steps", []),
        expected_outcomes=d.get("expected_outcomes", []),
        all_text=d.get("all_text", ""),
        source_doc=d.get("source_doc", ""),
    )


def _graph_node_from_dict(d: dict) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_id=d["node_id"],
        node_type=GraphNodeType(d["node_type"]),
        label=d.get("label", ""),
        properties=d.get("properties", {}),
    )


def _graph_edge_from_dict(d: dict) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        source=d["source"],
        target=d["target"],
        edge_type=GraphEdgeType(d["edge_type"]),
        properties=d.get("properties", {}),
    )


def _chunk_from_dict(d: dict) -> VectorChunkRecord:
    return VectorChunkRecord(
        chunk_id=d["chunk_id"],
        tc_id=d["tc_id"],
        chunk_type=VectorChunkType(d["chunk_type"]),
        text=d.get("text", ""),
        metadata=d.get("metadata", {}),
    )
