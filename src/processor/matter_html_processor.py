"""Matter HTML diff processor — extracts annotated spec diffs from appclusters_diff.html.

Parses an HTML file produced by the Matter spec Asciidoctor build that contains
diff annotations (``diff-new``, ``diff-old``, ``diff-chg`` CSS classes). Each changed
section in the spec becomes a ``FetchedDocument`` whose content is the annotated text:

    [ADDED: new text]
    [REMOVED: old text]
    [CHANGED: old text → new text]
    plain text (unchanged)

Usage::

    from src.processor.matter_html_processor import ProcessMatterHtmlDoc
    from src.fetcher.base_fetcher import FetchedDocument

    proc = ProcessMatterHtmlDoc()
    html_doc = FetchedDocument(path="appclusters_diff.html", content=html_string)
    sections = proc.process(html_doc)   # → List[FetchedDocument], one per diff section

The returned documents are ready for vector search and KG queries — they carry
all context the LLM needs to determine which test cases must be updated or created.
No LLM calls are made here.

Metadata keys on each returned ``FetchedDocument``:

  doc_type         "matter_spec_diff"
  cluster          Cluster chapter name, e.g. "On/Off"
  section_title    Section heading text
  section_level    AsciiDoc/HTML heading level (2–6)
  is_new_section   True when the entire section is newly added
  source_html      Original HTML file path
  _process_rules   Empty list (no further text-cleaning needed)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.fetcher.base_fetcher import FetchedDocument

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r'\s+')
_SECT_CLASS_RE = re.compile(r'^sect[1-6]$')
_HEADING_RE    = re.compile(r'^h[1-6]$')

# Sections that are metadata noise — skip them
_SKIP_TITLES_RE = re.compile(
    r'revision history|copyright|disclaimer|table of contents|introduction',
    re.I,
)

# Detects a [CHANGED: [N.NNN] → [N.NNN]] annotation — spec section cross-reference renumber.
# These represent structural reorganization only (e.g. [1.247] → [1.253]) with no
# behavioral delta.  Sections whose only diff is this pattern are false positives for
# test-case generation and must be filtered out before reaching the LLM.
_CROSS_REF_CHANGED_RE = re.compile(
    r'\[CHANGED:\s*\[\d+(?:\.\d+)+\]\s*→\s*\[\d+(?:\.\d+)+\]\]'
)

# Strips leading AsciiDoc section-number prefixes from cluster/section titles.
# e.g. "1.5. On/Off Cluster" → "On/Off Cluster", "11.7. Door Lock" → "Door Lock"
_SECTION_NUM_PREFIX_RE = re.compile(r'^\d+(?:\.\d+)*\.\s*')


# ---------------------------------------------------------------------------
# Internal helpers (ported from diff_ai_summary.py; LLM calls removed)
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Collapse whitespace runs and strip surrounding whitespace."""
    return _WHITESPACE_RE.sub(' ', text).strip()


def _is_pure_cross_ref_renumber(text: str) -> bool:
    """Return True when the ONLY diff annotations are spec cross-reference renumbers.

    Matches sections where every [CHANGED: ...] annotation is a bare section-number
    renumber of the form ``[CHANGED: [N.NNN] → [N.NNN]]`` (e.g. ``[CHANGED: [1.247] →
    [1.253]]``) and no [ADDED: ...] or [REMOVED: ...] annotations are present.

    Such sections contain no behavioral delta — they just reflect spec restructuring
    where cross-reference citation numbers were updated.  Passing them to the LLM
    produces false-positive test-case suggestions.
    """
    # Must have at least one diff annotation to be worth checking.
    if not re.search(r'\[(ADDED|REMOVED|CHANGED):', text):
        return False
    # Any ADDED or REMOVED annotation indicates a real content change.
    if re.search(r'\[(ADDED|REMOVED):', text):
        return False
    # Strip all cross-ref CHANGED annotations; if any [CHANGED: remains, not a pure renumber.
    stripped = _CROSS_REF_CHANGED_RE.sub('', text)
    return '[CHANGED:' not in stripped


def _section_title(div) -> str:  # Tag
    """Return the heading text of a sectN div, or empty string."""
    hdr = div.find(_HEADING_RE, recursive=False)
    return _clean(hdr.get_text()) if hdr else ''


