"""Deterministic rule-based extraction engine for Matter protocol documents.

All functions are pure — no I/O, no LLM calls.  They operate on clean plain
text (HTML already stripped) and the CanonicalSchema as a reference.

Public API
----------
detect_requirement_candidates(text) -> List[str]
    Filter sentences that may express a normative requirement.

classify_requirement_type(sentence, entity_refs, canonical_schema)
    -> (RequirementType, confidence, ambiguous, score_breakdown)
    Scoring engine: additive weights decide the winner.

extract_entities(text, canonical_schema) -> List[EntityMatch]
    Canonical-schema-first entity lookup, then protocol anchors.

extract_conditions_and_effects(sentence, entity_refs, entity_name_map)
    -> (List[ConditionRecord], List[EffectRecord], List[ConstraintRecord])

classify_testcase_mode(tc_id, title, purpose, procedure_steps, entity_refs)
    -> (TestMode, cluster_score, protocol_score)

detect_test_intents(title, purpose, procedure_steps) -> List[TestIntent]

infer_graph_edges(tc, spec_records, entity_refs) -> List[GraphEdgeRecord]
    Typed edges based on TestIntent / EntityType of referenced nodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from src.knowledge_graph.schema import (
    CanonicalEntityRef,
    CanonicalSchema,
    ConditionRecord,
    ConstraintRecord,
    EffectRecord,
    EntityType,
    GraphEdgeRecord,
    GraphEdgeType,
    PRRequirementRecord,
    RejectedCandidate,
    RequirementType,
    TestCaseRecord,
    TestIntent,
    TestMode,
)


# ---------------------------------------------------------------------------
# Compiled regex catalogue
# ---------------------------------------------------------------------------

# ── A. Normative-trigger keywords ────────────────────────────────────────────
_NORMATIVE_TRIGGERS = re.compile(
    r"\b(shall(?:\s+not)?|must(?:\s+not)?|required\s+to|is\s+required"
    r"|is\s+prohibited|prohibited\s+from|is\s+mandatory|mandated"
    r"|only\s+if|valid\s+only\s+when|invalid\s+if"
    r"|shall\s+return|shall\s+send|shall\s+set|shall\s+clear"
    r"|shall\s+reject|shall\s+accept|shall\s+stop|shall\s+terminate"
    r"|stops|terminates|rejects|accepts|returns|sent|not\s+sent)\b",
    re.I,
)

# ── B. Timing signals ─────────────────────────────────────────────────────────
_TIMING_RE = re.compile(
    r"\b(within\s+\d+\s*(ms|milliseconds?|seconds?|minutes?|hours?)"
    r"|after\s+\d+\s*(ms|milliseconds?|seconds?|minutes?|hours?)"
    r"|before\s+\d+\s*(ms|milliseconds?|seconds?|minutes?|hours?)"
    r"|no\s+(?:more|less)\s+than\s+\d+\s*(ms|milliseconds?|seconds?)"
    r"|deadline|expir(?:y|ation)|timeout|latency\s+requirement"
    r"|time\s+limit|periodic(?:ally)?|retry\s+interval"
    r"|\d+\s*(?:ms|s|sec|seconds?))\b",
    re.I,
)

# ── C. State-transition signals ───────────────────────────────────────────────
_STATE_TRANSITION_RE = re.compile(
    r"\b(transition\s+(?:to|from)|shall\s+enter|shall\s+exit"
    r"|move\s+to\s+state|change\s+(?:state|to)|set\s+to|cleared?\s+to"
    r"|become\s+(?:active|inactive)|go(?:es)?\s+to|leaves?\s+state"
    r"|initial(?:ize|ise)|startup\s+state|default\s+state)\b",
    re.I,
)

# ── D. Conditional signals ────────────────────────────────────────────────────
_CONDITIONAL_RE = re.compile(
    r"(if\b.{0,150}(?:then|shall)\b|when\b.{0,150}shall\b"
    r"|upon\s+receipt(?:\s+of)?|in\s+response\s+to|as\s+a\s+result\s+of"
    r"|only\s+when|valid\s+only\s+when|only\s+if|invalid\s+if"
    r"|conditioned\s+on|provided\s+that|given\s+that"
    # "If <condition>, <consequence>" — comma-demarcated conditional clause
    r"|if\b[^,\n]{0,100},\s*\w)\b",
    re.I | re.S,
)

# ── E. Lifecycle signals ──────────────────────────────────────────────────────
# Used as a secondary boost for STATE_TRANSITION_RULE (lifecycle events are
# state-triggering triggers, not their own requirement type any more).
_LIFECYCLE_RE = re.compile(
    r"\b(on\s+startup|on\s+power(?:\s+up)?|initialization|power-up|boot(?:ing)?"
    r"|reset|factory\s+reset|on\s+reboot|power\s+loss|recommission"
    r"|re-provisioning|join(?:ing)?\s+fabric|leave\s+fabric)\b",
    re.I,
)

# ── F. Protocol signals ───────────────────────────────────────────────────────
# Protocol keywords (BLE, commissioning, …).  No longer their own RequirementType;
# used only as a tiebreaker boost when co-occurring with timing or conditional.
_PROTOCOL_RE = re.compile(
    r"\b(fabric|commissioning|subscription|report(?:ing)?|discovery"
    r"|pairing|attestation|operational\s+credentials?"
    r"|ble|bluetooth\s+low\s+energy|dns-sd|mdns|qr\s+(?:code)?|nfc"
    r"|discriminator|passcode|setup\s+code|vendor\s+id|product\s+id"
    r"|csr|nocsr|noc|icac|rcac|dac|pai|paa"
    r"|matter\s+(?:fabric|network|device|controller|commissioner)"
    r"|cluster\s+revision|feature\s+map)\b",
    re.I,
)

# ── Cross-entity dependency ───────────────────────────────────────────────────
_CROSS_ENTITY_RE = re.compile(
    r"\b(depend(?:s|ing)?\s+on|requires\s+.*\s+(?:cluster|attribute|command)"
    r"|is\s+linked\s+to|ties?\s+to|cascades?\s+to|propagates?\s+to"
    r"|corresponds?\s+to|coupled\s+(?:with|to))\b",
    re.I,
)

# ── Entity-definition signals ────────────────────────────────────────────────
# Sentences that define what an entity IS or what it represents, rather than
# what it SHALL DO under a condition.
# Examples:
#   "The CurrentHue attribute shall contain the current hue value."
#   "The OnOff attribute indicates whether the device is on or off."
#   "The OccupancyChanged event represents a change in occupancy state."
_ENTITY_DEFINITION_RE = re.compile(
    r"\b(indicates?\s+(?:the|a|whether|if|how|when|that)\b"
    r"|represents?\s+(?:the|a|whether|an)\b"
    r"|is\s+defined\s+as\b"
    r"|describes?\s+(?:the|a|how|whether)\b"
    r"|specifies?\s+(?:the|a|how|whether|which)\b"
    r"|contains?\s+the\s+current\b"
    r"|stores?\s+(?:the|a|current)\b"
    r"|holds?\s+(?:the|a|current)\b"
    r"|identifies?\s+(?:the|a|an)\b"
    r"|reports?\s+(?:the|a|current|an)\b"
    r"|reflects?\s+(?:the|a)\b"
    r"|is\s+the\s+current\b"
    r"|provides?\s+(?:the|a|an)\b"
    r"|data\s+type\s+(?:is|shall\s+be)\b"
    r"|attribute\s+type\b"
    r"|type\s+of\s+the\b)\b",
    re.I,
)

# ── Value-constraint signals ─────────────────────────────────────────────────
# Sentences that constrain what values an entity may take (range, default, enum).
# Examples:
#   "The value of CurrentHue shall be in the range 0 to 254."
#   "The attribute default value shall be 0x00."
#   "The minimum value shall be 0."
_VALUE_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"\bin\s+the\s+range\b"
    r"|\bshall\s+be\s+(?:in|within|between)\b"
    r"|\bminimum\s+(?:value|of|is)\b"
    r"|\bmaximum\s+(?:value|of|is)\b"
    r"|\bvalid\s+values?\b"
    r"|\bshall\s+not\s+exceed\b"
    r"|\bshall\s+be\s+(?:at\s+(?:least|most)|no\s+(?:less|more)\s+than)\b"
    r"|\bdefault\s+(?:value|shall\s+be)\b"
    r"|\bdefault\s+is\b"
    r"|\brange\s+(?:of|is|shall\s+be)\b"
    r"|\blower\s+bound\b|\bupper\s+bound\b"
    r"|\ballowed\s+values?\b"
    r"|\bconstrained\s+to\b"
    r"|\bdata\s+type\s+shall\s+be\b"
    r"|\b\d+\s+to\s+\d+(?:\s+inclusive)?\b"
    r"|0x[0-9a-fA-F]+\s+to\s+0x[0-9a-fA-F]+"
    r")",
    re.I,
)

# ── Condition extraction ──────────────────────────────────────────────────────
_CONDITION_CLAUSE_RE = re.compile(
    r"(?:if|when|upon\s+receipt(?:\s+of)?|in\s+response\s+to"
    r"|only\s+when|only\s+if|valid\s+only\s+when|invalid\s+if)\s+([^,\.;]{3,140})",
    re.I,
)
# ── Effect extraction ─────────────────────────────────────────────────────────
_EFFECT_CLAUSE_RE = re.compile(r"(?:shall|must)\s+([^,\.;]{3,140})", re.I)
# ── Constraint extraction ─────────────────────────────────────────────────────
_CONSTRAINT_CLAUSE_RE = re.compile(
    r"(\b(?:between|in\s+the\s+range\s+of?|from|no\s+more\s+than"
    r"|no\s+less\s+than|at\s+most|at\s+least|greater\s+than|less\s+than"
    r"|equal\s+to|one\s+of|enum(?:erated)?|in\s+the\s+set\s+of)\b[^\.;]{2,120})",
    re.I,
)

# ── Test-intent patterns ──────────────────────────────────────────────────────
_INTENT_PATTERNS: List[Tuple[TestIntent, re.Pattern]] = [
    (TestIntent.READ_ATTRIBUTE,
     re.compile(r"\b(read(?:ing)?\s+(?:the\s+)?(?:attribute|value)"
                r"|TH\s+reads?|DUT\s+reads?|verify\s+(?:that\s+)?attribute)\b", re.I)),
    (TestIntent.WRITE_ATTRIBUTE,
     re.compile(r"\b(write(?:s|ing)?\s+(?:the\s+)?(?:attribute|value)"
                r"|TH\s+writes?|DUT\s+writes?|set(?:s|ting)?\s+attribute)\b", re.I)),
    (TestIntent.INVOKE_COMMAND,
     re.compile(r"\b(send(?:s|ing)?\s+(?:a\s+)?(?:command|request)"
                r"|invoke(?:s|d|ing)?\s+(?:the\s+)?command"
                r"|TH\s+sends?|DUT\s+sends?)\b", re.I)),
    (TestIntent.VALIDATE_RESPONSE,
     re.compile(r"\b(verify(?:ing)?\s+(?:the\s+)?response"
                r"|check(?:ing)?\s+(?:the\s+)?response"
                r"|assert(?:s|ing)?\s+(?:the\s+)?response"
                r"|response\s+(?:is|shall\s+be|contains?))\b", re.I)),
    (TestIntent.VALIDATE_ERROR,
     re.compile(r"\b((?:verify|check|expect)\s+(?:an?\s+)?error"
                r"|error\s+(?:code|response|status)"
                r"|status\s+(?:code\s+)?(?:UNSUPPORTED|INVALID|FAILURE|NOT_FOUND"
                r"|CONSTRAINT_ERROR|ACCESS_DENIED)"
                r"|shall\s+(?:return|respond\s+with)\s+(?:FAILURE|UNSUPPORTED|INVALID|ERROR))\b",
                re.I)),
    (TestIntent.OBSERVE_EVENT,
     re.compile(r"\b(event(?:s)?\s+(?:is|are|shall|generated|triggered|emitted)"
                r"|verify(?:ing)?\s+(?:the\s+)?event"
                r"|check(?:ing)?\s+(?:the\s+)?event"
                r"|subscribe\s+to\s+event|event\s+subscription)\b", re.I)),
    (TestIntent.VALIDATE_TIMING,
     re.compile(r"\b(within\s+\d+\s*(ms|seconds?|minutes?)"
                r"|timeout|latency|deadline|after\s+\d+\s*(ms|seconds?)"
                r"|periodic(?:ally)?|timing\s+(?:requirement|constraint))\b", re.I)),
    (TestIntent.VALIDATE_STATE_TRANSITION,
     re.compile(r"\b(state\s+(?:transition|change)"
                r"|transition\s+(?:to|from)\s+(?:state\s+)?\w+"
                r"|verify(?:ing)?\s+(?:the\s+)?state"
                r"|check\s+(?:the\s+)?(?:new\s+)?state)\b", re.I)),
    (TestIntent.VALIDATE_CROSS_ENTITY_DEPENDENCY,
     re.compile(r"\b(dependency|another\s+(?:cluster|attribute|command)"
                r"|multiple\s+(?:clusters?|attributes?|commands?)"
                r"|cluster\s+\w+.*cluster\s+\w+|attribute\s+\w+.*attribute\s+\w+)\b",
                re.I | re.S)),
    (TestIntent.VALIDATE_COMMISSIONING_FLOW,
     re.compile(r"\b(commission(?:ing|ed)?|provisioning|attestation"
                r"|operational\s+credentials?|NOC|ICAC|RCAC|CSR|DAC|PAI|PAA)\b", re.I)),
    (TestIntent.VALIDATE_DISCOVERY_BEHAVIOR,
     re.compile(r"\b(discover(?:y|ed|able)?|dns-sd|mdns|announce(?:ment)?"
                r"|advertis(?:e|ing|ement)|browse\s+for|resolve\s+service)\b", re.I)),
    (TestIntent.VALIDATE_ONBOARDING_PAYLOAD,
     re.compile(r"\b(QR\s+code|manual\s+entry\s+code|setup\s+(?:code|payload)"
                r"|discriminator|passcode|vendor\s+id|product\s+id|NFC\s+tag)\b", re.I)),
    (TestIntent.VALIDATE_BEHAVIOR_RULE,
     re.compile(r"\b(behavior|conformance|PICS|PICSMAP"
                r"|shall\s+(?:not\s+)?(?:support|implement|expose)"
                r"|mandatory\s+(?:attribute|command|feature|cluster))\b", re.I)),
    (TestIntent.NEGATIVE_SCENARIO,
     re.compile(r"\b(negative|invalid\s+(?:input|value|request|command)"
                r"|out(?:\s+of|\s*-\s*)range|unsupported|reject(?:ed|s|ing)?"
                r"|exceed(?:s|ing)?|malformed|forbidden|unauthorized"
                r"|beyond\s+(?:max|maximum)|below\s+(?:min|minimum))\b", re.I)),
]

# ── TC mode signals ───────────────────────────────────────────────────────────
_CLUSTER_CENTRIC_RE = re.compile(
    r"\b(attribute(?:s)?|command(?:s)?|event(?:s)?|feature(?:\s+map)?"
    r"|cluster|DUT\s+(?:reads?|writes?|sends?|receives?)"
    r"|verify(?:ing)?\s+(?:attribute|command|event)"
    r"|read\s+attribute|write\s+attribute|invoke\s+command)\b", re.I,
)
_PROTOCOL_MODE_RE = re.compile(
    r"\b(commission(?:ing|ed)?|subscription|report(?:ing)?|discovery"
    r"|ble|dns-sd|mdns|fabric|attestation|noc|icac|rcac|dac|pai|paa"
    r"|passcode|discriminator|setup\s+code|qr\s+code|nfc"
    r"|onboarding|pairing|operational\s+credentials?"
    r"|invoke\s+request|read\s+request|write\s+request|timed\s+request"
    r"|batched\s+command|interaction\s+model|chunked\s+message"
    r"|case\s+session|pase\s+session|sigma|mrp|tcp\s+connection"
    r"|bulk\s+data|bdx\s+transfer|access\s+control\s+entr"
    r"|subject\s+descriptor|privilege)\b", re.I,
)

# TC-ID prefixes that are DEFINITIVELY protocol-level (no cluster in DM XML ever).
# This is a fallback set used when DM XML has not been loaded.
# The live check uses `_is_protocol_prefix()` which is DM-XML-aware.
_DEFINITELY_PROTOCOL_TC_PREFIXES: frozenset = frozenset({
    "IDM",     # Interaction Data Model — read/write/invoke/subscribe protocol
    "SC",      # Secure Channel — CASE/PASE session establishment
    "BDX",     # Bulk Data eXchange protocol
    "DD",      # Device Discovery — commissioning / discovery flows
    "DA",      # Device Attestation
    "ACE",     # Access Control Engine
    "MC",      # Multicast / commissioning protocol
    "JFADMIN", # Joining Fabric Administrator
    "JF",      # Joining Fabric (short alias)
    "MCORE",   # Matter Core protocol behavior tests
    "DT",      # Descriptor cluster family (no DM-XML PICS prefix "DT" — protocol-adjacent)
    "SU",      # OTA Software Update — PICS prefix is OTAP/OTAPR, not SU
})

# Populated from DM XML at KB build time via configure_known_cluster_prefixes().
# When non-empty: any TC prefix NOT in this set is treated as protocol-level.
#
# WARNING: Thread-safety limitation — _known_cluster_prefixes is a module-level
# global that is mutated by configure_known_cluster_prefixes(). This is safe during
# single-threaded KG builds but NOT thread-safe for concurrent FastAPI requests.
# The FastAPI debug app should call configure_known_cluster_prefixes() once at
# startup (before accepting requests) and never mutate it afterward.
_known_cluster_prefixes: frozenset = frozenset()

# Keep the old name as an alias so that any external code importing
# _PROTOCOL_TC_PREFIXES still compiles.  New code should use _is_protocol_prefix().
_PROTOCOL_TC_PREFIXES = _DEFINITELY_PROTOCOL_TC_PREFIXES


def configure_known_cluster_prefixes(prefixes) -> None:
    """Populate the set of known cluster PICS prefixes derived from DM XML.

    Called by KnowledgeBaseBuilder after parsing the DM XML so that
    _is_protocol_prefix() can distinguish real clusters from protocol tests
    without relying on a hardcoded frozenset.

    .. warning::
        **Not thread-safe.** This function mutates the module-level global
        ``_known_cluster_prefixes``. It should only be called during
        single-threaded KG build or once at application startup before
        serving concurrent requests. Do not call from FastAPI request handlers.

    Args:
        prefixes: iterable of uppercase PICS prefix strings (e.g. {"OO", "ACL", "GC", ...})
    """
    global _known_cluster_prefixes
    _known_cluster_prefixes = frozenset(p.upper() for p in prefixes if p)


def _is_protocol_prefix(prefix: str) -> bool:
    """Return True if *prefix* identifies a protocol-level TC family (no cluster).

    Decision order:
    1. If prefix is in the hard-coded "definitely protocol" set → True
    2. If DM XML prefixes are loaded AND prefix IS in that set → False (it's a cluster)
    3. If DM XML prefixes are loaded AND prefix is NOT in that set → True (protocol)
    4. If DM XML not loaded yet → False (unknown, don't assume protocol)
    """
    up = prefix.upper()
    if up in _DEFINITELY_PROTOCOL_TC_PREFIXES:
        return True
    if _known_cluster_prefixes:
        return up not in _known_cluster_prefixes
    return False

# ── PICS gating ───────────────────────────────────────────────────────────────
_PICS_RE = re.compile(r"\bPICS(?:MAP)?\s*[:=]?\s*([A-Z][A-Z0-9_]+(?:\.[A-Z0-9][A-Z0-9_]*)+)", re.I)


# ---------------------------------------------------------------------------
# Entity name quality guards
# ---------------------------------------------------------------------------

# Names that are NEVER valid entity references regardless of cluster context.
# These are structural English words that happen to be attribute/command names
# but appear so ubiquitously in prose that any match is a false positive.
ABSOLUTE_BLOCKLIST: Set[str] = {
    # HTML/adoc procedure structure words
    "Step", "Description", "Condition", "Action", "Expected",
    # Over-generic data/state words
    "Type", "State", "Mode", "Status", "Other", "Order", "User",
    "Basic", "Start", "End", "Stop", "Next", "Move", "Auto",
    "Open", "Close", "Delay", "Pause", "Previous", "Extended",
    "Messages", "Application", "Supported", "Enabled", "Fault",
    "Energy", "Count", "Duration",
    # Protocol/transport primitives (covered by protocol anchors instead)
    "On", "Off",
}
# Lower-cased version for fast lookup
_ABSOLUTE_BLOCKLIST_LOWER: Set[str] = {n.lower() for n in ABSOLUTE_BLOCKLIST}

# Names that are valid ONLY when the section/TC cluster matches the entity cluster.
# These are short (≤8 chars) or common-but-legitimate names.
# Names NOT in this set and not in the blocklist use the tiered length rules below.
_REQUIRE_CLUSTER_CONTEXT: Set[str] = {
    "ACL", "ARL", "Audio", "BSSID", "Boost", "Fair", "Leave",
    "Level", "Lift", "Latch", "Login", "Maps", "Mask", "NOCs",
    "Offer", "OnOff", "PanId", "Play", "Power", "RFID", "RSSI",
    "Radar", "Reset", "Rinse", "Scale", "Seek", "Sleep", "Speed",
    "Spin", "Tilt", "Video",
}
_REQUIRE_CLUSTER_CONTEXT_LOWER: Set[str] = {n.lower() for n in _REQUIRE_CLUSTER_CONTEXT}

# Length thresholds for cross-cluster matching (when context_cluster != entity.cluster)
_MIN_LEN_CROSS_CLUSTER_FREE   = 10   # ≥10 chars → match freely cross-cluster (was 12)
_MIN_LEN_CROSS_CLUSTER_UNIQUE = 7    # 7-9 chars → match only if name is unique to one cluster (was 9)


def _build_multicluster_name_set(canonical_schema: CanonicalSchema) -> Set[str]:
    """Return the set of lowercased entity names that appear in more than one cluster.

    Called once per extraction pass and cached in the helper below.
    """
    name_to_clusters: Dict[str, Set[str]] = {}
    for entity in canonical_schema.entity_lookup.values():
        name_lower = entity.name.lower()
        name_to_clusters.setdefault(name_lower, set()).add(entity.cluster)
    return {name for name, clusters in name_to_clusters.items() if len(clusters) > 1}


def _entity_name_allowed(
    name: str,
    entity_cluster: str,
    context_cluster: str,
    multicluster_names: Set[str],
) -> bool:
    """Return True if this entity name is a credible match given the context.

    Rules (applied in order):
    1. Absolute blocklist → always False
    2. Same cluster as context → always True (any name length)
    3. Requires cluster context and cluster doesn't match → False
    4. Name len ≥ MIN_LEN_CROSS_CLUSTER_FREE → True
    5. Name len in [MIN_LEN_CROSS_CLUSTER_UNIQUE, MIN_LEN_CROSS_CLUSTER_FREE) →
       True only if name is unique to one cluster
    6. Otherwise → False
    """
    name_lower = name.lower()

    # Rule 1
    if name_lower in _ABSOLUTE_BLOCKLIST_LOWER:
        return False

    # Rule 2 — same cluster: trust the match
    if context_cluster and entity_cluster.lower() == context_cluster.lower():
        return True

    # Rule 3 — short/risky names require cluster context
    if name_lower in _REQUIRE_CLUSTER_CONTEXT_LOWER:
        return False   # context_cluster didn't match (or is empty)

    # Rule 3b — names shared across multiple clusters are always context-bound.
    # Without this guard, long names like "SupportedModes" (14 chars) or "ChangeToMode"
    # (12 chars) would pass Rule 4 and match freely across mode-family clusters,
    # creating phantom TESTS edges from Mode Select TCs to Laundry Washer Mode etc.
    if name_lower in multicluster_names:
        return False   # context_cluster didn't match in Rule 2

    # Rule 4 — long names match freely (only reached for unique names now)
    if len(name) >= _MIN_LEN_CROSS_CLUSTER_FREE:
        return True

    # Rule 5 — medium names only if unique (redundant after Rule 3b, but kept for clarity)
    if len(name) >= _MIN_LEN_CROSS_CLUSTER_UNIQUE:
        return name_lower not in multicluster_names

    # Rule 6 — short names with no cluster context → reject
    return False


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class EntityMatch:
    """One entity found in a text span."""
    entity_id: str                    # CanonicalEntityRef.id or protocol anchor ID
    entity_type: EntityType
    name: str
    cluster: str = ""
    match_source: str = "canonical"   # "canonical" | "protocol_anchor"
    span_text: str = ""               # The matched substring


# ---------------------------------------------------------------------------
# A. detect_requirement_candidates
# ---------------------------------------------------------------------------

def detect_requirement_candidates(text: str) -> List[str]:
    """Split text into sentences and return those containing normative triggers.

    Args:
        text: Clean plain text (no HTML).

    Returns:
        List of sentences that contain at least one normative-trigger keyword.
    """
    sentences = _split_sentences(text)
    return [s for s in sentences if _NORMATIVE_TRIGGERS.search(s)]


# ---------------------------------------------------------------------------
# B. classify_requirement_type
# ---------------------------------------------------------------------------

_TYPE_WEIGHTS: Dict[RequirementType, float] = {
    RequirementType.TIMING_REQUIREMENT:        4.0,
    RequirementType.STATE_TRANSITION_RULE:     3.5,
    RequirementType.ENTITY_DEFINITION:         3.5,
    RequirementType.VALUE_CONSTRAINT:          3.5,
    RequirementType.CONDITIONAL_BEHAVIOR_RULE: 3.0,
    RequirementType.CROSS_ENTITY_DEPENDENCY:   2.0,
    RequirementType.GENERAL_NORMATIVE:         1.0,
    # Backward-compat aliases: kept in enum but never generated; zero weight
    RequirementType.LIFECYCLE_REQUIREMENT:         0.0,
    RequirementType.PROTOCOL_BEHAVIOR_REQUIREMENT: 0.0,
}
_AMBIGUITY_THRESHOLD = 0.70   # winner_score / (winner + runner-up) below this → ambiguous
_CONFIDENCE_FLOOR    = 0.30   # raw match count mapped to [floor, 1.0]


def classify_requirement_type(
    sentence: str,
    entity_refs: List[str],
    canonical_schema: CanonicalSchema,
) -> Tuple[RequirementType, float, bool, Dict[str, float], List[str], List[str]]:
    """Score a normative sentence across all requirement types.

    Scoring priority (highest weight wins):
        timing_requirement        4.0  — "shall terminate after 900 s"
        state_transition_rule     3.5  — "shall enter On state"
        entity_definition_changed 3.5  — "attribute indicates whether…"
        value_constraint          3.5  — "shall be in the range 0 to 254"
        conditional_behavior_rule 3.0  — "if Occupancy is false, shall not…"
        cross_entity_dependency   2.0  — "depends on cluster X"
        general_normative         1.0  — baseline (every sentence gets this)

    Tiebreak boosts applied AFTER primary signals:
        timing + protocol keyword → timing += 0.5  (prevents BLE ambiguity)
        lifecycle keyword         → state_transition += 1.0
        timing + conditional      → timing += 1.0  ("if X, stops after Y s")
        value_constraint + entity_definition → value_constraint += 0.5

    Returns:
        (winner_type, confidence, ambiguous, score_breakdown, signals, alternatives)
        - signals: short names of signal patterns that fired, e.g. ["timing_bound", "conditional_if"]
        - alternatives: runner-up type values (score > general_normative baseline and ≠ winner),
          sorted by score descending, e.g. ["conditional_behavior_rule"]
    """
    scores: Dict[RequirementType, float] = {t: 0.0 for t in RequirementType}

    # ── Primary signals ──────────────────────────────────────────────────────
    has_timing    = bool(_TIMING_RE.search(sentence))
    has_state     = bool(_STATE_TRANSITION_RE.search(sentence))
    has_cond      = bool(_CONDITIONAL_RE.search(sentence))
    has_lifecycle = bool(_LIFECYCLE_RE.search(sentence))
    has_protocol  = bool(_PROTOCOL_RE.search(sentence))
    has_entity_def  = bool(_ENTITY_DEFINITION_RE.search(sentence))
    has_value_constr = bool(_VALUE_CONSTRAINT_RE.search(sentence))
    has_cross_entity = bool(_CROSS_ENTITY_RE.search(sentence)) or _has_multi_cluster_refs(entity_refs)

    if has_timing:
        scores[RequirementType.TIMING_REQUIREMENT] += _TYPE_WEIGHTS[RequirementType.TIMING_REQUIREMENT]

    if has_state:
        scores[RequirementType.STATE_TRANSITION_RULE] += _TYPE_WEIGHTS[RequirementType.STATE_TRANSITION_RULE]

    if has_cond:
        scores[RequirementType.CONDITIONAL_BEHAVIOR_RULE] += _TYPE_WEIGHTS[RequirementType.CONDITIONAL_BEHAVIOR_RULE]

    if has_entity_def:
        scores[RequirementType.ENTITY_DEFINITION] += _TYPE_WEIGHTS[RequirementType.ENTITY_DEFINITION]

    if has_value_constr:
        scores[RequirementType.VALUE_CONSTRAINT] += _TYPE_WEIGHTS[RequirementType.VALUE_CONSTRAINT]

    if has_cross_entity:
        scores[RequirementType.CROSS_ENTITY_DEPENDENCY] += _TYPE_WEIGHTS[RequirementType.CROSS_ENTITY_DEPENDENCY]

    # Baseline: every normative sentence
    scores[RequirementType.GENERAL_NORMATIVE] += _TYPE_WEIGHTS[RequirementType.GENERAL_NORMATIVE]

    # ── Tiebreak boosts ──────────────────────────────────────────────────────
    # Timing + protocol keyword: the protocol context confirms a timing bound
    # (e.g. "BLE advertisement shall terminate after 900 s").  Boost timing so
    # it wins decisively over CONDITIONAL and GENERAL.
    if has_timing and has_protocol:
        scores[RequirementType.TIMING_REQUIREMENT] += 0.5

    # Timing + conditional: "if X, shall complete within Y s" → timing wins
    if has_timing and has_cond:
        scores[RequirementType.TIMING_REQUIREMENT] += 1.0

    # Lifecycle trigger → this is really a state-transition scenario
    if has_lifecycle:
        scores[RequirementType.STATE_TRANSITION_RULE] += 1.0

    # Value-constraint sentences that also have definitional verbs (e.g. "stores
    # the hue value in range 0-254") → value_constraint wins over entity_def
    if has_value_constr and has_entity_def:
        scores[RequirementType.VALUE_CONSTRAINT] += 0.5

    # ── Pick winner ──────────────────────────────────────────────────────────
    # Exclude the zero-weight backward-compat aliases from competition
    _active_types = [
        RequirementType.TIMING_REQUIREMENT,
        RequirementType.STATE_TRANSITION_RULE,
        RequirementType.ENTITY_DEFINITION,
        RequirementType.VALUE_CONSTRAINT,
        RequirementType.CONDITIONAL_BEHAVIOR_RULE,
        RequirementType.CROSS_ENTITY_DEPENDENCY,
        RequirementType.GENERAL_NORMATIVE,
    ]
    sorted_types = sorted(
        [(t, scores[t]) for t in _active_types],
        key=lambda kv: kv[1], reverse=True,
    )
    winner_type, winner_score = sorted_types[0]
    runner_score = sorted_types[1][1] if len(sorted_types) > 1 else 0.0

    total = sum(v for t, v in sorted_types if v > 0)
    confidence = min(1.0, winner_score / max(total, 1.0))
    confidence = max(_CONFIDENCE_FLOOR, confidence)

    ambiguous = False
    if runner_score > 0 and (winner_score / (winner_score + runner_score)) < _AMBIGUITY_THRESHOLD:
        ambiguous = True

    score_breakdown = {t.value: round(scores[t], 3) for t in _active_types}

    # ── Build signals list ───────────────────────────────────────────────────
    # Short human-readable names for each boolean signal that fired.
    signals: List[str] = []
    if has_timing:       signals.append("timing_bound")
    if has_state:        signals.append("state_transition")
    if has_cond:         signals.append("conditional_if")
    if has_lifecycle:    signals.append("lifecycle_keyword")
    if has_protocol:     signals.append("protocol_keyword")
    if has_entity_def:   signals.append("entity_definition")
    if has_value_constr: signals.append("value_constraint_pattern")
    if has_cross_entity: signals.append("cross_entity_dep")

    # ── Build alternatives list ──────────────────────────────────────────────
    # Include runner-up types that scored above the general_normative baseline
    # (score > 1.0) and are not the winner — these are the competing hypotheses.
    _general_baseline = _TYPE_WEIGHTS[RequirementType.GENERAL_NORMATIVE]
    alternatives: List[str] = [
        t.value for t, s in sorted_types[1:]
        if s > _general_baseline and t != RequirementType.GENERAL_NORMATIVE
    ]

    return winner_type, confidence, ambiguous, score_breakdown, signals, alternatives


def _has_multi_cluster_refs(entity_refs: List[str]) -> bool:
    clusters: Set[str] = set()
    for ref in entity_refs:
        parts = ref.split("::")
        if len(parts) >= 2:
            clusters.add(parts[1])
    return len(clusters) >= 2


# ---------------------------------------------------------------------------
# C. extract_entities
# ---------------------------------------------------------------------------

# Protocol anchor node IDs — used when no canonical entity matches
_PROTOCOL_ANCHORS: List[Tuple[str, str, str]] = [
    # (anchor_id, display_name, regex_pattern)
    ("PROTO::BLE",                "BLE / Bluetooth LE",
     r"\b(ble|bluetooth\s+low\s+energy|bt\s+le)\b"),
    ("PROTO::Commissioning",      "Commissioning",
     r"\b(commission(?:ing|ed)?)\b"),
    ("PROTO::DNS-SD",             "DNS-SD / mDNS",
     r"\b(dns-sd|mdns|multicast\s+dns)\b"),
    ("PROTO::QRCode",             "QR Code / Setup Payload",
     r"\b(qr\s+code|setup\s+(?:code|payload)|manual\s+entry\s+code)\b"),
    ("PROTO::NFC",                "NFC",
     r"\b(nfc\s+tag?|near\s+field\s+communication)\b"),
    ("PROTO::Discriminator",      "Discriminator",
     r"\b(discriminator)\b"),
    ("PROTO::Passcode",           "Passcode",
     r"\b(passcode|setup\s+pin)\b"),
    ("PROTO::OperationalCreds",   "Operational Credentials",
     r"\b(noc|icac|rcac|csr|nocsr|operational\s+credentials?)\b"),
    ("PROTO::Attestation",        "Attestation",
     r"\b(attestation|dac|pai|paa|device\s+attestation)\b"),
    ("PROTO::Subscription",       "Subscription / Reporting",
     r"\b(subscription|reporting|subscribe(?:d)?|report\s+interval)\b"),
    ("PROTO::Fabric",             "Fabric",
     r"\b(fabric|multi-fabric)\b"),
    ("PROTO::Discovery",          "Discovery",
     r"\b(discover(?:y|ed|able)?|advertis(?:e|ing|ement))\b"),
]
_COMPILED_ANCHORS = [
    (aid, name, re.compile(pat, re.I)) for aid, name, pat in _PROTOCOL_ANCHORS
]


def extract_entities(
    text: str,
    canonical_schema: CanonicalSchema,
    context_cluster: str = "",
) -> List[EntityMatch]:
    """Return canonical entities found in ``text``, then protocol anchors.

    Args:
        text:            Clean plain text (no HTML).
        canonical_schema: Full schema for entity lookup.
        context_cluster: The cluster name that owns the section/TC being parsed.
                         Used to favour same-cluster entity matches and reject
                         cross-cluster false positives for short/generic names.

    Matching rules (see ``_entity_name_allowed`` for full logic):
    - Absolute blocklist names → never matched
    - Same-cluster names → matched at any length
    - Short/risky names from other clusters → rejected
    - Long names (≥12 chars) from other clusters → matched freely
    - Medium names (9-11 chars) from other clusters → only if unique to one cluster
    """
    text_lower = text.lower()
    found: List[EntityMatch] = []
    seen_ids: Set[str] = set()
    multicluster = _build_multicluster_name_set(canonical_schema)

    # ── 1. Canonical schema: clusters ───────────────────────────────────────
    for cluster in canonical_schema.clusters:
        cname_lower = cluster.name.lower()
        if len(cname_lower) < 3:
            continue
        if not re.search(r'\b' + re.escape(cname_lower) + r'\b', text_lower):
            continue
        if cluster.id in seen_ids:
            continue
        # Cluster names are multi-word and long — apply same guard
        if not _entity_name_allowed(cluster.name, cluster.name, context_cluster, multicluster):
            continue
        seen_ids.add(cluster.id)
        found.append(EntityMatch(
            entity_id=cluster.id,
            entity_type=EntityType.CLUSTER,
            name=cluster.name,
            cluster=cluster.name,
            match_source="canonical",
            span_text=cluster.name,
        ))

    # ── 2. Canonical schema: entities ───────────────────────────────────────
    for eid, entity in canonical_schema.entity_lookup.items():
        ename_lower = entity.name.lower()
        if len(ename_lower) < 3:
            continue
        if not re.search(r'\b' + re.escape(ename_lower) + r'\b', text_lower):
            continue
        if eid in seen_ids:
            continue
        if not _entity_name_allowed(entity.name, entity.cluster, context_cluster, multicluster):
            continue
        seen_ids.add(eid)
        found.append(EntityMatch(
            entity_id=eid,
            entity_type=entity.entity_type,
            name=entity.name,
            cluster=entity.cluster,
            match_source="canonical",
            span_text=entity.name,
        ))

    # ── 3. Protocol anchors ──────────────────────────────────────────────────
    for anchor_id, anchor_name, anchor_re in _COMPILED_ANCHORS:
        if anchor_re.search(text):
            if anchor_id not in seen_ids:
                seen_ids.add(anchor_id)
                found.append(EntityMatch(
                    entity_id=anchor_id,
                    entity_type=EntityType.CLUSTER,
                    name=anchor_name,
                    cluster="",
                    match_source="protocol_anchor",
                    span_text=anchor_name,
                ))

    return found


# ---------------------------------------------------------------------------
# D. extract_conditions_and_effects
# ---------------------------------------------------------------------------

def extract_conditions_and_effects(
    sentence: str,
    entity_refs: List[str],
    entity_name_map: Dict[str, Any],
    context_cluster: str = "",
    multicluster_names: Optional[Set[str]] = None,
) -> Tuple[List[ConditionRecord], List[EffectRecord], List[ConstraintRecord]]:
    """Extract structured condition/effect/constraint records from a sentence.

    ``entity_name_map`` maps lowercase entity name → CanonicalEntityRef and is
    typically pre-built once per document by the spec extractor.
    """
    conditions: List[ConditionRecord] = []
    effects: List[EffectRecord] = []
    constraints: List[ConstraintRecord] = []

    for m in _CONDITION_CLAUSE_RE.finditer(sentence):
        clause = m.group(1).strip()
        refs = match_entities_from_map(clause, entity_name_map, context_cluster, multicluster_names)
        conditions.append(ConditionRecord(text=clause, entity_refs=refs))

    for m in _EFFECT_CLAUSE_RE.finditer(sentence):
        clause = m.group(1).strip()
        refs = match_entities_from_map(clause, entity_name_map, context_cluster, multicluster_names)
        effects.append(EffectRecord(text=clause, entity_refs=refs))

    for m in _CONSTRAINT_CLAUSE_RE.finditer(sentence):
        clause = m.group(1).strip()
        attr_ref = next((r for r in entity_refs if r.startswith("ATTRIBUTE::")), "")
        constraints.append(ConstraintRecord(text=clause, attribute_ref=attr_ref))

    return conditions, effects, constraints


def match_entities_from_map(
    text: str,
    entity_name_map: Dict[str, Any],
    context_cluster: str = "",
    multicluster_names: Optional[Set[str]] = None,
) -> List[str]:
    """Fast entity ID lookup using a pre-built name→entity map.

    ``entity_name_map`` maps lower-cased names to either a single
    ``CanonicalEntityRef`` (legacy) or a ``List[CanonicalEntityRef]``
    (multi-cluster support).  When a name appears in multiple clusters,
    all variants are checked so the one matching ``context_cluster``
    can pass the quality guard.
    """
    text_lower = text.lower()
    matched: List[str] = []
    seen: Set[str] = set()
    mc = multicluster_names or set()

    for name_lower, entity_or_list in entity_name_map.items():
        if len(name_lower) < 3:
            continue
        if not re.search(r'\b' + re.escape(name_lower) + r'\b', text_lower):
            continue
        entities = entity_or_list if isinstance(entity_or_list, list) else [entity_or_list]
        for entity in entities:
            if entity.id in seen:
                continue
            if not _entity_name_allowed(entity.name, entity.cluster, context_cluster, mc):
                continue
            seen.add(entity.id)
            matched.append(entity.id)

    return matched


# ---------------------------------------------------------------------------
# E. classify_testcase_mode
# ---------------------------------------------------------------------------

def classify_testcase_mode(
    tc_id: str,
    title: str,
    purpose: str,
    procedure_steps: List[str],
    entity_refs: List[str],
) -> Tuple[TestMode, float, float]:
    """Score a test case for cluster-centric vs. protocol-behavior mode.

    Returns:
        (mode, cluster_score, protocol_score)  scores are in [0, ∞)
    """
    # Protocol-family override: TC-ID prefix wins unconditionally.
    # IDM/SC/BDX etc. test the protocol stack, not individual clusters.
    # Their text naturally contains "attribute/command/invoke" which would
    # otherwise push the heuristic toward cluster_centric.
    m_prefix = re.match(r"^TC-([A-Z0-9]+)-", tc_id, re.I)
    if m_prefix and _is_protocol_prefix(m_prefix.group(1)):
        return TestMode.PROTOCOL_BEHAVIOR_CENTRIC, 0.0, 1.0

    combined = " ".join([tc_id, title, purpose] + procedure_steps)

    cluster_hits = len(_CLUSTER_CENTRIC_RE.findall(combined))
    protocol_hits = len(_PROTOCOL_MODE_RE.findall(combined))

    # Entity refs — canonical entity refs indicate cluster-centric
    canonical_refs = sum(1 for r in entity_refs if not r.startswith("PROTO::"))
    proto_refs = sum(1 for r in entity_refs if r.startswith("PROTO::"))

    cluster_score = cluster_hits * 1.0 + canonical_refs * 0.5
    protocol_score = protocol_hits * 1.0 + proto_refs * 0.5

    total = cluster_score + protocol_score
    if total == 0:
        return TestMode.AMBIGUOUS, 0.0, 0.0

    ratio = cluster_score / total
    if ratio >= 0.65:
        mode = TestMode.CLUSTER_CENTRIC
    elif ratio <= 0.35:
        mode = TestMode.PROTOCOL_BEHAVIOR_CENTRIC
    else:
        mode = TestMode.MIXED

    return mode, cluster_score, protocol_score


# ---------------------------------------------------------------------------
# F. detect_test_intents
# ---------------------------------------------------------------------------

def detect_test_intents(
    title: str,
    purpose: str,
    procedure_steps: List[str],
) -> List[TestIntent]:
    """Return the ordered list of TestIntent values found in the test case text.

    Each pattern is matched independently; order is determined by the pattern
    list order (highest-signal first).  Deduplication preserves first occurrence.
    """
    combined = " ".join([title, purpose] + procedure_steps)
    intents: List[TestIntent] = []
    seen: Set[TestIntent] = set()
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(combined) and intent not in seen:
            intents.append(intent)
            seen.add(intent)
    return intents


# ---------------------------------------------------------------------------
# G.1  Attribute-validation edge refinement
# ---------------------------------------------------------------------------

# Ordered patterns used to pick the most specific validation edge type when a TC
# has VALIDATE_RESPONSE intent toward an ATTRIBUTE entity.  First match wins.
_ATTR_VALIDATION_PATTERNS: List[Tuple[re.Pattern, GraphEdgeType]] = [
    (re.compile(r"\bdata.?type\b|\btype\s+is\b|\btype\s+of\b|\bdata\s+type\b",   re.I), GraphEdgeType.VALIDATES_TYPE),
    (re.compile(r"\brange\b|\bmin(?:imum)?\b|\bmax(?:imum)?\b|\bbound(?:ary)?\b", re.I), GraphEdgeType.VALIDATES_RANGE),
    (re.compile(r"\bdefault\b",                                                    re.I), GraphEdgeType.VALIDATES_DEFAULT),
    (re.compile(r"\bnull(?:able)?\b",                                              re.I), GraphEdgeType.VALIDATES_QUIETER_REPORTING),
    (re.compile(r"\benum\b|\benumerat\w+\b",                                       re.I), GraphEdgeType.VALIDATES_ENUM),
    (re.compile(r"\baccess\b|\bACL\b|\bAdmin\b|\bView\b|\bOperate\b",             re.I), GraphEdgeType.VALIDATES_ACCESS),
    (re.compile(r"\bconform(?:ance)?\b|\bPICS\b|\bfeature\s+map\b",               re.I), GraphEdgeType.VALIDATES_CONFORMANCE),
]


def _infer_attribute_validation_edge(tc: "TestCaseRecord", entity_name: str) -> GraphEdgeType:
    """Refine VERIFIES_ATTRIBUTE → a precise validation edge type.

    Scans procedure steps (and purpose) for keywords that indicate *which*
    attribute property the TC is actually checking.  If no pattern matches,
    falls back to ``VERIFIES_ATTRIBUTE``.
    """
    text = " ".join(tc.procedure_steps + [tc.purpose, tc.setup])
    entity_lower = entity_name.lower()
    # Prefer sentences that directly mention the entity name
    sentences = text.split(".")
    relevant = [s for s in sentences if entity_lower in s.lower()]
    search_text = " ".join(relevant) if relevant else text
    for pattern, edge_type in _ATTR_VALIDATION_PATTERNS:
        if pattern.search(search_text):
            return edge_type
    return GraphEdgeType.VERIFIES_ATTRIBUTE


# ---------------------------------------------------------------------------
# G. infer_graph_edges  (typed TC → entity edges)
# ---------------------------------------------------------------------------

# Map TestIntent → preferred edge type toward the *entity* node
_INTENT_TO_EDGE: Dict[TestIntent, GraphEdgeType] = {
    TestIntent.READ_ATTRIBUTE:                    GraphEdgeType.READS,             # was VERIFIES_ATTRIBUTE
    TestIntent.WRITE_ATTRIBUTE:                   GraphEdgeType.WRITES,            # was VERIFIES_ATTRIBUTE
    TestIntent.INVOKE_COMMAND:                    GraphEdgeType.TESTS_COMMAND,
    TestIntent.VALIDATE_RESPONSE:                 GraphEdgeType.VERIFIES_ATTRIBUTE,  # refined further by _infer_attribute_validation_edge
    TestIntent.VALIDATE_ERROR:                    GraphEdgeType.NEGATIVE_TESTS,    # was TESTS_COMMAND
    TestIntent.OBSERVE_EVENT:                     GraphEdgeType.OBSERVES_EVENT,
    TestIntent.VALIDATE_TIMING:                   GraphEdgeType.VERIFIES_RULE,
    TestIntent.VALIDATE_STATE_TRANSITION:         GraphEdgeType.VERIFIES_RULE,
    TestIntent.VALIDATE_CROSS_ENTITY_DEPENDENCY:  GraphEdgeType.DEPENDS_ON,        # was IN_CONTEXT
    TestIntent.VALIDATE_COMMISSIONING_FLOW:       GraphEdgeType.IN_CONTEXT,
    TestIntent.VALIDATE_DISCOVERY_BEHAVIOR:       GraphEdgeType.IN_CONTEXT,
    TestIntent.VALIDATE_ONBOARDING_PAYLOAD:       GraphEdgeType.IN_CONTEXT,
    TestIntent.VALIDATE_BEHAVIOR_RULE:            GraphEdgeType.VERIFIES_RULE,
    TestIntent.NEGATIVE_SCENARIO:                 GraphEdgeType.NEGATIVE_TESTS,    # was TESTS_COMMAND
}

# Map EntityType → best edge when no intent override available
_ENTITY_TYPE_TO_EDGE: Dict[EntityType, GraphEdgeType] = {
    EntityType.ATTRIBUTE: GraphEdgeType.VERIFIES_ATTRIBUTE,
    EntityType.COMMAND:   GraphEdgeType.TESTS_COMMAND,
    EntityType.EVENT:     GraphEdgeType.OBSERVES_EVENT,
    EntityType.FEATURE:   GraphEdgeType.VERIFIES_RULE,
    EntityType.CLUSTER:   GraphEdgeType.TESTS,
}


def infer_graph_edges(
    tc: TestCaseRecord,
    spec_records: List[Any],         # List[SpecRecord] — typed as Any to avoid circular import
    entity_lookup: Dict[str, CanonicalEntityRef],
) -> List[GraphEdgeRecord]:
    """Build typed edges from a test case to its related graph nodes.

    Edge types are chosen based on the TC's detected intents and the entity
    types of the referenced nodes.  Falls back to TESTS for unknown combos.

    Args:
        tc:            The TestCaseRecord being processed.
        spec_records:  All SpecRecords (used for VERIFIES_REQUIREMENT edges).
        entity_lookup: canonical_schema.entity_lookup for entity-type resolution.

    Returns:
        List of GraphEdgeRecord (deduped by (src, tgt, type)).
    """
    edges: List[GraphEdgeRecord] = []
    seen: Set[tuple] = set()

    def _add(src: str, tgt: str, et: GraphEdgeType, **props) -> None:
        key = (src, tgt, et)
        if key not in seen:
            seen.add(key)
            edges.append(GraphEdgeRecord(source=src, target=tgt, edge_type=et, properties=props))

    intents = tc.intents  # already populated by test_plan_extractor via detect_test_intents

    # ── TC → entity refs (typed) ─────────────────────────────────────────────
    for ref in tc.entity_refs:
        entity = entity_lookup.get(ref)
        if entity is None:
            # CLUSTER:: refs come from entity_name_map (cluster names in TC text).
            # entity_lookup only holds attribute/command/event/feature IDs, so
            # CLUSTER:: IDs are always "missing" here — use TESTS, not IN_CONTEXT.
            if ref.startswith("CLUSTER::"):
                _add(tc.id, ref, GraphEdgeType.TESTS)
            else:
                # Protocol anchor or truly unknown ref
                _add(tc.id, ref, GraphEdgeType.IN_CONTEXT)
            continue

        # Pick best edge type: prefer intent-derived, fallback to entity-type
        chosen_edge = GraphEdgeType.TESTS
        for intent in intents:
            candidate = _INTENT_TO_EDGE.get(intent)
            if candidate:
                # Only use intent edge if it makes sense for this entity type
                if _intent_edge_matches_entity(intent, entity.entity_type):
                    chosen_edge = candidate
                    break
        if chosen_edge == GraphEdgeType.TESTS:
            chosen_edge = _ENTITY_TYPE_TO_EDGE.get(entity.entity_type, GraphEdgeType.TESTS)

        # ── Fix: verifies_attribute should never point to a COMMAND node ────────
        # VALIDATE_RESPONSE intent maps to VERIFIES_ATTRIBUTE, but when the target
        # entity is a COMMAND the correct edge type is TESTS_COMMAND.
        if chosen_edge == GraphEdgeType.VERIFIES_ATTRIBUTE and entity.entity_type == EntityType.COMMAND:
            chosen_edge = GraphEdgeType.TESTS_COMMAND

        # ── Refine VERIFIES_ATTRIBUTE further for VALIDATE_RESPONSE + ATTRIBUTE ──
        # When a TC validates an attribute response, scan its procedure text to
        # determine *which* property it validates (type, range, default, etc.).
        if (
            chosen_edge == GraphEdgeType.VERIFIES_ATTRIBUTE
            and entity.entity_type == EntityType.ATTRIBUTE
            and TestIntent.VALIDATE_RESPONSE in intents
        ):
            entity_name = ref.split("::")[-1]  # e.g. "ATTRIBUTE::On/Off::OnOff" → "OnOff"
            chosen_edge = _infer_attribute_validation_edge(tc, entity_name)

        _add(tc.id, ref, chosen_edge)

    # ── TC → spec requirements (VERIFIES_REQUIREMENT — only to REQ:: nodes) ────
    for spec_ref in tc.spec_refs:
        # Guard: only create VERIFIES_REQUIREMENT to actual requirement node IDs
        if spec_ref.startswith("REQ::") or spec_ref.startswith("REQUIREMENT::"):
            _add(tc.id, spec_ref, GraphEdgeType.VERIFIES_REQUIREMENT)
        else:
            # Fall back to IN_CONTEXT for protocol anchors or section IDs
            _add(tc.id, spec_ref, GraphEdgeType.IN_CONTEXT)

    # ── TC → cluster (TESTS) ─────────────────────────────────────────────────
    if tc.cluster:
        cluster_id = f"CLUSTER::{tc.cluster}"
        _add(tc.id, cluster_id, GraphEdgeType.TESTS)

    # ── PICS gating edges ────────────────────────────────────────────────────
    combined_tc_text = " ".join([tc.purpose, tc.prerequisites, tc.setup] + tc.procedure_steps)
    for m in _PICS_RE.finditer(combined_tc_text):
        pics_ref = m.group(1).strip()
        # PICS refs are virtual nodes; use GATED_BY_PICS
        _add(tc.id, f"PICS::{pics_ref}", GraphEdgeType.GATED_BY_PICS)

    return edges


def _intent_edge_matches_entity(intent: TestIntent, entity_type: EntityType) -> bool:
    """True when the intent-derived edge is semantically consistent with the entity type."""
    mapping = {
        TestIntent.READ_ATTRIBUTE:   {EntityType.ATTRIBUTE},
        TestIntent.WRITE_ATTRIBUTE:  {EntityType.ATTRIBUTE},
        TestIntent.INVOKE_COMMAND:   {EntityType.COMMAND},
        TestIntent.VALIDATE_ERROR:   {EntityType.COMMAND, EntityType.ATTRIBUTE},
        TestIntent.OBSERVE_EVENT:    {EntityType.EVENT},
        TestIntent.VALIDATE_RESPONSE:{EntityType.ATTRIBUTE, EntityType.COMMAND},
        TestIntent.NEGATIVE_SCENARIO:{EntityType.COMMAND, EntityType.ATTRIBUTE, EntityType.EVENT},
    }
    allowed = mapping.get(intent)
    if allowed is None:
        return True   # generic intents apply to anything
    return entity_type in allowed


# ---------------------------------------------------------------------------
# H. filter_invalid_requirement_candidates
# ---------------------------------------------------------------------------

# Glossary / conformance-table row pattern: "TERM | a key word that …"
_GLOSSARY_PIPE_RE = re.compile(
    r"^\s*\w[\w\s/\-]{0,40}\|\s*(?:[Aa]\s+key\s+word|[Aa]\s+term|normative|informative"
    r"|indicates?\s+that|is\s+used|means?\s+that)",
    re.I,
)
# Pure pipe-delimited table row with ≥3 columns (raw table cell dump)
_MULTI_PIPE_RE = re.compile(r"^[^|]{0,80}\|[^|]{1,80}\|[^|]{1,80}")
# Sentence that starts with SHALL/MUST with no subject (fragment)
_FRAGMENT_STARTS_MODAL_RE = re.compile(r"^\s*(shall|must|should|may)\b", re.I)
# Strip leading section numbers from a section title (e.g. "1.3.7.5.1. Effect on Receipt")
_FRAG_SEC_NUM_RE = re.compile(r"^\s*[\d]+(?:\.[\d]+)*\.?\s*")
# Known conformance-table prefixes used in CSA specs
_CONFORMANCE_TABLE_RE = re.compile(
    r"^\s*(M|O|P|D|C\[[^\]]{1,60}\]|desc|feature\s+[A-Z])\s*[|,]",
    re.I,
)
# Numeric table row prefix "1 | something | something"
_NUMERIC_ROW_RE = re.compile(r"^\s*\d{1,3}\s*\|")
# Continuation fragment: starts with a plain lowercase word (mid-sentence continuation from
# HTML paragraph boundary splits). The Matter spec always capitalizes sentence starts.
# We allow: sentences starting with a paragraph-ID prefix "[9.123]" (added by our parser),
# or CamelCase entity names — these are safe. Rejects: "the ...", "if ...", "element ...",
# "attribute ...", "and ...", etc.
_LOWERCASE_CONTINUATION_RE = re.compile(r"^[a-z][a-z0-9_\-]*\b")
# Modal verb in a short sentence — used to rescue short but complete normative requirements
_SHORT_MODAL_RE = re.compile(r"\b(shall|must|should|may)\b", re.I)
# Broader normative-word pattern — used when checking table-fragment tails (e.g. "is mandatory",
# "are required", "is prohibited") that don't use explicit SHALL/MUST wording.
# Sentences ending with a dangling conjunction/preposition/article — these are split fragments
# where the HTML parser hit a section boundary mid-sentence.
_INCOMPLETE_TAIL_RE = re.compile(
    r'\b(that|which|and|or|but|as|if|when|where|whose|of|in|a|an|the|with|by|to|for|from)\s*[,.]?\s*$',
    re.I,
)
# "This|The (attribute|field|command|event|cluster) SHALL (indicate|contain|represent|...)" —
# entity-definition sentences that name a field and say what it means.  These are schema
# annotations, not behavioral requirements that need separate test coverage.
_FIELD_DEFN_RE = re.compile(
    r'^(?:This|The)\s+(?:attribute|field|command|event|cluster|value|entry)\s+'
    r'(?:SHALL|MUST)\s+(?:indicate|contain|represent|be\s+set\s+to|show|provide|identify|specify)\b',
    re.I,
)
_NORMATIVE_WORD_RE = re.compile(
    r"\b(shall|must|should|may|mandatory|required|prohibited|forbidden)\b", re.I
)
# Extract text after the last pipe character in a table fragment row
_AFTER_LAST_PIPE_RE = re.compile(r"[|][^|]+$")


def filter_invalid_requirement_candidates(
    sentences: List[str],
    source_section: str = "",
    min_len: int = 30,
) -> Tuple[List[str], List["RejectedCandidate"]]:
    """Filter a list of normative candidate sentences for quality.

    Returns (valid_sentences, rejected_candidates).

    A sentence is rejected if it is:
    - Too short to express a complete requirement
    - A glossary table row ("SHALL | A key word that indicates …")
    - A raw pipe-delimited table dump with ≥3 columns
    - A conformance-table cell prefix (M, O, P, desc, …)
    - A numeric table row "1 | Device Manufacturer | …"
    - A modal fragment with no subject ("Shall be set to …")

    Sentences are *not* rejected purely for low entity count — short clauses
    with valid normative verbs are kept so that downstream classification can
    assign them lower confidence.
    """
    valid: List[str] = []
    rejected: List["RejectedCandidate"] = []

    for s in sentences:
        s_stripped = s.strip()

        if len(s_stripped) < min_len:
            # Rescue short but syntactically complete normative requirements.
            # Case A — subject present, starts with non-modal word, ≥4 words:
            #   "SHALL be editable by the user" is excluded because it starts with SHALL;
            #   those go to Case B below.
            if (
                _SHORT_MODAL_RE.search(s_stripped)
                and "|" not in s_stripped
                and not _FRAGMENT_STARTS_MODAL_RE.match(s_stripped)
                and len(s_stripped.split()) >= 4
            ):
                valid.append(s)
                continue
            # Case B — starts with a modal (no explicit subject), ≥4 words:
            #   Reconstruct using the section heading as an implicit subject,
            #   e.g. "Effect on Receipt: SHALL stop the EVSE charging."
            if (
                _FRAGMENT_STARTS_MODAL_RE.match(s_stripped)
                and "|" not in s_stripped
                and len(s_stripped.split()) >= 4
                and source_section
            ):
                subject = _FRAG_SEC_NUM_RE.sub("", source_section).strip()
                if subject:
                    valid.append(f"{subject}: {s_stripped}")
                    continue
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="too_short", source_section=source_section,
            ))
            continue

        if _GLOSSARY_PIPE_RE.match(s_stripped):
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="glossary_row", source_section=source_section,
            ))
            continue

        if _MULTI_PIPE_RE.match(s_stripped):
            # Before discarding the whole row, check if the text after the last pipe
            # contains an embedded normative requirement sentence.
            _tail_match = _AFTER_LAST_PIPE_RE.search(s_stripped)
            if _tail_match:
                _tail = _tail_match.group(0)[1:].strip()  # strip leading "|"
                # Use the broader _NORMATIVE_WORD_RE so that tails like
                # "...is mandatory to maintain backwards compatibility..." and
                # "...A commissioner is required to show..." are also rescued.
                if len(_tail) >= min_len and _NORMATIVE_WORD_RE.search(_tail):
                    # Extract the embedded requirement from the table notes column
                    valid.append(_tail)
                    continue
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="table_fragment", source_section=source_section,
            ))
            continue

        if _CONFORMANCE_TABLE_RE.match(s_stripped):
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="conformance_table_cell", source_section=source_section,
            ))
            continue

        if _NUMERIC_ROW_RE.match(s_stripped):
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="numeric_table_row", source_section=source_section,
            ))
            continue

        if _LOWERCASE_CONTINUATION_RE.match(s_stripped):
            # Starts with a plain lowercase word — almost certainly a mid-sentence fragment
            # from an HTML section-boundary split (e.g. "element in EnergyPriorities , the
            # new value of CurrentEnergyBalance SHALL be...").  The Matter spec invariably
            # capitalises sentence-initial words; lowercase-start sentences are continuations.
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="lowercase_fragment", source_section=source_section,
            ))
            continue

        if _FRAGMENT_STARTS_MODAL_RE.match(s_stripped):
            # Sentence starts with SHALL/MUST/SHOULD/MAY with no explicit subject.
            # This typically comes from a table cell where the subject is the row header
            # or the enclosing section title.  Reconstruct a usable sentence by prepending
            # the section title (stripped of its numbering prefix) as an implicit subject
            # rather than discarding the requirement entirely.
            if source_section:
                # Strip leading section numbers like "1.3.7.5.1. " from the section path
                subject = _FRAG_SEC_NUM_RE.sub("", source_section).strip()
                if subject:
                    valid.append(f"{subject}: {s_stripped}")
                    continue
            # No section context available — discard as fragment
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="modal_fragment_no_subject", source_section=source_section,
            ))
            continue

        if _INCOMPLETE_TAIL_RE.search(s_stripped):
            # Ends with a dangling conjunction/preposition/article — split fragment
            # from an HTML section boundary hit mid-sentence.
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="incomplete_tail_fragment", source_section=source_section,
            ))
            continue

        if _FIELD_DEFN_RE.match(s_stripped):
            # "This attribute SHALL indicate..." — entity schema annotation, not a
            # behavioral requirement needing standalone test coverage.
            rejected.append(RejectedCandidate(
                text=s_stripped, reason="entity_definition", source_section=source_section,
            ))
            continue

        valid.append(s)

    return valid, rejected


# ---------------------------------------------------------------------------
# I. extract_protocol_areas
# ---------------------------------------------------------------------------

# Section-number prefix stripper (reused here)
_SEC_NUM_RE = re.compile(r"^\s*[\d]+(?:\.[\d]+)*\.?\s*")

# Words that are part of cluster names (not protocol areas)
_CLUSTER_SUFFIX_RE = re.compile(
    r"\b(cluster|attribute|command|event|feature)\b", re.I
)


def extract_protocol_areas(section_path: str) -> List[str]:
    """Derive PROTOCOL_AREA node IDs from a section breadcrumb path.

    Input: "3 Clusters > 3.2 On/Off Cluster > 3.2.5 Attributes"
    Output: ["PROTOCOL_AREA::Clusters", "PROTOCOL_AREA::On_Off_Cluster",
             "PROTOCOL_AREA::Attributes"]

    Cluster-specific segments (containing "Cluster", "Attribute", etc.) are
    kept as protocol area segments since they still represent a hierarchy level
    even when they correspond to a data-model cluster.

    Returns an ordered list from outermost to innermost.
    """
    if not section_path:
        return []

    segments = [s.strip() for s in section_path.split(">") if s.strip()]
    ids: List[str] = []
    for seg in segments:
        # Strip leading numbering
        clean = _SEC_NUM_RE.sub("", seg).strip()
        if not clean or len(clean) < 3:
            continue
        # Normalize to a stable ID: replace spaces/slashes/hyphens with underscores
        slug = re.sub(r"[^A-Za-z0-9]+", "_", clean).strip("_")
        if slug:
            ids.append(f"PROTOCOL_AREA::{slug}")

    return ids


# ---------------------------------------------------------------------------
# J. extract_behavior_hints
# ---------------------------------------------------------------------------

# Patterns that suggest a named device behaviour
_BEHAVIOR_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("Power_Cycling",
     re.compile(r"\b(power(?:ed)?\s+(?:off|on|cycle|cycling)|power-cycling)\b", re.I)),
    ("BLE_Advertisement",
     re.compile(r"\b(ble\s+advert|bluetooth.*advert|advert(?:ise|ising|isement))\b", re.I)),
    ("Device_Discovery",
     re.compile(r"\b(device\s+discov|dns-sd|mdns|discoverability)\b", re.I)),
    ("Commissioning_Mode_Entry",
     re.compile(r"\b(commissioning\s+mode|enter.*commissioning|open.*commissioning)\b", re.I)),
    ("Commissioning_Flow",
     re.compile(r"\b(commissioning\s+(?:flow|process|procedure|step))\b", re.I)),
    ("Device_Blinking",
     re.compile(r"\b(blink(?:ing|s)?)\b", re.I)),
    ("Device_Reset",
     re.compile(r"\b(factory\s+reset|hard\s+reset|reboot|power\s+loss)\b", re.I)),
    ("Subscription_Reporting",
     re.compile(r"\b(subscription|report\s+interval|min\s+interval|max\s+interval)\b", re.I)),
    ("Fabric_Join",
     re.compile(r"\b(join(?:ing)?\s+fabric|add(?:ed)?\s+to\s+fabric)\b", re.I)),
    ("IPv4_Tolerance",
     re.compile(r"\b(ipv4\s+(?:coexistence|tolerance|traffic)|ipv4)\b", re.I)),
]


def extract_behavior_hints(text: str) -> List[str]:
    """Return BEHAVIOR node IDs inferred from procedure/behavior keywords in text."""
    ids: List[str] = []
    for name, pattern in _BEHAVIOR_PATTERNS:
        if pattern.search(text):
            ids.append(f"BEHAVIOR::{name}")
    return ids


# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> List[str]:
    """Split plain text into individual sentences."""
    # Split on sentence-ending punctuation followed by whitespace + capital letter
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    sentences = []
    for part in parts:
        for sub in part.split("\n"):
            sub = sub.strip()
            if sub:
                sentences.append(sub)
    return sentences


# ---------------------------------------------------------------------------
# K. extract_pr_requirements  (behavioural requirements from PR diff text)
# ---------------------------------------------------------------------------

# Stop words stripped before building KG search keywords from a requirement sentence.
# We keep domain-significant words like entity names, cluster names, timing values.
_PR_REQ_STOP_WORDS: frozenset = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "not", "no",
    "in", "on", "at", "to", "for", "of", "and", "or", "but", "with",
    "that", "this", "it", "its", "if", "when", "after", "before", "then",
    "sent", "set", "clear", "reject", "accept", "return", "terminate",
    "device", "node", "cluster", "attribute", "command", "event",
    "which", "such", "each", "all", "any", "some", "per", "via",
    "its", "from", "by", "as", "into", "out", "up", "only",
})


def _filter_pr_req_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from a requirement sentence.

    Splits on word boundaries, lowercases, strips stop-words,
    and de-duplicates while preserving order.  Numbers (e.g. timing values
    like "900") are also retained since they carry semantic weight.
    """
    seen: Set[str] = set()
    result: List[str] = []
    for token in re.findall(r'\b[a-zA-Z0-9]{2,}\b', text):
        w = token.lower()
        if w not in _PR_REQ_STOP_WORDS and w not in seen:
            seen.add(w)
            result.append(w)
    return result[:24]  # cap to avoid KG search noise


def extract_pr_requirements(
    text: str,
    canonical_schema: Optional[CanonicalSchema] = None,
    source_chunk_idx: int = -1,
) -> List[PRRequirementRecord]:
    """Extract normative behavioural/timing requirements from a PR diff chunk.

    Unlike ``ChangeExtractor`` which focuses on structural entity changes
    (new command added, attribute type changed), this function extracts
    behavioural requirements expressed as normative prose:

        "BLE advertisement shall terminate after 900 seconds"
        → timing_requirement, entity_refs=["PROTO::Discovery"], cluster=None

        "If Occupancy == false, event shall not be sent"
        → conditional_behavior_rule, inferred_cluster="Occupancy Sensing"

    The output records are stored in ``PipelineState["pr_requirements"]`` and
    consumed by ``search_knowledge_graph_node`` to look for matching test cases
    in the KG and flag coverage gaps.

    Args:
        text:             Plain text from a PR diff chunk (HTML already stripped).
        canonical_schema: Full schema for entity name resolution.  When ``None``
                          (e.g. warm-load run), only protocol anchors are matched.
        source_chunk_idx: Which PR chunk index this text came from.

    Returns:
        List of :class:`PRRequirementRecord`, one per normative sentence found.
        Returns an empty list when no normative sentences are detected.
    """
    candidates = detect_requirement_candidates(text)
    if not candidates:
        return []

    _canonical = canonical_schema if canonical_schema is not None else CanonicalSchema()

    results: List[PRRequirementRecord] = []
    for sentence in candidates:
        # ── Entity extraction ──────────────────────────────────────────────
        entity_matches = extract_entities(sentence, _canonical)
        entity_refs: List[str] = [m.entity_id for m in entity_matches]

        # ── Cluster inference: prefer first entity with a cluster name ─────
        inferred_cluster: Optional[str] = None
        for m in entity_matches:
            if m.cluster:        # skip protocol anchors (cluster == "")
                inferred_cluster = m.cluster
                break

        # ── Requirement type classification ───────────────────────────────
        req_type, confidence, ambiguous, _, signals, alternatives = classify_requirement_type(
            sentence, entity_refs, _canonical
        )

        # ── Keyword extraction for KG search ──────────────────────────────
        keywords = _filter_pr_req_keywords(sentence)

        results.append(PRRequirementRecord(
            text=sentence,
            requirement_type=req_type,
            confidence=confidence,
            ambiguous=ambiguous,
            keywords=keywords,
            entity_refs=entity_refs,
            inferred_cluster=inferred_cluster,
            source_chunk_idx=source_chunk_idx,
            signals=signals,
            alternatives=alternatives,
        ))

    return results
