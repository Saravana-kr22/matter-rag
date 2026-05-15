"""AsciiDoc document updater.

Applies LLM-suggested test case changes to Matter test plan ``.adoc`` files:

- **update_candidates**: replaces an existing TC section (located by ``tc_id``)
  with the LLM-generated ``adoc_section``.
- **missing_tests**: appends new TC sections to the relevant cluster's update
  file (or creates a new file when no existing source is found).

Output files are written to ``output_dir`` using canonical cluster names:
- Updated TCs (update_candidates): ``{Cluster}_updated_TCs.adoc``
- New TCs (missing_tests):         ``{Cluster}_new_TCs.adoc``
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from src.document_updater.base_updater import BaseDocumentUpdater

logger = logging.getLogger(__name__)

# Matches any TC heading at any AsciiDoc level, e.g. "== TC-OO-2.1 [DUT as Server]"
_TC_HEADING_RE = re.compile(
    r"^(={1,6})\s+(TC-[A-Z0-9]+-\d+\.\d+[^\n]*)$",
    re.MULTILINE,
)

# Pre-compiled "next heading at level N or higher" patterns (levels 1-6).
# Used by _replace_tc_section to find the end of a TC section.
_NEXT_HEADING_RE = {
    level: re.compile(r"^={1," + str(level) + r"}\s+", re.MULTILINE)
    for level in range(1, 7)
}


def _canonical_cluster(cluster: str) -> str:
    """Normalise a raw cluster name string to a canonical CamelCase identifier.

    Handles LLM output variations like "ON/OFF", "ON/OFF CLUSTER", "On/Off"
    all producing "OnOff", so multiple PR chunks that reference the same cluster
    are merged into a single output file.

    Examples::

        "ON/OFF"          → "OnOff"
        "ON/OFF CLUSTER"  → "OnOff"
        "On/Off"          → "OnOff"
        "DOOR LOCK"       → "DoorLock"
        "Level Control"   → "LevelControl"
    """
    # Strip trailing "cluster" word (common LLM addition)
    name = re.sub(r"\s+cluster\s*$", "", cluster.strip(), flags=re.IGNORECASE).strip()
    # Split on whitespace, slashes, or other separators; title-case each word
    words = re.split(r"[\s/\\_()\-]+", name)
    return "".join(w.capitalize() for w in words if w)


class AdocUpdater(BaseDocumentUpdater):
    """Write LLM-suggested Matter TC changes back to ``.adoc`` source files.

    The updater reads ``analysis_results[*]["llm_json"]`` which is expected to
    contain:

    .. code-block:: json

        {
          "missing_tests": [
            { "title": "TC-OO-3.1", "cluster": "OO", "adoc_section": "== TC-OO-3.1 ..." }
          ],
          "update_candidates": [
            { "tc_id": "TC-OO-2.1", "change_summary": "...", "adoc_section": "== TC-OO-2.1 ..." }
          ]
        }

    For ``update_candidates`` it locates the source ``.adoc`` file via the
    ``tc_id → absolute_path`` mapping built from ``search_results`` metadata.

    Output filenames use canonical CamelCase cluster names:
    - ``{Cluster}_updated_TCs.adoc``  — for update_candidates
    - ``{Cluster}_new_TCs.adoc``      — for missing_tests
    """

    @classmethod
    def supported_extension(cls) -> str:
        return ".adoc"

    def write_updates(
        self,
        analysis_results: List[dict],
        search_results: Dict[str, List],
        output_dir: str,
        tc_index: Optional[Dict] = None,
    ) -> List[str]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Build tc_id → absolute_path — primary source: tc_index (pre-built routing index)
        tc_id_to_path: Dict[str, str] = {}

        if tc_index:
            tc_id_to_path.update(tc_index.get("tc_map", {}))

        # Supplement / override from FAISS search result metadata
        for results in search_results.values():
            for r in results:
                tc_id = r.metadata.get("tc_id", "")
                abs_path = r.metadata.get("absolute_path", "")
                if tc_id and abs_path:
                    tc_id_to_path[tc_id] = abs_path

        # Collect updates grouped by source file
        # updates_by_file: absolute_path → [{tc_id, adoc_section, change_summary}]
        updates_by_file: Dict[str, List[dict]] = defaultdict(list)
        # new_tcs_by_cluster: cluster → [{title, adoc_section}]
        new_tcs_by_cluster: Dict[str, List[dict]] = defaultdict(list)

        for result in analysis_results:
            # Support both old format (llm_json wrapper) and new format (direct keys).
            llm_json = result.get("llm_json", {})
            update_items = llm_json.get("update_candidates", []) if llm_json else result.get("update_candidates", [])
            missing_items = llm_json.get("missing_tests", []) if llm_json else result.get("missing_tests", [])

            for uc in update_items:
                tc_id = uc.get("tc_id", "").strip()
                adoc_section = uc.get("adoc_section", "").strip()
                if not tc_id or not adoc_section:
                    continue
                source_path = tc_id_to_path.get(tc_id, "")
                if not source_path:
                    logger.warning(
                        "[AdocUpdater] No source file found for tc_id=%s — skipping", tc_id
                    )
                    continue
                updates_by_file[source_path].append({
                    "tc_id": tc_id,
                    "adoc_section": adoc_section,
                    "change_summary": uc.get("change_summary", ""),
                })

            for mt in missing_items:
                raw_cluster = mt.get("cluster", "UNKNOWN").strip()
                # Normalize to canonical CamelCase key so that "ON/OFF",
                # "ON/OFF CLUSTER", and "On/Off" all merge into one file.
                cluster_key = _canonical_cluster(raw_cluster) or "Unknown"
                adoc_section = mt.get("adoc_section", "").strip()
                if not adoc_section:
                    continue
                new_tcs_by_cluster[cluster_key].append({
                    "title": mt.get("title", ""),
                    "adoc_section": adoc_section,
                })

        written: List[str] = []

        # Apply updates to existing source files
        for source_path, updates in updates_by_file.items():
            out_path = self._apply_updates(source_path, updates, out_dir)
            if out_path:
                written.append(out_path)

        # Write new TCs — append to a cluster's update file if it already exists,
        # otherwise create a new file for the cluster
        for cluster, new_tcs in new_tcs_by_cluster.items():
            out_path = self._write_new_tcs(
                cluster, new_tcs, tc_id_to_path, search_results, out_dir
            )
            if out_path:
                written.append(out_path)

        logger.info("[AdocUpdater] Wrote %d file(s) to %s", len(written), out_dir)
        return written

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_updates(
        self,
        source_path: str,
        updates: List[dict],
        out_dir: Path,
    ) -> Optional[str]:
        """Replace TC sections in source_path and write {Cluster}_updated_TCs.adoc."""
        src = Path(source_path)
        if not src.exists():
            logger.warning("[AdocUpdater] Source file not found: %s", source_path)
            return None

        content = src.read_text(encoding="utf-8")
        for update in updates:
            content = self._replace_tc_section(
                content,
                update["tc_id"],
                update["adoc_section"],
                update.get("change_summary", ""),
            )

        canonical = _canonical_cluster(src.stem) or src.stem
        out_filename = f"{canonical}_updated_TCs.adoc"
        out_path = out_dir / out_filename
        out_path.write_text(content, encoding="utf-8")
        logger.info("[AdocUpdater] Updated %s → %s", src.name, out_path.name)
        return str(out_path)

    def _replace_tc_section(
        self,
        content: str,
        tc_id: str,
        new_section: str,
        change_summary: str,
    ) -> str:
        """Find the TC section for tc_id in content and replace it with new_section."""
        # tc_re must vary per tc_id; Python's re module caches the last N compiled patterns
        tc_pattern = r"^(={1,6})\s+" + re.escape(tc_id) + r"[^\n]*$"
        m = re.search(tc_pattern, content, re.MULTILINE)
        if not m:
            logger.warning(
                "[AdocUpdater] TC heading '%s' not found — appending to file", tc_id
            )
            change_note = (
                f"\n// AI RAG UPDATE: {change_summary}\n" if change_summary else "\n"
            )
            return content.rstrip() + change_note + "\n" + new_section.strip() + "\n"

        level = len(m.group(1))
        tc_start = m.start()

        # End of this TC: next heading at same or higher level (fewer '=' marks)
        # Use pre-compiled patterns from _NEXT_HEADING_RE
        next_re = _NEXT_HEADING_RE.get(level)
        next_m = next_re.search(content, m.end()) if next_re else None
        tc_end = next_m.start() if next_m else len(content)

        change_note = (
            f"// AI RAG UPDATE: {change_summary}\n" if change_summary else ""
        )
        return (
            content[:tc_start]
            + change_note
            + new_section.strip()
            + "\n\n"
            + content[tc_end:]
        )

    def _write_new_tcs(
        self,
        cluster: str,
        new_tcs: List[dict],
        tc_id_to_path: Dict[str, str],
        search_results: Dict[str, List],
        out_dir: Path,
    ) -> Optional[str]:
        """Append new TC sections for a cluster to {Cluster}_new_TCs.adoc."""
        if not new_tcs:
            return None

        # `cluster` is already the canonical CamelCase key (e.g. "OnOff")
        out_filename = f"{cluster}_new_TCs.adoc"
        out_path = out_dir / out_filename
        try:
            existing = out_path.read_text(encoding="utf-8") if out_path.exists() else (
                f"= {cluster} Cluster — New Test Cases\n"
                f":toc:\n\n"
                f"// AI RAG: New test cases generated from PR analysis\n\n"
            )
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("[AdocUpdater] Failed to read existing file %s: %s", out_path, exc)
            existing = (
                f"= {cluster} Cluster — New Test Cases\n"
                f":toc:\n\n"
                f"// AI RAG: New test cases generated from PR analysis\n\n"
            )

        new_content = existing.rstrip() + "\n\n"
        for tc in new_tcs:
            new_content += "// AI RAG: NEW TEST CASE\n"
            new_content += tc["adoc_section"].strip() + "\n\n"

        out_path.write_text(new_content, encoding="utf-8")
        logger.info("[AdocUpdater] New TCs for cluster %s → %s", cluster, out_path.name)
        return str(out_path)
