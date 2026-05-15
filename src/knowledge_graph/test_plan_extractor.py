"""Test plan extractor — converts test plan HTML/adoc docs to TestCaseRecords.

Input:  List of ``FetchedDocument`` objects from the test plan sources.
        Prefers HTML files (parsed via ``html_semantic_parser.parse_test_cases``);
        falls back to plain text for non-HTML.

Output: List of ``TestCaseRecord`` objects, each with:
          - TC ID (e.g. "TC-OO-2.1"), title, cluster
          - mode (cluster_centric / protocol_behavior_centric / mixed / ambiguous)
          - intents (list of TestIntent values derived from procedure steps)
          - entity_refs (canonical IDs matched against CanonicalSchema)
          - spec_refs (linked SpecRecord IDs by cluster + entity overlap)
          - structured subsections: purpose, setup, procedure_steps, expected_outcomes
          - all_text (pre-built embedding string)

Classification is fully deterministic — no LLM required.

Parallelism
-----------
Set ``max_workers > 1`` (or 0 for auto) to process multiple HTML files in
parallel using ``ProcessPoolExecutor``.  Each document is processed in an
independent worker process — safe on macOS where the default start method is
``spawn``.  The ``_init_worker_paths`` initialiser ensures the project root is
on ``sys.path`` in every worker process.

Intent detection keyword map:
    read_attribute    — "TH reads", "read the", "read attribute"
    write_attribute   — "TH writes", "write", "set attribute"
    invoke_command    — "TH sends", "send command", "invoke"
    verify_event      — "verify event", "event is generated", "check event"
    verify_report     — "report", "receive report", "verify report"
    verify_subscribe  — "subscribe", "subscription"
    verify_constraint — "verify.*between", "value.*range", "constraint"
    negative_test     — "error", "failure", "invalid", "should not", "shall not"
    setup_only        — step has no assertion keyword (pure setup/config step)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.fetcher.base_fetcher import FetchedDocument
from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    SpecRecord,
    TestCaseRecord,
    TestIntent,
    TestMode,
)
from src.knowledge_graph.rule_engine import (
    classify_testcase_mode,
    detect_test_intents,
    extract_entities,
    match_entities_from_map,
    _is_protocol_prefix,
)
from src.knowledge_graph.rule_engine import _build_multicluster_name_set as _build_mc_set

logger = logging.getLogger(__name__)

# Cluster detection - prefer explicit mention in TC title/purpose
_CLUSTER_FROM_TITLE_RE = re.compile(r'\[TC-([A-Z0-9_]+)-\d', re.I)

# Map TC-ID prefix → chapter-area words used for _proto: chapter-level fallback linking.
_TC_PREFIX_TO_PROTO_AREA: Dict[str, str] = {
    "DD":      "device discovery",
    "SC":      "secure channel",
    "IDM":     "interaction data model",
    "BDX":     "bulk data exchange",
    "DA":      "device attestation",
    "ACE":     "access control",
    "MC":      "multicast commissioning",
    "JFADMIN": "fabric joining administrator",
    "JF":      "joining fabric",
    "MCORE":   "matter core",
}

# Spec chapter root names excluded from _proto: bulk linking (too generic to be useful).
_PROTO_SECTION_BLOCKLIST: frozenset = frozenset({
    "architecture",
    "cryptographic primitives",
    "data model specification",
    "device attestation",
})


# ---------------------------------------------------------------------------
# Worker initialiser (macOS spawn safety)
# ---------------------------------------------------------------------------

def _init_worker_paths() -> None:
    """Add project root to sys.path in spawned worker processes."""
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


# ---------------------------------------------------------------------------
# Module-level worker function (must be picklable — no closures)
# ---------------------------------------------------------------------------

def _process_single_doc(args: tuple):
    """Process one FetchedDocument and return its TestCaseRecords.

    Returns ``(doc_records, doc_label, doc_idx, elapsed_s)``.
    """
    (
        doc_idx, total,
        doc_path, doc_content, doc_ext,
        entity_name_map, spec_index, multicluster_names,
        canonical_schema, prefix_map, section_map,
        spec_id_to_cluster,
    ) = args

    t0 = time.time()
    doc_label = Path(doc_path).name if doc_path else f"doc-{doc_idx}"
    _pfx = f"[test_plan_extractor] ({doc_idx}/{total}) {doc_label}"

    print(f"{_pfx}: parsing HTML…", flush=True)
    if doc_ext in ("html", "htm"):
        tcs = _parse_html_raw(doc_content, doc_path)
    else:
        tcs = _parse_text_raw(doc_content)

    n_raw = len(tcs)
    print(f"{_pfx}: parsed {n_raw} raw TCs — building records…", flush=True)

    records = []
    _PROGRESS_EVERY = 100
    for i, tc_raw in enumerate(tcs, 1):
        record = _build_record(
            tc_raw, entity_name_map, spec_index, doc_path,
            canonical_schema, multicluster_names, prefix_map, section_map,
            spec_id_to_cluster,
        )
        records.append(record)
        if i % _PROGRESS_EVERY == 0 or i == n_raw:
            print(f"{_pfx}:   {i}/{n_raw} records built  ({time.time()-t0:.0f}s)", flush=True)

    elapsed = time.time() - t0
    return records, doc_label, doc_idx, elapsed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_test_cases(
    documents: List[FetchedDocument],
    canonical_schema: CanonicalSchema,
    spec_records: Optional[List[SpecRecord]] = None,
    max_workers: int = 0,   # 0 = auto (min(doc_count, cpu_count, 8))
) -> List[TestCaseRecord]:
    """Extract ``TestCaseRecord`` objects from test plan FetchedDocuments."""
    entity_name_map = _build_entity_name_map(canonical_schema)
    prefix_map = _build_prefix_map(canonical_schema)
    spec_index, spec_id_to_cluster = _build_spec_index(spec_records or [])
    section_map = _load_protocol_tc_section_map()
    multicluster_names = _build_mc_set(canonical_schema)

    total = len(documents)
    logger.info("[test_plan_extractor] ── Stage: Extract Test Cases ── %d documents to process", total)
    t0 = time.time()

    if total == 0:
        return []

    # Resolve worker count
    if total <= 1:
        workers = 1
    else:
        auto = os.cpu_count() or 1
        requested = max_workers if max_workers > 0 else auto
        workers = min(total, requested, 8)

    # Serialise CanonicalSchema minimally — pass only what workers need
    args_list = [
        (
            doc_idx, total,
            doc.path or "",
            doc.content or "",
            (doc.path or "").rsplit(".", 1)[-1].lower() if "." in (doc.path or "") else "",
            entity_name_map,
            spec_index,
            multicluster_names,
            canonical_schema,
            prefix_map,
            section_map,
            spec_id_to_cluster,
        )
        for doc_idx, doc in enumerate(documents, 1)
    ]

    all_records: List[TestCaseRecord] = []

    if workers > 1:
        logger.info(
            "[test_plan_extractor] Parallel mode — ProcessPoolExecutor(max_workers=%d)", workers,
        )
        pending: Dict[int, tuple] = {}
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker_paths,
        ) as executor:
            future_map = {
                executor.submit(_process_single_doc, args): args[0]
                for args in args_list
            }
            for future in as_completed(future_map):
                try:
                    doc_records, doc_label, doc_idx, elapsed = future.result()
                except Exception as exc:
                    doc_idx = future_map[future]
                    logger.error("[test_plan_extractor] Worker failed for doc %d: %s", doc_idx, exc)
                    continue
                pending[doc_idx] = (doc_records, doc_label, elapsed)
                logger.info(
                    "[test_plan_extractor] (%d/%d) %-40s  built=%d  (%.1fs)",
                    doc_idx, total, doc_label, len(doc_records), elapsed,
                )

        # Reconstruct in document order (skip docs whose workers failed)
        for i in range(1, total + 1):
            if i not in pending:
                continue
            doc_records, _, _ = pending[i]
            all_records.extend(doc_records)
    else:
        for args in args_list:
            doc_records, doc_label, doc_idx, elapsed = _process_single_doc(args)
            logger.info(
                "[test_plan_extractor] (%d/%d) %-40s  built=%d  (%.1fs)",
                doc_idx, total, doc_label, len(doc_records), elapsed,
            )
            all_records.extend(doc_records)

    elapsed_total = time.time() - t0
    logger.info(
        "[test_plan_extractor] ── Done ── %d test cases from %d documents  (%.1fs)",
        len(all_records), total, elapsed_total,
    )
    return all_records


# ---------------------------------------------------------------------------
# HTML / text parsing helpers (module-level so workers can call them)
# ---------------------------------------------------------------------------

def _parse_html_raw(content: str, path: str) -> List[dict]:
    """Parse HTML test plan via html_semantic_parser.parse_test_cases."""
    try:
        from src.processor.html_semantic_parser import parse_test_cases  # type: ignore
        return parse_test_cases(content, doc_id=path)
    except Exception as exc:
        logger.warning("[test_plan_extractor] HTML parse error for %s: %s", path, exc)
        return []


def _parse_text_raw(content: str) -> List[dict]:
    """Minimal fallback: extract TC headings from plain text."""
    tc_re = re.compile(r'\[TC-([A-Z0-9_]+-[\d.]+)\]?\s*(.*)', re.I)
    results = []
    current: Optional[dict] = None
    for line in (content or "").splitlines():
        m = tc_re.match(line.strip())
        if m:
            if current:
                results.append(current)
            current = {
                "tc_id": f"TC-{m.group(1).upper()}",
                "title": m.group(2).strip(),
                "all_subsections": {},
                "all_text": "",
            }
        elif current:
            current["all_text"] = current.get("all_text", "") + "\n" + line
    if current:
        results.append(current)
    return results


# ---------------------------------------------------------------------------
# HTML parsing via html_semantic_parser (kept for backward compat)
# ---------------------------------------------------------------------------

def _parse_html_doc(doc: FetchedDocument) -> List[dict]:
    return _parse_html_raw(doc.content, doc.path)


def _parse_text_doc(doc: FetchedDocument) -> List[dict]:
    return _parse_text_raw(doc.content)


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def _build_record(
    tc_raw: dict,
    entity_name_map: Dict[str, Any],
    spec_index: Dict[str, List[str]],
    source_doc: str,
    canonical_schema: Optional[CanonicalSchema] = None,
    multicluster_names: Optional[set] = None,
    prefix_map: Optional[Dict[str, str]] = None,
    section_map: Optional[dict] = None,
    spec_id_to_cluster: Optional[Dict[str, str]] = None,
) -> TestCaseRecord:
    # Support both old key ("tc_id") and actual parser key ("test_case_id")
    tc_id = (tc_raw.get("test_case_id") or tc_raw.get("tc_id") or tc_raw.get("id", "")).strip()
    title = tc_raw.get("title", "")
    all_text = tc_raw.get("all_text", "")

    # Build flat all_text if missing
    if not all_text:
        all_text = _flatten_raw_to_text(tc_raw)

    # Structured subsections — map from actual html_semantic_parser output fields
    purpose     = tc_raw.get("purpose", "") or ""
    prereqs     = _join_list(tc_raw.get("preconditions", []))
    setup       = tc_raw.get("test_setup", "") or tc_raw.get("device_topology", "")
    dut_type    = ""
    default_dut = ""

    # DUT type from required_devices list
    for dev in tc_raw.get("required_devices", []):
        if isinstance(dev, dict):
            name = dev.get("name", "")
            if "DUT" in name:
                dut_type = dev.get("description", "")

    # Procedure steps — test_procedure is a list of step dicts
    procedure_steps = []
    expected_outcomes = []
    for step in tc_raw.get("test_procedure", []):
        if isinstance(step, dict):
            action = step.get("action", "")
            expected = step.get("expected", "")
            if action:
                procedure_steps.append(action)
            if expected:
                expected_outcomes.append(expected)
        elif isinstance(step, str) and step.strip():
            procedure_steps.append(step)

    # Fallback text parsing for non-HTML (text split into steps)
    if not procedure_steps:
        procedure_raw = tc_raw.get("procedure", tc_raw.get("test_steps", ""))
        procedure_steps = _split_numbered_steps(procedure_raw)

    # Cluster inference (need a first pass with no context to seed cluster)
    full_search = f"{title}\n{all_text}"
    # First pass: infer cluster without context (uses title/TC-ID heuristics)
    entity_refs_pass1 = match_entities_from_map(
        full_search, entity_name_map,
        context_cluster="",
        multicluster_names=multicluster_names or set(),
    )
    cluster = _infer_cluster(tc_id, title, entity_refs_pass1, entity_name_map, prefix_map or {})

    # Second pass: re-run entity matching with cluster context for accuracy
    entity_refs_canonical = match_entities_from_map(
        full_search, entity_name_map,
        context_cluster=cluster,
        multicluster_names=multicluster_names or set(),
    )

    # Add protocol anchors (commissioning, BLE, DNS-SD, etc.) via rule engine
    proto_refs: List[str] = []
    if canonical_schema is not None:
        matches = extract_entities(full_search, canonical_schema, context_cluster=cluster)
        proto_refs = [m.entity_id for m in matches if m.match_source == "protocol_anchor"]

    entity_refs = list(dict.fromkeys(entity_refs_canonical + proto_refs))

    # Intent detection from procedure steps (rule engine)
    intents = detect_test_intents(title, purpose, procedure_steps)

    # Mode detection (rule engine scoring)
    mode, _cs, _ps = classify_testcase_mode(tc_id, title, purpose, procedure_steps, entity_refs)

    # Spec linkage — pass TC text for Tier 2 section-keyword matching
    spec_refs = _link_spec_records(
        cluster, entity_refs, spec_index, tc_id=tc_id,
        tc_title=title, section_map=section_map or {},
        tc_purpose=purpose, procedure_actions=procedure_steps,
        spec_id_to_cluster=spec_id_to_cluster,
    )

    return TestCaseRecord(
        id=tc_id,
        title=title,
        cluster=cluster,
        mode=mode,
        intents=intents,
        entity_refs=entity_refs,
        spec_refs=spec_refs,
        purpose=purpose,
        dut_type=dut_type,
        default_dut=default_dut,
        prerequisites=prereqs,
        setup=setup,
        procedure_steps=procedure_steps,
        expected_outcomes=expected_outcomes,
        all_text=all_text,
        source_doc=source_doc,
        pics_codes=_extract_pics_from_tc_raw(tc_raw, all_text),
    )


# ---------------------------------------------------------------------------
# Cluster inference
# ---------------------------------------------------------------------------

def _infer_cluster(
    tc_id: str,
    title: str,
    entity_refs: List[str],
    entity_name_map: Dict[str, Any],
    prefix_map: Dict[str, str],
) -> str:
    # 0. Protocol-family override: these tests target the protocol stack,
    #    not any individual cluster.  Assigning them a cluster based on
    #    incidentally-used entities (e.g. TC-IDM-1.2 reads an On/Off attribute
    #    to exercise the IDM protocol) would be wrong.
    # NOTE: _CLUSTER_FROM_TITLE_RE requires a leading '[' so use a bare-TC-ID
    # match here — tc_id arrives as "TC-ACE-1.1", not "[TC-ACE-1.1]".
    _bare_pre = re.match(r'^TC-([A-Z0-9_]+)-\d', tc_id, re.I)
    if _bare_pre and _is_protocol_prefix(_bare_pre.group(1)):
        return ""

    # 1. From TC-PREFIX tag - look up in schema-derived + override map
    m = _CLUSTER_FROM_TITLE_RE.search(tc_id)
    if m:
        prefix = m.group(1).upper()
        if prefix in prefix_map:
            return prefix_map[prefix]

    # 2. Most-referenced cluster in entity refs
    cluster_counts: Dict[str, int] = {}
    for ref in entity_refs:
        parts = ref.split("::")
        if len(parts) >= 2 and not ref.startswith("CLUSTER::"):
            cluster_counts[parts[1]] = cluster_counts.get(parts[1], 0) + 1
        elif ref.startswith("CLUSTER::") and len(parts) == 2:
            cluster_counts[parts[1]] = cluster_counts.get(parts[1], 0) + 1
    if cluster_counts:
        return max(cluster_counts, key=cluster_counts.__getitem__)

    # 3. Entity name map scan on title
    title_lower = title.lower()
    for name_lower, entity_or_list in entity_name_map.items():
        if len(name_lower) < 4:
            continue
        if name_lower in title_lower:
            entities = entity_or_list if isinstance(entity_or_list, list) else [entity_or_list]
            return entities[0].cluster

    return ""


# ---------------------------------------------------------------------------
# Entity name map builder
# ---------------------------------------------------------------------------

def _build_entity_name_map(schema: CanonicalSchema) -> Dict[str, List]:
    from src.knowledge_graph.schema import EntityType
    mapping: Dict[str, List] = {}
    for cluster in schema.clusters:
        entry = CanonicalEntityRef(
            id=cluster.id, entity_type=EntityType.CLUSTER,
            name=cluster.name, cluster=cluster.name,
        )
        mapping.setdefault(cluster.name.lower(), []).append(entry)
    for entity in schema.entity_lookup.values():
        mapping.setdefault(entity.name.lower(), []).append(entity)
    return mapping


def _build_prefix_map(schema: CanonicalSchema) -> Dict[str, str]:
    """Build a TC-prefix -> cluster-name map from DM XML picsCode attributes.

    Each DM XML cluster has a ``<classification picsCode="OO">`` element; this
    is parsed into ``ClusterRecord.pics_code`` and used here directly so no
    manual overrides are needed.  Any cluster added to the DM XML is picked up
    automatically on the next rebuild.
    """
    return {c.pics_code: c.name for c in schema.clusters if c.pics_code}


# ---------------------------------------------------------------------------
# Spec linkage
# ---------------------------------------------------------------------------

def _normalize_cluster_key(name: str) -> str:
    """Normalize cluster name for index lookup.

    Strips trailing ' Cluster' suffix and lowercases so that
    'Door Lock' and 'Door Lock Cluster' resolve to the same key.
    """
    n = name.lower().strip()
    n = re.sub(r'\s+cluster$', '', n)
    return n


def _build_spec_index(
    spec_records: List[SpecRecord],
) -> tuple:
    """Build lookup index for fast TC → spec record linking.

    Returns ``(index, spec_id_to_cluster)`` where:

    - ``index`` is a ``Dict[str, List[str]]`` with keys:
      - ``<cluster_lower>``            — cluster-bound records (normalized)
      - ``<entity_name_lower>``        — records with entity refs (by entity name only)
      - ``_proto:<chapter_words>``     — protocol-level records (chapter-level fallback)
      - ``_section:<full_path>``       — protocol records keyed by cleaned section path
      - ``_section_word:<word>``       — section-path leaf word index for Tier 2

    - ``spec_id_to_cluster`` is a ``Dict[str, str]`` mapping each spec record
      ID to its normalized cluster key (empty string for protocol-level records).
      Used for unconditional cluster filtering in ``_link_spec_records``.
    """
    index: Dict[str, List[str]] = {}
    spec_id_to_cluster: Dict[str, str] = {}
    for rec in spec_records:
        spec_id_to_cluster[rec.id] = _normalize_cluster_key(rec.cluster) if rec.cluster else ""
        if rec.cluster:
            norm_key = _normalize_cluster_key(rec.cluster)
            index.setdefault(norm_key, []).append(rec.id)
            raw_key = rec.cluster.lower()
            if raw_key != norm_key:
                index.setdefault(raw_key, []).append(rec.id)
        for ref in rec.entity_refs:
            parts = ref.split("::")
            if len(parts) >= 3:
                entity_key = parts[2].lower()
                index.setdefault(entity_key, []).append(rec.id)

        # Tier 2: section-path leaf word indexing (for all records, not just protocol)
        section_path = rec.section_path or ""
        if section_path:
            # Extract leaf segment: "4.3.1. Sigma1 Message" → "sigma1 message"
            leaf = section_path.split(" > ")[-1] if " > " in section_path else section_path
            leaf_clean = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', leaf).lower().strip()
            if leaf_clean and len(leaf_clean) > 3:
                # Index by individual words in the leaf
                for word in leaf_clean.split():
                    if len(word) > 3 and word not in _SECTION_WORD_STOP:
                        section_key = f"_section_word:{word}"
                        index.setdefault(section_key, []).append(rec.id)

                # Extract entity name from section heading patterns like:
                # "OverrunCount Attribute" → "overruncount"
                # "ResetCounts Command" → "resetcounts"
                # "AssociationFailure Event" → "associationfailure"
                # "TestMode Feature" → "testmode"
                # Also handle: "OnOff" (standalone, no type suffix)
                _entity_type_suffixes = ("attribute", "command", "event", "feature",
                                         "field", "struct", "enum", "bitmap")
                leaf_words = leaf_clean.split()
                for i, w in enumerate(leaf_words):
                    # Check if next word is an entity type suffix
                    is_before_type = (i + 1 < len(leaf_words)
                                      and leaf_words[i + 1] in _entity_type_suffixes)
                    # Check if this word IS an entity name (not a type word, not too short)
                    is_entity_name = (len(w) > 3
                                      and w not in _SECTION_WORD_STOP
                                      and w not in _entity_type_suffixes)
                    if is_entity_name and (is_before_type or len(leaf_words) == 1):
                        entity_key = f"_entity:{w}"
                        index.setdefault(entity_key, []).append(rec.id)

        if not rec.cluster and rec.section_path:
            chapter = re.sub(r'^\d[\d.]*\s*', '', rec.section_path).strip().lower()
            root_chapter = chapter.split(">")[0].strip()
            if any(root_chapter.startswith(blocked) for blocked in _PROTO_SECTION_BLOCKLIST):
                continue
            # Chapter-level fallback key (first 4–5 words)
            chapter_words = chapter.split()[:5]
            if chapter_words:
                area_key = "_proto:" + " ".join(chapter_words)
                index.setdefault(area_key, []).append(rec.id)
            # Section-path key: strip leading numbers from each breadcrumb segment
            # so "4. Device Discovery > 4.3 Commissionable Node" →
            # "_section:device discovery > commissionable node"
            segments = rec.section_path.lower().split(">")
            cleaned_segs = [re.sub(r'^\d[\d.]*\s*', '', s).strip() for s in segments]
            full_path = " > ".join(s for s in cleaned_segs if s)
            if full_path:
                index.setdefault(f"_section:{full_path}", []).append(rec.id)
    return index, spec_id_to_cluster


def _load_protocol_tc_section_map() -> dict:
    """Load ``config/protocol_tc_section_map.yaml`` for protocol TC → section linking.

    Returns an empty dict if the file is absent or malformed (graceful degradation
    to the chapter-level fallback).
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "protocol_tc_section_map.yaml"
    if not config_path.is_file():
        return {}
    try:
        import yaml as _yaml
        data = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return data or {}
    except Exception as exc:
        logger.warning("[_load_protocol_tc_section_map] Failed to load %s: %s", config_path, exc)
        return {}


