"""Structured change extractor — converts PR diff text into typed JSON change records.

For each semantic PR chunk, extracts a structured ``StructuredChange`` that describes:

  - what kind of change it is (add/remove/modify an attribute, command, etc.)
  - which cluster, entity, and field is affected
  - old and new values (for modifications)
  - conditions / conformance implications

**Strategy (two-pass):**

1. **Rule-based pass** — regex patterns against the diff text, [ADDED/REMOVED/CHANGED]
   annotations, and table row prefixes produce high-confidence results without any LLM.

2. **LLM fallback** — when ``ambiguous=True`` or ``confidence < threshold``, the chunk
   is sent to the LLM with a compact prompt.  LLM is invoked once per chunk (not per
   entity), and its JSON output is merged back into the structured record.

Output dataclass ``StructuredChange``::

    {
      "change_kind": "MODIFY_ATTRIBUTE",     # see ChangeKind enum
      "cluster":     "On/Off",
      "entities":    [{"type": "attribute", "name": "OnOff", "id": "0x0000"}],
      "conditions":  ["conformance changed M → O"],
      "effects":     ["behavior may change for non-lighting devices"],
      "old_value":   "M",
      "new_value":   "O",
      "confidence":  0.9,
      "ambiguous":   false,
      "source_text": "...",          # first 300 chars of original chunk
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Change kind taxonomy
# ---------------------------------------------------------------------------

class ChangeKind(str, Enum):
    # Cluster-level
    ADD_CLUSTER        = "ADD_CLUSTER"
    REMOVE_CLUSTER     = "REMOVE_CLUSTER"
    MODIFY_CLUSTER     = "MODIFY_CLUSTER"
    # Attribute
    ADD_ATTRIBUTE      = "ADD_ATTRIBUTE"
    REMOVE_ATTRIBUTE   = "REMOVE_ATTRIBUTE"
    MODIFY_ATTRIBUTE   = "MODIFY_ATTRIBUTE"
    # Attribute column-specific changes
    QUIETER_REPORTING_CHANGED = "QUIETER_REPORTING_CHANGED"  # Quality Q added/removed (Quieter Reporting)
    NON_VOLATILE_CHANGED = "NON_VOLATILE_CHANGED"   # Quality N added/removed
    CONFORMANCE_CHANGED  = "CONFORMANCE_CHANGED"    # M↔O↔P↔D conformance change
    ACCESS_CHANGED       = "ACCESS_CHANGED"         # R/W/RW access change
    DATATYPE_CHANGED     = "DATATYPE_CHANGED"       # Type column change
    CONSTRAINT_CHANGED   = "CONSTRAINT_CHANGED"     # Constraint column change
    FALLBACK_CHANGED     = "FALLBACK_CHANGED"       # Default/fallback value change
    # Command
    ADD_COMMAND        = "ADD_COMMAND"
    REMOVE_COMMAND     = "REMOVE_COMMAND"
    MODIFY_COMMAND     = "MODIFY_COMMAND"
    # Event
    ADD_EVENT          = "ADD_EVENT"
    REMOVE_EVENT       = "REMOVE_EVENT"
    MODIFY_EVENT       = "MODIFY_EVENT"
    # Feature
    ADD_FEATURE        = "ADD_FEATURE"
    REMOVE_FEATURE     = "REMOVE_FEATURE"
    MODIFY_FEATURE     = "MODIFY_FEATURE"
    # Requirement / behavior
    ADD_REQUIREMENT    = "ADD_REQUIREMENT"
    REMOVE_REQUIREMENT = "REMOVE_REQUIREMENT"
    MODIFY_REQUIREMENT = "MODIFY_REQUIREMENT"
    MODIFY_BEHAVIOR    = "MODIFY_BEHAVIOR"
    # Protocol / commissioning
    MODIFY_PROTOCOL    = "MODIFY_PROTOCOL"
    # Fallback
    UNKNOWN            = "UNKNOWN"


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class StructuredChange:
    change_kind:  str             = ChangeKind.UNKNOWN
    cluster:      str             = ""
    entities:     List[Dict]      = field(default_factory=list)
    conditions:   List[str]       = field(default_factory=list)
    effects:      List[str]       = field(default_factory=list)
    old_value:    str             = ""
    new_value:    str             = ""
    confidence:   float           = 0.5
    ambiguous:    bool            = True
    source_text:  str             = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "change_kind": self.change_kind,
            "cluster":     self.cluster,
            "entities":    self.entities,
            "conditions":  self.conditions,
            "effects":     self.effects,
            "old_value":   self.old_value,
            "new_value":   self.new_value,
            "confidence":  self.confidence,
            "ambiguous":   self.ambiguous,
            "source_text": self.source_text,
        }


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_CLUSTER_ADDED_RE    = re.compile(
    r'^\+\s*=+\s*(.+?)\s*[Cc]luster', re.MULTILINE)
_CLUSTER_REMOVED_RE  = re.compile(
    r'^-\s*=+\s*(.+?)\s*[Cc]luster', re.MULTILINE)
_SECTION_ADDED_RE    = re.compile(
    r'(?:\[ADDED:\s*(.+?)\]|\[ADDED[^\]]*\])', re.DOTALL)
_SECTION_REMOVED_RE  = re.compile(
    r'(?:\[REMOVED:\s*(.+?)\]|\[REMOVED[^\]]*\])', re.DOTALL)
_SECTION_CHANGED_RE  = re.compile(
    r'\[CHANGED:\s*(.+?)\s*→\s*(.+?)\]', re.DOTALL)

# Table row patterns: +| 0x0000 | OnOff | boolean ...
_ROW_ADDED_RE    = re.compile(r'^\+\|.*\|\s*(0x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9_]+)\s*\|', re.MULTILINE)
_ROW_REMOVED_RE  = re.compile(r'^-\|.*\|\s*(0x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9_]+)\s*\|', re.MULTILINE)

_CLUSTER_NAME_RE = re.compile(r'\b([A-Z][A-Za-z\s/]{2,30})\s+[Cc]luster\b')
_ATTR_NAME_RE    = re.compile(r'\battribute\s+([A-Za-z][A-Za-z0-9]+)\b', re.I)
_CMD_NAME_RE     = re.compile(r'\bcommand\s+([A-Z][A-Za-z0-9]+)\b')
_EVT_NAME_RE     = re.compile(r'\bevent\s+([A-Z][A-Za-z0-9]+)\b')

# Extract the entity name from a section heading: "OnTime Attribute" or "Off Command"
_SECTION_ENTITY_NAME_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9]+)\s+(?:Attribute|Command|Event|Feature)\b'
)

# Words that can follow "attribute" in spec prose but are NOT attribute names.
# These get filtered when falling back to body-text regex so we don't extract
# verbs/modals as entity names (e.g. "This attribute specifies..." → "specifies").
_ATTR_BODY_STOP_WORDS = frozenset({
    "specifies", "can", "shall", "must", "may", "will", "would", "should",
    "indicates", "is", "has", "contains", "defines", "represents",
    "provides", "describes", "stores", "holds", "returns", "reports",
    "value", "type", "access", "conformance", "constraint", "quality",
    "default", "field", "list", "set", "table", "entry", "row",
})

# Stop-words for entity names extracted from diff table rows — filter out spec keywords
# that appear in conformance/access/type columns and get mis-extracted as entity names.
_ENTITY_NAME_STOP_WORDS = frozenset({
    "SHALL", "MUST", "MAY", "SHOULD", "NOT", "NULL", "TRUE", "FALSE",
    "OPTIONAL", "MANDATORY", "PROVISIONAL", "DEPRECATED", "DISALLOWED",
    "STRUCT", "ENUM", "BITMAP", "ARRAY", "LIST", "OCTSTR", "STRING",
    "BOOLEAN", "UINT", "INT", "SINGLE", "DOUBLE", "TEMPERATURE",
    "Read", "Write", "Fabric", "Timed", "Admin", "Operate", "View",
    "Server", "Client", "Cluster", "Attribute", "Command", "Event", "Feature",
})

# Attribute table row entity extraction: "0x0000 | EntityName |" or "0x0000 EntityName type..."
# Uses {2,} to also match 2-digit command/event IDs (0x00, 0x01) not just 4-digit attribute IDs.
_TABLE_ROW_ENTITY_RE = re.compile(
    r'(?:0x[0-9a-fA-F]{2,})\s*\|?\s*([A-Z][A-Za-z0-9]+)', re.MULTILINE)

# Diff table row extraction: +|- lines in unified-diff format.
# Matches: "+| 0x0000 | EntityName |..." or "+| 0 | EntityName |..." (features use decimal bit index)
_DIFF_ROW_ENTITY_RE = re.compile(
    r'^[+-]\|\s*(?:0x[0-9a-fA-F]{2,}|\d+)\s*\|\s*([A-Z][A-Za-z0-9]+)',
    re.MULTILINE)

# Heading level + text
_HEADING_RE = re.compile(r'^=+\s*(.+)$', re.MULTILINE)

# Conformance / access value pairs from CHANGED annotations
_CONFORMANCE_CHANGED_RE = re.compile(
    r'conformance[:\s]+(\w+)\s*(?:→|->)\s*(\w+)', re.I)
_ACCESS_CHANGED_RE = re.compile(
    r'access[:\s]+([A-Z\s]+)\s*(?:→|->)\s*([A-Z\s]+)', re.I)

# --- Attribute column-specific change patterns ---
# Matter Quality flags: Q=Quieter Reporting, N=Non-volatile, P=Fixed, S=Scene, X=Fixed-by-manufacturer, C=Changed-omitted
# NOTE: Nullable is NOT a Quality flag — it is a type modifier in the Type column (e.g. "nullable uint16")
_QUALITY_FLAGS: frozenset = frozenset("QNPSXC")
_QUALITY_NAMES = {
    "Q": "Quieter Reporting", "N": "Non-volatile", "P": "Fixed",
    "S": "Scene", "X": "Fixed-by-manufacturer", "C": "Changed-omitted",
}
# ADDED/REMOVED single-letter quality flags in diff annotations
_QUALITY_ADDED_RE   = re.compile(r'\[ADDED:\s*([A-Z](?:\s+[A-Z])*)\s*\]')
_QUALITY_REMOVED_RE = re.compile(r'\[REMOVED:\s*([A-Z](?:\s+[A-Z])*)\s*\]')

# Conformance single-letter codes: M O P D X  (may also be compound like "[LT]")
_CONFORMANCE_CODES  = frozenset("MOPDX")
_CONFORMANCE_COL_RE = re.compile(
    r'\[CHANGED:\s*([MOPDX](?:\s*,\s*[MOPDX])*|\[[A-Z]+\])\s*→\s*([MOPDX](?:\s*,\s*[MOPDX])*|\[[A-Z]+\])\s*\]')

# Access codes: R, W, RW, F, VO, VM, VA
_ACCESS_TOKENS  = re.compile(r'^(?:RW?|W|F|VO|VM|VA|T|N)(?:\s+(?:RW?|W|F|VO|VM|VA|T|N))*$')
_ACCESS_COL_RE  = re.compile(
    r'\[CHANGED:\s*([RWFVMAT]+(?:\s+[RWFVMAT]+)*)\s*→\s*([RWFVMAT]+(?:\s+[RWFVMAT]+)*)\s*\]')

# Data type change: e.g. [CHANGED: uint16 → int32]
_DATATYPE_RE    = re.compile(
    r'\[CHANGED:\s*(uint\d+|int\d+|bool(?:ean)?|string|octstr|list|struct|enum\d+|bitmap\d+|single|double|epoch-us|epoch-s|utc|posix-ms|systime-us|temperature)\s*→\s*'
    r'(uint\d+|int\d+|bool(?:ean)?|string|octstr|list|struct|enum\d+|bitmap\d+|single|double|epoch-us|epoch-s|utc|posix-ms|systime-us|temperature)\s*\]', re.I)

# Constraint change: e.g. [CHANGED: 0 to 254 → 0 to 65534]
_CONSTRAINT_RE  = re.compile(
    r'\[CHANGED:\s*([^→\]]{1,60}?)\s*→\s*([^→\]]{1,60}?)\s*\]')

# Fallback/default value change: e.g. [CHANGED: TRUE → FALSE] or [CHANGED: 0 → 1]
_FALLBACK_RE    = re.compile(
    r'\[CHANGED:\s*(TRUE|FALSE|null|\d+(?:\.\d+)?)\s*→\s*(TRUE|FALSE|null|\d+(?:\.\d+)?)\s*\]', re.I)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ChangeExtractor:
    """Extract structured change records from PR diff text.

    Args:
        llm_provider: optional LLM instance (from ``get_llm()``).
                      When None, ambiguous cases are returned as-is without LLM fallback.
        confidence_threshold: Chunks with rule-based confidence below this value
                              are sent to the LLM when a provider is configured.
    """

    def __init__(
        self,
        llm_provider=None,
        confidence_threshold: float = 0.6,
    ) -> None:
        self._llm = llm_provider
        self._threshold = confidence_threshold

    def extract(
        self,
        text: str,
        cluster_hint: str = "",
        section_hint: str = "",
        change_types_hint: Optional[List[str]] = None,
    ) -> StructuredChange:
        """Extract a StructuredChange from *text*.

        Args:
            text:               PR diff / annotated text for one semantic chunk.
            cluster_hint:       Cluster name from upstream metadata (e.g. SemanticChunker).
            section_hint:       Section heading from upstream metadata.
            change_types_hint:  ["ADDED", "REMOVED", "CHANGED"] from upstream metadata.

        Returns:
            StructuredChange — always (never raises).
        """
        change = self._rule_based(text, cluster_hint, section_hint, change_types_hint or [])
        change.source_text = text[:300]

        if change.ambiguous and change.confidence < self._threshold and self._llm:
            change = self._llm_fallback(text, change)

        return change

    # ------------------------------------------------------------------
    # Rule-based pass
    # ------------------------------------------------------------------

    def _rule_based(
        self,
        text: str,
        cluster_hint: str,
        section_hint: str,
        change_types_hint: List[str],
    ) -> StructuredChange:
        change = StructuredChange()

        # 1. Cluster detection
        cluster = cluster_hint
        if not cluster:
            m = _CLUSTER_NAME_RE.search(text)
            cluster = m.group(1).strip() if m else ""
        change.cluster = cluster

        # 2. Determine what kind of change this is

        # A. Cluster added/removed (new/deleted section)
        if _CLUSTER_ADDED_RE.search(text):
            change.change_kind = ChangeKind.ADD_CLUSTER
            change.confidence = 0.9
            change.ambiguous = False
            return change
        if _CLUSTER_REMOVED_RE.search(text):
            change.change_kind = ChangeKind.REMOVE_CLUSTER
            change.confidence = 0.9
            change.ambiguous = False
            return change

        # B. Infer entity type from section heading
        section_lower = section_hint.lower()
        entity_type = ""
        if "attribute" in section_lower:
            entity_type = "attribute"
        elif "command" in section_lower:
            entity_type = "command"
        elif "event" in section_lower:
            entity_type = "event"
        elif "feature" in section_lower:
            entity_type = "feature"
        elif any(w in section_lower for w in ("requirement", "shall", "must", "behavior", "behaviour")):
            entity_type = "requirement"

        # C. Annotation-based change kind
        has_added   = "ADDED" in change_types_hint or bool(_SECTION_ADDED_RE.search(text))
        has_removed = "REMOVED" in change_types_hint or bool(_SECTION_REMOVED_RE.search(text))
        has_changed = "CHANGED" in change_types_hint or bool(_SECTION_CHANGED_RE.search(text))

        if entity_type == "attribute":
            # Before assuming ADD/REMOVE, check if the annotations describe a *column* change
            # within an existing attribute row (e.g. [ADDED: Q] = quality flag, not a new row).
            # A new attribute row looks like: whole diff line "+| 0xHHHH | Name | ..."
            _whole_row_added   = bool(_ROW_ADDED_RE.search(text))
            _whole_row_removed = bool(_ROW_REMOVED_RE.search(text))

            # Quality flags or conformance/access/type annotations in [ADDED:]/[CHANGED:] → MODIFY
            _has_col_change = bool(
                _QUALITY_ADDED_RE.search(text) or _QUALITY_REMOVED_RE.search(text) or
                _CONFORMANCE_COL_RE.search(text) or _DATATYPE_RE.search(text) or
                _FALLBACK_RE.search(text)
            )

            if _whole_row_added and not has_removed and not _has_col_change:
                change.change_kind = ChangeKind.ADD_ATTRIBUTE
            elif _whole_row_removed and not has_added and not _has_col_change:
                change.change_kind = ChangeKind.REMOVE_ATTRIBUTE
            else:
                # Default to MODIFY; _detect_attr_table_column_changes will refine further
                change.change_kind = ChangeKind.MODIFY_ATTRIBUTE
        elif entity_type == "command":
            change.change_kind = (
                ChangeKind.ADD_COMMAND if has_added and not has_removed else
                ChangeKind.REMOVE_COMMAND if has_removed and not has_added else
                ChangeKind.MODIFY_COMMAND
            )
        elif entity_type == "event":
            change.change_kind = (
                ChangeKind.ADD_EVENT if has_added and not has_removed else
                ChangeKind.REMOVE_EVENT if has_removed and not has_added else
                ChangeKind.MODIFY_EVENT
            )
        elif entity_type == "feature":
            change.change_kind = (
                ChangeKind.ADD_FEATURE if has_added and not has_removed else
                ChangeKind.REMOVE_FEATURE if has_removed and not has_added else
                ChangeKind.MODIFY_FEATURE
            )
        elif entity_type == "requirement":
            change.change_kind = (
                ChangeKind.ADD_REQUIREMENT if has_added and not has_removed else
                ChangeKind.REMOVE_REQUIREMENT if has_removed and not has_added else
                ChangeKind.MODIFY_REQUIREMENT
            )
        elif has_added or has_removed or has_changed:
            change.change_kind = ChangeKind.MODIFY_BEHAVIOR
        else:
            change.change_kind = ChangeKind.UNKNOWN
            change.ambiguous = True
            change.confidence = 0.2
            return change

        # 3. Extract entity names
        entities: List[Dict] = []
        if entity_type == "attribute":
            # First priority: extract from section heading ("OnTime Attribute" → "OnTime").
            # This is far more reliable than body-text regex which picks up spec prose verbs
            # (e.g. "This attribute specifies..." → "specifies").
            if section_hint:
                for m in _SECTION_ENTITY_NAME_RE.finditer(section_hint):
                    entities.append({"type": "attribute", "name": m.group(1)})
            if not entities:
                # Fallback: scan body text but filter out common prose verbs/modals
                for m in _ATTR_NAME_RE.finditer(text):
                    name = m.group(1)
                    if name.lower() not in _ATTR_BODY_STOP_WORDS:
                        entities.append({"type": "attribute", "name": name})
            # Try diff table rows (+/- lines) before last-resort annotation scan
            if not entities:
                for m in _DIFF_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "attribute", "name": m.group(1)})
            # Last resort: attribute table rows in annotation text ("0x0000 EntityName ...")
            if not entities:
                for m in _TABLE_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "attribute", "name": m.group(1)})
        elif entity_type == "command":
            if section_hint:
                for m in _SECTION_ENTITY_NAME_RE.finditer(section_hint):
                    entities.append({"type": "command", "name": m.group(1)})
            if not entities:
                for m in _CMD_NAME_RE.finditer(text):
                    entities.append({"type": "command", "name": m.group(1)})
            # Diff table rows for added/removed command rows
            if not entities:
                for m in _DIFF_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "command", "name": m.group(1)})
            if not entities:
                for m in _TABLE_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "command", "name": m.group(1)})
        elif entity_type == "event":
            if section_hint:
                for m in _SECTION_ENTITY_NAME_RE.finditer(section_hint):
                    entities.append({"type": "event", "name": m.group(1)})
            if not entities:
                for m in _EVT_NAME_RE.finditer(text):
                    entities.append({"type": "event", "name": m.group(1)})
            if not entities:
                for m in _DIFF_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "event", "name": m.group(1)})
            if not entities:
                for m in _TABLE_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "event", "name": m.group(1)})
        elif entity_type == "feature":
            # Features: section heading first, then diff rows (bit | CODE | Name format)
            if section_hint:
                for m in _SECTION_ENTITY_NAME_RE.finditer(section_hint):
                    entities.append({"type": "feature", "name": m.group(1)})
            if not entities:
                for m in _DIFF_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "feature", "name": m.group(1)})
            if not entities:
                for m in _TABLE_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": "feature", "name": m.group(1)})
        # Universal fallback: entity_type unknown (cluster-level section) — scan all row patterns
        if not entities:
            # Try section heading pattern first (catches "EntityName Attribute/Command/Event/Feature")
            if section_hint:
                for m in _SECTION_ENTITY_NAME_RE.finditer(section_hint):
                    inferred_type = section_hint[m.end():].strip().split()[0].lower() if section_hint[m.end():].strip() else "entity"
                    entities.append({"type": inferred_type, "name": m.group(1)})
            # Then try diff rows and annotation table rows
            if not entities:
                for m in _DIFF_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": entity_type or "entity", "name": m.group(1)})
            if not entities:
                for m in _TABLE_ROW_ENTITY_RE.finditer(text):
                    entities.append({"type": entity_type or "entity", "name": m.group(1)})
        # For requirement/behavior sections: extract entities from annotation text
        if not entities and (entity_type in ("requirement", "") or change.change_kind in (
            ChangeKind.MODIFY_BEHAVIOR, ChangeKind.ADD_REQUIREMENT,
            ChangeKind.REMOVE_REQUIREMENT, ChangeKind.MODIFY_REQUIREMENT,
        )):
            # Extract entity names from [ADDED/REMOVED/CHANGED: EntityName ...] annotations
            _ann_entity_re = re.compile(
                r'\[(ADDED|REMOVED|CHANGED):\s*([A-Z][A-Za-z0-9]+)'
            )
            for _m in _ann_entity_re.finditer(text):
                _ename = _m.group(2)
                if _ename not in _ENTITY_NAME_STOP_WORDS and len(_ename) > 2:
                    entities.append({"type": "entity", "name": _ename})
            # Also try to extract from PICS-code-like patterns
            _pics_entity_re = re.compile(r'\b([A-Z]{2,6})\.S\b')
            for _m in _pics_entity_re.finditer(text):
                entities.append({"type": "cluster_pics", "name": _m.group(1)})
        # Deduplicate by name; also drop known false-positive spec keyword names
        seen: set = set()
        deduped = []
        for e in entities:
            if e["name"] in _ENTITY_NAME_STOP_WORDS:
                continue
            k = (e["type"], e["name"])
            if k not in seen:
                seen.add(k)
                deduped.append(e)
        change.entities = deduped

        # 4. For MODIFY_ATTRIBUTE, detect specific column changes (Quality/Conformance/Access/Type)
        if change.change_kind == ChangeKind.MODIFY_ATTRIBUTE:
            change = self._detect_attr_table_column_changes(text, change)

        # 5. Extract conditions / old→new values (general conformance/access prose patterns)
        for m in _CONFORMANCE_CHANGED_RE.finditer(text):
            change.conditions.append(f"conformance: {m.group(1)} → {m.group(2)}")
            change.old_value = change.old_value or m.group(1)
            change.new_value = change.new_value or m.group(2)
        for m in _ACCESS_CHANGED_RE.finditer(text):
            change.conditions.append(f"access: {m.group(1).strip()} → {m.group(2).strip()}")

        # 6. Extract CHANGED old→new pairs for explicit annotations
        for m in _SECTION_CHANGED_RE.finditer(text):
            old, new = m.group(1).strip()[:100], m.group(2).strip()[:100]
            if not change.old_value:
                change.old_value = old
            if not change.new_value:
                change.new_value = new

        # 7. Confidence
        confidence = 0.5
        if cluster:
            confidence += 0.1
        if entities:
            confidence += 0.15
        if change.conditions or change.old_value:
            confidence += 0.1
        if has_added or has_removed or has_changed:
            confidence += 0.15
        change.confidence = min(confidence, 1.0)
        change.ambiguous = change.confidence < self._threshold or change.change_kind == ChangeKind.UNKNOWN

        return change

    # ------------------------------------------------------------------
    # Attribute table column-specific change detection
    # ------------------------------------------------------------------

    def _detect_attr_table_column_changes(
        self,
        text: str,
        change: StructuredChange,
    ) -> StructuredChange:
        """Detect specific attribute table column changes and update change_kind + conditions.

        Inspects [ADDED:], [REMOVED:], [CHANGED:] annotations for:
          - Quality flags (Q=Quieter Reporting, N=Non-volatile, P=Fixed, S=Scene, X=Fixed-by-manufacturer)
          - Conformance column (M → O, O → M, etc.)
          - Access column (R → RW, RW → R, etc.)
          - Data type column (uint16 → int32, etc.)
          - Fallback/default value column

        Updates ``change.change_kind`` to the most specific ChangeKind detected and
        appends human-readable entries to ``change.conditions``.
        """
        detected: List[str] = []

        # --- Quality flag changes: [ADDED: Q], [REMOVED: N], etc. ---
        for m in _QUALITY_ADDED_RE.finditer(text):
            for flag in m.group(1).split():
                if flag in _QUALITY_FLAGS:
                    name = _QUALITY_NAMES[flag]
                    change.conditions.append(f"quality: {name} ({flag}) added")
                    change.new_value = change.new_value or flag
                    if flag == "Q":
                        detected.append(ChangeKind.QUIETER_REPORTING_CHANGED)
                    elif flag == "N":
                        detected.append(ChangeKind.NON_VOLATILE_CHANGED)

        for m in _QUALITY_REMOVED_RE.finditer(text):
            for flag in m.group(1).split():
                if flag in _QUALITY_FLAGS:
                    name = _QUALITY_NAMES[flag]
                    change.conditions.append(f"quality: {name} ({flag}) removed")
                    change.old_value = change.old_value or flag
                    if flag == "Q":
                        detected.append(ChangeKind.QUIETER_REPORTING_CHANGED)
                    elif flag == "N":
                        detected.append(ChangeKind.NON_VOLATILE_CHANGED)

        # --- Conformance column changes (M → O, O → M, etc.) ---
        for m in _CONFORMANCE_COL_RE.finditer(text):
            old_conf, new_conf = m.group(1).strip(), m.group(2).strip()
            if (all(c in _CONFORMANCE_CODES or c in " ,[]" for c in old_conf) and
                    all(c in _CONFORMANCE_CODES or c in " ,[]" for c in new_conf)):
                change.conditions.append(f"conformance: {old_conf} → {new_conf}")
                change.old_value = change.old_value or old_conf
                change.new_value = change.new_value or new_conf
                detected.append(ChangeKind.CONFORMANCE_CHANGED)

        # --- Access column changes (R → RW, etc.) ---
        for m in _ACCESS_COL_RE.finditer(text):
            old_acc, new_acc = m.group(1).strip(), m.group(2).strip()
            # Heuristic: access tokens are short uppercase/F/V combos; avoid full sentences
            if len(old_acc) <= 10 and len(new_acc) <= 10:
                change.conditions.append(f"access: {old_acc} → {new_acc}")
                change.old_value = change.old_value or old_acc
                change.new_value = change.new_value or new_acc
                detected.append(ChangeKind.ACCESS_CHANGED)

        # --- Data type changes ---
        for m in _DATATYPE_RE.finditer(text):
            old_t, new_t = m.group(1).strip(), m.group(2).strip()
            change.conditions.append(f"type: {old_t} → {new_t}")
            change.old_value = change.old_value or old_t
            change.new_value = change.new_value or new_t
            detected.append(ChangeKind.DATATYPE_CHANGED)

        # --- Fallback/default value changes ---
        for m in _FALLBACK_RE.finditer(text):
            old_v, new_v = m.group(1).strip(), m.group(2).strip()
            change.conditions.append(f"default/fallback: {old_v} → {new_v}")
            change.old_value = change.old_value or old_v
            change.new_value = change.new_value or new_v
            detected.append(ChangeKind.FALLBACK_CHANGED)

        # Assign most specific change_kind (quality > conformance > access > type > fallback)
        _KIND_PRIORITY = [
            ChangeKind.QUIETER_REPORTING_CHANGED, ChangeKind.NON_VOLATILE_CHANGED,
            ChangeKind.CONFORMANCE_CHANGED, ChangeKind.ACCESS_CHANGED,
            ChangeKind.DATATYPE_CHANGED, ChangeKind.CONSTRAINT_CHANGED,
            ChangeKind.FALLBACK_CHANGED,
        ]
        for kind in _KIND_PRIORITY:
            if kind in detected:
                change.change_kind = kind
                change.ambiguous = False
                change.confidence = max(change.confidence, 0.85)
                break

        return change

    # ------------------------------------------------------------------
    # LLM fallback
    # ------------------------------------------------------------------

    def _llm_fallback(self, text: str, partial: StructuredChange) -> StructuredChange:
        """Use LLM to fill gaps when rule-based confidence is low."""
        prompt = _LLM_EXTRACTION_PROMPT.format(
            cluster=partial.cluster or "unknown",
            change_text=text[:3000],
        )
        try:
            response = self._llm.complete(prompt, system=_LLM_EXTRACTION_SYSTEM)
            llm_json = _parse_llm_json(response)
            if llm_json:
                partial = _merge_llm_result(partial, llm_json)
        except Exception as exc:
            logger.warning("[ChangeExtractor] LLM fallback failed: %s", exc)
        return partial


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_LLM_EXTRACTION_SYSTEM = (
    "You are a Matter protocol specification expert. Extract structured change "
    "information from PR diff text. Reply ONLY with a JSON object."
)

_LLM_EXTRACTION_PROMPT = """\
Cluster context: {cluster}