def _heading_is_new(section_div) -> bool:  # Tag
    """Return True when the section heading is entirely inside diff-new elements.

    Handles three cases:
      1. The <hN> element itself is wrapped by ins.diff-new
      2. ALL heading text comes from ins.diff-new children (exact match)
      3. Section number prefix ("11.7.8.10.") is plain text but the title
         portion is entirely in ins.diff-new.
    """
    try:
        from bs4 import Tag, NavigableString
    except ImportError as exc:
        raise ImportError(
            "beautifulsoup4 is required for MatterHtmlDiffProcessor. "
            "Install it with: pip install beautifulsoup4 lxml"
        ) from exc

    hdr = section_div.find(_HEADING_RE, recursive=False)
    if not hdr:
        return False

    hdr_parent = hdr.parent
    if isinstance(hdr_parent, Tag) and hdr_parent.name == 'ins' and 'diff-new' in hdr_parent.get('class', []):
        return True

    ins_texts = ''.join(i.get_text() for i in hdr.find_all('ins', class_='diff-new'))
    if not ins_texts:
        return False

    ins_clean = _clean(ins_texts)
    all_clean  = _clean(hdr.get_text())

    if ins_clean == all_clean:
        return True

    # Strip leading numeric section prefix and compare
    title_only = re.sub(r'^[\d]+(?:\.\d+)*\.?\s*', '', all_clean).strip()
    if title_only and ins_clean == title_only:
        return True

    return False


def _annotated_text(section_div) -> str:  # Tag
    """Walk the section div and produce a flat annotated string.

    Annotations:
      [REMOVED: ...]    for del.diff-old
      [ADDED: ...]      for ins.diff-new
      [CHANGED: x → y] for paired del.diff-old / ins.diff-chg

    Stops recursing into child sectN divs so that each section is reported
    independently without duplicating content from sub-sections.

    When ``_heading_is_new`` is True for this section, all plain body text
    is also wrapped as [ADDED: ...] because the entire section is new.
    """
    try:
        from bs4 import NavigableString, Tag
    except ImportError as exc:
        raise ImportError(
            "beautifulsoup4 is required for MatterHtmlDiffProcessor. "
            "Install it with: pip install beautifulsoup4 lxml"
        ) from exc

    new_section = _heading_is_new(section_div)
    parts: list[str] = []
    _skip_element = None  # Track the exact paired ins element to skip

    def walk(node, is_root: bool = False) -> None:
        nonlocal _skip_element

        if isinstance(node, NavigableString):
            t = _clean(str(node))
            if t:
                parts.append(f'[ADDED: {t}]' if new_section else t)
            return

        if not isinstance(node, Tag):
            return

        cls = node.get('class', [])

        # Stop at nested section boundaries (but not the root div itself).
        # Use _SECT_CLASS_RE (sect[1-6]) — NOT startswith('sect'), which would
        # also match 'sectionbody', the AsciiDoc wrapper div that holds all actual
        # section content and must be recursed into.
        if not is_root and node.name == 'div' and any(_SECT_CLASS_RE.match(c) for c in cls):
            return

        # del.diff-old — look ahead for a paired ins.diff-chg
        if node.name == 'del' and 'diff-old' in cls:
            old_txt = _clean(node.get_text())
            nxt = node.next_sibling
            while nxt and isinstance(nxt, NavigableString) and nxt.strip() == '':
                nxt = nxt.next_sibling
            if isinstance(nxt, Tag) and nxt.name == 'ins' and 'diff-chg' in nxt.get('class', []):
                new_txt = _clean(nxt.get_text())
                if old_txt and new_txt and old_txt != new_txt:
                    parts.append(f'[CHANGED: {old_txt} → {new_txt}]')
                _skip_element = nxt  # Store exact element to skip
            else:
                if old_txt:
                    parts.append(f'[REMOVED: {old_txt}]')
            return

        # ins.diff-chg — may have been consumed above
        if node.name == 'ins' and 'diff-chg' in cls:
            if node is _skip_element:
                _skip_element = None
                return
            t = _clean(node.get_text())
            if t:
                parts.append(f'[ADDED: {t}]')
            return

        # ins.diff-new
        if node.name == 'ins' and 'diff-new' in cls:
            t = _clean(node.get_text())
            if t:
                parts.append(f'[ADDED: {t}]')
            return

        # Regular element — recurse
        for child in node.children:
            walk(child)

    walk(section_div, is_root=True)
    return ' '.join(parts)


