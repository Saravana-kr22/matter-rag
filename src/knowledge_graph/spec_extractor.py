"""Deterministic spec extractor — produces SectionRecords and SpecRecords.

Input:  List of ``FetchedDocument`` objects from spec sources (HTML or plain text).

Output: Tuple of (List[SectionRecord], List[SpecRecord], List[RejectedCandidate]):
  - ``SectionRecord``  — one per section heading, with clean ``full_text``
  - ``SpecRecord``     — one per normative sentence, linked to its parent section
  - ``RejectedCandidate`` — sentences filtered out before classification

HTML files are parsed with ``html_semantic_parser.parse_spec()`` so:
  - No raw HTML/CSS in extracted text
  - Section headings are preserved as structured metadata
  - Cluster-matching sections are linked to DM XML canonical IDs

Classification is fully deterministic — keyword patterns decide requirement type.

Parallelism — three-phase pipeline
-----------------------------------
Phase 1 — HTML parsing (one worker per file):
  Each doc is parsed with ``html_semantic_parser.parse_spec()`` in a separate
  worker process.  For a single large file this still uses only 1 worker, but
  the BeautifulSoup parse is fast (~60-120 s) compared to sentence classification.

Phase 2 — Cluster-context propagation (serial in main process, ~seconds):
  A forward-pass over all sections assigns ``cluster_name``/``cluster_id`` to
  each section.  This must be serial because the assignment carries state
  (``current_cluster_ctx``) across sections within a document.

Phase 3 — Section-batch sentence classification (one worker per batch):
  After cluster context is known, sections are split into N batches
  (``_INTRA_SPLIT_THRESHOLD = 200`` sections triggers intra-doc splitting).
  Each batch is dispatched to a separate worker so a single large file uses
  all available CPU cores instead of just one.

  Speedup for a large single-file build: roughly ``min(sections, workers)``×
  (e.g. 8 workers on a 2 000-section file → ~8× faster section processing).

Content-hash caching
--------------------
Pass ``cache_dir`` (default ``data/knowledge_graph/spec_parse_cache``) to enable
on-disk caching.  The cache key is SHA-256 of the document content plus sorted
cluster names — so the cache is automatically invalidated when the spec HTML or
DM XML changes.  Cache format: pickle of ``(version, (sections, records, rejected))``.

On a cache hit the entire per-document processing is skipped; total cost per
unchanged spec file drops to < 1 s.  Subsequent KG rebuilds after the initial
build are effectively instant for any unchanged documents.

Rejected-records log
--------------------
Pass ``output_dir`` to get a ``spec_extractor_rejected_records.txt`` file in
that directory listing every rejected candidate with its reason and source
section.  Useful to audit the filter and tune ``filter_invalid_requirement_candidates``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    RejectedCandidate,
    SectionRecord,
    SpecRecord,
)
from src.knowledge_graph.rule_engine import (
    classify_requirement_type,
    detect_requirement_candidates,
    extract_conditions_and_effects,
    extract_protocol_areas,
    filter_invalid_requirement_candidates,
    match_entities_from_map,
)
from src.knowledge_graph.rule_engine import _build_multicluster_name_set as _build_mc_set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compile-time regex patterns
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")

_BOILERPLATE_TITLE_RE = re.compile(
    r"copyright|disclaimer|notice of use|license"
    r"|revision history"
    r"|table of contents|list of tables|list of figures"
    r"|toc|participants"
    r"|acronyms\s+and\s+abbreviations|abbreviations"
    r"|definitions",
    re.I,
)
_SECTION_NUM_RE = re.compile(r"^\s*[\d]+(?:\.[\d]+)*\.?\s*")

# Normative keywords that signal a table cell contains a real requirement
_TABLE_NORMATIVE_RE = re.compile(
    r"\b(shall|shall not|must|must not|is required|is prohibited|may not)\b",
    re.I,
)
_CONFORMANCE_CELL_RE = re.compile(
    r"^(M|O|P|D|C\[[^\]]*\]|desc|deprecated|provisional|[A-Z0-9_]+\.a[0-9]*)$",
    re.I,
)

# Minimum section count in a document that triggers intra-document splitting
_INTRA_SPLIT_THRESHOLD = 200

# Cache format version — bump when the cached data structure changes
_CACHE_VERSION = 3

# Headings that describe cross-cutting protocol concerns (PKI, commissioning, device
# attestation) rather than a specific cluster's behaviour.  When a section heading
# matches this pattern, it and all of its sub-sections are excluded from cluster
# attribution even if the breadcrumb path still contains a cluster name.
_CROSS_CUTTING_HEADING_RE = re.compile(
    r"\bcertificate\s+(format|encoding|extension|profile|validation|requirement"
    r"|structure|encoding\s+rule|policy)\b"
    r"|\bcertificate\s+encoding\b"
    r"|\b(noc|rcac|icac)\b"
    r"|\bnode\s+operational\s+certificate\b"
    r"|\broot\s+ca\s+certificate\b"
    r"|\bintermediate\s+(ca|certificate\s+authority)\s+certificate\b"
    r"|\bca\s+certificate\b"
    r"|\bfirmware\s+signing\b"
    r"|\bvendor\s+id\s+verification\s+signer\b"
    r"|\bdevice\s+attestation\s+(element|certificate|procedure|flow|credential|dag|pai|dac)\b"
    r"|\bpki\b"
    r"|\bx\.509\b"
    r"|\bkey\s+usage\s+(extension|encoding)\b"
    r"|\bbasic\s+constraints\s+extension\b"
    r"|\bsubject\s+key\s+identifier\b"
    r"|\bauthority\s+key\s+identifier\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Worker initialiser — must be module-level for ProcessPoolExecutor pickling
# ---------------------------------------------------------------------------

def _init_worker_paths() -> None:
    """Add project root to sys.path in spawn'd worker processes."""
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(here))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