PR change text (may contain [ADDED:], [REMOVED:], [CHANGED:] annotations):
---
{change_text}
---

Extract a JSON object with these fields (use empty string / empty list when unknown):
{{
  "change_kind": "ADD_ATTRIBUTE | REMOVE_ATTRIBUTE | MODIFY_ATTRIBUTE | QUIETER_REPORTING_CHANGED | NON_VOLATILE_CHANGED | CONFORMANCE_CHANGED | ACCESS_CHANGED | DATATYPE_CHANGED | CONSTRAINT_CHANGED | FALLBACK_CHANGED | ADD_COMMAND | REMOVE_COMMAND | MODIFY_COMMAND | ADD_EVENT | REMOVE_EVENT | MODIFY_EVENT | ADD_FEATURE | REMOVE_FEATURE | MODIFY_FEATURE | ADD_REQUIREMENT | REMOVE_REQUIREMENT | MODIFY_REQUIREMENT | MODIFY_BEHAVIOR | ADD_CLUSTER | REMOVE_CLUSTER | MODIFY_CLUSTER | UNKNOWN",
  "cluster": "cluster name",
  "entities": [{{"type": "attribute|command|event|feature", "name": "EntityName", "id": "0x0000"}}],
  "conditions": ["conformance: M → O", "access changed", ...],
  "effects":    ["behavior change description", ...],
  "old_value":  "previous value",
  "new_value":  "new value",
  "confidence": 0.0-1.0,
  "ambiguous":  false
}}
"""


def _parse_llm_json(response: str) -> Optional[Dict]:
    """Extract JSON from LLM response (handles code fences)."""
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON
    m = re.search(r'(\{[^{}]*\})', response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def _merge_llm_result(partial: StructuredChange, llm: Dict) -> StructuredChange:
    """Merge LLM output into a partial StructuredChange."""
    if llm.get("change_kind") and llm["change_kind"] != ChangeKind.UNKNOWN:
        partial.change_kind = llm["change_kind"]
    if llm.get("cluster") and not partial.cluster:
        partial.cluster = llm["cluster"]
    if llm.get("entities"):
        existing_names = {e["name"] for e in partial.entities}
        for e in llm["entities"]:
            if e.get("name") and e["name"] not in existing_names:
                partial.entities.append(e)
    if llm.get("conditions"):
        partial.conditions.extend(
            c for c in llm["conditions"] if c not in partial.conditions
        )
    if llm.get("effects"):
        partial.effects = llm["effects"]
    if llm.get("old_value") and not partial.old_value:
        partial.old_value = llm["old_value"]
    if llm.get("new_value") and not partial.new_value:
        partial.new_value = llm["new_value"]
    partial.confidence = float(llm.get("confidence", partial.confidence))
    partial.ambiguous = bool(llm.get("ambiguous", partial.confidence < 0.6))
    return partial
