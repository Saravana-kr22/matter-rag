"""Matter schema extractor — parses canonical data-model entity tables from spec diff HTML.

Reads the original ``appclusters_diff.html`` (full HTML, *not* the annotated-text
sections produced by ``ProcessMatterHtmlDoc``) and builds a structured schema dict
of every Matter data-model entity that appears in a diff-touched cluster:

    {
      "clusters": [
        {
          "name": "On/Off",
          "section_id": "ref_OnOff",
          "diff_status": "changed",   # added | changed | unchanged
          "attributes": [
            {
              "id": "0x0000",
              "name": "OnOff",
              "type": "boolean",
              "access": "R V",
              "quality": "",
              "default": "FALSE",
              "conformance": "M",
              "diff_status": "unchanged"
            },
            ...
          ],
          "commands": [...],
          "events": [...],
          "features": [...]
        }
      ]
    }

Each entity row carries a ``diff_status``:
  - ``"added"``     — row is inside an ``<ins class="diff-new">`` wrapper or the cluster
                       itself is entirely new
  - ``"removed"``   — row is inside a ``<del class="diff-old">`` wrapper
  - ``"changed"``   — row has mixed diff-old / diff-chg cells (value changed)
  - ``"unchanged"`` — no diff annotations on the row

Usage::

    from src.processor.matter_schema_extractor import MatterSchemaExtractor
    extractor = MatterSchemaExtractor()
    schema = extractor.extract(html_string)   # returns the dict above
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SECT_CLASS_RE = re.compile(r'^sect[1-6]$')
_HEADING_TAG_RE = re.compile(r'^h[1-6]$')
_WHITESPACE_RE = re.compile(r'\s+')

# Heading keywords used to classify entity table type
_ATTR_KEYWORDS_RE  = re.compile(r'\battribut', re.I)
_CMD_KEYWORDS_RE   = re.compile(r'\bcommand', re.I)
_EVENT_KEYWORDS_RE = re.compile(r'\bevent', re.I)
_FEAT_KEYWORDS_RE  = re.compile(r'\bfeature', re.I)

# Heading titles that indicate "noise" sections to skip
_SKIP_SECTION_RE = re.compile(
    r'revision history|copyright|disclaimer|table of contents|introduction'
    r'|conformance|status|terms|acronyms',
    re.I,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub(' ', text).strip()


def _section_heading_text(div) -> str:
    """First heading text inside *div* (not inside child sectN divs)."""
    from bs4 import Tag
    for child in div.children:
        if isinstance(child, Tag) and _HEADING_TAG_RE.match(child.name):
            return _clean(child.get_text())
    return ''


def _section_level(div) -> int:
    for cls in div.get('class', []):
        m = re.match(r'^sect(\d)$', cls)
        if m:
            return int(m.group(1))
    return 0


def _has_diff_anywhere(element) -> bool:
    """True if *element* or any descendant has a diff-* CSS class."""
    if any(
        c.startswith('diff-')
        for c in element.get('class', [])
    ):
        return True
    return bool(element.find(class_=re.compile(r'^diff-')))


def _row_diff_status(tr) -> str:
    """Determine the diff status of a single <tr> row.

    Check order:
    1. If the row (or a wrapping element) has diff-new class → "added"
    2. If the row has diff-old class → "removed"
    3. If any cell has diff-old / diff-chg / diff-new → "changed"
    4. Otherwise → "unchanged"
    """
    from bs4 import Tag

    # Check the row itself
    tr_classes = tr.get('class', [])
    if any(c == 'diff-new' for c in tr_classes):
        return 'added'
    if any(c == 'diff-old' for c in tr_classes):
        return 'removed'

    # Check if row is wrapped in ins/del
    parent = tr.parent
    if isinstance(parent, Tag):
        p_cls = parent.get('class', [])
        if parent.name == 'ins' and 'diff-new' in p_cls:
            return 'added'
        if parent.name == 'del' and 'diff-old' in p_cls:
            return 'removed'

    # Look for diff annotations inside the cells
    has_old = bool(tr.find('del', class_='diff-old'))
    has_new = bool(tr.find('ins', class_=re.compile(r'^diff-')))
    if has_old or has_new:
        return 'changed'

    return 'unchanged'


def _cell_value(td) -> str:
    """Extract clean text from a table cell, preferring ins.diff-chg (new value).

    If the cell contains a changed pair (del.diff-old followed by ins.diff-chg),
    we return the new value from ins.diff-chg. Removed-only cells return empty
    to signal the row should be captured as 'removed' via diff_status.
    """
    from bs4 import Tag

    # Changed cell: prefer the ins.diff-chg value
    chg_ins = td.find('ins', class_='diff-chg')
    if chg_ins:
        return _clean(chg_ins.get_text())

    # Added-only cell
    new_ins = td.find('ins', class_='diff-new')
    if new_ins:
        # If the whole cell is new, return full text
        all_text = _clean(td.get_text())
        return all_text

    # Removed-only: return the old value (will be marked removed at row level)
    old_del = td.find('del', class_='diff-old')
    if old_del:
        # Still useful to know what was there
        return _clean(old_del.get_text())

    return _clean(td.get_text())


def _cell_old_value(td) -> Optional[str]:
    """Return the old (removed) value when a cell has been changed, else None."""
    old_del = td.find('del', class_='diff-old')
    return _clean(old_del.get_text()) if old_del else None


# ---------------------------------------------------------------------------
# Table classification
# ---------------------------------------------------------------------------

def _classify_table(section_heading: str, header_cells: List[str]) -> Optional[str]:
    """Return entity type string or None if table can't be classified.

    Entity types: "attributes", "commands", "events", "features"

    Classification precedence:
    1. Section heading contains a keyword
    2. Column header set matches known patterns
    """
    heading_lower = section_heading.lower()

    if _ATTR_KEYWORDS_RE.search(heading_lower):
        return 'attributes'
    if _CMD_KEYWORDS_RE.search(heading_lower):
        return 'commands'
    if _EVENT_KEYWORDS_RE.search(heading_lower):
        return 'events'
    if _FEAT_KEYWORDS_RE.search(heading_lower):
        return 'features'

    # Fallback: infer from column headers
    cols_lower = {h.lower() for h in header_cells}

    if cols_lower & {'quality', 'nullable', 'access', 'privilege'}:
        return 'attributes'
    if 'direction' in cols_lower or 'response' in cols_lower:
        return 'commands'
    if 'priority' in cols_lower and ('event' in ' '.join(cols_lower)):
        return 'events'
    if 'bit' in cols_lower or 'feature' in ' '.join(cols_lower):
        return 'features'

    return None


# ---------------------------------------------------------------------------
# Row → entity dict
# ---------------------------------------------------------------------------

def _extract_table_rows(table, entity_type: str, cluster_is_new: bool) -> List[Dict[str, Any]]:
    """Parse all body rows of *table* into entity dicts.

    Each dict contains one key per column header (snake_case-ified) plus
    ``diff_status``. Changed cells also store the old value under
    ``{col}_old``.
    """
    from bs4 import Tag

    # Extract header row
    header_row = table.find('tr')
    if not header_row:
        return []

    headers = [_clean(th.get_text()) for th in header_row.find_all(['th', 'td'])]
    if not headers:
        return []

    # Normalise header names
    col_names = [re.sub(r'[^a-z0-9]+', '_', h.lower()).strip('_') for h in headers]

    rows = []
    for tr in table.find_all('tr')[1:]:  # skip header row
        cells = tr.find_all(['td', 'th'])
        if not cells:
            continue

        if cluster_is_new:
            status = 'added'
        else:
            status = _row_diff_status(tr)

        entity: Dict[str, Any] = {'diff_status': status}
        for col, td in zip(col_names, cells):
            entity[col] = _cell_value(td)
            old = _cell_old_value(td)
            if old is not None and old != entity[col]:
                entity[f'{col}_old'] = old

        rows.append(entity)

    return rows


# ---------------------------------------------------------------------------
# MatterSchemaExtractor
# ---------------------------------------------------------------------------

class MatterSchemaExtractor:
    """Extract canonical Matter data-model entities from a spec diff HTML document.

    Works on the *full* HTML (preserving table structure) — not the annotated-text
    output of ``ProcessMatterHtmlDoc``.

    Algorithm:
    1. Find all sect2 divs that have diff markup anywhere in their subtree.
    2. For each such cluster div, iterate its sect3/sect4 child divs.
    3. For each child, look for ``<table>`` elements and classify them as
       attributes / commands / events / features using the sub-section heading
       and column headers.
    4. Parse each table row into an entity dict with ``diff_status``.
    5. Return a structured ``schema`` dict.

    Args:
        parser:         BeautifulSoup parser (default: 'lxml', falls back to 'html.parser').
        diff_only:      If True (default), only include clusters that have at least
                        one diff-touched row. If False, include all clusters found
                        even when no rows changed.
    """

    def __init__(self, parser: str = 'lxml', diff_only: bool = True) -> None:
        self._parser = parser
        self._diff_only = diff_only

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def extract(self, html_content: str) -> Dict[str, Any]:
        """Parse *html_content* and return a canonical Matter schema dict.

        Returns::

            {
              "clusters": [
                {
                  "name": "On/Off",
                  "diff_status": "changed",
                  "attributes": [...],
                  "commands": [...],
                  "events": [...],
                  "features": [...]
                },
                ...
              ]
            }

        Returns ``{"clusters": []}`` when no relevant content is found.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error(
                "[MatterSchemaExtractor] beautifulsoup4 is required. "
                "Install with: pip install beautifulsoup4 lxml"
            )
            return {"clusters": []}

        if not html_content or not html_content.strip():
            return {"clusters": []}

        try:
            soup = BeautifulSoup(html_content, self._parser)
        except Exception:
            try:
                soup = BeautifulSoup(html_content, 'html.parser')
            except Exception as exc:
                logger.error("[MatterSchemaExtractor] HTML parse failed: %s", exc)
                return {"clusters": []}

        clusters = self._extract_clusters(soup)

        if self._diff_only:
            # Keep only clusters that have at least one entity with diff_status != "unchanged"
            filtered = []
            for c in clusters:
                all_entities = (
                    c.get('attributes', []) + c.get('commands', []) +
                    c.get('events', []) + c.get('features', [])
                )
                if c['diff_status'] != 'unchanged' or any(
                    e.get('diff_status', 'unchanged') != 'unchanged' for e in all_entities
                ):
                    filtered.append(c)
            clusters = filtered

        logger.info(
            "[MatterSchemaExtractor] Extracted schema: %d cluster(s) with diff content",
            len(clusters),
        )
        return {"clusters": clusters}

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _extract_clusters(self, soup) -> List[Dict[str, Any]]:
        """Find all sect2 divs that represent cluster chapters."""
        from bs4 import Tag

        clusters = []
        for div in soup.find_all('div', class_=_SECT_CLASS_RE):
            level = _section_level(div)
            if level != 2:
                continue

            heading = _section_heading_text(div)
            if not heading or _SKIP_SECTION_RE.search(heading):
                continue

            # Only process clusters that have diff markup in their subtree
            if not _has_diff_anywhere(div):
                continue

            cluster_is_new = self._is_new_cluster(div)
            if cluster_is_new:
                diff_status = 'added'
            elif _has_diff_anywhere(div):
                diff_status = 'changed'
            else:
                diff_status = 'unchanged'

            entities = self._extract_entities(div, cluster_is_new)

            # Try to find an anchor / section id
            anchor = div.get('id', '')
            heading_tag = div.find(_HEADING_TAG_RE, recursive=False)
            if heading_tag:
                a = heading_tag.find('a', id=True)
                if a:
                    anchor = a.get('id', anchor)

            clusters.append({
                'name':        heading,
                'section_id':  anchor,
                'diff_status': diff_status,
                'attributes':  entities.get('attributes', []),
                'commands':    entities.get('commands', []),
                'events':      entities.get('events', []),
                'features':    entities.get('features', []),
            })

        return clusters

    def _is_new_cluster(self, cluster_div) -> bool:
        """True when the cluster's heading is entirely inside diff-new elements."""
        from bs4 import Tag
        hdr = cluster_div.find(_HEADING_TAG_RE, recursive=False)
        if not hdr:
            return False
        parent = hdr.parent
        if isinstance(parent, Tag) and parent.name == 'ins' and 'diff-new' in parent.get('class', []):
            return True
        ins_texts = ''.join(i.get_text() for i in hdr.find_all('ins', class_='diff-new'))
        if ins_texts and _clean(ins_texts) == _clean(hdr.get_text()):
            return True
        return False

    def _extract_entities(self, cluster_div, cluster_is_new: bool) -> Dict[str, List]:
        """Walk sub-sections of *cluster_div* and extract entity tables."""
        from bs4 import Tag

        result: Dict[str, List] = {
            'attributes': [],
            'commands':   [],
            'events':     [],
            'features':   [],
        }

        # Iterate direct child sectN divs (sub-sections of the cluster)
        for child in cluster_div.find_all('div', class_=_SECT_CLASS_RE, recursive=True):
            child_level = _section_level(child)
            if child_level <= 2:
                continue  # skip the cluster itself or outer containers

            heading = _section_heading_text(child)
            if not heading or _SKIP_SECTION_RE.search(heading):
                continue

            # Find tables that are direct or near-direct children of this section
            # (not inside child sub-sub-sections)
            tables = self._find_own_tables(child)
            for table in tables:
                header_cells = self._header_cells(table)
                entity_type = _classify_table(heading, header_cells)
                if entity_type is None:
                    continue

                rows = _extract_table_rows(table, entity_type, cluster_is_new)
                if rows:
                    result[entity_type].extend(rows)

        return result

    def _find_own_tables(self, section_div) -> list:
        """Return tables that belong to *section_div* but not to its child sectN sub-sections."""
        from bs4 import Tag

        tables = []
        for el in section_div.find_all('table', recursive=True):
            # Check that no ancestor between el and section_div is another sectN div
            in_child_sect = False
            for ancestor in el.parents:
                if ancestor is section_div:
                    break
                if isinstance(ancestor, Tag):
                    if any(_SECT_CLASS_RE.match(c) for c in ancestor.get('class', [])):
                        in_child_sect = True
                        break
            if not in_child_sect:
                tables.append(el)

        return tables

    def _header_cells(self, table) -> List[str]:
        """Extract text from the first row of *table* (header cells)."""
        first_row = table.find('tr')
        if not first_row:
            return []
        return [_clean(th.get_text()) for th in first_row.find_all(['th', 'td'])]