# ---------------------------------------------------------------------------
# Phase-1 worker: parse one document → sections_raw
# ---------------------------------------------------------------------------

def _parse_doc_sections_worker(args: tuple) -> Tuple[int, List[dict], str]:
    """Parse a single spec document to raw section dicts.

    Returns ``(doc_idx, sections_raw, doc_label)``.
    """
    doc_idx, total, doc_content, doc_path, doc_ext = args
    doc_label = doc_path.split("/")[-1] if doc_path else f"doc-{doc_idx}"
    doc = SimpleNamespace(content=doc_content, path=doc_path)
    if doc_ext in ("html", "htm"):
        sections_raw = _parse_html_spec(doc)
    else:
        sections_raw = _parse_text_spec(doc)
    print(
        f"  [parse {doc_idx}/{total}] {doc_label}  —  {len(sections_raw)} sections",
        file=sys.stderr, flush=True,
    )
    return doc_idx, sections_raw, doc_label


# ---------------------------------------------------------------------------
# Phase-3 worker: classify sentences in a section batch
# ---------------------------------------------------------------------------

def _process_section_batch(args: tuple) -> Tuple[List, List, List]:
    """Classify requirement candidates for a batch of sections.

    ``args`` is ``(batch_id, doc_label, doc_id, sections_with_clusters,
    entity_name_map, multicluster_names, canonical_schema)``.

    ``sections_with_clusters`` is a list of ``(sec_raw, cluster_name, cluster_id)``
    tuples pre-computed by ``_assign_cluster_contexts`` in the main process.

    Returns ``(doc_sections, doc_records, doc_rejected)``.
    """
    (
        _batch_id, doc_label, doc_id,
        sections_with_clusters,
        entity_name_map,
        multicluster_names,
        canonical_schema,
    ) = args

    doc_sections: List[SectionRecord] = []
    doc_records: List[SpecRecord] = []
    doc_rejected: List[RejectedCandidate] = []
    req_counter: Dict[str, int] = {}

    for sec_raw, cluster_name, cluster_id in sections_with_clusters:
        if cluster_name == "__SKIP__":
            continue

        heading = sec_raw.get("heading", "")
        section_path = sec_raw.get("section_path", "")
        full_text = sec_raw.get("full_text", "")
        table_normative_sentences = _extract_normative_from_tables(
            sec_raw.get("tables", []), heading
        )

        sec_id = f"SECTION::{doc_label}::{heading[:80]}"
        doc_sections.append(SectionRecord(
            id=sec_id,
            title=heading,
            cluster=cluster_name,
            cluster_id=cluster_id,
            full_text=full_text,
            section_path=section_path,
            source_doc=doc_id,
        ))

        raw_candidates = detect_requirement_candidates(full_text)
        raw_candidates += table_normative_sentences

        valid_sentences, rejected = filter_invalid_requirement_candidates(
            raw_candidates, source_section=heading
        )
        doc_rejected.extend(rejected)

        # Build a paragraph-to-text lookup so each sentence can be stored with
        # the full paragraph that contains it.  This gives the LLM enough context
        # to interpret anaphoric references like "this field", "it", "the event".
        _paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]

        for sentence in valid_sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # Find the enclosing paragraph for context.  Fall back to the sentence
            # itself when no paragraph match is found (e.g. single-line sections).
            context_text = sentence
            for para in _paragraphs:
                if sentence in para:
                    context_text = para
                    break

            entity_refs = match_entities_from_map(
                sentence, entity_name_map,
                context_cluster=cluster_name,
                multicluster_names=multicluster_names,
            )
            req_type, confidence, ambiguous, score_breakdown, signals, alternatives = (
                classify_requirement_type(sentence, entity_refs, canonical_schema)
            )
            conditions, effects, constraints = extract_conditions_and_effects(
                sentence, entity_refs, entity_name_map,
                context_cluster=cluster_name,
                multicluster_names=multicluster_names,
            )

            cluster = cluster_name or _infer_cluster(entity_refs, canonical_schema)
            prefix = f"REQ::{cluster}" if cluster else "REQ"
            idx = req_counter.get(prefix, 0)
            req_counter[prefix] = idx + 1
            rec_id = f"{prefix}::{idx}"

            doc_records.append(SpecRecord(
                id=rec_id,
                requirement_type=req_type,
                cluster=cluster,
                section_id=sec_id,
                entity_refs=entity_refs,
                normative_text=sentence,
                context_text=context_text,
                conditions=conditions,
                effects=effects,
                constraints=constraints,
                section_path=section_path or heading,
                source_doc=doc_id,
                confidence=confidence,
                ambiguous=ambiguous,
                score_breakdown=score_breakdown,
                signals=signals,
                alternatives=alternatives,
            ))

    return doc_sections, doc_records, doc_rejected


