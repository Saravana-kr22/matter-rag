"""LLM-assisted Knowledge Graph refinement for spec records.

After the rule engine extracts requirements from the spec, this module
sends each spec section to an LLM to:

1. Identify cross-cluster dependencies not captured by the DM XML FEATURE nodes
   (e.g. "If the Level Control cluster is present, the OnOff attribute shall...")
   → emits CLUSTER →[DEPENDS_ON]→ CLUSTER edges

2. Link requirement nodes to cross-cluster entities
   (e.g. a Level Control requirement that references OnOff::OnOff attribute)
   → emits REQ →[REFERENCES]→ ATTRIBUTE edges

Results are cached per-section (SHA-256 of cluster + section_path + text) so
re-runs only call the LLM for sections whose spec text has changed.

Usage::

    from src.knowledge_graph.llm_spec_refiner import LLMSpecRefiner
    from src.llm.llm_provider import get_llm

    refiner = LLMSpecRefiner(
        llm=get_llm(config.llm),
        canonical_schema=kb.canonical_schema,
        cache_path=Path("data/knowledge_graph/spec_refiner_cache.json"),
        max_sections=200,
    )
    extra_edges = refiner.refine(kb.spec_records, kb.section_records)
    # extra_edges is List[GraphEdgeRecord] — merge into GraphBundle before import
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.knowledge_graph.schema import (
        CanonicalEntityRef,
        CanonicalSchema,
        GraphEdgeRecord,
        SectionRecord,
        SpecRecord,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a structured data extraction assistant specializing in the Matter IoT "
    "protocol specification. You analyze spec text and return cross-cluster "
    "dependencies and entity references as JSON. "
    "Return ONLY valid JSON — no explanation, no markdown code fences."
)

_SECTION_PROMPT = """\
TASK: Analyze this Matter protocol specification section and extract cross-cluster relationships.

CLUSTER: {cluster_name}
SECTION: {section_title}

SPEC TEXT:
{section_text}

KNOWN ENTITIES FOR THIS CLUSTER (from DM XML — exact names):
  Attributes : {attrs}
  Commands   : {cmds}
  Events     : {events}

ALL MATTER CLUSTERS (detect cross-cluster references against this list):
{cluster_list}

INSTRUCTIONS:
1. Find every place this section explicitly names another cluster from the list above.
   These are cross-cluster dependencies of CLUSTER: {cluster_name}.
2. Find requirement sentences (containing "shall", "must", "is required") that reference
   entities belonging to a DIFFERENT cluster. Only include if the entity name is clearly
   from another cluster — do not guess.

Return ONLY this JSON (no other text):
{{
  "cross_cluster_deps": ["<exact cluster name from ALL MATTER CLUSTERS>", ...],
  "req_entity_links": [
    {{
      "req_text_fragment": "<first 80 chars of the requirement sentence>",
      "cross_cluster_entities": ["<entity name>"],
      "entity_cluster": "<cluster that owns the entity>"
    }}
  ]
}}