def _get_mapped_sections(tc_id: str, tc_title: str, section_map: dict) -> List[str]:
    """Return spec section path substrings from the mapping file for this TC.

    Resolution order:
    1. Exact TC-ID match in ``tc_id_patterns``
    2. Case-insensitive title keyword match in ``title_patterns``
    3. Empty list → caller falls back to chapter-level bulk linking
    """
    tc_id_patterns = section_map.get("tc_id_patterns") or {}
    title_patterns = section_map.get("title_patterns") or {}

    if tc_id in tc_id_patterns:
        entry = tc_id_patterns[tc_id]
        return list(entry.get("sections", [])) if isinstance(entry, dict) else []

    title_lower = tc_title.lower()
    matched: List[str] = []
    for keyword, entry in title_patterns.items():
        if keyword.lower() in title_lower and isinstance(entry, dict):
            matched.extend(entry.get("sections", []))
    return list(dict.fromkeys(matched))


# Section-path leaf words that are too generic to produce useful Tier 2 matches.
# These appear in hundreds of section headings and would match nearly every TC.
_SECTION_WORD_STOP: frozenset = frozenset({
    "attribute", "attributes", "command", "commands", "event", "events",
    "feature", "features", "cluster", "clusters", "field", "fields",
    "type", "types", "table", "overview", "general", "introduction",
    "description", "specification", "requirements", "behavior", "protocol",
    "message", "messages", "data", "model", "section", "struct",
})