# ---------------------------------------------------------------------------
# Phase-2: cluster-context propagation (serial, preserves forward-pass state)
# ---------------------------------------------------------------------------

def _assign_cluster_contexts(
    sections_raw: List[dict],
    cluster_name_set: Dict[str, any],
) -> List[Tuple[dict, str, str]]:
    """Pre-compute (section, cluster_name, cluster_id) for every raw section.

    Must run serially because the ``current_cluster_ctx`` state variable carries
    forward across consecutive sections within a document (a subsection inherits
    the cluster from its parent heading).

    Sections whose heading is boilerplate (copyright, TOC, etc.) get
    ``cluster_name = "__SKIP__"`` so the batch worker can skip them quickly.

    Cross-cutting sections (PKI/certificate formats, device attestation, etc.)
    are detected by ``_CROSS_CUTTING_HEADING_RE``.  Such a section and all its
    sub-sections receive ``cluster_name = ""`` — they are not attributed to the
    cluster that happens to contain them structurally (e.g. certificate format
    sections nested inside the Access Control chapter).

    Returns a list of ``(sec_raw, cluster_name, cluster_id)`` tuples.
    """
    result: List[Tuple[dict, str, str]] = []
    current_cluster_ctx: Tuple[str, str] = ("", "")
    # section_path of the active cross-cutting section (empty = not suppressing)
    _cross_cutting_prefix: str = ""

    for sec_raw in sections_raw:
        heading = sec_raw.get("heading", "")
        section_path = sec_raw.get("section_path", "")

        _heading_stripped = _SECTION_NUM_RE.sub("", heading)
        if _BOILERPLATE_TITLE_RE.search(_heading_stripped):
            result.append((sec_raw, "__SKIP__", ""))
            continue

        # ── Cross-cutting heading: suppress this section and its sub-tree ──
        if _CROSS_CUTTING_HEADING_RE.search(_heading_stripped):
            result.append((sec_raw, "", ""))
            # Record the path prefix so sub-sections are also suppressed.
            _cross_cutting_prefix = section_path
            continue

        # ── Inside a cross-cutting sub-tree: suppress attribution ──────────
        # A section is inside the cross-cutting sub-tree when its full breadcrumb
        # path starts with the cross-cutting section's path.
        if _cross_cutting_prefix and section_path.startswith(_cross_cutting_prefix):
            result.append((sec_raw, "", ""))
            continue
        else:
            # Exited the cross-cutting sub-tree (or never in one).
            _cross_cutting_prefix = ""

        # ── Normal cluster attribution ──────────────────────────────────────
        cluster_name, cluster_id = _match_cluster(heading, cluster_name_set, section_path)
        direct_match_name, direct_match_id = _match_cluster(heading, cluster_name_set)
        if direct_match_name:
            current_cluster_ctx = (direct_match_name, direct_match_id)
        elif not cluster_name:
            ctx_name, ctx_id = current_cluster_ctx
            if ctx_name and ctx_name.lower() in section_path.lower():
                cluster_name, cluster_id = ctx_name, ctx_id
            else:
                current_cluster_ctx = ("", "")

        result.append((sec_raw, cluster_name, cluster_id))

    return result


