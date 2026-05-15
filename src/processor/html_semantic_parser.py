"""HTML semantic parser for Matter spec and test plan documents.

Parses AsciiDoc-generated HTML into clean structured JSON, stripping all
presentational noise (style, script, CSS, navigation chrome) and retaining
only semantic content: headings, paragraphs, lists, tables, notes.

Two output modes
----------------
``parse_spec(html, doc_id)``
    Generic section-based parsing for spec documents.  Returns the
    ``GenericDocument`` schema with sections → chunks.

``parse_test_plan(html, doc_id)``
    Test-case-aware parsing.  Detects ``[TC-*]`` headings and returns
    the ``TestPlanDocument`` schema with one object per test case.

Both functions return plain dicts ready for ``json.dumps``.

Usage::

    from src.processor.html_semantic_parser import parse_spec, parse_test_plan

    with open("appclusters.html", encoding="utf-8") as f:
        html = f.read()

    doc = parse_spec(html, doc_id="appclusters")
    tc_doc = parse_test_plan(html, doc_id="allclusters")
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tags whose entire subtree we throw away
_STRIP_TAGS = {"style", "script", "noscript", "svg", "template", "head", "link", "meta"}

# AsciiDoc Asciidoctor boilerplate IDs that are not document content
_SKIP_IDS = {
    "header", "footer", "toc", "toctitle",
    "preamble",                     # AsciiDoc preamble div (often blank)
}

# AsciiDoc boilerplate section *titles* to skip.
# Strips leading numbering (e.g. "1.2. ") before matching so patterns like
# "1.3. Definitions" and "Copyright Notice, License and Disclaimer" are caught.
_SKIP_TITLE_RE = re.compile(
    r"copyright|disclaimer|notice of use|license"
    r"|revision history"
    r"|table of contents|list of tables|list of figures"
    r"|\btoc\b|participants"
    r"|acronyms\s+and\s+abbreviations|abbreviations"
    r"|definitions",
    re.I,
)
# Strip leading section-number prefix before title matching (e.g. "1.2. " → "")
_SECTION_NUM_RE = re.compile(r"^\s*[\d]+(?:\.[\d]+)*\.?\s*")

# TC heading pattern  e.g. "[TC-OO-2.1]" or "[TC-OO-2.1] Attributes …"
_TC_HEADING_RE = re.compile(r"\[TC-[A-Z0-9_]+-[\d.]+\]?", re.I)

# Test-plan sub-section names we know how to map
_TP_SECTION_MAP: Dict[str, str] = {
    "purpose":                     "purpose",
    "pics":                        "pics",
    "pixit":                       "pixit",
    "preconditions":               "preconditions",
    "precondition":                "preconditions",
    "required devices":            "required_devices",
    "required device":             "required_devices",
    "device topology":             "device_topology",
    "test setup":                  "test_setup",
    "setup":                       "test_setup",
    "test procedure":              "test_procedure",
    "procedure":                   "test_procedure",
    "notes":                       "notes",
    "notes/testing considerations": "notes",
    "testing considerations":      "notes",
}

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n{3,}")

# Detect text that is mostly CSS/HTTP noise rather than human-readable content.
# Matches: CSS declarations like `color:red`, URL-encoded Google Fonts strings,
# CSS selectors with braces, and base64 data URIs.
_CSS_NOISE_RE = re.compile(
    r"(?:"
    r"[a-zA-Z-]+:[^;{}\n]{2,80}[;{}]"  # CSS property:value; or closing brace
    r"|%7C[A-Za-z+]"                     # URL-encoded pipe in Google Fonts URLs
    r"|family=[A-Za-z+%:]"               # Google Fonts ?family= query param
    r"|data:[a-z]+/[a-z]+;base64,"       # data URI
    r")"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Collapse whitespace, strip leading/trailing blanks."""
    text = unicodedata.normalize("NFKC", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINE_RE.sub("\n\n", text)
    return text.strip()


def _is_css_noise(text: str) -> bool:
    """Return True when *text* is predominantly CSS or HTTP/font noise.

    Used to discard ``<p>`` or ``<div>`` blocks that slipped through the
    ``_STRIP_TAGS`` decomposition and contain raw CSS declarations, Google
    Fonts URL fragments, or other non-content noise.
    """
    if len(text) < 20:
        return False
    # Count CSS-noise matches relative to text length
    matches = _CSS_NOISE_RE.findall(text)
    if not matches:
        return False
    # If more than 10 % of the text length is covered by noise patterns, skip it.
    noise_chars = sum(len(m) for m in matches)
    return noise_chars / len(text) > 0.10


def _visible_text(tag) -> str:
    """Return visible text of *tag*, collapsing whitespace."""
    return _clean_text(tag.get_text(separator=" "))


def _heading_level(tag_name: str) -> int:
    """h1 → 1, h6 → 6."""
    return int(tag_name[1])


def _strip_noise(soup) -> None:
    """Remove all noise subtrees from *soup* in-place."""
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    # CHANGE: also strip <link rel="stylesheet"> tags that appear in <body>
    # (AsciiDoc sometimes inlines stylesheet links outside <head>).
    # Belt-and-suspenders: <head> is already in _STRIP_TAGS, but <link> may
    # appear in body fragments parsed without a full document wrapper.
    for tag in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        tag.decompose()
    # Remove AsciiDoc anchor-only <a> tags (presentational, no text)
    for a in soup.find_all("a", class_="anchor"):
        a.decompose()
    # Remove page chrome divs by id
    for div_id in _SKIP_IDS:
        el = soup.find(id=div_id)
        if el:
            el.decompose()


def _parse_table(table_tag) -> Dict[str, Any]:
    """Extract a table into headers + rows (list of lists of strings).

    Also produces a ``text`` representation: one sentence per row.
    """
    headers: List[str] = []
    rows: List[List[str]] = []

    thead = table_tag.find("thead")
    if thead:
        for th in thead.find_all(["th", "td"]):
            headers.append(_visible_text(th))

    tbody = table_tag.find("tbody")
    body_rows = (tbody or table_tag).find_all("tr")
    for tr in body_rows:
        cells = [_visible_text(td) for td in tr.find_all(["td", "th"])]
        if any(c for c in cells):
            rows.append(cells)

    # Build readable text form
    lines: List[str] = []
    if headers:
        lines.append(" | ".join(headers))
        lines.append("-" * min(60, sum(len(h) + 3 for h in headers)))
    for row in rows:
        lines.append(" | ".join(row))

    return {
        "headers": headers,
        "rows": rows,
        "text": "\n".join(lines),
    }


def _is_note_block(tag) -> bool:
    classes = tag.get("class", [])
    return any(c in ("admonitionblock", "note", "important", "warning", "tip", "caution")
               for c in classes)


def _iter_content_blocks(container) -> List[Dict[str, Any]]:
    """Walk *container* and yield chunk dicts for all semantic content blocks.

    Skips nested headings (handled by the section splitter).
    """
    chunks: List[Dict[str, Any]] = []
    visited_ids: set = set()

    for tag in container.find_all(
        ["p", "ul", "ol", "dl", "table", "blockquote", "div"],
        recursive=True,
    ):
        # Guard: skip if tag was decomposed by a parent iteration
        if tag.parent is None:
            continue
        tag_id = id(tag)
        if tag_id in visited_ids:
            continue

        # Skip headings contained inside — they are structural, not content
        if tag.parent and tag.parent.name in ("h1","h2","h3","h4","h5","h6"):
            continue

        tag_name = tag.name

        if tag_name == "table":
            tdata = _parse_table(tag)
            if tdata["rows"]:
                chunks.append({
                    "block_type": "table",
                    "text": tdata["text"],
                    "table": {"headers": tdata["headers"], "rows": tdata["rows"]},
                })
            # Mark all descendants visited to prevent double-counting
            for desc in tag.find_all(True):
                visited_ids.add(id(desc))
            visited_ids.add(tag_id)
            continue

        if tag_name in ("ul", "ol"):
            items = []
            for li in tag.find_all("li", recursive=False):
                txt = _visible_text(li)
                if txt:
                    items.append(txt)
            if items:
                for item in items:
                    chunks.append({"block_type": "list_item", "text": item})
            for desc in tag.find_all(True):
                visited_ids.add(id(desc))
            visited_ids.add(tag_id)
            continue

        if tag_name == "dl":
            # Definition list: term + description
            terms = tag.find_all("dt")
            descs = tag.find_all("dd")
            for dt, dd in zip(terms, descs):
                txt = f"{_visible_text(dt)}: {_visible_text(dd)}"
                if txt.strip(": "):
                    chunks.append({"block_type": "list_item", "text": txt})
            for desc in tag.find_all(True):
                visited_ids.add(id(desc))
            visited_ids.add(tag_id)
            continue

        if tag_name == "blockquote" or (tag_name == "div" and _is_note_block(tag)):
            txt = _visible_text(tag)
            if txt and not _is_css_noise(txt):
                chunks.append({"block_type": "note", "text": txt})
            for desc in tag.find_all(True):
                visited_ids.add(id(desc))
            visited_ids.add(tag_id)
            continue

        if tag_name == "p":
            txt = _visible_text(tag)
            if txt and not _is_css_noise(txt):
                chunks.append({"block_type": "paragraph", "text": txt})
            visited_ids.add(tag_id)
            continue

    return chunks


# ---------------------------------------------------------------------------
# Section tree builder
# ---------------------------------------------------------------------------

@dataclass
class _Section:
    section_id: str
    title: str
    level: int
    path: List[str] = field(default_factory=list)
    raw_tag: Any = field(default=None, repr=False)   # the heading tag
    container: Any = field(default=None, repr=False)  # parent sectN div or None


def _collect_sections(soup) -> List[_Section]:
    """Return a flat list of sections in document order.

    AsciiDoc Asciidoctor wraps each section in ``<div class="sectN">``; we use
    that when present. Falls back to heading-tag scanning when not.
    """
    sections: List[_Section] = []

    # Try AsciiDoc sectN divs first (most structured)
    sect_divs = soup.find_all(
        "div",
        class_=re.compile(r"^sect[1-6]$"),
    )

    if sect_divs:
        for div in sect_divs:
            heading = div.find(re.compile(r"^h[1-6]$"), recursive=False)
            if heading is None:
                # heading may be the first element inside a nested div
                heading = div.find(re.compile(r"^h[1-6]$"))
            if heading is None:
                continue
            title = _visible_text(heading)
            if not title or _SKIP_TITLE_RE.search(_SECTION_NUM_RE.sub("", title)):
                continue
            sect_id = heading.get("id") or div.get("id") or ""
            level = _heading_level(heading.name)
            sections.append(_Section(
                section_id=sect_id,
                title=title,
                level=level,
                raw_tag=heading,
                container=div,
            ))
    else:
        # Fallback: scan heading tags directly
        content_root = soup.find(id="content") or soup.body or soup
        for heading in content_root.find_all(re.compile(r"^h[1-6]$")):
            title = _visible_text(heading)
            if not title or _SKIP_TITLE_RE.search(_SECTION_NUM_RE.sub("", title)):
                continue
            sect_id = heading.get("id") or ""
            level = _heading_level(heading.name)
            sections.append(_Section(
                section_id=sect_id,
                title=title,
                level=level,
                raw_tag=heading,
                container=None,
            ))

    # Build section paths
    path_stack: List[str] = []
    level_stack: List[int] = []
    for sec in sections:
        while level_stack and level_stack[-1] >= sec.level:
            level_stack.pop()
            if path_stack:
                path_stack.pop()
        path_stack.append(sec.title)
        level_stack.append(sec.level)
        sec.path = list(path_stack)

    return sections


def _chunks_for_section(section: _Section, chunk_id_base: str) -> List[Dict]:
    """Return content chunks for *section* (using its container div or siblings)."""
    from bs4 import BeautifulSoup as _BS, Tag as _Tag
    import copy

    if section.container is not None:
        container = copy.copy(section.container)
        # Remove nested sect divs — their content is handled separately
        for child_sect in container.find_all("div", class_=re.compile(r"^sect[1-6]$")):
            child_sect.decompose()
        # Remove the section's own heading
        h = container.find(re.compile(r"^h[1-6]$"), recursive=False)
        if h:
            h.decompose()
    else:
        # No container: gather siblings between this heading and the next same-or-higher heading
        container = _BS("<div></div>", "html.parser").div
        node = section.raw_tag.next_sibling
        while node is not None:
            if isinstance(node, _Tag) and re.match(r"^h[1-6]$", node.name or ""):
                if _heading_level(node.name) <= section.level:
                    break
            next_node = node.next_sibling
            container.append(copy.copy(node))
            node = next_node

    raw_chunks = _iter_content_blocks(container)
    result = []
    for i, c in enumerate(raw_chunks):
        c["chunk_id"] = f"{chunk_id_base}_c{i}"
        result.append(c)
    return result


# ===========================================================================
# Public API — spec parsing
# ===========================================================================

def parse_spec(html: str, doc_id: str = "document") -> Dict[str, Any]:
    """Parse a Matter spec HTML document into section-based structured JSON.

    Parameters
    ----------
    html:
        Raw HTML string (full document or fragment).
    doc_id:
        Identifier embedded in ``doc_id`` and used as a prefix for
        ``section_id`` / ``chunk_id`` values.

    Returns
    -------
    dict matching the ``GenericDocument`` schema::

        {
          "doc_id": str,
          "sections": [
            {
              "section_id": str,
              "section_path": [str, ...],
              "title": str,
              "chunks": [
                {
                  "chunk_id": str,
                  "block_type": "paragraph|list_item|table|note",
                  "text": str,
                  "table": {"headers": [...], "rows": [...]}  # table only
                }
              ]
            }
          ]
        }
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    _strip_noise(soup)

    sections_meta = _collect_sections(soup)
    out_sections: List[Dict] = []

    for idx, sec in enumerate(sections_meta):
        sec_id = sec.section_id or f"{doc_id}_s{idx}"
        chunks = _chunks_for_section(sec, chunk_id_base=sec_id)
        if not chunks:
            continue  # skip empty sections
        # full_text concatenates all chunk texts — used for embedding the section
        # as a single dense string without requiring the caller to join chunks.
        full_text = "\n\n".join(c["text"] for c in chunks if c.get("text"))
        out_sections.append({
            "section_id": sec_id,
            "section_path": sec.path,
            "title": sec.title,
            "full_text": full_text,
            "chunks": chunks,
        })

    return {"doc_id": doc_id, "sections": out_sections}


# ===========================================================================
# Public API — test plan parsing
# ===========================================================================

def _extract_tp_subsections(tc_container) -> Dict[str, Any]:
    """Parse the subsections inside a test-case container div.

    Returns a dict with keys: purpose, pics, pixit, preconditions,
    required_devices, device_topology, test_setup, test_procedure, notes.
    """
    result: Dict[str, Any] = {
        "purpose": "",
        "pics": [],
        "pixit": [],
        "preconditions": [],
        "required_devices": [],
        "device_topology": "",
        "test_setup": "",
        "test_procedure": [],
        "notes": [],
    }

    # Walk direct sectN divs at any nesting level (TC subsections can be sect3-sect6
    # depending on where the TC heading lives in the document hierarchy).
    # We search recursively but only one level at a time by using recursive=True and
    # then skipping divs that are grandchildren of another sectN (already owned by a
    # sibling sub_div).  Simple approach: find_all recursive is fine because each
    # sub_div is processed independently and headings are consumed by decompose().
    sub_divs = tc_container.find_all(
        "div",
        class_=re.compile(r"^sect[1-6]$"),
        recursive=True,
    )

    for sub in sub_divs:
        heading = sub.find(re.compile(r"^h[1-6]$"), recursive=False)
        if heading is None:
            continue
        heading_text = _visible_text(heading).lower()
        field_name = _TP_SECTION_MAP.get(heading_text)
        if field_name is None:
            continue

        heading.decompose()  # remove heading from content before extracting

        if field_name == "test_procedure":
            # Prefer table extraction
            tbl = sub.find("table")
            if tbl:
                tdata = _parse_table(tbl)
                # Each table row → one step dict
                steps: List[Dict[str, str]] = []
                for row in tdata["rows"]:
                    if len(row) >= 4:
                        steps.append({
                            "step": row[0],
                            "ref":  row[1] if len(row) > 1 else "",
                            "pics": row[2] if len(row) > 2 else "",
                            "action": row[3] if len(row) > 3 else "",
                            "expected": row[4] if len(row) > 4 else "",
                        })
                    elif len(row) >= 2:
                        steps.append({"step": row[0], "action": " | ".join(row[1:])})
                result["test_procedure"] = steps
            else:
                # Fall back to ordered list
                items = [_visible_text(li) for li in sub.find_all("li")]
                result["test_procedure"] = items or [_visible_text(sub)]

        elif field_name == "pics":
            # List or simple text
            items = [_visible_text(li) for li in sub.find_all("li")]
            if not items:
                txt = _visible_text(sub)
                items = [t.strip() for t in txt.split() if t.strip()]
            result["pics"] = [i for i in items if i]

        elif field_name == "pixit":
            items = [_visible_text(li) for li in sub.find_all("li")]
            if not items:
                txt = _visible_text(sub)
                items = [t.strip() for t in txt.splitlines() if t.strip()]
            result["pixit"] = [i for i in items if i]

        elif field_name == "preconditions":
            items = [_visible_text(li) for li in sub.find_all("li")]
            if not items:
                txt = _visible_text(sub)
                items = [t.strip() for t in txt.splitlines() if t.strip() and t.strip() != "."]
            result["preconditions"] = [i for i in items if i]

        elif field_name == "required_devices":
            tbl = sub.find("table")
            if tbl:
                tdata = _parse_table(tbl)
                devs: List[Dict[str, str]] = []
                for row in tdata["rows"]:
                    if len(row) >= 2:
                        devs.append({"name": row[1] if len(row) > 1 else row[0],
                                     "description": row[2] if len(row) > 2 else ""})
                result["required_devices"] = devs
            else:
                result["required_devices"] = [_visible_text(sub)]

        elif field_name == "notes":
            items = [_visible_text(li) for li in sub.find_all("li")]
            if not items:
                txt = _visible_text(sub)
                items = [t.strip() for t in txt.splitlines() if t.strip()]
            result["notes"] = [i for i in items if i]

        else:
            # Single-text fields: purpose, device_topology, test_setup
            result[field_name] = _visible_text(sub)

    return result


def parse_test_plan(html: str, doc_id: str = "test_plan") -> Dict[str, Any]:
    """Parse a Matter test plan HTML document into one JSON object per test case.

    Detects test case boundaries from headings matching ``[TC-*]``.

    Parameters
    ----------
    html:
        Raw HTML string (full document or fragment).
    doc_id:
        Identifier embedded in output ``doc_id``.

    Returns
    -------
    dict matching the ``TestPlanDocument`` schema::

        {
          "doc_id": str,
          "test_cases": [
            {
              "test_case_id": "TC-OO-2.1",
              "title": "[TC-OO-2.1] Attributes with server as DUT",
              "section_path": ["On/Off Cluster Test Plan", "Test Cases", ...],
              "purpose": str,
              "pics": [str, ...],
              "pixit": [str, ...],
              "preconditions": [str, ...],
              "required_devices": [...],
              "device_topology": str,
              "test_setup": str,
              "test_procedure": [{"step": str, "pics": str, "action": str, "expected": str}],
              "notes": [str, ...]
            }
          ]
        }
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    _strip_noise(soup)

    sections = _collect_sections(soup)
    test_cases: List[Dict[str, Any]] = []

    for sec in sections:
        if not _TC_HEADING_RE.search(sec.title):
            continue

        # Extract TC id from title
        m = re.search(r"\[?(TC-[A-Z0-9_]+-[\d.]+)\]?", sec.title, re.I)
        tc_id = m.group(1).upper() if m else sec.title

        subsections = _extract_tp_subsections(sec.container) if sec.container else {}

        # all_text: flat string of all subsection text joined together.
        # Used for embedding the full test case as a single dense unit.
        all_text_parts: List[str] = [sec.title]
        for v in subsections.values():
            if isinstance(v, str) and v:
                all_text_parts.append(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item:
                        all_text_parts.append(item)
                    elif isinstance(item, dict):
                        all_text_parts.append(" ".join(str(x) for x in item.values() if x))
        all_text = "\n\n".join(all_text_parts)

        test_cases.append({
            "test_case_id": tc_id,
            "title": sec.title,
            "section_path": sec.path[:-1],  # path without the TC title itself
            "all_text": all_text,
            **subsections,
        })

    return {"doc_id": doc_id, "test_cases": test_cases}


# ===========================================================================
# Convenience: auto-detect mode and parse a file path
# ===========================================================================

def parse_file(
    path: str,
    doc_id: Optional[str] = None,
    mode: str = "auto",
) -> Dict[str, Any]:
    """Parse an HTML file and return structured JSON.

    Parameters
    ----------
    path:
        Path to the HTML file.
    doc_id:
        Document identifier (defaults to file stem).
    mode:
        ``"spec"`` | ``"test_plan"`` | ``"auto"`` (default).
        In auto mode, ``parse_test_plan`` is used when the document contains
        at least one ``[TC-*]`` heading; otherwise ``parse_spec`` is used.
    """
    p = Path(path).expanduser().resolve()
    if doc_id is None:
        doc_id = p.stem
    try:
        html = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        html = p.read_text(encoding="latin-1")

    if mode == "test_plan":
        return parse_test_plan(html, doc_id=doc_id)
    if mode == "spec":
        return parse_spec(html, doc_id=doc_id)

    # auto: presence of [TC-*] headings → test plan mode
    if _TC_HEADING_RE.search(html):
        return parse_test_plan(html, doc_id=doc_id)
    return parse_spec(html, doc_id=doc_id)


# ===========================================================================
# Public helper functions
# ===========================================================================

def clean_html(html: str) -> str:
    """Strip all noise from an HTML string and return clean visible text.

    Removes ``<style>``, ``<script>``, navigation chrome, CSS fragments, and
    AsciiDoc boilerplate.  Returns a plain-text string with collapsed whitespace.

    Example::

        text = clean_html(raw_html)
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    _strip_noise(soup)
    return _clean_text(soup.get_text(separator=" "))


def parse_sections(html: str, doc_id: str = "document") -> List[Dict[str, Any]]:
    """Parse HTML into a flat list of section dicts (shortcut for ``parse_spec``).

    Each dict has keys: ``section_id``, ``section_path``, ``title``,
    ``full_text``, ``chunks``.

    Example::

        sections = parse_sections(html, doc_id="appclusters")
        for sec in sections:
            embed(sec["full_text"])
    """
    return parse_spec(html, doc_id=doc_id)["sections"]


def parse_table(table_html: str) -> Dict[str, Any]:
    """Parse a ``<table>`` HTML fragment and return ``{headers, rows, text}``.

    ``table_html`` may be a full document or a bare ``<table>…</table>``
    fragment — the function locates the first ``<table>`` element.

    Example::

        result = parse_table("<table><thead>…</thead><tbody>…</tbody></table>")
        print(result["headers"])  # ["Step", "Action", "Expected"]
        print(result["rows"])     # [["1", "Do X", "Y happens"], ...]
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(table_html, "lxml")
    tbl = soup.find("table")
    if tbl is None:
        return {"headers": [], "rows": [], "text": ""}
    return _parse_table(tbl)


def parse_test_cases(html: str, doc_id: str = "test_plan") -> List[Dict[str, Any]]:
    """Parse HTML and return a list of test case dicts (shortcut for ``parse_test_plan``).

    Each dict has keys: ``test_case_id``, ``title``, ``section_path``,
    ``all_text``, plus subsection keys (``purpose``, ``pics``, etc.).

    Returns an empty list when no ``[TC-*]`` headings are found.

    Example::

        tcs = parse_test_cases(html, doc_id="allclusters")
        for tc in tcs:
            print(tc["test_case_id"], len(tc["test_procedure"]))
    """
    return parse_test_plan(html, doc_id=doc_id)["test_cases"]


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "data/test_plans/allclusters.html"
    mode = sys.argv[2] if len(sys.argv) > 2 else "auto"

    result = parse_file(path, mode=mode)

    if "test_cases" in result:
        tcs = result["test_cases"]
        print(f"Parsed {len(tcs)} test cases from {path}")
        if tcs:
            print("\nFirst test case:")
            print(json.dumps(tcs[0], indent=2))
    else:
        secs = result.get("sections", [])
        total_chunks = sum(len(s["chunks"]) for s in secs)
        print(f"Parsed {len(secs)} sections, {total_chunks} chunks from {path}")
        if secs:
            print("\nFirst section:")
            print(json.dumps(secs[0], indent=2))