def _has_own_diff(div) -> bool:  # Tag
    """True if the section has diff elements in its OWN content (not inside child sectN divs)."""
    from bs4 import Tag
    for el in div.find_all(['del', 'ins'], class_=re.compile('diff-')):
        in_child_sect = False
        for ancestor in el.parents:
            if ancestor is div:
                break
            if isinstance(ancestor, Tag):
                cls = ancestor.get('class', [])
                if any(_SECT_CLASS_RE.match(c) for c in cls):
                    in_child_sect = True
                    break
        if not in_child_sect:
            return True
    return False


def _collect_sections(
    soup,
    cluster_filter: str = '',
    section_filter: str = '',
) -> list[dict]:
    """Return a list of dicts: {cluster, title, level, text, is_new_section}.

    Every sectN div (level >= 2) that has diff elements in its OWN direct
    content is included. Child sections are included independently.

    When ``cluster_filter`` is given, restricts collection to descendants of
    the h3-headed sectN div matching that name.
    """
    try:
        from bs4 import Tag
    except ImportError as exc:
        raise ImportError(
            "beautifulsoup4 is required for MatterHtmlDiffProcessor. "
            "Install it with: pip install beautifulsoup4 lxml"
        ) from exc

    # Resolve cluster filter to specific h3-headed sectN root div(s)
    cluster_roots: list | None = None
    if cluster_filter:
        cluster_roots = []
        for div in soup.find_all('div', class_=_SECT_CLASS_RE):
            hdr = div.find(_HEADING_RE, recursive=False)
            if hdr and hdr.name == 'h3':
                title = _clean(hdr.get_text())
                if cluster_filter.lower() in title.lower():
                    cluster_roots.append(div)

    results = []
    for div in soup.find_all('div', class_=_SECT_CLASS_RE):
        cls = div.get('class', [])
        level = int(cls[0][-1]) if cls else 0
        if level < 2:
            continue

        title = _section_title(div)
        if not title or _SKIP_TITLES_RE.search(title):
            continue

        if not _has_own_diff(div):
            continue

        if cluster_roots is not None:
            # Include the cluster root section itself (div IS the root) OR any
            # section that is a descendant of a cluster root.
            if div not in cluster_roots and not any(
                any(ancestor is root for ancestor in div.parents)
                for root in cluster_roots
            ):
                continue

        # Find parent cluster heading (sect2/sect1 ancestor)
        cluster = ''
        for ancestor in div.parents:
            if isinstance(ancestor, Tag):
                acls = ancestor.get('class', [])
                if 'sect2' in acls or 'sect1' in acls:
                    cluster = _section_title(ancestor)
                    break

        # Strip leading section-number prefix (e.g. "1.5. On/Off Cluster" → "On/Off Cluster").
        # The raw HTML section title includes the AsciiDoc section number, which is not part
        # of the canonical cluster name and causes KG lookups to fail downstream.
        cluster = _SECTION_NUM_PREFIX_RE.sub('', cluster).strip()

        if section_filter and section_filter.lower() not in title.lower():
            continue

        text = _annotated_text(div)
        if '[REMOVED:' not in text and '[ADDED:' not in text and '[CHANGED:' not in text:
            continue

        # Skip sections whose only diff is a spec cross-reference renumber.
        # These look like [CHANGED: [1.247] → [1.253]] — the section citation index
        # was renumbered after spec restructuring, but the normative content is unchanged.
        # Passing them to the LLM causes false-positive test-case suggestions.
        if _is_pure_cross_ref_renumber(text):
            logger.debug(
                "[MatterHtmlProcessor] Skipping pure cross-ref renumber section: %s / %s",
                cluster, title,
            )
            continue

        results.append({
            'cluster':        cluster,
            'title':          title,
            'level':          level,
            'text':           text,
            'is_new_section': _heading_is_new(div),
        })

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class DiffSection:
    """Structured representation of one annotated diff section."""
    cluster: str
    title: str
    level: int
    text: str
    is_new_section: bool = False