_TIER2_STOP_WORDS: frozenset = frozenset({
    "shall", "must", "should", "that", "this", "from", "with", "when",
    "then", "have", "been", "will", "were", "they", "them", "their",
    "there", "these", "those", "what", "which", "where", "each", "every",
    "verify", "check", "reads", "read", "write", "writes", "sends",
    "send", "step", "steps", "test", "following", "value", "values",
    "command", "attribute", "event", "cluster", "response", "request",
    "field", "fields", "table", "list", "type", "status", "data",
    "device", "node", "endpoint", "server", "client", "commissioner",
    "commissionee", "over", "into", "onto", "upon", "also", "does",
    "other", "about", "after", "before", "between", "during", "under",
})


def _link_spec_records(
    cluster: str,
    entity_refs: List[str],
    spec_index: Dict[str, List[str]],
    tc_id: str = "",
    tc_title: str = "",
    section_map: Optional[dict] = None,
    tc_purpose: str = "",
    procedure_actions: Optional[List[str]] = None,
    spec_id_to_cluster: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Return SpecRecord IDs linked to this TC via 3-tier matching.

    Tier 1 — Entity-level matching (most precise):
        When ``entity_refs`` is non-empty, use entity names to find matching
        requirements. Only returns entity-matched results, NOT bulk cluster ones.

    Tier 2 — Section-path keyword matching (for protocol/sparse TCs):
        When Tier 1 produces zero matches (common for protocol TCs with empty
        ``entity_refs``), extract keywords from the TC's text (title + purpose +
        step actions) and match against requirement section_path leaf word index.

    Tier 3 — Cluster-level fallback (current behavior):
        When neither Tier 1 nor Tier 2 produce matches, fall back to bulk
        cluster-level candidates.

    For protocol-family TCs (no cluster), the existing two-step resolution is
    preserved: precise section-map lookup, then chapter-level ``_proto:`` bulk
    match.
    """
    # ── Protocol-family TCs (no cluster) — existing path preserved ──────
    if not cluster and tc_id:
        return _link_protocol_spec_records(
            tc_id, tc_title, spec_index, section_map or {},
            tc_purpose=tc_purpose, procedure_actions=procedure_actions,
        )

    # ── Cluster-centric TCs — 3-tier strategy ──────────────────────────

    # Build cluster-level candidate pool (used as fallback in Tier 3)
    cluster_candidates: List[str] = []
    if cluster:
        norm_cluster = _normalize_cluster_key(cluster)
        cluster_candidates.extend(spec_index.get(norm_cluster, []))
        raw_cluster = cluster.lower()
        if raw_cluster != norm_cluster:
            cluster_candidates.extend(spec_index.get(raw_cluster, []))
        cluster_candidates = list(dict.fromkeys(cluster_candidates))

    # Tier 1: entity-level matches
    entity_matched: List[str] = []
    entity_names_searched: set = set()
    for ref in entity_refs:
        parts = ref.split("::")
        if len(parts) >= 3:
            entity_name = parts[2].lower()
            if entity_name and len(entity_name) > 2 and entity_name not in entity_names_searched:
                entity_names_searched.add(entity_name)
                entity_matched.extend(spec_index.get(entity_name, []))
                entity_matched.extend(spec_index.get(f"_section_word:{entity_name}", []))
                entity_matched.extend(spec_index.get(f"_entity:{entity_name}", []))
        elif len(parts) == 2:
            key = parts[1].lower()
            if key and len(key) > 2 and key not in entity_names_searched:
                entity_names_searched.add(key)
                entity_matched.extend(spec_index.get(key, []))
                entity_matched.extend(spec_index.get(f"_section_word:{key}", []))
                entity_matched.extend(spec_index.get(f"_entity:{key}", []))

    # Deduplicate entity matches
    entity_matched = list(dict.fromkeys(entity_matched))

    # Tier 1.5: When entity_refs produced nothing, extract entity names from
    # procedure step text. Steps like "TH reads OverrunCount attribute from DUT"
    # contain the exact entity name. This is more precise than Tier 2 keyword
    # matching because it identifies WHAT the TC exercises, not just generic words.
    if not entity_matched and procedure_actions:
        _STEP_ENTITY_RE = re.compile(
            r'(?:reads?|writes?|sends?|subscribes?\s+to|verifies?|checks?)\s+'
            r'(?:a\s+|the\s+|an\s+)?'
            r'([A-Z][A-Za-z0-9]+(?:[A-Z][a-z0-9]+)*)',  # CamelCase entity name
            re.I,
        )
        step_entities: set = set()
        for step in procedure_actions:
            for m in _STEP_ENTITY_RE.finditer(step):
                ename = m.group(1).lower()
                if len(ename) > 3 and ename not in _TIER2_STOP_WORDS:
                    step_entities.add(ename)
        # Also extract entity names from PICS codes in step text
        # Pattern: PICS_PREFIX.S.A0000(EntityName) or PICS_PREFIX.S.C00(CommandName)
        _PICS_ENTITY_RE = re.compile(r'[A-Z]+\.S\.[ACE][0-9a-f]+\(([A-Za-z]+)\)', re.I)
        for step in procedure_actions:
            for m in _PICS_ENTITY_RE.finditer(step):
                ename = m.group(1).lower()
                if len(ename) > 3:
                    step_entities.add(ename)

        if step_entities:
            for ename in step_entities:
                # Check bare entity name, section-word index, AND entity index
                entity_matched.extend(spec_index.get(ename, []))
                entity_matched.extend(spec_index.get(f"_section_word:{ename}", []))
                entity_matched.extend(spec_index.get(f"_entity:{ename}", []))
            entity_matched = list(dict.fromkeys(entity_matched))
            logger.debug(
                "[_link_spec_records] TC %s: Tier 1.5 extracted %d step entities: %s",
                tc_id, len(step_entities), ", ".join(sorted(step_entities)[:10]),
            )

    # Filter entity matches to only include those belonging to the TC's cluster.
    # Uses spec_id_to_cluster reverse map for unconditional filtering (even when
    # cluster_candidates is empty, e.g. VirtualClusters).
    if entity_matched and cluster:
        norm_tc = _normalize_cluster_key(cluster)
        if spec_id_to_cluster:
            entity_matched = [
                sid for sid in entity_matched
                if spec_id_to_cluster.get(sid, "") == norm_tc
            ]
        elif cluster_candidates:
            cluster_set = set(cluster_candidates)
            entity_matched = [c for c in entity_matched if c in cluster_set]

    if entity_matched:
        # Tier 1 wins — entity-level precision
        return entity_matched

    # Tier 2: section-path keyword matching
    section_matched: List[str] = []
    tc_text_parts = [tc_title]
    if tc_purpose:
        tc_text_parts.append(tc_purpose)
    if procedure_actions:
        tc_text_parts.extend(procedure_actions)
    tc_text = " ".join(tc_text_parts).lower()
    tc_words = set(re.findall(r'[a-z]{4,}', tc_text))
    tc_words -= _TIER2_STOP_WORDS

    if tc_words:
        for word in tc_words:
            section_key = f"_section_word:{word}"
            section_matched.extend(spec_index.get(section_key, []))
        section_matched = list(dict.fromkeys(section_matched))
        if section_matched and cluster:
            norm_tc = _normalize_cluster_key(cluster)
            if spec_id_to_cluster:
                section_matched = [
                    sid for sid in section_matched
                    if spec_id_to_cluster.get(sid, "") == norm_tc
                ]
            elif cluster_candidates:
                cluster_set = set(cluster_candidates)
                section_matched = [c for c in section_matched if c in cluster_set]

    if section_matched:
        # Tier 2 — section-keyword matching
        return section_matched

    # Tier 3: cluster-level fallback (same as previous behavior)
    return cluster_candidates


def _link_protocol_spec_records(
    tc_id: str,
    tc_title: str,
    spec_index: Dict[str, List[str]],
    section_map: dict,
    tc_purpose: str = "",
    procedure_actions: Optional[List[str]] = None,
) -> List[str]:
    """Link protocol-family TCs (no cluster) to spec records.

    Three-step resolution:
    1. Precise section-map lookup from ``config/protocol_tc_section_map.yaml``
    2. Section-path keyword matching from TC text (Tier 2)
    3. Chapter-level ``_proto:`` bulk match as fallback
    """
    candidates: List[str] = []
    m_pre = re.match(r'^TC-([A-Z0-9]+)-', tc_id, re.I)
    if not m_pre:
        return []

    prefix = m_pre.group(1).upper()
    area = _TC_PREFIX_TO_PROTO_AREA.get(prefix, "")
    if not area:
        return []

    # Step 1: Precise section-map lookup
    mapped_sections = _get_mapped_sections(tc_id, tc_title, section_map)
    if mapped_sections:
        for mapped in mapped_sections:
            mapped_lower = mapped.lower()
            for key, ids in spec_index.items():
                if key.startswith("_section:") and mapped_lower in key[len("_section:"):]:
                    candidates.extend(ids)

    # Step 2: Section-path keyword matching from TC text (Tier 2)
    if not candidates:
        tc_text_parts = [tc_title]
        if tc_purpose:
            tc_text_parts.append(tc_purpose)
        if procedure_actions:
            tc_text_parts.extend(procedure_actions)
        tc_text = " ".join(tc_text_parts).lower()
        tc_words = set(re.findall(r'[a-z]{4,}', tc_text))
        tc_words -= _TIER2_STOP_WORDS

        if tc_words:
            for word in tc_words:
                candidates.extend(spec_index.get(f"_section_word:{word}", []))
                candidates.extend(spec_index.get(f"_entity:{word}", []))
            candidates = list(dict.fromkeys(candidates))

    # Step 3: Chapter-level fallback (only if steps 1 and 2 found nothing)
    if not candidates:
        area_words = set(area.split())
        for key, ids in spec_index.items():
            if key.startswith("_proto:"):
                key_words = set(key[len("_proto:"):].split())
                if area_words & key_words:
                    candidates.extend(ids)

    return list(dict.fromkeys(candidates))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _pick(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
        if v and isinstance(v, list):
            return "\n".join(str(x) for x in v).strip()
    return ""


_PICS_EXTRACT_RE = re.compile(
    r'\b([A-Z][A-Z0-9]{0,15}\.[SCM]\.[ACEF][0-9A-Fa-f]{2,6})'
    r'(?:\.Rsp)?(?:\([A-Za-z][A-Za-z0-9_.]*\))?'
)

# Matches 2-part cluster-gate PICS codes (e.g. "HEPAFREMON.S", "WEBRTCR.C").
# These appear in the TC-level PICS section table but have no entity type or hex ID.
# Only applied to items from tc_raw["pics"] (not free text) to avoid false positives.
_PICS_CLUSTER_GATE_RE = re.compile(
    r'^([A-Z][A-Z0-9]{1,15}\.[SCM])(?:\([^)]*\))?$'
)


def _extract_pics_from_tc_raw(tc_raw: dict, all_text: str) -> List[str]:  # noqa: ARG001 (all_text unused — kept for API compat)
    """Extract PICS codes from a raw TC dict.

    Only scans the TC-level PICS section (tc_raw["pics"]) and per-step PICS
    conditions.  The full all_text scan was deliberately removed: precondition
    and setup sections describe TH capability requirements (e.g. "TH must
    support WNCV.S.F00") which are not DUT PICS declarations and cause
    wrong_side false positives in validation.
    """
    found: List[str] = []
    # Scan TC-level PICS section (tc_raw["pics"] = list of items from the HTML PICS block).
    # This captures 2-part cluster-gate codes the 3-part regex cannot match.
    for item in tc_raw.get("pics", []):
        item_stripped = (item or "").strip()
        m = _PICS_CLUSTER_GATE_RE.match(item_stripped)
        if m:
            found.append(m.group(1))
        else:
            for raw_code in _PICS_EXTRACT_RE.findall(item_stripped):
                found.append(raw_code)
    # Also scan all step fields — PICS codes may be in the action, pics, or expected column
    # depending on how the HTML table columns are ordered per test plan document.
    for step in tc_raw.get("test_procedure", []):
        step_text = ""
        if isinstance(step, dict):
            step_text = " ".join(filter(None, [
                step.get("action", ""),
                step.get("pics", ""),
                step.get("expected", ""),
                step.get("ref", ""),
            ]))
        elif isinstance(step, str):
            step_text = step
        for raw_code in _PICS_EXTRACT_RE.findall(step_text):
            found.append(raw_code)
    return list(dict.fromkeys(found))  # deduplicate, preserve order


def _split_numbered_steps(text: str) -> List[str]:
    """Split numbered list text into individual step strings."""
    if not text:
        return []
    # Try splitting on numbered list patterns: "1. ", "2. ", "Step 1:", etc.
    parts = re.split(r'(?:^|\n)\s*(?:\d+\.|Step\s+\d+[:.]\s*)', text, flags=re.M)
    steps = [p.strip() for p in parts if p.strip()]
    # If no numbered pattern found, fall back to newline-splitting
    if not steps:
        steps = [l.strip() for l in text.splitlines() if l.strip()]
    return steps


def _join_list(val) -> str:
    """Return val as a plain string.

    Handles the two forms a field value can take in the html_semantic_parser output:
    - Already a string  → returned as-is
    - A list of strings → joined with newline
    """
    if isinstance(val, list):
        return "\n".join(str(item) for item in val if item)
    return str(val) if val else ""


def _flatten_raw_to_text(tc_raw: dict) -> str:
    """Build an all_text string from the fields of a raw TC dict."""
    parts = []
    for key in ("title", "purpose", "preconditions", "test_setup", "device_topology"):
        val = tc_raw.get(key, "")
        if val:
            parts.append(_join_list(val))
    # Add procedure step actions
    for step in tc_raw.get("test_procedure", []):
        if isinstance(step, dict):
            action = step.get("action", "")
            expected = step.get("expected", "")
            if action:
                parts.append(action)
            if expected:
                parts.append(expected)
    return "\n\n".join(p for p in parts if p)

