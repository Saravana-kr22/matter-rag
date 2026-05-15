"""Matter TC-aware AsciiDoc chunker.

Produces two tiers of Document objects for every test case:

Primary chunk  (chunk_type="primary")
    - page_content : reconstructed clean text suitable for embedding
    - metadata["tc_record"] : dict matching the TCRecord schema (all structured fields)
    - metadata["tc_id"], ["cluster_name"], ["pics_codes"] : kept for backward compat

Secondary chunks  (chunk_type = subfield name)
    One Document per non-empty subfield:
        "purpose", "pics", "preconditions", "required_devices",
        "device_topology", "test_setup", "test_steps", "notes"
    - page_content : raw AsciiDoc text of that section (for targeted retrieval)
    - metadata["section_type"] kept for backward compat

Non-TC content falls back to GenericChunker (chunk_type="preamble").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from src.chunker.base_chunker import BaseChunker, GenericChunker


# ---------------------------------------------------------------------------
# Module-level patterns
# ---------------------------------------------------------------------------

_TC_HEADING_RE = re.compile(
    r"^(={1,6})\s+(TC-[A-Z0-9]+-\d+\.\d+.*?)$",
    re.MULTILINE,
)

_PICS_INLINE_RE = re.compile(r"\[PICS\.([A-Za-z0-9_.]+)\]")

_STEP_RE = re.compile(r"^\s*(?:\d+\.|Step\s+\d+:)", re.MULTILINE)

_SUB_HEADING_RE = re.compile(r"^(={2,6})\s+(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Section-name → canonical key mapping
# ---------------------------------------------------------------------------

_SECTION_ALIASES: Dict[str, str] = {
    "purpose":              "purpose",
    "description":          "purpose",
    "pics":                 "pics",
    "preconditions":        "preconditions",
    "precondition":         "preconditions",
    "test environment":     "preconditions",
    "test preconditions":   "preconditions",
    "required devices":     "required_devices",
    "required device":      "required_devices",
    "devices":              "required_devices",
    "device topology":      "device_topology",
    "topology":             "device_topology",
    "setup":                "test_setup",
    "test setup":           "test_setup",
    "test procedure":       "test_steps",
    "test procedure steps": "test_steps",
    "test steps":           "test_steps",
    "test step":            "test_steps",
    "procedure":            "test_steps",
    "steps":                "test_steps",
    "expected results":     "expected_results",
    "expected result":      "expected_results",
    "notes":                "notes",
    "note":                 "notes",
}

# Map from canonical key to legacy section_type value (backward compat)
_CHUNK_TYPE_TO_SECTION_TYPE: Dict[str, str] = {
    "primary":          "primary",
    "purpose":          "purpose",
    "pics":             "pics",
    "preconditions":    "env",
    "required_devices": "env",
    "device_topology":  "env",
    "test_setup":       "env",
    "test_steps":       "steps",
    "expected_results": "expected",
    "notes":            "other",
    "preamble":         "preamble",
}


def _canonicalize_section(heading: str) -> str:
    return _SECTION_ALIASES.get(heading.strip().lower(), "other")


# ---------------------------------------------------------------------------
# TCRecord — structured representation of one test case
# ---------------------------------------------------------------------------

@dataclass
class TCRecord:
    """Structured representation of one Matter test case.

    Produced by :func:`_parse_tc_record` and stored in
    ``Document.metadata["tc_record"]`` of every primary chunk.

    JSON schema example::

        {
          "test_case_id": "TC-PAVST-2.2",
          "title": "Verify reading CurrentConnections attribute ...",
          "source_file": "push_av_stream_transport.adoc",
          "section_group": "Test Cases",
          "category": null,
          "purpose": "This test case verifies ...",
          "pics": ["PAVST.S", "AVSM.S"],
          "preconditions": ["DUT has been commissioned to TH"],
          "required_devices": [
              {"name": "TH", "description": "Test Harness Controller"},
              {"name": "DUT", "description": "PushAVStreamTransport-enabled device"}
          ],
          "device_topology": "TH and DUT are on the same fabric.",
          "test_setup": "{comDutTH}.",
          "test_steps": [
              {
                "step_no": 1,
                "text": "TH Reads CurrentConnections attribute ...",
                "expected": "Verify the number of PushAV Connections ...",
                "pics": []
              }
          ],
          "notes": [],
          "entities": [],
          "test_intents": []
        }
    """

    test_case_id: str
    title: str
    source_file: str = ""
    section_group: str = "Test Cases"
    category: Optional[str] = None
    purpose: str = ""
    pics: List[str] = field(default_factory=list)
    preconditions: List[str] = field(default_factory=list)
    required_devices: List[Dict[str, str]] = field(default_factory=list)
    device_topology: str = ""
    test_setup: str = ""
    test_steps: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)   # filled by KG pipeline
    test_intents: List[str] = field(default_factory=list)  # filled by KG pipeline

    def to_dict(self) -> dict:
        return {
            "test_case_id":   self.test_case_id,
            "title":          self.title,
            "source_file":    self.source_file,
            "section_group":  self.section_group,
            "category":       self.category,
            "purpose":        self.purpose,
            "pics":           self.pics,
            "preconditions":  self.preconditions,
            "required_devices": self.required_devices,
            "device_topology": self.device_topology,
            "test_setup":     self.test_setup,
            "test_steps":     self.test_steps,
            "notes":          self.notes,
            "entities":       self.entities,
            "test_intents":   self.test_intents,
        }


# ---------------------------------------------------------------------------
# IgnoreRule (unchanged from previous version)
# ---------------------------------------------------------------------------

@dataclass
class IgnoreRule:
    """A single rule describing text to drop before chunking.

    Attributes:
        pattern:        Text string or regex pattern to match against.
        match:          ``"contains"`` (default) | ``"startswith"`` |
                        ``"exact"`` | ``"regex"``
        scope:          ``"line"`` (default) | ``"paragraph"`` | ``"block"``
        case_sensitive: If ``False`` (default), matching ignores case.
    """

    pattern: str
    match: str = "contains"
    scope: str = "line"
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        valid_match = {"contains", "startswith", "exact", "regex"}
        valid_scope = {"line", "paragraph", "block"}
        if self.match not in valid_match:
            raise ValueError(f"IgnoreRule.match must be one of {valid_match}, got {self.match!r}")
        if self.scope not in valid_scope:
            raise ValueError(f"IgnoreRule.scope must be one of {valid_scope}, got {self.scope!r}")

    @classmethod
    def from_dict(cls, d: dict) -> "IgnoreRule":
        return cls(
            pattern=d["pattern"],
            match=d.get("match", "contains"),
            scope=d.get("scope", "line"),
            case_sensitive=d.get("case_sensitive", False),
        )


# ---------------------------------------------------------------------------
# Ignore-rule engine (unchanged)
# ---------------------------------------------------------------------------

def _unit_matches(unit: str, rule: IgnoreRule) -> bool:
    text = unit if rule.case_sensitive else unit.lower()
    pat = rule.pattern if rule.case_sensitive else rule.pattern.lower()

    if rule.match == "exact":
        return text.strip() == pat.strip()
    if rule.match == "startswith":
        return text.lstrip().startswith(pat)
    if rule.match == "contains":
        return pat in text
    if rule.match == "regex":
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        return bool(re.search(rule.pattern, unit, flags))
    return False


def _apply_rule(text: str, rule: IgnoreRule) -> str:
    if rule.scope == "line":
        lines = text.split("\n")
        lines = [ln for ln in lines if not _unit_matches(ln, rule)]
        return "\n".join(lines)

    if rule.scope == "paragraph":
        paragraphs = re.split(r"\n{2,}", text)
        paragraphs = [p for p in paragraphs if not _unit_matches(p, rule)]
        return "\n\n".join(paragraphs)

    if rule.scope == "block":
        blocks = re.split(r"(\n{2,})", text)
        result = []
        i = 0
        while i < len(blocks):
            chunk = blocks[i]
            if _unit_matches(chunk, rule):
                i += 1
                if i < len(blocks) and re.fullmatch(r"\n+", blocks[i]):
                    i += 1
                continue
            result.append(chunk)
            i += 1
        return "".join(result)

    return text


def apply_ignore_rules(text: str, rules: List[IgnoreRule]) -> str:
    """Apply all *rules* to *text* in order and return cleaned text."""
    for rule in rules:
        text = _apply_rule(text, rule)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ---------------------------------------------------------------------------
# TC-heading helpers (unchanged)
# ---------------------------------------------------------------------------

def _extract_tc_id(heading: str) -> str:
    m = re.match(r"(TC-[A-Z0-9]+-\d+\.\d+)", heading.strip())
    return m.group(1) if m else heading.strip()


def _extract_cluster_name(tc_id: str) -> str:
    parts = tc_id.split("-")
    return parts[1] if len(parts) >= 3 else ""


def _parse_tc_heading(heading: str) -> tuple:
    """Return (tc_id, descriptive_title) from a raw TC heading string."""
    m = re.match(r"(TC-[A-Z0-9]+-\d+\.\d+)\s*(.*)", heading.strip())
    if not m:
        return heading.strip(), ""
    tc_id = m.group(1)
    title = m.group(2).strip()
    return tc_id, title


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_bullet_list(text: str) -> List[str]:
    """Extract items from a bullet or numbered list."""
    items: List[str] = []
    current: List[str] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if re.match(r"^(\*{1,3}|-{1,2}|•|\d+\.)\s+", stripped):
            if current:
                items.append(" ".join(current))
            current = [re.sub(r"^(\*{1,3}|-{1,2}|•|\d+\.)\s+", "", stripped)]
        elif stripped and current:
            current.append(stripped)
        elif stripped and not current:
            # Non-list prose — treat as a single item
            items.append(stripped)
    if current:
        items.append(" ".join(current))
    return [i.strip() for i in items if i.strip()]


def _parse_adoc_table_cells(content: str) -> tuple:
    """Parse AsciiDoc table content (between |=== markers) into (headers, rows).

    Handles two cell layouts:
    - Compact (all on one line): ``| a | b | c``
    - Expanded (one cell per line): ``| a\\n| b\\n| c``
    - Multi-line cells: continued by non-``|``-prefixed lines

    The first blank-line-separated group is the header row.
    """
    # Strip [cols=...] and similar attribute lines
    content = re.sub(r"^\[.*?\]\s*\n", "", content, flags=re.MULTILINE)
    content = content.strip()

    def parse_cell_group(group: str) -> List[str]:
        cells: List[str] = []
        current: Optional[str] = None

        for line in group.strip().splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("|"):
                # Split on "|" — first element is empty (before leading |)
                parts = [p.strip() for p in stripped.split("|")]
                inline_cells = [p for p in parts[1:]]  # keep empty strings between ||

                if len(inline_cells) > 1:
                    # Compact form: "| a | b | c" → multiple cells on one line
                    if current is not None:
                        cells.append(current.strip())
                        current = None
                    cells.extend(c for c in inline_cells if c)  # skip empty from trailing |
                else:
                    # Expanded form: "| cell_text" — single cell starting on this line
                    if current is not None:
                        cells.append(current.strip())
                    current = inline_cells[0] if inline_cells else ""
            elif current is not None and stripped:
                # Continuation of the current multi-line cell
                current = (current + " " + stripped).strip()

        if current is not None:
            cells.append(current.strip())
        return cells

    row_groups = [g for g in re.split(r"\n\s*\n", content) if g.strip()]
    if not row_groups:
        return [], []

    headers = parse_cell_group(row_groups[0])
    rows = [parse_cell_group(g) for g in row_groups[1:]]
    rows = [r for r in rows if r]
    return headers, rows


def _classify_table_header(h: str) -> str:
    h = h.lower().strip()
    if re.search(r"\bstep\b|\bno\b|^#$", h):
        return "step"
    if re.search(r"\baction\b|\bdirection\b|\bprocedure\b", h):
        return "text"
    if re.search(r"\bexpect\b|\bverif\b|\bresult\b|\boutcome\b", h):
        return "expected"
    if "pics" in h:
        return "pics"
    # Default: first unclassified col is text
    return "text"


def _parse_required_devices(text: str) -> List[Dict[str, str]]:
    """Parse a required-devices section into a list of {name, description} dicts."""
    devices: List[Dict[str, str]] = []

    # Try AsciiDoc table first
    table_m = re.search(r"\|===\s*(.*?)\s*\|===", text, re.DOTALL)
    if table_m:
        headers, rows = _parse_adoc_table_cells(table_m.group(1))
        h_lower = [h.lower() for h in headers]
        name_col = next((i for i, h in enumerate(h_lower) if "name" in h or "device" in h), 0)
        desc_col = next((i for i, h in enumerate(h_lower) if "desc" in h or "role" in h), 1)
        for row in rows:
            name = row[name_col].strip() if name_col < len(row) else ""
            desc = row[desc_col].strip() if desc_col < len(row) else ""
            if name:
                devices.append({"name": name, "description": desc})
        if devices:
            return devices

    # Fallback: bullet list  "* TH (Test Harness Controller)"
    for line in text.strip().splitlines():
        stripped = re.sub(r"^[-*•]\s+", "", line.strip())
        if not stripped:
            continue
        # "NAME (description)"
        m = re.match(r"^(\w+)\s*\(([^)]+)\)", stripped)
        if m:
            devices.append({"name": m.group(1), "description": m.group(2)})
            continue
        # "NAME - description" or "NAME: description"
        m2 = re.match(r"^(\w+)\s*[-:]\s+(.+)$", stripped)
        if m2:
            devices.append({"name": m2.group(1), "description": m2.group(2).strip()})
            continue
        # Plain token
        if re.match(r"^\w+$", stripped):
            devices.append({"name": stripped, "description": ""})

    return devices


def _parse_test_steps_table(text: str) -> List[Dict]:
    """Parse an AsciiDoc table into a list of test-step dicts."""
    table_m = re.search(r"\|===\s*(.*?)\s*\|===", text, re.DOTALL)
    if not table_m:
        return []

    headers, rows = _parse_adoc_table_cells(table_m.group(1))
    if not headers or not rows:
        return []

    header_roles = [_classify_table_header(h) for h in headers]
    # If we couldn't identify any 'text' column, assign sensibly
    if "text" not in header_roles and len(header_roles) >= 2:
        # Assume: col 0 = step, col 1 = text, col 2 = expected
        header_roles = (["step"] + ["text"] + ["expected"] * max(0, len(header_roles) - 2))[
            : len(header_roles)
        ]

    steps: List[Dict] = []
    for row in rows:
        step: Dict = {"step_no": len(steps) + 1, "text": "", "expected": "", "pics": []}
        for i, cell in enumerate(row):
            if i >= len(header_roles):
                break
            role = header_roles[i]
            if role == "step":
                try:
                    step["step_no"] = int(re.sub(r"\D", "", cell) or str(len(steps) + 1))
                except ValueError:
                    pass
            elif role == "text":
                step["text"] = (step["text"] + " " + cell).strip()
            elif role == "expected":
                step["expected"] = (step["expected"] + " " + cell).strip()
            elif role == "pics":
                step["pics"] = _PICS_INLINE_RE.findall(cell)
        if step["text"] or step["expected"]:
            steps.append(step)

    return steps


def _parse_test_steps_list(text: str) -> List[Dict]:
    """Parse a numbered / AsciiDoc ordered-list section into test-step dicts."""
    steps: List[Dict] = []
    current: Optional[Dict] = None

    for line in text.strip().splitlines():
        raw = line
        stripped = raw.strip()

        # Numbered item: "1. text" or "Step 1: text"
        m_num = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        m_step = re.match(r"^[Ss]tep\s+(\d+)[:.]?\s+(.+)$", stripped)
        # AsciiDoc ordered item: ". text" (but not ".." for nested)
        m_adoc = re.match(r"^\.(?!\.)\.?\s+(.+)$", stripped)

        if m_num:
            if current:
                steps.append(current)
            current = {"step_no": int(m_num.group(1)), "text": m_num.group(2), "expected": "", "pics": []}
        elif m_step:
            if current:
                steps.append(current)
            current = {"step_no": int(m_step.group(1)), "text": m_step.group(2), "expected": "", "pics": []}
        elif m_adoc:
            if current:
                steps.append(current)
            current = {"step_no": len(steps) + 1, "text": m_adoc.group(1), "expected": "", "pics": []}
        elif current and stripped:
            # Inline expected/verify annotation
            m_exp = re.match(r"^[*\-•]?\s*[Ee]xpected:?\s+(.+)$", stripped)
            m_ver = re.match(r"^[*\-•]?\s*[Vv]erif(?:y|ication):?\s+(.+)$", stripped)
            if m_exp:
                current["expected"] = (current["expected"] + " " + m_exp.group(1)).strip()
            elif m_ver:
                current["expected"] = (current["expected"] + " " + m_ver.group(1)).strip()
            elif stripped in ("+", "--", ""):
                pass  # AsciiDoc list continuation markers
            else:
                current["text"] = (current["text"] + " " + stripped).strip()

    if current:
        steps.append(current)

    return steps


def _parse_test_steps(text: str) -> List[Dict]:
    """Try table format first; fall back to list format."""
    if "|===" in text:
        steps = _parse_test_steps_table(text)
        if steps:
            return steps
    return _parse_test_steps_list(text)


def _parse_notes(text: str) -> List[str]:
    """Extract NOTE / TIP / WARNING lines and other short note items."""
    notes: List[str] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip AsciiDoc NOTE: / TIP: / WARNING: prefixes
        cleaned = re.sub(r"^(?:NOTE|TIP|WARNING|IMPORTANT|CAUTION):\s*", "", stripped, flags=re.IGNORECASE)
        # Strip bullet markers
        cleaned = re.sub(r"^[-*•]\s+", "", cleaned).strip()
        if cleaned:
            notes.append(cleaned)
    return notes


# ---------------------------------------------------------------------------
# TC body parser — splits body into canonical section dict
# ---------------------------------------------------------------------------

def _parse_tc_sections(body: str) -> Dict[str, str]:
    """Split a TC body (everything after the `== TC-*` heading) into a dict.

    Returns ``{canonical_section_name: raw_text}``.
    Sections that don't map to a canonical name are stored as ``"other:<heading>"``.
    """
    result: Dict[str, str] = {}
    sub_matches = list(_SUB_HEADING_RE.finditer(body))

    if not sub_matches:
        result["purpose"] = body.strip()
        return result

    # Text before the first sub-heading
    preamble = body[: sub_matches[0].start()].strip()
    if preamble:
        result.setdefault("purpose", preamble)

    for i, m in enumerate(sub_matches):
        heading_text = m.group(2)
        canonical = _canonicalize_section(heading_text)
        start = m.end()
        end = sub_matches[i + 1].start() if i + 1 < len(sub_matches) else len(body)
        section_text = body[start:end].strip()

        key = canonical if canonical != "other" else f"other:{heading_text.lower().strip()}"
        if key in result:
            result[key] = result[key] + "\n\n" + section_text
        else:
            result[key] = section_text

    return result


# ---------------------------------------------------------------------------
# TCRecord builder
# ---------------------------------------------------------------------------

def _parse_tc_record(
    tc_id: str,
    tc_heading: str,
    body: str,
    base_metadata: dict,
) -> TCRecord:
    """Build a :class:`TCRecord` from a parsed TC block."""
    _, title = _parse_tc_heading(tc_heading)
    source_file = Path(base_metadata.get("path", base_metadata.get("source", ""))).name
    category = base_metadata.get("category")

    sections = _parse_tc_sections(body)

    purpose = sections.get("purpose", "").strip()
    pics_text = sections.get("pics", "")
    precond_text = sections.get("preconditions", "")
    devices_text = sections.get("required_devices", "")
    topology_text = sections.get("device_topology", "")
    setup_text = sections.get("test_setup", "")
    steps_text = sections.get("test_steps", "")
    notes_text = sections.get("notes", "")

    # PICS: from dedicated section + any inline [PICS.XXX] in body
    pics = _PICS_INLINE_RE.findall(pics_text) if pics_text else []
    if not pics:
        pics = _PICS_INLINE_RE.findall(body)

    # Preconditions: bullet list items
    preconditions = _parse_bullet_list(precond_text) if precond_text else []

    # Required devices
    required_devices = _parse_required_devices(devices_text) if devices_text else []

    # Test steps
    test_steps = _parse_test_steps(steps_text) if steps_text else []

    # Notes
    notes: List[str] = []
    if notes_text:
        notes = _parse_notes(notes_text)
    # Also collect inline NOTE: lines from the body
    for line in body.splitlines():
        if re.match(r"^NOTE:\s+", line.strip()):
            cleaned = re.sub(r"^NOTE:\s+", "", line.strip())
            if cleaned and cleaned not in notes:
                notes.append(cleaned)

    return TCRecord(
        test_case_id=tc_id,
        title=title,
        source_file=source_file,
        section_group="Test Cases",
        category=category,
        purpose=purpose,
        pics=pics,
        preconditions=preconditions,
        required_devices=required_devices,
        device_topology=topology_text.strip(),
        test_setup=setup_text.strip(),
        test_steps=test_steps,
        notes=notes,
        entities=[],
        test_intents=[],
    )


# ---------------------------------------------------------------------------
# Primary page_content builder
# ---------------------------------------------------------------------------

def _build_primary_page_content(record: TCRecord) -> str:
    """Build embedding-friendly text from a :class:`TCRecord`.

    Includes all key fields so that semantic search can find this TC
    by any of: ID, title, purpose, PICS codes, step content.
    """
    parts: List[str] = []
    parts.append(f"{record.test_case_id}: {record.title}" if record.title else record.test_case_id)

    if record.purpose:
        parts.append(f"\nPurpose: {record.purpose}")

    if record.pics:
        parts.append(f"\nPICS: {', '.join(record.pics)}")

    if record.preconditions:
        preconds = "\n".join(f"- {p}" for p in record.preconditions)
        parts.append(f"\nPreconditions:\n{preconds}")

    if record.device_topology:
        parts.append(f"\nTopology: {record.device_topology}")

    if record.test_steps:
        lines = []
        for s in record.test_steps:
            line = f"{s['step_no']}. {s['text']}"
            if s.get("expected"):
                line += f"\n   Expected: {s['expected']}"
            lines.append(line)
        parts.append("\nTest Steps:\n" + "\n".join(lines))

    if record.notes:
        parts.append("\nNotes: " + "; ".join(record.notes))

    return "".join(parts)


# ---------------------------------------------------------------------------
# Document factory helpers
# ---------------------------------------------------------------------------

def _make_doc(page_content: str, chunk_type: str, tc_meta: dict) -> "Document":  # type: ignore[name-defined]
    """Construct a Document with unified metadata."""
    from src.loader.base_loader import Document

    section_type = _CHUNK_TYPE_TO_SECTION_TYPE.get(chunk_type, chunk_type)
    meta = {
        **tc_meta,
        "chunk_type": chunk_type,
        "section_type": section_type,   # backward compat
    }
    return Document(page_content=page_content, metadata=meta)


def _secondary_docs(record: TCRecord, tc_meta: dict) -> list:
    """Return secondary Document objects for each non-empty subfield."""
    docs = []
    subfields = [
        ("purpose",          record.purpose),
        ("pics",             "\n".join(f"[PICS.{p}]" for p in record.pics)),
        ("preconditions",    "\n".join(f"* {p}" for p in record.preconditions)),
        ("required_devices", "\n".join(f"* {d['name']} ({d['description']})" for d in record.required_devices)),
        ("device_topology",  record.device_topology),
        ("test_setup",       record.test_setup),
        ("test_steps",       "\n".join(
            f"{s['step_no']}. {s['text']}" + (f"\n   Expected: {s['expected']}" if s.get("expected") else "")
            for s in record.test_steps
        )),
        ("notes",            "\n".join(f"NOTE: {n}" for n in record.notes)),
    ]
    for chunk_type, text in subfields:
        text = text.strip()
        if text:
            docs.append(_make_doc(text, chunk_type, tc_meta))
    return docs


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

class MatterTCChunker(BaseChunker):
    """TC-aware chunker for Matter AsciiDoc test plans.

    For each ``TC-*`` heading, produces:

    1. **Primary chunk** (``chunk_type="primary"``) — one Document with:
       - ``page_content``: reconstructed clean text for embedding
       - ``metadata["tc_record"]``: :class:`TCRecord` dict (full structured data)
       - ``metadata["tc_id"]``, ``["cluster_name"]``, ``["pics_codes"]``: backward compat

    2. **Secondary chunks** — one Document per non-empty subfield:
       ``purpose``, ``pics``, ``preconditions``, ``required_devices``,
       ``device_topology``, ``test_setup``, ``test_steps``, ``notes``

    Content without TC headings falls back to :class:`GenericChunker`.

    Args:
        chunk_size:    Used by the preamble fallback chunker only.
        chunk_overlap: Used by the preamble fallback chunker only.
        ignore_rules:  Strip rules applied to the full text before TC splitting.
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        ignore_rules: Optional[List] = None,
    ) -> None:
        self._fallback = GenericChunker(chunk_size, chunk_overlap)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._ignore_rules: List[IgnoreRule] = []
        for r in (ignore_rules or []):
            if isinstance(r, IgnoreRule):
                self._ignore_rules.append(r)
            elif isinstance(r, dict):
                self._ignore_rules.append(IgnoreRule.from_dict(r))
            else:
                raise TypeError(f"ignore_rules entries must be IgnoreRule or dict, got {type(r)}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(self, text: str, metadata: dict) -> list:
        """Chunk *text* with TC-awareness.

        Returns a mix of primary and secondary Documents for TC blocks,
        and GenericChunker Documents for non-TC preamble content.
        Ignore rules are applied to the full text before any splitting.
        """
        from src.loader.base_loader import Document  # noqa: F401 (used via _make_doc)

        text = text.strip()
        if not text:
            return []

        if self._ignore_rules:
            text = apply_ignore_rules(text, self._ignore_rules)
            text = text.strip()
            if not text:
                return []

        tc_matches = list(_TC_HEADING_RE.finditer(text))
        if not tc_matches:
            return self._fallback.chunk(text, metadata)

        docs: list = []

        # Preamble before the first TC heading
        preamble = text[: tc_matches[0].start()].strip()
        if preamble:
            preamble_meta = {**metadata, "section_type": "preamble", "chunk_type": "preamble"}
            docs.extend(self._fallback.chunk(preamble, preamble_meta))

        for i, match in enumerate(tc_matches):
            tc_heading = match.group(2)
            tc_id = _extract_tc_id(tc_heading)
            cluster_name = _extract_cluster_name(tc_id)

            body_start = match.end()
            body_end = tc_matches[i + 1].start() if i + 1 < len(tc_matches) else len(text)
            body = text[body_start:body_end].strip()

            # Extract PICS codes for backward compat (all inline codes from body)
            pics_codes = _PICS_INLINE_RE.findall(body)

            # Base metadata shared by all chunks of this TC
            tc_meta = {
                **metadata,
                "tc_id":        tc_id,
                "cluster_name": cluster_name,
                "pics_codes":   pics_codes,
            }

            # Build structured record
            record = _parse_tc_record(tc_id, tc_heading, body, metadata)

            # ---- Primary chunk ----
            primary_text = _build_primary_page_content(record)
            primary_meta = {
                **tc_meta,
                "tc_record": record.to_dict(),
            }
            docs.append(_make_doc(primary_text, "primary", primary_meta))

            # ---- Secondary chunks ----
            docs.extend(_secondary_docs(record, tc_meta))

        return docs