class ProcessMatterHtmlDoc:
    """Parse a Matter spec diff HTML document and extract annotated diff sections.

    The HTML is expected to be produced by the Matter spec Asciidoctor build
    (e.g. ``appclusters_diff.html``) and contain ``diff-new``, ``diff-old``,
    and ``diff-chg`` CSS class annotations.

    Returned ``FetchedDocument`` objects have their ``content`` set to the
    annotated diff text and ``metadata`` populated with cluster/section context.
    These are intended to flow through the RAG pipeline:
      search_test_plan_vector_db_node → search_knowledge_graph_node → analyze_with_llm_node

    No LLM calls are made inside this class.
    """

    def __init__(
        self,
        cluster_filter: str = '',
        section_filter: str = '',
        parser: str = 'lxml',
    ) -> None:
        """
        Args:
            cluster_filter: If set, restrict to sections under the named cluster
                            (matched by h3 heading substring, case-insensitive).
            section_filter: If set, restrict to sections whose title contains
                            this substring (case-insensitive).
            parser:         BeautifulSoup parser. 'lxml' (fast) or 'html.parser'
                            (stdlib, no extra dep). Falls back to html.parser if lxml
                            is not installed.
        """
        self._cluster_filter = cluster_filter
        self._section_filter = section_filter
        self._parser = parser

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process(self, doc: "FetchedDocument") -> List["FetchedDocument"]:
        """Parse *doc* (HTML) and return one ``FetchedDocument`` per diff section.

        Returns an empty list when no diff sections are found or when the HTML
        cannot be parsed. Never raises.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error(
                "[ProcessMatterHtmlDoc] beautifulsoup4 not installed. "
                "Run: pip install beautifulsoup4 lxml"
            )
            return []

        if not doc.content.strip():
            logger.warning("[ProcessMatterHtmlDoc] Empty HTML document: %s", doc.path)
            return []

        parser = self._parser
        try:
            soup = BeautifulSoup(doc.content, parser)
        except Exception:
            # Try stdlib fallback
            try:
                soup = BeautifulSoup(doc.content, 'html.parser')
            except Exception as exc:
                logger.error("[ProcessMatterHtmlDoc] Failed to parse %s: %s", doc.path, exc)
                return []

        try:
            sections = _collect_sections(soup, self._cluster_filter, self._section_filter)
        except Exception as exc:
            logger.error("[ProcessMatterHtmlDoc] Section extraction failed for %s: %s",
                         doc.path, exc)
            return []

        if not sections:
            logger.info("[ProcessMatterHtmlDoc] No diff sections found in %s", doc.path)
            return []

        # Import lazily to avoid circular imports
        from src.fetcher.base_fetcher import FetchedDocument as _FD

        result: List[_FD] = []
        for i, sec in enumerate(sections):
            # Build a descriptive path: original_file::cluster::section_title
            cluster_slug = re.sub(r'[^a-z0-9]+', '_', sec['cluster'].lower()).strip('_')
            title_slug   = re.sub(r'[^a-z0-9]+', '_', sec['title'].lower()).strip('_')
            section_path = f"{doc.path}::{cluster_slug}::{title_slug}"

            meta = {
                **doc.metadata,
                "doc_type":       "matter_spec_diff",
                "cluster":        sec['cluster'],
                "section_title":  sec['title'],
                "section_level":  sec['level'],
                "is_new_section": sec['is_new_section'],
                "source_html":    doc.path,
                "section_index":  i,
                "_process_rules": [],   # no further cleaning needed
            }
            result.append(_FD(path=section_path, content=sec['text'], metadata=meta))

        logger.info(
            "[ProcessMatterHtmlDoc] Extracted %d diff sections from %s",
            len(result), doc.path,
        )
        return result

    # ------------------------------------------------------------------
    # Convenience: parse raw HTML string without FetchedDocument
    # ------------------------------------------------------------------

    def parse_html(self, html_content: str, source_path: str = "document.html") -> List[DiffSection]:
        """Parse raw HTML and return ``DiffSection`` dataclass instances.

        Use this when you do not have a ``FetchedDocument`` wrapper, e.g. in tests.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("[ProcessMatterHtmlDoc] beautifulsoup4 not installed.")
            return []

        parser = self._parser
        try:
            soup = BeautifulSoup(html_content, parser)
        except Exception:
            soup = BeautifulSoup(html_content, 'html.parser')

        sections = _collect_sections(soup, self._cluster_filter, self._section_filter)
        return [
            DiffSection(
                cluster=s['cluster'],
                title=s['title'],
                level=s['level'],
                text=s['text'],
                is_new_section=s['is_new_section'],
            )
            for s in sections
        ]
