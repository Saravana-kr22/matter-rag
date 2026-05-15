"""TC Index Builder — scans adoc test plan files and builds a routing index.

The index is saved as ``data/tc_index.json`` and used by ``write_updated_adocs``
to find the correct adoc file for each TC-ID / cluster without guessing.

Three maps are built:

    tc_map     — exact TC-ID  →  absolute path of the adoc file that contains it
                 e.g. "TC-OO-2.1" → ".../onoff.adoc"

    prefix_map — TC prefix    →  absolute path of the adoc file whose headings use it
                 e.g. "OO" → ".../onoff.adoc"

    stem_map   — filename stem (lower) → absolute path
                 e.g. "onoff" → ".../onoff.adoc"
                 Used as last-resort fuzzy match when the cluster name normalises to the stem.

Usage
-----
    # From Python
    from src.document_updater.tc_index_builder import build_tc_index, load_tc_index

    index = build_tc_index("data/test_plan_adocs/src", "data/tc_index.json")
    index = load_tc_index("data/tc_index.json")

    # From CLI
    python scripts/build_tc_index.py
    python scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src --output data/tc_index.json
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex — flexible heading: any level (==..====), optional brackets around ID
# Captures: (heading_prefix, tc_id)
# Matches:
#   == TC-OO-2.1 Title
#   === TC-OO-2.1 Title
#   ==== [TC-OO-2.1] Title
#   == [TC-OO-2.1] Attributes with server as DUT
# ---------------------------------------------------------------------------
_TC_HEADING_RE = re.compile(
    r"^={2,6}\s+\[?(TC-[A-Z][A-Z0-9]*-\d+(?:\.\d+)*)\]?",
    re.MULTILINE,
)

# AsciiDoc attribute resolution — handle :key: value definitions and {key} substitution.
# Most cluster adoc files use `:picsCode: PAVST` + `TC-{picsCode}-2.1` so the literal
# TC-ID never appears in the raw text and _TC_HEADING_RE finds nothing.
_ADOC_ATTR_DEF_RE = re.compile(r"^:([A-Za-z][A-Za-z0-9_-]*):\s+(.+)$", re.MULTILINE)
_ADOC_ATTR_REF_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]*)\}")


def _resolve_adoc_attributes(content: str) -> str:
    """Substitute AsciiDoc {attr} references with their :attr: defined values.

    Scans the file for ``:key: value`` definitions (document-header attributes)
    and replaces every ``{key}`` token.  This turns headings like
    ``== TC-{picsCode}-2.1`` into ``== TC-PAVST-2.1`` before the TC-ID regex runs.

    Unknown references are left intact so the regex can still skip them cleanly.
    """
    attrs: dict = {}
    for m in _ADOC_ATTR_DEF_RE.finditer(content):
        attrs[m.group(1)] = m.group(2).strip()
    if not attrs:
        return content

    def _sub(m: re.Match) -> str:
        return attrs.get(m.group(1), m.group(0))

    return _ADOC_ATTR_REF_RE.sub(_sub, content)


def _normalize_stem(name: str) -> str:
    """Normalise a cluster name or filename stem to a compact lowercase key.

    e.g. "On/Off Cluster" → "onoff", "levelcontrol" → "levelcontrol"
    Strips unicode, collapses non-alphanumeric to "", lowercases.
    """
    # NFKD decompose, strip combining chars
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_tc_index(adoc_dir: str, output_path: str = "data/tc_index.json") -> dict:
    """Scan *adoc_dir* recursively for .adoc files, extract all TC-IDs, write index JSON.

    Returns the index dict (same structure as what is written to disk).
    This is the CLI / out-of-band entry point.  The pipeline uses
    ``build_tc_index_from_docs`` which works from already-loaded documents.
    """
    adoc_root = Path(adoc_dir)
    if not adoc_root.exists():
        logger.warning("[tc_index_builder] adoc_dir does not exist: %s", adoc_root)
        return _empty_index(adoc_dir)

    adoc_files: List[Path] = sorted(adoc_root.rglob("*.adoc"))
    if not adoc_files:
        logger.warning("[tc_index_builder] No .adoc files found under %s", adoc_root)
        return _empty_index(adoc_dir)

    # Build (path, content) pairs and delegate to the shared builder
    path_content: List[tuple] = []
    for adoc_path in adoc_files:
        try:
            content = adoc_path.read_text(encoding="utf-8", errors="replace")
            path_content.append((str(adoc_path.resolve()), content))
        except Exception as exc:
            logger.warning("[tc_index_builder] Could not read %s: %s", adoc_path, exc)

    return _build_index_from_path_content(
        path_content,
        adoc_root=str(adoc_root.resolve()),
        output_path=output_path,
    )


def build_tc_index_from_docs(
    docs: list,  # List[FetchedDocument]
    output_path: str = "data/tc_index.json",
) -> dict:
    """Build the TC index from already-loaded FetchedDocument objects.

    This is the pipeline entry point called by ``fetch_documents_node`` after
    adoc sources are loaded into ``test_plan_adoc_sources``.  It avoids any
    directory scanning — the exact set of loaded documents is used.

    Rebuilds the index if the file is absent or any document's path is newer
    than the existing index's ``generated_at`` timestamp.  If the index is
    up-to-date, loads and returns the cached version.
    """
    if not docs:
        return _empty_index("(no docs)")

    # Check whether a rebuild is needed
    existing = load_tc_index(output_path)
    if existing and not _needs_rebuild(existing, docs):
        logger.info(
            "[tc_index_builder] tc_index up-to-date (%d tc_ids) — skipping rebuild",
            len(existing.get("tc_map", {})),
        )
        return existing

    # Build (abs_path, content) pairs from docs
    path_content: List[tuple] = []
    for doc in docs:
        abs_path = str(Path(doc.path).resolve())
        path_content.append((abs_path, doc.content))

    # Derive a representative root from the common ancestor of all doc paths
    all_parents = [Path(p).parent for p, _ in path_content]
    adoc_root = str(_common_ancestor(all_parents)) if all_parents else "(docs)"

    return _build_index_from_path_content(
        path_content,
        adoc_root=adoc_root,
        output_path=output_path,
    )


def load_tc_index(index_path: str) -> Optional[dict]:
    """Load an existing tc_index.json from disk.  Returns None if the file does not exist."""
    p = Path(index_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[tc_index_builder] Could not load %s: %s", p, exc)
        return None


def resolve_adoc_path(
    index: dict,
    *,
    tc_id: Optional[str] = None,
    prefix: Optional[str] = None,
    cluster_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve an adoc file path from the index using up to three fallback strategies.

    Priority:
      1. tc_map[tc_id]           — exact TC-ID match (most reliable)
      2. prefix_map[prefix]      — TC prefix (e.g. "OO")
      3. stem_map[normalize(cluster_name)] — cluster name → stem fuzzy match

    Returns the absolute path string, or None if no match found.
    """
    tc_map     = index.get("tc_map", {})
    prefix_map = index.get("prefix_map", {})
    stem_map   = index.get("stem_map", {})

    # 1. Exact TC-ID
    if tc_id:
        path = tc_map.get(tc_id)
        if path:
            logger.debug("[tc_index] tc_map hit: %s → %s", tc_id, Path(path).name)
            return path

    # 2. TC prefix (derive from tc_id if not supplied)
    effective_prefix = prefix
    if not effective_prefix and tc_id:
        parts = tc_id.split("-")
        if len(parts) >= 2:
            effective_prefix = parts[1].upper()
    if effective_prefix:
        path = prefix_map.get(effective_prefix.upper())
        if path:
            logger.debug("[tc_index] prefix_map hit: %s → %s", effective_prefix, Path(path).name)
            return path

    # 3. Cluster name stem fuzzy
    if cluster_name:
        stem = _normalize_stem(cluster_name)
        # Direct stem lookup
        path = stem_map.get(stem)
        if path:
            logger.debug("[tc_index] stem_map hit: '%s' → %s", stem, Path(path).name)
            return path
        # Substring match: e.g. "onoff" in "onoffswitch" or "onoffswitch" in "onoff"
        for map_stem, map_path in stem_map.items():
            if stem in map_stem or map_stem in stem:
                logger.debug(
                    "[tc_index] stem_map substring hit: '%s' ~ '%s' → %s",
                    stem, map_stem, Path(map_path).name,
                )
                return map_path

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_index(adoc_dir: str) -> dict:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "adoc_root": adoc_dir,
        "stats": {"adoc_files_scanned": 0, "tc_ids_indexed": 0, "prefixes_indexed": 0},
        "tc_map": {},
        "prefix_map": {},
        "stem_map": {},
    }