# ---------------------------------------------------------------------------
# Content-hash caching helpers
# ---------------------------------------------------------------------------

def _doc_cache_key(content: str, cluster_name_set: Dict) -> str:
    """SHA-256 of doc content + sorted cluster names.

    Including cluster names means the cache is invalidated automatically when
    the DM XML changes (new clusters → new entity names → different extraction).
    """
    cluster_sig = "|".join(sorted(cluster_name_set.keys()))
    return hashlib.sha256((content + "\x00" + cluster_sig).encode()).hexdigest()


def _load_cached_result(cache_dir: str, key: str) -> Optional[tuple]:
    """Return cached (sections, records, rejected) or None on miss/error."""
    p = Path(cache_dir) / f"{key}.pkl"
    if not p.exists():
        return None
    try:
        with open(p, "rb") as fh:
            version, data = pickle.load(fh)
        if version == _CACHE_VERSION:
            return data
    except Exception as exc:
        logger.debug("[spec_extractor] cache load failed for %s: %s", key[:20], exc)
    return None


def _save_cached_result(cache_dir: str, key: str, data: tuple) -> None:
    """Persist (sections, records, rejected) under a content-hash key."""
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        p = Path(cache_dir) / f"{key}.pkl"
        with open(p, "wb") as fh:
            pickle.dump((_CACHE_VERSION, data), fh)
    except Exception as exc:
        logger.debug("[spec_extractor] cache write failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_spec_sections_and_records(
    documents: list,                   # List[FetchedDocument]
    canonical_schema: CanonicalSchema,
    source_doc: str = "",
    max_workers: int = 0,              # 0 = auto (min(cpu_count, 8))
    output_dir: str = "",              # write rejected log here when non-empty
    cache_dir: str = "data/knowledge_graph/spec_parse_cache",
) -> Tuple[List[SectionRecord], List[SpecRecord], List[RejectedCandidate]]:
    """Parse spec documents and return (sections, requirements, rejected_candidates).

    Three-phase parallel pipeline
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Phase 1 — HTML parsing (parallel, one worker per file):
        ``html_semantic_parser.parse_spec()`` per document.

    Phase 2 — Cluster-context assignment (serial, ~seconds):
        Forward-pass over all sections to propagate cluster inheritance.

    Phase 3 — Sentence classification (parallel, N workers × M batches):
        Each document with ≥ ``_INTRA_SPLIT_THRESHOLD`` sections is split into
        ``workers`` roughly equal batches so a single large file uses all cores.

    Content-hash caching (``cache_dir``):
        Per-document pickle cache keyed on SHA-256(content + cluster names).
        Cache hit → skip all three phases for that document (< 1 s per file).
        Set ``cache_dir=""`` to disable.

    When ``output_dir`` is non-empty, writes rejected candidates to
    ``<output_dir>/spec_extractor_rejected_records.txt``.
    """
    entity_name_map = _build_entity_name_map(canonical_schema)
    cluster_name_set = {c.name.lower(): c for c in canonical_schema.clusters}
    multicluster_names = _build_mc_set(canonical_schema)

    total = len(documents)
    auto = os.cpu_count() or 1
    requested = max_workers if max_workers > 0 else auto
    workers = min(max(requested, 1), 8)

    logger.info(
        "[spec_extractor] ── Stage: Extract Spec Records ── %d documents  workers=%d  cache=%s",
        total, workers, "on" if cache_dir else "off",
    )
    print(
        f"\n[spec_extractor] Crunching {total} spec document(s) with up to {workers} worker(s)…",
        file=sys.stderr, flush=True,
    )
    t0 = time.time()

    # Build per-document metadata
    doc_meta: List[dict] = []
    for doc_idx, doc in enumerate(documents, 1):
        path = _get_path(doc)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        label = path.split("/")[-1] if path else f"doc-{doc_idx}"
        doc_meta.append({
            "idx": doc_idx,
            "path": path,
            "ext": ext,
            "label": label,
            "content": _get_content(doc),
            "doc_id": source_doc or path,
        })

    # ── Phase 1: HTML parsing ──────────────────────────────────────────────
    # Check cache first; only parse uncached docs.
    print("\n[spec_extractor] Phase 1: parsing HTML…", file=sys.stderr, flush=True)

    cached_results: Dict[int, tuple] = {}   # doc_idx → (sections, records, rejected)
    parse_args_list = []

    for m in doc_meta:
        if cache_dir:
            key = _doc_cache_key(m["content"], cluster_name_set)
            hit = _load_cached_result(cache_dir, key)
            if hit is not None:
                cached_results[m["idx"]] = hit
                print(
                    f"  [cache hit] {m['label']}  —  "
                    f"{len(hit[0])} sections  {len(hit[1])} requirements",
                    file=sys.stderr, flush=True,
                )
                continue
        parse_args_list.append((
            m["idx"], total,
            m["content"], m["path"], m["ext"],
        ))

    # Parse uncached documents (parallel per file)
    parsed_sections: Dict[int, List[dict]] = {}  # doc_idx → sections_raw
    if parse_args_list:
        parse_workers = min(len(parse_args_list), workers)
        if parse_workers > 1:
            with ProcessPoolExecutor(
                max_workers=parse_workers,
                initializer=_init_worker_paths,
            ) as executor:
                future_map = {
                    executor.submit(_parse_doc_sections_worker, args): args[0]
                    for args in parse_args_list
                }
                for future in as_completed(future_map):
                    doc_idx_r, sections_raw, doc_label_r = future.result()
                    parsed_sections[doc_idx_r] = sections_raw
        else:
            for args in parse_args_list:
                doc_idx_r, sections_raw, doc_label_r = _parse_doc_sections_worker(args)
                parsed_sections[doc_idx_r] = sections_raw

    # ── Phase 2: Cluster-context assignment (serial) ───────────────────────
    print("\n[spec_extractor] Phase 2: assigning cluster contexts…", file=sys.stderr, flush=True)

    # Build section batches for uncached docs
    all_batches: List[tuple] = []          # args for _process_section_batch
    # Map batch index → (doc_idx, batch_local_idx) for result ordering
    batch_origin: List[Tuple[int, int]] = []  # (doc_idx, batch_local_idx)

    for m in doc_meta:
        doc_idx = m["idx"]
        if doc_idx in cached_results:
            continue  # will be merged directly from cache

        sections_raw = parsed_sections.get(doc_idx, [])
        sections_with_clusters = _assign_cluster_contexts(sections_raw, cluster_name_set)

        # Split large docs into per-core batches
        n = len(sections_with_clusters)
        if n >= _INTRA_SPLIT_THRESHOLD and workers > 1:
            batch_size = max(50, (n + workers - 1) // workers)
        else:
            batch_size = n or 1

        for batch_local, start in enumerate(range(0, max(n, 1), batch_size)):
            batch = sections_with_clusters[start:start + batch_size]
            all_batches.append((
                len(all_batches),          # batch_id (unique)
                m["label"], m["doc_id"],
                batch,
                entity_name_map, multicluster_names, canonical_schema,
            ))
            batch_origin.append((doc_idx, batch_local))

    # ── Phase 3: Sentence classification (parallel batch workers) ─────────
    print(
        f"\n[spec_extractor] Phase 3: classifying {len(all_batches)} section batch(es) "
        f"across {workers} worker(s)…",
        file=sys.stderr, flush=True,
    )

    batch_results: List[tuple] = []
    if all_batches:
        batch_workers = min(len(all_batches), workers)
        if batch_workers > 1:
            with ProcessPoolExecutor(
                max_workers=batch_workers,
                initializer=_init_worker_paths,
            ) as executor:
                future_map = {
                    executor.submit(_process_section_batch, args): i
                    for i, args in enumerate(all_batches)
                }
                batch_results_map: Dict[int, tuple] = {}
                for future in as_completed(future_map):
                    i = future_map[future]
                    try:
                        batch_results_map[i] = future.result()
                    except Exception as exc:
                        logger.error("[spec_extractor] batch %d failed: %s", i, exc)
                        batch_results_map[i] = ([], [], [])
                # Preserve order
                batch_results = [batch_results_map[i] for i in range(len(all_batches))]
        else:
            for args in all_batches:
                try:
                    batch_results.append(_process_section_batch(args))
                except Exception as exc:
                    logger.error("[spec_extractor] batch failed: %s", exc)
                    batch_results.append(([], [], []))

    # Save newly processed docs to cache (merge batches per doc first)
    if cache_dir and batch_results:
        # Aggregate batch results per doc_idx
        doc_batch_results: Dict[int, Tuple[list, list, list]] = {}
        for i, (doc_idx, _batch_local) in enumerate(batch_origin):
            secs, recs, rej = batch_results[i]
            if doc_idx not in doc_batch_results:
                doc_batch_results[doc_idx] = ([], [], [])
            doc_batch_results[doc_idx][0].extend(secs)
            doc_batch_results[doc_idx][1].extend(recs)
            doc_batch_results[doc_idx][2].extend(rej)

        for m in doc_meta:
            doc_idx = m["idx"]
            if doc_idx in cached_results or doc_idx not in doc_batch_results:
                continue
            key = _doc_cache_key(m["content"], cluster_name_set)
            _save_cached_result(cache_dir, key, doc_batch_results[doc_idx])

    # ── Merge all results in document order ───────────────────────────────
    all_sections: List[SectionRecord] = []
    all_records: List[SpecRecord] = []
    all_rejected: List[RejectedCandidate] = []

    # Gather batch results indexed by doc
    doc_from_batches: Dict[int, Tuple[list, list, list]] = {}
    for i, (doc_idx, _batch_local) in enumerate(batch_origin):
        if i < len(batch_results):
            secs, recs, rej = batch_results[i]
            if doc_idx not in doc_from_batches:
                doc_from_batches[doc_idx] = ([], [], [])
            doc_from_batches[doc_idx][0].extend(secs)
            doc_from_batches[doc_idx][1].extend(recs)
            doc_from_batches[doc_idx][2].extend(rej)

    for m in doc_meta:
        doc_idx = m["idx"]
        if doc_idx in cached_results:
            secs, recs, rej = cached_results[doc_idx]
        else:
            secs, recs, rej = doc_from_batches.get(doc_idx, ([], [], []))
        all_sections.extend(secs)
        all_records.extend(recs)
        all_rejected.extend(rej)

        logger.info(
            "[spec_extractor] (%d/%d) %-40s  sections=%d  requirements=%d  rejected=%d",
            doc_idx, total, m["label"],
            len(secs), len(recs), len(rej),
        )

    # Re-number requirement IDs globally to guarantee uniqueness after parallel merge
    _renumber_spec_records(all_records)

    elapsed = time.time() - t0
    logger.info(
        "[spec_extractor] ── Done ── %d sections, %d spec records, %d rejected  "
        "from %d documents  (%.1fs)",
        len(all_sections), len(all_records), len(all_rejected), total, elapsed,
    )
    print(
        f"\n[spec_extractor] Done — {len(all_sections)} sections  "
        f"{len(all_records)} requirements  {len(all_rejected)} rejected  ({elapsed:.1f}s)",
        file=sys.stderr, flush=True,
    )

    if output_dir and all_rejected:
        _write_rejected_log(all_rejected, output_dir)

    return all_sections, all_records, all_rejected


# ---------------------------------------------------------------------------
# HTML and text parsing
# ---------------------------------------------------------------------------

def _parse_html_spec(doc) -> List[dict]:
    """Parse an HTML spec doc via html_semantic_parser → list of section dicts."""
    try:
        from src.processor.html_semantic_parser import parse_spec  # type: ignore
        path = _get_path(doc)
        result = parse_spec(_get_content(doc), doc_id=path)
        raw_sections = result.get("sections", [])
        out = []
        for sec in raw_sections:
            heading = sec.get("title", sec.get("heading", ""))
            full_text = sec.get("full_text", "")
            if not full_text:
                chunks = sec.get("chunks", [])
                full_text = "\n\n".join(c.get("text", "") for c in chunks if c.get("text"))
            full_text = _strip_html(full_text)
            out.append({
                "heading": _strip_html(heading),
                "full_text": full_text,
                "section_path": " > ".join(
                    str(p) for p in sec.get("section_path", sec.get("path", [heading]))
                ),
                "tables": sec.get("tables", []),
            })
        return out
    except Exception as exc:
        logger.warning("[spec_extractor] HTML parse failed: %s — falling back to text", exc)
        return _parse_text_spec(doc)


def _parse_text_spec(doc) -> List[dict]:
    """Minimal fallback: split plain text on heading-like lines."""
    content = _strip_html(_get_content(doc))
    sections = []
    current_heading = ""
    current_lines: List[str] = []
    heading_re = re.compile(r"^#{1,6}\s+(.+)|^=+\s+(.+)|^(\d+\.[\d.]*\s+[A-Z].{3,})")

    for line in content.splitlines():
        m = heading_re.match(line.strip())
        if m:
            if current_lines:
                sections.append({
                    "heading": current_heading,
                    "full_text": "\n".join(current_lines).strip(),
                    "section_path": current_heading,
                    "tables": [],
                })
            current_heading = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            current_lines = []
        else:
            if line.strip():
                current_lines.append(line)

    if current_lines:
        sections.append({
            "heading": current_heading,
            "full_text": "\n".join(current_lines).strip(),
            "section_path": current_heading,
            "tables": [],
        })

    if not sections:
        sections = [{"heading": "", "full_text": content, "section_path": "", "tables": []}]
    return sections


# ---------------------------------------------------------------------------
# Table normative extraction
# ---------------------------------------------------------------------------

def _extract_normative_from_tables(
    tables: list,
    heading: str = "",
) -> List[str]:
    """Extract normative sentences from structured table data."""
    normative: List[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows", [])
        headers = [str(h).strip() for h in table.get("headers", [])]
        _skip_header_re = re.compile(
            r"^(id|code|conformance|access|type|format|quality|default|constraint"
            r"|feature|bit|value|enum|range|units?|priority)$",
            re.I,
        )
        prose_col_indices = (
            [i for i, h in enumerate(headers) if not _skip_header_re.match(h)]
            if headers else None
        )
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            for i, cell in enumerate(row):
                cell_text = str(cell).strip()
                if prose_col_indices is not None and i not in prose_col_indices:
                    continue
                if _CONFORMANCE_CELL_RE.match(cell_text):
                    continue
                if len(cell_text) < 30:
                    continue
                if _TABLE_NORMATIVE_RE.search(cell_text):
                    normative.append(cell_text)
    return normative


# ---------------------------------------------------------------------------
# Cluster matching
# ---------------------------------------------------------------------------

def _match_cluster(
    heading: str,
    cluster_name_set: Dict[str, any],
    section_path: str = "",
) -> Tuple[str, str]:
    """Return (cluster_name, cluster_id) if heading or path contains a cluster name."""
    for search_text in (heading, section_path):
        if not search_text:
            continue
        text_lower = search_text.lower()
        text_nospace = text_lower.replace(" ", "").replace("-", "")
        best = ""
        for cname_lower, cluster_rec in cluster_name_set.items():
            # Standard substring match (handles "On/Off Cluster" in heading)
            if cname_lower in text_lower and len(cname_lower) > len(best):
                best = cname_lower
            # Also try space-stripped match (handles CamelCase cluster names
            # matching spaced heading text, e.g. "MyCustomCluster" in "My Custom Cluster")
            elif cname_lower.replace(" ", "").replace("-", "") in text_nospace and len(cname_lower) > len(best):
                best = cname_lower
        if best:
            rec = cluster_name_set[best]
            return rec.name, f"CLUSTER::{rec.name}"
    return "", ""


# ---------------------------------------------------------------------------
# Entity matching
# ---------------------------------------------------------------------------

def _camel_to_spaced(name: str) -> str:
    """Convert CamelCase to lowercase space-separated tokens."""
    import re as _re
    spaced = _re.sub(r'(?<=[a-z0-9])([A-Z])', r' \1', name)
    spaced = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', spaced)
    return spaced.lower().strip()


def _build_entity_name_map(schema: CanonicalSchema) -> Dict[str, List]:
    """Build lowercase name → List[CanonicalEntityRef] lookup.

    Stores all cluster variants per name so match_entities_from_map() can
    check each against context_cluster for multicluster entities.
    """
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


def _infer_cluster(entity_refs: List[str], schema: CanonicalSchema) -> str:
    cluster_counts: Dict[str, int] = {}
    for ref in entity_refs:
        parts = ref.split("::")
        if len(parts) >= 2:
            cluster = parts[1]
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
    if not cluster_counts:
        return ""
    return max(cluster_counts, key=cluster_counts.__getitem__)


# ---------------------------------------------------------------------------
# Post-merge helpers
# ---------------------------------------------------------------------------

def _renumber_spec_records(records: List[SpecRecord]) -> None:
    """Re-number SpecRecord IDs globally to guarantee uniqueness after parallel merge."""
    global_counter: Dict[str, int] = {}
    for rec in records:
        parts = rec.id.rsplit("::", 1)
        prefix = parts[0] if len(parts) == 2 and parts[1].isdigit() else rec.id
        idx = global_counter.get(prefix, 0)
        global_counter[prefix] = idx + 1
        rec.id = f"{prefix}::{idx}"


def _write_rejected_log(rejected: List[RejectedCandidate], output_dir: str) -> None:
    """Write rejected candidates to ``<output_dir>/spec_extractor_rejected_records.txt``."""
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "spec_extractor_rejected_records.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("=== Spec Extractor — Rejected Candidates Report ===\n")
            fh.write(f"Total rejected: {len(rejected)}\n\n")
            from collections import Counter
            counts = Counter(r.reason for r in rejected)
            fh.write("Summary by reason:\n")
            for reason, count in sorted(counts.items(), key=lambda x: -x[1]):
                fh.write(f"  {reason:<40s} {count:>5d}\n")
            fh.write("\n" + "─" * 80 + "\n\n")
            for i, r in enumerate(rejected, 1):
                fh.write(f"[{i:04d}] reason={r.reason}\n")
                if r.source_section:
                    fh.write(f"       section: {r.source_section[:120]}\n")
                fh.write(f"       text:    {r.text[:200]}\n\n")
        logger.info(
            "[spec_extractor] Rejected records written → %s  (%d entries)", out_path, len(rejected),
        )
    except OSError as exc:
        logger.warning("[spec_extractor] Could not write rejected log to %s: %s", out_path, exc)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    text = _HTML_TAG_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&nbsp;", " ").replace("&#8203;", "")
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def _get_content(doc) -> str:
    """Get text content from FetchedDocument or loader Document."""
    if hasattr(doc, "content") and doc.content:
        return doc.content
    if hasattr(doc, "page_content") and doc.page_content:
        return doc.page_content
    if hasattr(doc, "text") and doc.text:
        return doc.text
    return ""


def _get_path(doc) -> str:
    if hasattr(doc, "path") and doc.path:
        return doc.path
    meta = getattr(doc, "metadata", {}) or {}
    return meta.get("source", meta.get("absolute_path", ""))


# ---------------------------------------------------------------------------
# Legacy per-document worker (kept for backward compatibility)
# ---------------------------------------------------------------------------

def _process_single_doc(args: tuple):
    """Process one spec document end-to-end (legacy — not used by the main pipeline).

    The main pipeline now uses the three-phase approach:
    ``_parse_doc_sections_worker`` → ``_assign_cluster_contexts`` →
    ``_process_section_batch``.  This function is retained for tests or
    callers that invoke it directly.
    """
    (
        doc_idx, total,
        doc_content, doc_path, doc_ext,
        entity_name_map,
        cluster_name_set,
        multicluster_names,
        canonical_schema,
        source_doc,
    ) = args

    doc_label = doc_path.split("/")[-1] if doc_path else f"doc-{doc_idx}"
    doc_id = source_doc or doc_path
    t_doc = time.time()

    doc = SimpleNamespace(content=doc_content, path=doc_path)
    if doc_ext in ("html", "htm"):
        sections_raw = _parse_html_spec(doc)
    else:
        sections_raw = _parse_text_spec(doc)

    sections_with_clusters = _assign_cluster_contexts(sections_raw, cluster_name_set)
    batch_args = (0, doc_label, doc_id, sections_with_clusters,
                  entity_name_map, multicluster_names, canonical_schema)
    doc_sections, doc_records, doc_rejected = _process_section_batch(batch_args)

    elapsed = time.time() - t_doc
    print(
        f"\r  [{doc_idx}/{total}] {doc_label}  done — "
        f"{len(doc_sections)} sections  {len(doc_records)} requirements  "
        f"{len(doc_rejected)} rejected  ({elapsed:.1f}s)          ",
        file=sys.stderr, flush=True,
    )
    return doc_sections, doc_records, doc_rejected, doc_label, doc_idx, elapsed
