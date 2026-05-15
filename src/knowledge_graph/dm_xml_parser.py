"""DM XML parser — converts MatterXMLFetcher schema dicts to typed dataclasses.

Input:  ``FetchedDocument.metadata["schema"]`` dicts produced by
        ``src/fetcher/sources/matter_xml_fetcher.MatterXMLFetcher``.

Output: ``CanonicalSchema`` containing a ``ClusterRecord`` per cluster, each
        with a list of ``CanonicalEntityRef`` objects for attributes, commands,
        events and features.  A flat ``entity_lookup`` dict is populated for
        O(1) entity resolution by canonical ID.

Canonical ID conventions (deterministic, human-readable):
    Cluster:   ``CLUSTER::On/Off``
    Attribute: ``ATTRIBUTE::On/Off::OnOff``
    Command:   ``COMMAND::On/Off::Off``
    Event:     ``EVENT::On/Off::SwitchLatched``
    Feature:   ``FEATURE::On/Off::Lighting``

Usage::

    from src.fetcher.fetcher_registry import load_sources, create_fetcher
    from src.knowledge_graph.dm_xml_parser import parse_data_model_documents

    fetched = [doc for s in sources if s["role"] == "data_model"
               for doc in create_fetcher(s, cfg).fetch()]
    schema = parse_data_model_documents(fetched)
    entity = schema.entity_lookup.get("ATTRIBUTE::On/Off::OnOff")
"""

from __future__ import annotations

import logging
from typing import List

from src.fetcher.base_fetcher import FetchedDocument
from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    ClusterRecord,
    EntityType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_data_model_documents(documents: List[FetchedDocument]) -> CanonicalSchema:
    """Convert a list of DM-XML FetchedDocuments into a typed CanonicalSchema.

    Documents that lack ``metadata["schema"]`` are skipped with a warning.
    Duplicate cluster names (same name, different files) are merged — the
    second file's entities are *appended* to the first.
    """
    import time
    schema = CanonicalSchema()
    seen_clusters: dict[str, ClusterRecord] = {}   # cluster_name → record

    total = len(documents)
    logger.info("[dm_xml_parser] ── Stage: Parse DM XML ── %d files to process", total)
    t0 = time.time()

    for idx, doc in enumerate(documents, 1):
        raw = doc.metadata.get("schema") if doc.metadata else None
        if not raw:
            logger.debug("[dm_xml_parser] Skipping doc without schema metadata: %s", doc.path)
            continue
        cluster_name = raw.get("cluster_name", "").strip()
        if not cluster_name:
            logger.warning("[dm_xml_parser] Cluster without name in %s — skipping", doc.path)
            continue

        n_attrs   = len(raw.get("attributes", []))
        n_cmds    = len(raw.get("commands",   []))
        n_events  = len(raw.get("events",     []))
        n_feats   = len(raw.get("features",   []))

        if cluster_name in seen_clusters:
            record = seen_clusters[cluster_name]
            logger.debug("[dm_xml_parser] Merging duplicate cluster '%s' from %s", cluster_name, doc.path)
        else:
            record = ClusterRecord(
                id=f"CLUSTER::{cluster_name}",
                name=cluster_name,
                code=raw.get("cluster_id", ""),
                revision=str(raw.get("revision", "")),
                pics_code=raw.get("pics_code", ""),
                hierarchy=raw.get("hierarchy", ""),
                base_cluster=raw.get("base_cluster", ""),
                source_file=doc.path,
            )
            seen_clusters[cluster_name] = record

        record.entities.extend(_parse_attributes(raw, cluster_name))
        record.entities.extend(_parse_commands(raw, cluster_name))
        record.entities.extend(_parse_events(raw, cluster_name))
        record.entities.extend(_parse_features(raw, cluster_name))

        # Create alias ClusterRecord entries for each <clusterId> in <clusterIds>.
        # Base-cluster XML files (ConcentrationMeasurement, ResourceMonitoring, etc.)
        # list concrete cluster aliases with their own names and PICS codes.
        for alias in raw.get("cluster_aliases", []):
            alias_name = alias.get("name", "").strip()
            alias_pics = alias.get("picsCode", "").strip()
            alias_id   = alias.get("id", "").strip()
            if not alias_name or alias_name == cluster_name:
                continue
            if alias_name in seen_clusters:
                alias_rec = seen_clusters[alias_name]
            else:
                alias_rec = ClusterRecord(
                    id=f"CLUSTER::{alias_name}",
                    name=alias_name,
                    code=alias_id,
                    revision=str(raw.get("revision", "")),
                    pics_code=alias_pics,
                    hierarchy="alias",
                    base_cluster=cluster_name,
                    source_file=doc.path,
                )
                seen_clusters[alias_name] = alias_rec
                logger.debug(
                    "[dm_xml_parser]   alias cluster '%s' (PICS: %s) from base '%s'",
                    alias_name, alias_pics, cluster_name,
                )
            alias_rec.entities.extend(_parse_attributes(raw, alias_name))
            alias_rec.entities.extend(_parse_commands(raw, alias_name))
            alias_rec.entities.extend(_parse_events(raw, alias_name))
            alias_rec.entities.extend(_parse_features(raw, alias_name))

        logger.info(
            "[dm_xml_parser] (%d/%d) %-45s  attrs=%d  cmds=%d  events=%d  features=%d",
            idx, total, cluster_name, n_attrs, n_cmds, n_events, n_feats,
        )

    schema.clusters = list(seen_clusters.values())

    # Build flat lookup dicts
    for cluster in schema.clusters:
        schema.cluster_lookup[cluster.name.lower()] = cluster
        for entity in cluster.entities:
            schema.entity_lookup[entity.id] = entity

    elapsed = time.time() - t0
    logger.info(
        "[dm_xml_parser] ── Done ── %d clusters, %d entities  (%.1fs)",
        len(schema.clusters), len(schema.entity_lookup), elapsed,
    )
    return schema


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_attributes(raw: dict, cluster_name: str) -> List[CanonicalEntityRef]:
    out: List[CanonicalEntityRef] = []
    for attr in raw.get("attributes", []):
        name = attr.get("name", "").strip()
        if not name:
            continue
        out.append(CanonicalEntityRef(
            id=f"ATTRIBUTE::{cluster_name}::{name}",
            entity_type=EntityType.ATTRIBUTE,
            name=name,
            cluster=cluster_name,
            code=attr.get("id", ""),
            datatype=attr.get("type", ""),
            access=attr.get("access", ""),
            conformance=attr.get("conformance", ""),
            quality=attr.get("quality", ""),
            default=attr.get("default", ""),
        ))
    return out


