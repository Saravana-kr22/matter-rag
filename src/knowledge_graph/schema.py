"""Python dataclasses and enums mirroring the Matter KB JSON Schema.

This module is the single source of truth for all typed data structures
flowing through the knowledge-base pipeline:

    DM XML  ──►  CanonicalSchema   (ClusterRecord, CanonicalEntityRef)
    Spec    ──►  SpecRecord        (ConditionRecord, EffectRecord, ConstraintRecord)
    TP      ──►  TestCaseRecord
    Graph   ──►  GraphBundle       (GraphNodeRecord, GraphEdgeRecord)
    Vectors ──►  VectorChunkRecord

All classes use ``@dataclass`` with ``field(default_factory=...)`` to avoid
mutable-default pitfalls.  Every enum is a ``str`` subclass so values
serialise cleanly to JSON without extra conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    CLUSTER   = "cluster"
    ATTRIBUTE = "attribute"
    COMMAND   = "command"
    EVENT     = "event"
    FEATURE   = "feature"


class GraphNodeType(str, Enum):
    CLUSTER         = "CLUSTER"
    ATTRIBUTE       = "ATTRIBUTE"
    COMMAND         = "COMMAND"
    EVENT           = "EVENT"
    FEATURE         = "FEATURE"
    # Protocol / behaviour backbone  (new)
    PROTOCOL_AREA   = "PROTOCOL_AREA"
    BEHAVIOR        = "BEHAVIOR"
    REQUIREMENT     = "REQUIREMENT"
    BEHAVIOR_RULE   = "BEHAVIOR_RULE"
    # Test plan
    TEST_CASE       = "TEST_CASE"
    PICS_ITEM       = "PICS_ITEM"
    # Structural
    SECTION         = "SECTION"
    # Transient
    PR_CHANGE       = "PR_CHANGE"


class GraphEdgeType(str, Enum):
    # Data-model backbone (CLUSTER → entity)
    HAS_ATTRIBUTE = "HAS_ATTRIBUTE"
    HAS_COMMAND   = "HAS_COMMAND"
    HAS_EVENT     = "HAS_EVENT"
    HAS_FEATURE   = "HAS_FEATURE"

    # Traceability
    COVERS       = "covers"
    TESTS        = "tests"
    VALIDATES    = "validates"
    IMPLEMENTS   = "implements"

    # Typed test→entity edges (from rule engine)
    TESTS_COMMAND          = "tests_command"
    VERIFIES_ATTRIBUTE     = "verifies_attribute"
    OBSERVES_EVENT         = "observes_event"
    VERIFIES_REQUIREMENT   = "verifies_requirement"
    VERIFIES_RULE          = "verifies_rule"
    GATED_BY_PICS          = "gated_by_pics"
    HAS_INTENT             = "has_intent"
    IN_CONTEXT             = "in_context"
    GOVERNS                = "governs"
    HAS_CONDITION          = "has_condition"
    HAS_EFFECT             = "has_effect"

    # Fine-grained TC → ATTRIBUTE interaction edges
    # These replace the coarse VERIFIES_ATTRIBUTE for read/write/validate operations
    # so that impact queries like "find tests that validate_range for Foo.Bar" work precisely.
    READS               = "reads"               # TC reads an attribute value
    WRITES              = "writes"              # TC writes an attribute value
    VALIDATES_TYPE      = "validates_type"      # TC validates attribute data type
    VALIDATES_RANGE     = "validates_range"     # TC validates value range / bounds
    VALIDATES_DEFAULT   = "validates_default"   # TC validates default value
    VALIDATES_QUIETER_REPORTING = "validates_quieter_reporting"  # TC validates quieter reporting behaviour
    VALIDATES_ENUM      = "validates_enum"      # TC validates enum / value list
    VALIDATES_ACCESS    = "validates_access"    # TC validates access control / ACL
    VALIDATES_CONFORMANCE = "validates_conformance"  # TC validates feature conformance / PICS

    # Fine-grained TC → entity interaction edges (broader)
    NEGATIVE_TESTS      = "negative_tests"      # TC tests negative / error scenario
    DEPENDS_ON          = "depends_on"          # TC validates cross-entity dependency

    # Change / impact
    IMPACTS      = "impacts"

    # Structural
    BELONGS_TO   = "belongs_to"
    # Protocol-area hierarchy  (new)
    BELONGS_TO_PROTOCOL_AREA = "BELONGS_TO_PROTOCOL_AREA"
    # Behavior backbone  (new)
    HAS_BEHAVIOR_RULE = "HAS_BEHAVIOR_RULE"

    # Misc
    REFERENCES     = "references"
    DERIVED_FROM   = "derived_from"
    CONFLICTS_WITH = "conflicts_with"
    RELATED_TO     = "related_to"
    ALIAS_OF       = "alias_of"


class RequirementType(str, Enum):
    TIMING_REQUIREMENT        = "timing_requirement"
    CONDITIONAL_BEHAVIOR_RULE = "conditional_behavior_rule"
    CROSS_ENTITY_DEPENDENCY   = "cross_entity_dependency"
    ENTITY_DEFINITION         = "entity_definition_changed"   # defines what an entity IS / DOES
    VALUE_CONSTRAINT          = "value_constraint"            # constrains values, ranges, defaults
    STATE_TRANSITION_RULE     = "state_transition_rule"
    GENERAL_NORMATIVE         = "general_normative_requirement"

    # ── Backward-compat aliases (no longer generated, but still accepted by load_from_json) ──
    LIFECYCLE_REQUIREMENT          = "lifecycle_requirement"          # → STATE_TRANSITION_RULE
    PROTOCOL_BEHAVIOR_REQUIREMENT  = "protocol_behavior_requirement"  # → CONDITIONAL_BEHAVIOR_RULE


class ChangeKind(str, Enum):
    """Categories of changes a PR can make to a Matter entity.

    Used by ``analyze_impact_for_change()`` to map the change to the set of
    edge types that most directly connect a test case to the affected entity.
    """
    ENTITY_ADDED          = "entity_added"          # brand-new entity (command/attr/event/feature)
    ENTITY_REMOVED        = "entity_removed"        # entity deleted from the cluster
    DATATYPE_CHANGED      = "datatype_changed"      # attribute data-type changed
    CONSTRAINT_CHANGED    = "constraint_changed"    # min/max value range changed
    DEFAULT_CHANGED       = "default_changed"       # default value changed
    QUIETER_REPORTING_CHANGED = "quieter_reporting_changed"  # Q quality flag added/removed
    ENUM_CHANGED          = "enum_changed"          # enum value list changed
    ACCESS_CHANGED        = "access_changed"        # access control / ACL changed
    CONFORMANCE_CHANGED   = "conformance_changed"   # feature conformance / PICS changed
    BEHAVIOR_CHANGED      = "behavior_changed"      # behavioural semantics changed
    DEPENDENCY_CHANGED    = "dependency_changed"    # cross-entity dependency changed
    STATE_MACHINE_CHANGED = "state_machine_changed" # state-machine transition changed


class TestMode(str, Enum):
    CLUSTER_CENTRIC             = "cluster_centric"
    PROTOCOL_BEHAVIOR_CENTRIC   = "protocol_behavior_centric"
    MIXED                       = "mixed"
    AMBIGUOUS                   = "ambiguous"


class TestIntent(str, Enum):
    READ_ATTRIBUTE                    = "read_attribute"
    WRITE_ATTRIBUTE                   = "write_attribute"
    INVOKE_COMMAND                    = "invoke_command"
    VALIDATE_RESPONSE                 = "validate_response"
    VALIDATE_ERROR                    = "validate_error"
    OBSERVE_EVENT                     = "observe_event"
    VALIDATE_TIMING                   = "validate_timing"
    VALIDATE_STATE_TRANSITION         = "validate_state_transition"
    VALIDATE_CROSS_ENTITY_DEPENDENCY  = "validate_cross_entity_dependency"
    VALIDATE_COMMISSIONING_FLOW       = "validate_commissioning_flow"
    VALIDATE_DISCOVERY_BEHAVIOR       = "validate_discovery_behavior"
    VALIDATE_ONBOARDING_PAYLOAD       = "validate_onboarding_payload"
    VALIDATE_BEHAVIOR_RULE            = "validate_behavior_rule"
    NEGATIVE_SCENARIO                 = "negative_scenario"


class VectorChunkType(str, Enum):
    FULL           = "full"
    INTENT_SUMMARY = "intent_summary"
    PROCEDURE      = "procedure"
    SETUP          = "setup"


# ---------------------------------------------------------------------------
# Canonical schema types (from DM XML)
# ---------------------------------------------------------------------------

@dataclass
class CanonicalEntityRef:
    """One attribute / command / event / feature from DM XML."""
    id: str                         # e.g. "ATTRIBUTE::OnOff::OnOff"
    entity_type: EntityType
    name: str
    cluster: str                    # owning cluster name
    code: str = ""                  # hex ID e.g. "0x0000"
    datatype: str = ""              # type for attributes, "" for others
    access: str = ""                # e.g. "R V"
    conformance: str = ""           # e.g. "M", "O", "P"
    quality: str = ""               # e.g. "N", "P"
    default: str = ""
    direction: str = ""             # for commands: "commandToServer" etc.
    response: str = ""              # for commands
    priority: str = ""              # for events
    # feature-map specifics
    bit: str = ""
    code_short: str = ""            # feature code e.g. "LT"
    summary: str = ""


@dataclass
class ClusterRecord:
    """One Matter cluster parsed from DM XML."""
    id: str                         # "CLUSTER::On/Off"
    name: str                       # "On/Off"
    code: str = ""                  # "0x0006"
    revision: str = ""
    pics_code: str = ""             # e.g. "OO" from <classification picsCode="OO">
    hierarchy: str = ""             # "derived" | "base" | "" — from <classification hierarchy="...">
    base_cluster: str = ""          # e.g. "Mode Base Cluster" — from <classification baseCluster="...">
    entities: List[CanonicalEntityRef] = field(default_factory=list)
    source_file: str = ""


@dataclass
class CanonicalSchema:
    """Full collection of clusters from all DM XML files."""
    clusters: List[ClusterRecord] = field(default_factory=list)
    # Quick-lookup dict: entity id → CanonicalEntityRef  (built by dm_xml_parser)
    entity_lookup: Dict[str, CanonicalEntityRef] = field(default_factory=dict)
    # Quick-lookup: lowercase cluster name → ClusterRecord
    cluster_lookup: Dict[str, ClusterRecord] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Spec types (from cleaned spec HTML/adoc)
# ---------------------------------------------------------------------------

@dataclass
class SectionRecord:
    """One section heading from a spec document."""
    id: str                        # e.g. "SECTION::appclusters::3.2.5 On/Off Cluster"
    title: str                     # clean heading text
    cluster: str = ""              # cluster name if heading matches a DM XML cluster
    cluster_id: str = ""           # "CLUSTER::On/Off" if matched
    full_text: str = ""            # clean body text for embedding
    section_path: str = ""        # breadcrumb e.g. "3 Clusters > 3.2 On/Off Cluster"
    source_doc: str = ""


@dataclass
class ConditionRecord:
    """A condition expression extracted from spec text."""
    text: str
    entity_refs: List[str] = field(default_factory=list)   # CanonicalEntityRef.id values


@dataclass
class EffectRecord:
    """An effect / action extracted from spec text."""
    text: str
    entity_refs: List[str] = field(default_factory=list)


@dataclass
class ConstraintRecord:
    """A constraint (value range, format, timing) extracted from spec text."""
    text: str
    attribute_ref: str = ""        # CanonicalEntityRef.id if attribute-bound
    operator: str = ""             # ">=", "<=", "in", "enum", ...
    value: str = ""


@dataclass
class SpecRecord:
    """One normative sentence / paragraph extracted from the spec."""
    id: str                        # e.g. "REQ::OccupancySensing::OccupancySensorType::0"
    requirement_type: RequirementType
    cluster: str = ""
    section_id: str = ""           # parent SectionRecord.id
    entity_refs: List[str] = field(default_factory=list)    # CanonicalEntityRef.id values
    normative_text: str = ""       # the single extracted normative sentence
    context_text: str = ""         # the full paragraph containing normative_text (for LLM context)
    conditions: List[ConditionRecord] = field(default_factory=list)
    effects: List[EffectRecord] = field(default_factory=list)
    constraints: List[ConstraintRecord] = field(default_factory=list)
    section_path: str = ""         # e.g. "3.2.5 Occupancy Sensor Type Attribute"
    source_doc: str = ""
    # Rule engine scoring output
    confidence: float = 1.0
    ambiguous: bool = False
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    # Interpretability fields — which signals fired and what the alternatives were
    signals: List[str] = field(default_factory=list)        # e.g. ["timing_bound", "conditional_if"]
    alternatives: List[str] = field(default_factory=list)   # runner-up types, e.g. ["conditional_behavior_rule"]


@dataclass
class PRRequirementRecord:
    """A normative behavioural/timing requirement extracted from a PR diff chunk.

    Unlike :class:`SpecRecord` (which comes from the authoritative spec), this
    record is derived on-the-fly from PR diff text and represents a **proposed**
    or **changed** requirement that needs test coverage.

    Examples:
        - "BLE advertisement shall terminate after 900 seconds"
          → timing_requirement, entity_refs=["PROTO::Discovery"], cluster=None

        - "If Occupancy == false, event shall not be sent"
          → conditional_behavior_rule, inferred_cluster="Occupancy Sensing"
    """
    text: str                           # The normative sentence (original)
    requirement_type: RequirementType   # Classified type
    confidence: float                   # Classification confidence [0–1]
    ambiguous: bool                     # True if classification is uncertain
    keywords: List[str]                 # Stop-word-filtered keywords for KG search
    entity_refs: List[str] = field(default_factory=list)    # entity_id values found in sentence
    inferred_cluster: Optional[str] = None   # Cluster inferred from entity_refs (may be None for protocol-level)
    source_chunk_idx: int = -1          # Index of the pr_chunk this came from
    signals: List[str] = field(default_factory=list)        # e.g. ["timing_bound", "conditional_if"]
    alternatives: List[str] = field(default_factory=list)   # runner-up types, e.g. ["conditional_behavior_rule"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "requirement_type": self.requirement_type.value if hasattr(self.requirement_type, "value") else str(self.requirement_type),
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "keywords": self.keywords,
            "entity_refs": self.entity_refs,
            "inferred_cluster": self.inferred_cluster,
            "source_chunk_idx": self.source_chunk_idx,
            "signals": self.signals,
            "alternatives": self.alternatives,
        }


# ---------------------------------------------------------------------------
# Test case types (from test plan HTML/adoc)
# ---------------------------------------------------------------------------

@dataclass
class TestCaseRecord:
    """One test case extracted from a test plan document."""
    id: str                        # e.g. "TC-OO-2.1"
    title: str
    cluster: str = ""
    mode: TestMode = TestMode.AMBIGUOUS
    intents: List[TestIntent] = field(default_factory=list)
    entity_refs: List[str] = field(default_factory=list)    # CanonicalEntityRef.id values
    spec_refs: List[str] = field(default_factory=list)      # SpecRecord.id values
    # Structured subsections
    purpose: str = ""
    dut_type: str = ""             # e.g. "Server", "Client"
    default_dut: str = ""
    prerequisites: str = ""
    setup: str = ""
    procedure_steps: List[str] = field(default_factory=list)
    expected_outcomes: List[str] = field(default_factory=list)
    # Full embedding text (pre-computed by extractor)
    all_text: str = ""
    source_doc: str = ""
    # PICS codes declared in this test case (extracted from test plan HTML tables)
    pics_codes: List[str] = field(default_factory=list)  # e.g. ["OO.S.A0000", "OO.S.C01"]


# ---------------------------------------------------------------------------
# Graph types (for NetworkX builder output)
# ---------------------------------------------------------------------------

@dataclass
class GraphNodeRecord:
    """Typed node ready for insertion into the knowledge graph."""
    node_id: str
    node_type: GraphNodeType
    label: str
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdgeRecord:
    """Typed edge ready for insertion into the knowledge graph."""
    source: str
    target: str
    edge_type: GraphEdgeType
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphBundle:
    """Complete graph representation from one build pass."""
    nodes: List[GraphNodeRecord] = field(default_factory=list)
    edges: List[GraphEdgeRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Vector chunk types
# ---------------------------------------------------------------------------

@dataclass
class VectorChunkRecord:
    """One chunk of text ready for embedding + vector-store insertion."""
    chunk_id: str                  # e.g. "TC-OO-2.1::full"
    tc_id: str                     # parent TestCaseRecord.id
    chunk_type: VectorChunkType
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level KnowledgeBase
# ---------------------------------------------------------------------------

@dataclass
class ProtocolAreaRecord:
    """One node in the protocol / behaviour backbone hierarchy."""
    id: str                        # e.g. "PROTOCOL_AREA::Commissionable_Node_Discovery"
    name: str                      # human-readable name
    parent_id: str = ""            # parent PROTOCOL_AREA.id (empty for top-level)
    full_path: str = ""            # full breadcrumb e.g. "Discovery > Commissionable Node Discovery"
    source_doc: str = ""


@dataclass
class BehaviorRecord:
    """A named device behaviour extracted from procedural spec text."""
    id: str                        # e.g. "BEHAVIOR::Power_Cycling"
    name: str
    requirement_ids: List[str] = field(default_factory=list)  # linked SpecRecord IDs
    source_doc: str = ""


@dataclass
class RejectedCandidate:
    """A candidate requirement sentence that was rejected by the filter."""
    text: str
    reason: str                    # e.g. "too_short", "glossary_row", "table_fragment"
    source_section: str = ""


@dataclass
class ValidationReport:
    """Graph quality report produced by validate_graph()."""
    total_nodes: int = 0
    total_edges: int = 0
    # Requirement quality
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
    orphan_requirements: List[str] = field(default_factory=list)
    orphan_test_cases: List[str] = field(default_factory=list)
    # Edge quality
    invalid_edges: List[str] = field(default_factory=list)  # human-readable problems
    # Coverage
    requirements_without_protocol_area: List[str] = field(default_factory=list)
    test_cases_with_no_links: List[str] = field(default_factory=list)
    # Warnings
    warnings: List[str] = field(default_factory=list)

@dataclass
class KnowledgeBase:
    """Fully assembled knowledge base — output of knowledge_base.py."""
    canonical_schema: CanonicalSchema = field(default_factory=CanonicalSchema)
    section_records: List[SectionRecord] = field(default_factory=list)
    spec_records: List[SpecRecord] = field(default_factory=list)
    test_case_records: List[TestCaseRecord] = field(default_factory=list)
    graph: GraphBundle = field(default_factory=GraphBundle)
    vector_chunks: List[VectorChunkRecord] = field(default_factory=list)
    # New backbone records
    protocol_area_records: List[ProtocolAreaRecord] = field(default_factory=list)
    behavior_records: List[BehaviorRecord] = field(default_factory=list)
    # Quality reporting
    validation_report: Optional[ValidationReport] = None
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