If there are no cross-cluster dependencies or entity links, return:
{{"cross_cluster_deps": [], "req_entity_links": []}}"""


# ---------------------------------------------------------------------------
# Stats dataclass
# ---------------------------------------------------------------------------

@dataclass
class RefinerStats:
    sections_processed: int = 0
    sections_from_cache: int = 0
    sections_skipped: int = 0
    llm_errors: int = 0
    new_depends_on_edges: int = 0
    new_references_edges: int = 0


# ---------------------------------------------------------------------------
# LLMSpecRefiner
# ---------------------------------------------------------------------------

class LLMSpecRefiner:
    """Refine a GraphBundle with LLM-identified cross-cluster edges.

    Calls the LLM once per spec section.  Results are cached by a SHA-256 key
    of (cluster, section_path, text) so subsequent runs with unchanged spec are
    free (no LLM calls).

    Parameters
    ----------
    llm:
        Any provider from ``src.llm.llm_provider`` (ClaudeProvider,
        ClaudeSubprocessProvider, OllamaProvider — all share the same interface).
    canonical_schema:
        The ``CanonicalSchema`` produced by ``DataModelExtractor``.  Used to
        build per-cluster entity lists for the prompt and to resolve entity IDs.
    cache_path:
        Where to read/write the JSON cache.  Default:
        ``data/knowledge_graph/spec_refiner_cache.json``.
    max_sections:
        Stop after processing this many sections (cost control).  Sections are
        processed in ``section_path`` order.  Default 200.
    """

    def __init__(
        self,
        llm: Any,
        canonical_schema: "CanonicalSchema",
        cache_path: Optional[Path] = None,
        max_sections: int = 200,
    ) -> None:
        self.llm = llm
        self.schema = canonical_schema
        self.cache_path = cache_path or Path(
            "data/knowledge_graph/spec_refiner_cache.json"
        )
        self.max_sections = max_sections
        self._cache: Dict[str, Any] = self._load_cache()

        # Cluster name → canonical cluster ID map
        self._cluster_ids: Dict[str, str] = {
            c.name: c.id for c in canonical_schema.clusters
        }
        # Sorted cluster name list (deterministic prompt)
        self._cluster_names: List[str] = sorted(self._cluster_ids.keys())
        # Entity name (lowercase) → CanonicalEntityRef
        self._entity_by_name: Dict[str, "CanonicalEntityRef"] = {}
        for cl in canonical_schema.clusters:
            for e in cl.entities:
                self._entity_by_name[e.name.lower()] = e

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refine(
        self,
        spec_records: "List[SpecRecord]",
        section_records: "List[SectionRecord]",
    ) -> "List[GraphEdgeRecord]":
        """Run LLM refinement; return extra edges to merge into the GraphBundle.

        The caller should deduplicate before appending to avoid double-edges
        (``build_knowledge_graph_node`` does this automatically).

        Returns
        -------
        List[GraphEdgeRecord]
            New DEPENDS_ON and REFERENCES edges discovered by the LLM.
        """
        from src.knowledge_graph.schema import GraphEdgeRecord, GraphEdgeType  # noqa: F401

        sections = self._group_records(spec_records, section_records)
        stats = RefinerStats()
        extra_edges: "List[GraphEdgeRecord]" = []

        # Pre-scan to count how many sections will actually need an LLM call
        # (not in cache, not skipped, within max_sections budget).
        _llm_needed = 0
        _cached_count = 0
        _skipped_count = 0
        for _sk, _sd in sorted(sections.items()):
            _cn = _sd["cluster"]
            _st = _sd["text"].strip()
            if not _cn or _cn not in self._cluster_ids or not _st:
                _skipped_count += 1
                continue
            _ck = _make_cache_key(_cn, _sk, _st)
            if self._cache.get(_ck):
                _cached_count += 1
            elif _llm_needed < self.max_sections:
                _llm_needed += 1
        logger.info(
            "[llm_spec_refiner] Starting — total=%d  need_llm=%d  cached=%d  "
            "skipped=%d  max_sections=%d",
            len(sections), _llm_needed, _cached_count, _skipped_count, self.max_sections,
        )

        processed = 0
        llm_call_num = 0
        for sec_key, sec_data in sorted(sections.items()):
            if processed >= self.max_sections:
                logger.info(
                    "[llm_spec_refiner] max_sections=%d reached — %d sections not processed.",
                    self.max_sections, len(sections) - processed,
                )
                break

            cluster_name = sec_data["cluster"]
            if not cluster_name or cluster_name not in self._cluster_ids:
                stats.sections_skipped += 1
                continue

            section_text = sec_data["text"].strip()
            if not section_text:
                stats.sections_skipped += 1
                continue

            cache_key = _make_cache_key(cluster_name, sec_key, section_text)
            cached = self._cache.get(cache_key)

            if cached:
                stats.sections_from_cache += 1
                result = cached["result"]
            else:
                llm_call_num += 1
                logger.info(
                    "[llm_spec_refiner] [%d/%d] LLM call — cluster=%r  section=%r",
                    llm_call_num, _llm_needed, cluster_name, sec_data["title"],
                )
                result = self._call_llm(
                    cluster_name=cluster_name,
                    section_title=sec_data["title"],
                    section_text=section_text,
                )
                if result is None:
                    stats.llm_errors += 1
                    processed += 1
                    continue
                self._cache[cache_key] = {
                    "ts": _now_iso(),
                    "cluster": cluster_name,
                    "section_path": sec_key,
                    "result": result,
                }
                stats.sections_processed += 1
                # Save cache after every LLM call so a killed run preserves completed work.
                self._save_cache()

            new_edges = self._result_to_edges(
                cluster_name=cluster_name,
                result=result,
                spec_records_in_section=sec_data["spec_records"],
            )
            extra_edges.extend(new_edges)
            for e in new_edges:
                if e.edge_type.value == "depends_on":
                    stats.new_depends_on_edges += 1
                else:
                    stats.new_references_edges += 1

            processed += 1

        self._save_cache()  # final save (covers cached-only runs where no per-call save ran)

        logger.info(
            "[llm_spec_refiner] Done — processed=%d  cached=%d  skipped=%d  "
            "errors=%d  new_depends_on=%d  new_references=%d",
            stats.sections_processed, stats.sections_from_cache,
            stats.sections_skipped, stats.llm_errors,
            stats.new_depends_on_edges, stats.new_references_edges,
        )
        return extra_edges

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    def _group_records(
        self,
        spec_records: "List[SpecRecord]",
        section_records: "List[SectionRecord]",
    ) -> Dict[str, Any]:
        """Group SpecRecords by section_path, enriched with SectionRecord metadata."""
        sec_title_map: Dict[str, str] = {}
        sec_cluster_map: Dict[str, str] = {}
        sec_fulltext_map: Dict[str, str] = {}
        for sr in section_records:
            if sr.section_path:
                sec_title_map[sr.section_path] = sr.title
                sec_fulltext_map[sr.section_path] = getattr(sr, "full_text", "") or ""
                if sr.cluster:
                    sec_cluster_map[sr.section_path] = sr.cluster

        sections: Dict[str, Any] = {}
        for rec in spec_records:
            key = rec.section_path or rec.cluster or "unknown"
            if key not in sections:
                cluster = rec.cluster or sec_cluster_map.get(key, "")
                # Prefer SectionRecord.full_text; fall back to joining sentences
                sections[key] = {
                    "cluster": cluster,
                    "title": sec_title_map.get(key, key),
                    "spec_records": [],
                    "text": sec_fulltext_map.get(key, ""),
                }
            sections[key]["spec_records"].append(rec)
            # Accumulate sentence text as fallback
            if not sections[key]["text"] and rec.normative_text:
                sections[key]["text"] += rec.normative_text + " "

        # If full_text was empty, the accumulated sentences are now in "text"
        return sections

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        cluster_name: str,
        section_title: str,
        section_text: str,
    ) -> Optional[Dict]:
        prompt = self._build_prompt(cluster_name, section_title, section_text)
        try:
            raw = self.llm.complete(prompt, system=_SYSTEM_PROMPT)
            return _parse_json(raw)
        except Exception as exc:
            logger.warning(
                "[llm_spec_refiner] LLM error  cluster=%r  section=%r: %s",
                cluster_name, section_title, exc,
            )
            return None

    def _build_prompt(
        self,
        cluster_name: str,
        section_title: str,
        section_text: str,
    ) -> str:
        cluster_rec = next(
            (c for c in self.schema.clusters if c.name == cluster_name), None
        )
        if cluster_rec:
            from src.knowledge_graph.schema import EntityType
            attrs = ", ".join(
                e.name for e in cluster_rec.entities
                if e.entity_type == EntityType.ATTRIBUTE
            ) or "none"
            cmds = ", ".join(
                e.name for e in cluster_rec.entities
                if e.entity_type == EntityType.COMMAND
            ) or "none"
            events = ", ".join(
                e.name for e in cluster_rec.entities
                if e.entity_type == EntityType.EVENT
            ) or "none"
        else:
            attrs = cmds = events = "none"

        cluster_list = "\n".join(f"  - {n}" for n in self._cluster_names)
        # Cap section text at ~3 000 chars to stay within model context
        text = section_text[:3000]

        return _SECTION_PROMPT.format(
            cluster_name=cluster_name,
            section_title=section_title,
            section_text=text,
            attrs=attrs,
            cmds=cmds,
            events=events,
            cluster_list=cluster_list,
        )

    # ------------------------------------------------------------------
    # Edge construction
    # ------------------------------------------------------------------

    def _result_to_edges(
        self,
        cluster_name: str,
        result: Dict,
        spec_records_in_section: list,
    ) -> "List[GraphEdgeRecord]":
        from src.knowledge_graph.schema import GraphEdgeRecord, GraphEdgeType

        src_id = self._cluster_ids.get(cluster_name)
        if not src_id:
            return []

        edges: "List[GraphEdgeRecord]" = []

        # 1. Cross-cluster DEPENDS_ON edges
        for dep_name in result.get("cross_cluster_deps", []):
            if not isinstance(dep_name, str):
                continue
            dep_id = self._resolve_cluster_id(dep_name)
            if dep_id and dep_id != src_id:
                edges.append(GraphEdgeRecord(
                    source=src_id,
                    target=dep_id,
                    edge_type=GraphEdgeType.DEPENDS_ON,
                    properties={"source": "llm_refiner"},
                ))
                logger.debug(
                    "[llm_spec_refiner] DEPENDS_ON: %s → %s  (hint=%r)",
                    src_id, dep_id, dep_name,
                )

        # 2. REQ → cross-cluster entity REFERENCES edges
        for link in result.get("req_entity_links", []):
            if not isinstance(link, dict):
                continue
            frag = (link.get("req_text_fragment") or "").lower().strip()
            entity_names = link.get("cross_cluster_entities") or []
            if not frag or not entity_names:
                continue

            # Find the SpecRecord whose normative_text best matches the fragment
            matching_rec = None
            for rec in spec_records_in_section:
                if frag[:40] in rec.normative_text.lower():
                    matching_rec = rec
                    break
            if matching_rec is None:
                continue

            for ename in entity_names:
                if not isinstance(ename, str):
                    continue
                entity = self._entity_by_name.get(ename.lower())
                if entity and entity.cluster != cluster_name:
                    edges.append(GraphEdgeRecord(
                        source=matching_rec.id,
                        target=entity.id,
                        edge_type=GraphEdgeType.REFERENCES,
                        properties={"source": "llm_refiner"},
                    ))
                    logger.debug(
                        "[llm_spec_refiner] REFERENCES: %s → %s",
                        matching_rec.id, entity.id,
                    )

        return edges

    def _resolve_cluster_id(self, name: str) -> Optional[str]:
        """Fuzzy-resolve a cluster name string to a canonical cluster ID."""
        if name in self._cluster_ids:
            return self._cluster_ids[name]
        n = _norm(name)
        ns = re.sub(r"cluster$", "", n)
        best_id: Optional[str] = None
        best_score = 0
        for cname, cid in self._cluster_ids.items():
            cn = _norm(cname)
            cns = re.sub(r"cluster$", "", cn)
            if ns == cns or n == cn:
                return cid  # exact normalised match
            if ns and (cns.startswith(ns) or ns.startswith(cns)):
                score = len(ns)
                if score > best_score:
                    best_score = score
                    best_id = cid
        return best_id

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _load_cache(self) -> Dict[str, Any]:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text())
            except Exception:
                return {}
        return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=2))
        except Exception as exc:
            logger.warning("[llm_spec_refiner] Could not save cache: %s", exc)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Lowercase and strip all non-alphanumeric chars for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _make_cache_key(cluster: str, section_path: str, text: str) -> str:
    h = hashlib.sha256(
        f"{cluster}|{section_path}|{text}".encode()
    ).hexdigest()
    return h[:16]  # 16 hex chars is plenty for deduplication


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(text: str) -> Optional[Dict]:
    """Extract and parse a JSON object from an LLM response string."""
    text = text.strip()
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        logger.warning("[llm_spec_refiner] No JSON object found in LLM response")
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("[llm_spec_refiner] JSON parse error: %s", exc)
        return None