def _parse_commands(raw: dict, cluster_name: str) -> List[CanonicalEntityRef]:
    out: List[CanonicalEntityRef] = []
    for cmd in raw.get("commands", []):
        name = cmd.get("name", "").strip()
        if not name:
            continue
        out.append(CanonicalEntityRef(
            id=f"COMMAND::{cluster_name}::{name}",
            entity_type=EntityType.COMMAND,
            name=name,
            cluster=cluster_name,
            code=cmd.get("id", ""),
            direction=cmd.get("direction", ""),
            response=cmd.get("response", ""),
            conformance=cmd.get("conformance", ""),
            access=cmd.get("access", ""),
        ))
    return out


def _parse_events(raw: dict, cluster_name: str) -> List[CanonicalEntityRef]:
    out: List[CanonicalEntityRef] = []
    for evt in raw.get("events", []):
        name = evt.get("name", "").strip()
        if not name:
            continue
        out.append(CanonicalEntityRef(
            id=f"EVENT::{cluster_name}::{name}",
            entity_type=EntityType.EVENT,
            name=name,
            cluster=cluster_name,
            code=evt.get("id", ""),
            priority=evt.get("priority", ""),
            conformance=evt.get("conformance", ""),
        ))
    return out


def _parse_features(raw: dict, cluster_name: str) -> List[CanonicalEntityRef]:
    out: List[CanonicalEntityRef] = []
    for feat in raw.get("features", []):
        name = feat.get("name", "").strip()
        if not name:
            continue
        out.append(CanonicalEntityRef(
            id=f"FEATURE::{cluster_name}::{name}",
            entity_type=EntityType.FEATURE,
            name=name,
            cluster=cluster_name,
            bit=str(feat.get("bit", "")),
            code_short=feat.get("code", ""),
            conformance=feat.get("conformance", ""),
            summary=feat.get("summary", ""),
        ))
    return out
