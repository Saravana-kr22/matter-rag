"""Adoc test plan updater.

Applies LLM analysis results (update_candidates + missing_tests) to source
.adoc test plan files and writes per-cluster output files.

Output: one updated adoc per modified source file, written to
``<output_dir>/<original_stem>_updated.adoc``.

Design
------
- find_tc_section()    — locate a TC section by TC-ID (flexible heading level + brackets)
- replace_tc_section() — swap the old TC section with the LLM-revised one
- append_tc_section()  — append a new TC block at the end of the file
- write_updated_adocs() — top-level function that orchestrates all of the above;
                          uses a tc_index dict (from tc_index_builder) for routing
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.fetcher.base_fetcher import FetchedDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section finder — flexible heading level + optional bracket syntax
#
# Matches any of:
#   == TC-OO-2.1 Title
#   === TC-OO-2.1 Title
#   ==== [TC-OO-2.1] Attributes with DUT as Server
# ---------------------------------------------------------------------------

_TC_HEADING_RE = re.compile(
    r"^(={2,6})\s+\[?(TC-[A-Z][A-Z0-9]*-\d+(?:\.\d+)*)\]?",
    re.MULTILINE,
)


def find_tc_section(content: str, tc_id: str) -> Optional[Tuple[int, int]]:
    """Return (start_line_idx, end_line_idx) of the TC section, or None.

    The range is half-open: ``lines[start:end]`` is the complete TC section.
    ``end`` is the index of the first line of the *next* section at the same
    or lower heading level, or ``len(lines)`` if no such section follows.

    Handles any AsciiDoc heading level (==, ===, ====, etc.) and TC-IDs
    wrapped in square brackets (e.g. ``==== [TC-OO-2.1] Title``).
    """
    lines = content.splitlines(keepends=True)

    # Pattern: any heading level, optional brackets, TC-ID, optional title
    tc_pattern = re.compile(
        rf"^(={{2,6}})\s+\[?{re.escape(tc_id)}\]?",
        re.IGNORECASE,
    )

    start_idx: Optional[int] = None
    heading_level: int = 0

    for i, line in enumerate(lines):
        m = tc_pattern.match(line)
        if m:
            start_idx = i
            heading_level = len(m.group(1))  # number of '=' signs
            break

    if start_idx is None:
        return None

    # Section ends at the next heading of equal or lower level (fewer/same '=' signs)
    # e.g. if the TC heading is "====", stop at the next "====", "===", or "==".
    end_heading_re = re.compile(rf"^={{2,{heading_level}}}\s+\S")

    for i in range(start_idx + 1, len(lines)):
        if end_heading_re.match(lines[i]):
            return start_idx, i

    return start_idx, len(lines)


# ---------------------------------------------------------------------------
# Content mutators
# ---------------------------------------------------------------------------

def replace_tc_section(content: str, tc_id: str, new_section: str) -> str:
    """Replace the TC section identified by *tc_id* with *new_section*.

    Returns the original *content* unchanged if *tc_id* is not found.
    """
    result = find_tc_section(content, tc_id)
    if result is None:
        logger.warning("[adoc_updater] TC-ID '%s' not found — skipping replacement", tc_id)
        return content

    start, end = result
    lines = content.splitlines(keepends=True)

    # Ensure new_section ends with a single blank line for clean splicing.
    section_lines = (new_section.rstrip("\n") + "\n\n").splitlines(keepends=True)

    new_lines = lines[:start] + section_lines + lines[end:]
    return "".join(new_lines)


def append_tc_section(content: str, new_section: str) -> str:
    """Append *new_section* at the end of *content* with a blank line separator."""
    return content.rstrip("\n") + "\n\n" + new_section.strip() + "\n"


# ---------------------------------------------------------------------------
# Cluster prefix inference (used as fallback when no tc_index available)
# ---------------------------------------------------------------------------

def find_cluster_prefix(content: str) -> str:
    """Return the TC cluster prefix (e.g. 'OO') from the first TC heading found.

    Returns an empty string if no TC heading is present.
    """
    m = _TC_HEADING_RE.search(content)
    if not m:
        return ""
    tc_id = m.group(2)           # e.g. "TC-OO-2.1"
    parts = tc_id.split("-")     # ["TC", "OO", "2", "1"]
    return parts[1] if len(parts) >= 2 else ""


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def write_updated_adocs(
    adoc_sources: List[FetchedDocument],
    missing_tests: List[dict],
    update_candidates: List[dict],
    output_dir: str,
    tc_index: Optional[dict] = None,
) -> List[str]:
    """Apply analysis results to source adoc files and write updated copies.

    Routing strategy (for each TC in update_candidates / missing_tests):

    1. If *tc_index* is provided (from ``tc_index_builder.build_tc_index``):
       - ``update_candidates``: look up the exact TC-ID in ``tc_index["tc_map"]``
         to find which source file owns it.
       - ``missing_tests``: look up TC prefix → ``tc_index["prefix_map"]``, then
         fall back to cluster-name stem → ``tc_index["stem_map"]``.
       This is the reliable path — no heuristic scanning.

    2. If *tc_index* is absent (backward-compat fallback):
       - Build an in-memory prefix_map by scanning each adoc's first TC heading.
       - Behaviour is the same as before the index was introduced.

    Files that receive no modifications are not written.

    Returns a list of paths of the files that were written.
    """
    if not adoc_sources:
        return []

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build path → content working copies
    working: Dict[str, str] = {doc.path: doc.content for doc in adoc_sources}

    # ----- Build routing helpers -----
    # path_by_tc_id: TC-OO-2.1 → doc.path  (from tc_index or live scan)
    # path_by_prefix: OO → doc.path
    # path_by_stem: "onoff" → doc.path

    path_by_tc_id:  Dict[str, str] = {}
    path_by_prefix: Dict[str, str] = {}
    path_by_stem:   Dict[str, str] = {}

    if tc_index:
        index_tc_map     = tc_index.get("tc_map", {})
        index_prefix_map = tc_index.get("prefix_map", {})
        index_stem_map   = tc_index.get("stem_map", {})

        # Map index absolute paths back to doc.path keys used in working dict.
        # Use metadata["absolute_path"] set by the fetcher — resolving the
        # relative doc.path against CWD gives the wrong path when the fetcher
        # root is not CWD (e.g. LocalFolderFetcher with path="data/test_plan_adocs/src").
        abs_to_doc: Dict[str, str] = {}
        for doc in adoc_sources:
            abs_key = doc.metadata.get("absolute_path") or str(Path(doc.path).resolve())
            abs_to_doc[abs_key] = doc.path

        for tc_id, abs_path in index_tc_map.items():
            doc_path = abs_to_doc.get(abs_path)
            if doc_path and doc_path in working:
                path_by_tc_id[tc_id] = doc_path

        for prefix, abs_path in index_prefix_map.items():
            doc_path = abs_to_doc.get(abs_path)
            if doc_path and doc_path in working:
                path_by_prefix[prefix.upper()] = doc_path

        for stem, abs_path in index_stem_map.items():
            doc_path = abs_to_doc.get(abs_path)
            if doc_path and doc_path in working:
                path_by_stem[stem] = doc_path
    else:
        # Fallback: scan each loaded adoc for its TC headings
        for doc_path, content in working.items():
            for m in _TC_HEADING_RE.finditer(content):
                tc_id = m.group(2)
                if tc_id not in path_by_tc_id:
                    path_by_tc_id[tc_id] = doc_path
                parts = tc_id.split("-")
                if len(parts) >= 2:
                    prefix = parts[1].upper()
                    if prefix not in path_by_prefix:
                        path_by_prefix[prefix] = doc_path
            stem = Path(doc_path).stem.lower()
            if stem not in path_by_stem:
                path_by_stem[stem] = doc_path

    # ----- Apply update candidates -----
    applied_updates: Dict[str, List[str]] = {p: [] for p in working}
    suggested_updates_content: Dict[str, str] = {}

    for uc in update_candidates:
        tc_id        = (uc.get("tc_id") or "").strip()
        adoc_section = (uc.get("adoc_section") or "").strip()
        if not tc_id or not adoc_section:
            logger.debug("[adoc_updater] update_candidate missing tc_id or adoc_section — skip")
            continue

        target_path = path_by_tc_id.get(tc_id)
        if target_path is None:
            # Secondary: maybe it's in a file the index doesn't know about — scan working set
            for p, content in working.items():
                if find_tc_section(content, tc_id) is not None:
                    target_path = p
                    break

        if target_path is None:
            # No source adoc found — write to a per-prefix fallback file instead of dropping
            parts = tc_id.split("-")
            prefix_key = parts[1].lower() if len(parts) >= 2 else "unknown"
            key = f"{prefix_key}_suggested_updates"
            if key not in suggested_updates_content:
                suggested_updates_content[key] = (
                    f"= Suggested Updates — {prefix_key.upper()} (no source adoc found)\n\n"
                    f"// These TCs were flagged as update candidates but their source adoc\n"
                    f"// file is not in the current working set. Review manually.\n\n"
                )
            suggested_updates_content[key] += adoc_section.strip() + "\n\n"
            logger.info(
                "[adoc_updater] TC '%s' has no source adoc — will write to %s_suggested_updates.adoc",
                tc_id, prefix_key,
            )
            continue

        working[target_path] = replace_tc_section(working[target_path], tc_id, adoc_section)
        applied_updates[target_path].append(tc_id)
        logger.info("[adoc_updater] Updated TC %s in %s", tc_id, Path(target_path).name)

    # ----- Append new TCs -----
    appended_new: Dict[str, List[str]] = {p: [] for p in working}
    new_adoc_content: Dict[str, str] = {}

    for nt in missing_tests:
        cluster      = (nt.get("cluster") or "").strip()
        adoc_section = (nt.get("adoc_section") or "").strip()
        title        = (nt.get("title") or "").strip()
        if not adoc_section:
            logger.debug("[adoc_updater] missing_test '%s' has no adoc_section — skip", title)
            continue

        target_path = _resolve_new_tc_path(
            nt, cluster, path_by_prefix, path_by_stem,
        )

        if target_path is None:
            # Derive a filename stem for the new adoc file
            suggested_id = (nt.get("tc_id") or nt.get("title") or "")
            _m = re.search(r"TC-([A-Z][A-Z0-9]*)-\d+", suggested_id, re.IGNORECASE)
            if _m:
                stem = _m.group(1).lower()
            elif cluster:
                stem = _normalize_stem(cluster)
            else:
                stem = "protocol"
            key = f"{stem}_new_tcs"
            if key not in new_adoc_content:
                header_label = cluster or stem.replace("_", " ").title()
                new_adoc_content[key] = f"= {header_label} — New Test Cases\n\n"
            new_adoc_content[key] += adoc_section.strip() + "\n\n"
            logger.info(
                "[adoc_updater] No source adoc for '%s' — will write to new file %s_new_tcs.adoc",
                cluster, stem,
            )
            # Fall through so the TC is counted in appended_new via the new-file path
            continue

        working[target_path] = append_tc_section(working[target_path], adoc_section)
        appended_new[target_path].append(title)
        logger.info("[adoc_updater] Appended new TC '%s' to %s", title, Path(target_path).name)

    # ----- Write modified files -----
    written: List[str] = []

    for doc in adoc_sources:
        original = doc.content
        updated  = working[doc.path]
        if updated == original:
            continue

        stem     = Path(doc.path).stem
        out_path = out_dir / f"{stem}_updated.adoc"
        out_path.write_text(updated, encoding="utf-8")
        written.append(str(out_path))

        n_updates = len(applied_updates[doc.path])
        n_new     = len(appended_new[doc.path])
        logger.info(
            "[adoc_updater] Wrote %s  (updates=%d  new_tcs=%d)",
            out_path, n_updates, n_new,
        )

    # ----- Write newly-created adoc files for unrouted TCs -----
    for stem, content in new_adoc_content.items():
        out_path = out_dir / f"{stem}_new_tcs.adoc"
        out_path.write_text(content, encoding="utf-8")
        written.append(str(out_path))
        logger.info("[adoc_updater] Created new adoc file: %s", out_path)

    # ----- Write fallback files for update candidates with no source adoc -----
    for stem, content in suggested_updates_content.items():
        out_path = out_dir / f"{stem}.adoc"
        out_path.write_text(content, encoding="utf-8")
        written.append(str(out_path))
        logger.info("[adoc_updater] Created suggested-updates fallback file: %s", out_path)

    return written


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_stem(name: str) -> str:
    """Compact lowercase alphanum key from cluster name / filename stem."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def _resolve_new_tc_path(
    tc: dict,
    cluster: str,
    path_by_prefix: Dict[str, str],
    path_by_stem: Dict[str, str],
) -> Optional[str]:
    """Find the target adoc path for a new (missing) TC.

    Strategy:
    1. TC prefix from suggested tc_id (e.g. "TC-OO-2.x" → "OO")
    2. Cluster name → normalised stem (e.g. "On/Off" → "onoff")
    3. Substring stem match
    """
    # 1. Try to derive prefix from a suggested tc_id in the TC dict
    suggested_id = (tc.get("tc_id") or tc.get("title") or "")
    m = re.search(r"TC-([A-Z][A-Z0-9]*)-\d+", suggested_id, re.IGNORECASE)
    if m:
        prefix = m.group(1).upper()
        path = path_by_prefix.get(prefix)
        if path:
            return path

    # 2. Cluster name → prefix (e.g. "On/Off" contains "OO"? — no, use stem)
    cluster_stem = _normalize_stem(cluster)

    # Direct stem lookup
    path = path_by_stem.get(cluster_stem)
    if path:
        return path

    # 3. Substring: e.g. "onoff" in "onoffswitch" or vice versa
    for stem, stem_path in path_by_stem.items():
        if cluster_stem and (cluster_stem in stem or stem in cluster_stem):
            return stem_path

    # 4. Last resort: check prefix_map against cleaned cluster name
    for prefix, ppath in path_by_prefix.items():
        if prefix.lower() in cluster_stem or cluster_stem in prefix.lower():
            return ppath

    return None