def _build_index_from_path_content(
    path_content: List[tuple],  # [(abs_path_str, content_str), ...]
    adoc_root: str,
    output_path: str,
) -> dict:
    """Core builder: extract TC-IDs from (path, content) pairs and write JSON."""
    tc_map: Dict[str, str] = {}
    prefix_map: Dict[str, str] = {}
    stem_map: Dict[str, str] = {}

    for abs_path, content in path_content:
        stem = _normalize_stem(Path(abs_path).stem)
        if stem and stem not in stem_map:
            stem_map[stem] = abs_path

        # Resolve AsciiDoc {attr} substitutions before regex matching so that
        # headings like "== TC-{picsCode}-2.1" are expanded to "== TC-PAVST-2.1".
        resolved_content = _resolve_adoc_attributes(content)

        tc_ids_in_file: List[str] = []
        for m in _TC_HEADING_RE.finditer(resolved_content):
            tc_id = m.group(1)
            tc_ids_in_file.append(tc_id)

            if tc_id not in tc_map:
                tc_map[tc_id] = abs_path
            else:
                logger.debug(
                    "[tc_index_builder] Duplicate TC-ID %s — first in %s, also in %s",
                    tc_id, Path(tc_map[tc_id]).name, Path(abs_path).name,
                )

            parts = tc_id.split("-")
            if len(parts) >= 2:
                prefix = parts[1].upper()
                if prefix not in prefix_map:
                    prefix_map[prefix] = abs_path

        logger.debug(
            "[tc_index_builder] %s — %d TC-ID(s): %s",
            Path(abs_path).name,
            len(tc_ids_in_file),
            ", ".join(tc_ids_in_file[:5]) + ("…" if len(tc_ids_in_file) > 5 else ""),
        )

    index = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "adoc_root": adoc_root,
        "stats": {
            "adoc_files_scanned": len(path_content),
            "tc_ids_indexed": len(tc_map),
            "prefixes_indexed": len(prefix_map),
        },
        "tc_map": tc_map,
        "prefix_map": prefix_map,
        "stem_map": stem_map,
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    logger.info(
        "[tc_index_builder] Index written → %s  (tc_ids=%d  prefixes=%d  files=%d)",
        out_path, len(tc_map), len(prefix_map), len(path_content),
    )
    return index


def _needs_rebuild(existing_index: dict, docs: list) -> bool:
    """Return True if any doc file is newer than the index's generated_at timestamp."""
    try:
        generated_at = datetime.strptime(
            existing_index["generated_at"], "%Y-%m-%d %H:%M:%S"
        )
    except (KeyError, ValueError):
        return True  # Can't parse timestamp — rebuild to be safe

    generated_ts = generated_at.timestamp()

    for doc in docs:
        p = Path(doc.path)
        if p.exists():
            try:
                if p.stat().st_mtime > generated_ts:
                    logger.debug(
                        "[tc_index_builder] %s is newer than index — rebuild triggered",
                        p.name,
                    )
                    return True
            except OSError:
                pass  # Can't stat — ignore

    return False


def _common_ancestor(paths: List[Path]) -> Path:
    """Return the longest common ancestor directory of a list of Paths."""
    if not paths:
        return Path(".")
    try:
        from pathlib import PurePath
        parts_list = [p.resolve().parts for p in paths]
        common = parts_list[0]
        for parts in parts_list[1:]:
            common = common[:len([a for a, b in zip(common, parts) if a == b])]
        return Path(*common) if common else Path(".")
    except Exception:
        return paths[0].parent
