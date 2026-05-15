"""Candidate test-case re-ranker for the Matter RAG pipeline.

Sits between vector DB retrieval (step 4) and knowledge-graph lookup / LLM
analysis (steps 6–7).  Takes the top-K candidates returned by FAISS and
re-scores them using structural signals that the embedding model cannot
distinguish:

  - entity-level overlap with the structured change record
  - cluster name matching (exact + token-level)
  - condition / effect entity coverage for behavioural rules
  - test intent alignment with ChangeKind
  - knowledge-graph direct / indirect hit bonuses
  - lexical token overlap with the original PR/spec change text
  - chunk-type preference (intent_summary > setup > teardown)
  - original vector retrieval score passthrough (tiebreaker)

The re-ranker is NOT the final reasoning stage — it produces a ranked list
that the LLM then uses for gap analysis.  Precision matters more than recall
here; the vector DB already handled recall.

Usage::

    from src.search.reranker import CandidateReranker, RerankerWeights

    reranker = CandidateReranker()                   # default weights
    ranked   = reranker.rerank(
        structured_change = change.to_dict(),
        query_text        = pr_chunk.text,
        candidates        = vector_results,          # list[dict] from FAISS
        kg_hits           = kg_result,               # optional
        top_n             = 5,
    )
    for r in ranked:
        print(f"{r.final_score:.3f}  {r.test_case_id}  {r.reason}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

@dataclass
class RerankerWeights:
    """Configurable per-component scoring weights.

    Each weight is multiplied by a normalised [0, 1] raw component score.
    The final candidate score is ``min(1.0, sum(weighted_components))``.

    Recommended baseline (see module docstring for rationale):

    +---------------------------------+--------+------------------------------------------+
    | Component                       | Weight | Main signal                              |
    +=================================+========+==========================================+
    | entity_overlap                  |  0.25  | Exact entity names from change record    |
    | cluster_match                   |  0.15  | Cluster name (exact / token)             |
    | condition_effect_overlap        |  0.15  | Both sides of a behaviour rule covered   |
    | intent_match                    |  0.15  | Test intents align with ChangeKind       |
    | kg_direct_bonus                 |  0.20  | KG has a direct edge to this TC          |
    | kg_indirect_bonus               |  0.08  | KG has a 2-hop edge to this TC           |
    | lexical_similarity              |  0.08  | Token overlap with PR/spec change text   |
    | chunk_type_bonus                |  0.05  | intent_summary preferred over setup      |
    | retrieval_score                 |  0.04  | Original cosine similarity (tiebreaker)  |
    +---------------------------------+--------+------------------------------------------+

    kg_direct_bonus and kg_indirect_bonus are mutually exclusive; only the
    larger applies (direct takes priority).
    """

    entity_overlap: float          = 0.25
    cluster_match: float           = 0.15
    condition_effect_overlap: float = 0.15
    intent_match: float            = 0.15
    kg_direct_bonus: float         = 0.20
    kg_indirect_bonus: float       = 0.08
    lexical_similarity: float      = 0.08
    chunk_type_bonus: float        = 0.05
    retrieval_score: float         = 0.04


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass
class RankedCandidate:
    """A single re-ranked candidate test-case chunk."""

    candidate_id: str
    test_case_id: str
    final_score: float
    score_breakdown: Dict[str, float]
    reason: str

    # Original candidate fields — preserved for downstream use
    chunk_type: str               = ""
    title: str                    = ""
    text: str                     = ""
    metadata: Dict[str, Any]      = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ChangeKind → relevant test-intent mapping
# ---------------------------------------------------------------------------

# Intents always worth having, regardless of change kind.
_UNIVERSAL_INTENTS: Set[str] = {"validate_schema", "validate_conformance"}

# Per-ChangeKind sets of test intents that are directly relevant.
# Intent strings are lower-cased throughout for case-insensitive comparison.
_CHANGE_KIND_INTENT_MAP: Dict[str, Set[str]] = {
    "ADD_ATTRIBUTE":      {"validate_attribute_existence", "validate_attribute_value",
                           "validate_schema"},
    "REMOVE_ATTRIBUTE":   {"validate_attribute_existence", "validate_schema"},
    "MODIFY_ATTRIBUTE":   {"validate_attribute_value", "validate_conformance",
                           "validate_access", "validate_attribute_existence"},
    "ADD_COMMAND":        {"validate_command_support", "invoke_command", "validate_schema"},
    "REMOVE_COMMAND":     {"validate_command_support", "validate_schema"},
    "MODIFY_COMMAND":     {"validate_command_behavior", "invoke_command",
                           "validate_command_support"},
    "ADD_EVENT":          {"observe_event", "validate_event_generation", "validate_schema"},
    "REMOVE_EVENT":       {"observe_event", "validate_event_generation"},
    "MODIFY_EVENT":       {"observe_event", "validate_event_generation",
                           "validate_behavior_rule"},
    "ADD_FEATURE":        {"validate_feature_conformance", "validate_feature_behavior"},
    "REMOVE_FEATURE":     {"validate_feature_conformance", "validate_schema"},
    "MODIFY_FEATURE":     {"validate_feature_conformance", "validate_feature_behavior"},
    "ADD_REQUIREMENT":    {"validate_behavior_rule", "validate_conformance",
                           "validate_state_transition"},
    "REMOVE_REQUIREMENT": {"validate_behavior_rule", "validate_conformance"},
    "MODIFY_REQUIREMENT": {"validate_behavior_rule", "validate_conformance",
                           "validate_state_transition"},
    "MODIFY_BEHAVIOR":    {"validate_behavior_rule", "validate_state_transition",
                           "observe_event"},
    "MODIFY_PROTOCOL":    {"validate_commissioning", "validate_discovery",
                           "validate_timing", "validate_ble", "validate_advertisement"},
    "ADD_CLUSTER":        {"validate_cluster_existence", "validate_schema"},
    "REMOVE_CLUSTER":     {"validate_cluster_existence"},
    "MODIFY_CLUSTER":     {"validate_schema", "validate_cluster_behavior"},
    # JSON-style variant (used in prompt examples / structured_change dicts)
    "conditional_behavior_rule": {"validate_behavior_rule", "validate_state_transition",
                                   "observe_event"},
    "UNKNOWN":            set(),
}

# ChangeKinds where condition/effect overlap scoring is meaningful.
_BEHAVIORAL_CHANGE_KINDS: Set[str] = {
    "MODIFY_BEHAVIOR", "ADD_REQUIREMENT", "MODIFY_REQUIREMENT", "REMOVE_REQUIREMENT",
    "MODIFY_EVENT", "ADD_EVENT", "conditional_behavior_rule",
}

# Intents that indicate a test validates a rule's condition+effect chain.
_BEHAVIORAL_INTENTS: Set[str] = {
    "validate_behavior_rule", "validate_state_transition", "observe_event",
}

# Chunk types ordered by descending relevance (higher score → more relevant).
_CHUNK_TYPE_SCORE: Dict[str, float] = {
    "intent_summary":     1.00,
    "behavior_rule":      0.90,
    "state_transition":   0.85,
    "timing_requirement": 0.85,
    "requirement":        0.80,
    "test_step":          0.60,
    "prerequisite":       0.50,
    "setup":              0.40,
    "teardown":           0.20,
}
_DEFAULT_CHUNK_TYPE_SCORE = 0.50

# Keywords that signal timing / protocol-level tests.
_TIMING_KEYWORDS: frozenset = frozenset([
    "timeout", "timing", "delay", "ms", "millisecond", "second", "interval",
    "advertisement", "window", "backoff", "retry", "period", "duration",
    "discovery", "commissioning", "ble", "onboarding", "payload", "broadcast",
    "keepalive", "heartbeat", "schedule", "deadline",
])

# Common English stop words to exclude from lexical similarity.
_STOP_WORDS: frozenset = frozenset([
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "has",
    "have", "had", "do", "does", "did", "will", "would", "should", "shall",
    "may", "might", "can", "could", "not", "no", "nor", "so", "yet", "if",
    "as", "its", "it", "this", "that", "these", "those", "then", "than",
    "when", "where", "which", "who", "how", "what", "all", "each", "any",
    "both", "some", "such", "own", "same", "other", "only", "also", "just",
])


# ---------------------------------------------------------------------------
# Low-level text utilities
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Split text into lowercase alphabetic/numeric tokens.

    Handles CamelCase splitting so ``OccupancySensing`` → ``["occupancy", "sensing"]``
    and mixed strings like ``On/Off`` → ``["on", "off"]``.
    """
    # Insert space before uppercase letters that follow lowercase letters (CamelCase)
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split on anything that is not alphanumeric
    return [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", expanded) if t]


def _normalize_name(name: str) -> str:
    """Lowercase + remove all non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _camel_tokens(text: str) -> Set[str]:
    """Return the set of lower-case tokens after CamelCase expansion."""
    return {t for t in _tokenize(text) if len(t) > 1}


def _extract_camel_names_from_text(text: str) -> Set[str]:
    """Extract CamelCase identifiers from a free-text string.

    Used to pull entity names out of condition/effect description strings
    such as ``"conformance: M → O for attribute OnOff"``.
    """
    camel_re = re.compile(r"\b([A-Z][a-z][A-Za-z0-9]{2,})\b")
    return {_normalize_name(m.group(1)) for m in camel_re.finditer(text)}


def _cluster_tokens(cluster: str) -> Set[str]:
    """Tokenise a cluster name for partial-match comparison."""
    return {t for t in _tokenize(cluster) if len(t) > 2}


# ---------------------------------------------------------------------------
# Per-component scoring functions
# ---------------------------------------------------------------------------

def _score_entity_overlap(
    change_entities: List[Dict[str, Any]],
    candidate_meta_entities: List[str],
    candidate_text: str,
) -> float:
    """Fraction of change entities found in the candidate (metadata or text).

    Each entity matched in metadata gets full credit (1.0 per entity).
    An entity found only in candidate text gets partial credit (0.5).
    Score is normalised to [0, 1] by the number of change entities.
    """
    if not change_entities:
        return 0.0

    # Normalised entity names from the structured change record.
    change_names: Set[str] = {
        _normalize_name(e.get("name", ""))
        for e in change_entities
        if e.get("name")
    }
    if not change_names:
        return 0.0

    # Entity names declared in the candidate's metadata list.
    meta_entity_set: Set[str] = {_normalize_name(e) for e in candidate_meta_entities if e}

    # Also build a coarse token set from candidate text for fallback matching.
    text_tokens: Set[str] = set(_tokenize(candidate_text.lower()))

    matched_score = 0.0
    for name in change_names:
        # Exact metadata hit
        if name in meta_entity_set:
            matched_score += 1.0
        # Token-in-text partial credit (e.g. name = "onoff", text contains "onoff")
        elif name in text_tokens:
            matched_score += 0.5
        else:
            # Try individual CamelCase sub-tokens against text tokens
            sub = _camel_tokens(name)
            if sub and sub <= text_tokens:
                matched_score += 0.3

    return min(1.0, matched_score / len(change_names))


def _score_cluster_match(
    change_cluster: str,
    candidate_cluster: str,
    candidate_text: str,
) -> float:
    """Score cluster name similarity between the change record and the candidate.

    Returns:
        1.0 — exact match after normalisation
        0.7 — full token-set overlap (handles "On/Off" ↔ "OnOff")
        0.3–0.5 — partial token overlap
        0.0 — no match
    """
    if not change_cluster:
        return 0.0

    cc_norm = _normalize_name(change_cluster)

    # 1. Exact match after normalisation
    if cc_norm == _normalize_name(candidate_cluster):
        return 1.0

    cc_tokens = _cluster_tokens(change_cluster)

    # 2. Token-level match against declared cluster field
    if candidate_cluster:
        cand_tokens = _cluster_tokens(candidate_cluster)
        if cc_tokens and cand_tokens:
            overlap = len(cc_tokens & cand_tokens) / len(cc_tokens)
            if overlap >= 1.0:
                return 0.7
            if overlap >= 0.5:
                return 0.5 * overlap

    # 3. Fallback: cluster name tokens found in candidate text
    if cc_tokens:
        text_tokens = set(_tokenize(candidate_text.lower()))
        text_overlap = len(cc_tokens & text_tokens) / len(cc_tokens)
        if text_overlap >= 1.0:
            return 0.4
        if text_overlap >= 0.5:
            return 0.25

    return 0.0


def _extract_rule_entity_names(items: List[Any]) -> Set[str]:
    """Extract normalised entity names from a conditions/effects list.

    Handles both structured dicts (``{"entity_type": ..., "name": ...}``) and
    plain description strings (``"conformance: M → O for attribute OnOff"``).
    """
    names: Set[str] = set()
    for item in items:
        if isinstance(item, dict):
            name = item.get("name", "")
            if name:
                names.add(_normalize_name(name))
        elif isinstance(item, str):
            # Pull out CamelCase identifiers embedded in the description string.
            names.update(_extract_camel_names_from_text(item))
    return names


def _score_condition_effect_overlap(
    structured_change: Dict[str, Any],
    candidate_meta_entities: List[str],
    candidate_intents: List[str],
    candidate_text: str,
) -> float:
    """Score coverage of a behavioural rule's condition AND effect entities.

    A test that verifies BOTH the condition side and the effect side of a rule
    (e.g. "when Occupancy==false, OccupancyChanged shall NOT be sent") is more
    valuable than one that only mentions either the attribute or the event.

    Scoring:
      - Base: (matched_cond + matched_effect) / total_rule_entities
      - Bonus +0.20: candidate covers BOTH condition and effect sides
      - Bonus +0.10: candidate has a relevant behavioural intent declared
    """
    change_kind = structured_change.get("change_kind", "")
    conditions  = structured_change.get("conditions", [])
    effects     = structured_change.get("effects", [])

    # Only meaningful for behaviour/rule-oriented change kinds.
    is_behavioral = (
        change_kind in _BEHAVIORAL_CHANGE_KINDS
        or bool(conditions)
        or bool(effects)
    )
    if not is_behavioral:
        return 0.0

    cond_names   = _extract_rule_entity_names(conditions)
    effect_names = _extract_rule_entity_names(effects)
    all_rule_entities = cond_names | effect_names

    if not all_rule_entities:
        return 0.0

    # Candidate entity sources
    meta_set   = {_normalize_name(e) for e in candidate_meta_entities if e}
    text_tokens = set(_tokenize(candidate_text.lower()))

    def _match_score(names: Set[str]) -> float:
        total = 0.0
        for name in names:
            if name in meta_set:
                total += 1.0
            elif name in text_tokens:
                total += 0.5
            else:
                sub = _camel_tokens(name)
                if sub and sub <= text_tokens:
                    total += 0.3
        return total

    matched_cond   = _match_score(cond_names)
    matched_effect = _match_score(effect_names)
    base = (matched_cond + matched_effect) / len(all_rule_entities)

    # Bonus: candidate covers entities from BOTH sides of the rule.
    both_sides_bonus = 0.0
    if cond_names and effect_names:
        if matched_cond > 0 and matched_effect > 0:
            both_sides_bonus += 0.20

    # Bonus: candidate declares a relevant behavioural intent.
    cand_intent_set = {_normalize_name(i) for i in candidate_intents}
    if cand_intent_set & _BEHAVIORAL_INTENTS:
        both_sides_bonus += 0.10

    return min(1.0, base + both_sides_bonus)


def _keyword_based_intent_score(change_kind: str, candidate_text: str) -> float:
    """Fallback intent scoring using keyword presence when candidate has no declared intents.

    Used when ``metadata["test_intents"]`` is absent or empty.
    """
    cand_tokens = set(_tokenize(candidate_text.lower()))
    relevant_intents = _CHANGE_KIND_INTENT_MAP.get(change_kind, set())
    if not relevant_intents:
        return 0.0

    # Convert intent labels to keyword bags, e.g. "validate_behavior_rule" → {"validate","behavior","rule"}
    intent_keywords: Set[str] = set()
    for intent in relevant_intents:
        intent_keywords.update(_tokenize(intent))
    intent_keywords -= _STOP_WORDS

    # Special case: timing-related protocol changes.
    if change_kind == "MODIFY_PROTOCOL":
        timing_hit = bool(_TIMING_KEYWORDS & cand_tokens)
        return min(1.0, (len(intent_keywords & cand_tokens) / max(len(intent_keywords), 1)) + (0.4 if timing_hit else 0.0))

    overlap = len(intent_keywords & cand_tokens)
    return min(1.0, overlap / max(len(intent_keywords), 1))


def _score_intent_match(
    change_kind: str,
    candidate_intents: List[str],
    query_text: str,
    candidate_text: str,
) -> float:
    """Score alignment between the candidate's declared test intents and the ChangeKind.

    Falls back to keyword scanning if no intents are declared.
    Adds timing bonus for MODIFY_PROTOCOL changes when both query and candidate
    mention timing-related keywords.
    """
    relevant_intents = _CHANGE_KIND_INTENT_MAP.get(change_kind, set()) | _UNIVERSAL_INTENTS

    if not candidate_intents:
        return _keyword_based_intent_score(change_kind, candidate_text)

    cand_intent_set = {_normalize_name(i) for i in candidate_intents}
    relevant_norm   = {_normalize_name(i) for i in relevant_intents}

    matched  = len(cand_intent_set & relevant_norm)
    possible = len(relevant_norm)
    base = matched / possible if possible > 0 else 0.0

    # Timing alignment bonus for protocol-area changes.
    if change_kind == "MODIFY_PROTOCOL":
        query_timing   = bool(_TIMING_KEYWORDS & set(_tokenize(query_text.lower())))
        cand_timing    = bool(_TIMING_KEYWORDS & set(_tokenize(candidate_text.lower())))
        if query_timing and cand_timing:
            base = min(1.0, base + 0.30)

    return min(1.0, base)


def _score_lexical_similarity(query_text: str, candidate_text: str) -> float:
    """Dice coefficient over non-stop-word tokens shared between query and candidate.

    Dice = 2 * |A ∩ B| / (|A| + |B|)

    This is a quick, dependency-free approximation of BM25-style lexical matching.
    It rewards tests that use the same terminology as the change text (important
    for spec-level terminology like cluster IDs, requirement IDs, SHALL/MUST language).
    """
    q_tokens = set(_tokenize(query_text.lower())) - _STOP_WORDS
    c_tokens = set(_tokenize(candidate_text.lower())) - _STOP_WORDS

    if not q_tokens or not c_tokens:
        return 0.0

    intersection = len(q_tokens & c_tokens)
    return (2 * intersection) / (len(q_tokens) + len(c_tokens))


def _score_chunk_type(chunk_type: str) -> float:
    """Return a preference score for the candidate's chunk type.

    ``intent_summary`` chunks summarise what a test validates — the most useful
    context for re-ranking.  ``setup`` / ``teardown`` chunks rarely contain
    the core behavioural assertion, so they rank lower.
    """
    return _CHUNK_TYPE_SCORE.get(chunk_type.lower() if chunk_type else "", _DEFAULT_CHUNK_TYPE_SCORE)


# ---------------------------------------------------------------------------
# Reason string builder
# ---------------------------------------------------------------------------

def _build_reason(
    breakdown: Dict[str, float],
    weights: RerankerWeights,
    structured_change: Dict[str, Any],
    candidate: Dict[str, Any],
) -> str:
    """Generate a concise human-readable explanation of why this candidate ranked highly."""
    parts: List[str] = []

    if breakdown["kg_direct_bonus"] > 0:
        parts.append("KG direct link")
    elif breakdown["kg_indirect_bonus"] > 0:
        parts.append("KG indirect link")

    eo = breakdown["entity_overlap"]
    if eo >= weights.entity_overlap * 0.9:
        entities = structured_change.get("entities", [])
        names = [e.get("name", "") for e in entities if e.get("name")]
        if names:
            parts.append(f"entity match: {', '.join(names[:3])}")
        else:
            parts.append("full entity overlap")
    elif eo >= weights.entity_overlap * 0.5:
        parts.append("partial entity overlap")

    cm = breakdown["cluster_match"]
    if cm >= weights.cluster_match * 0.9:
        parts.append(f"cluster match ({structured_change.get('cluster', '')})")
    elif cm >= weights.cluster_match * 0.5:
        parts.append("partial cluster match")

    ceo = breakdown["condition_effect_overlap"]
    if ceo >= weights.condition_effect_overlap * 0.9:
        parts.append("covers both condition and effect entities")
    elif ceo >= weights.condition_effect_overlap * 0.5:
        parts.append("covers condition/effect entities")

    im = breakdown["intent_match"]
    if im >= weights.intent_match * 0.8:
        intents = candidate.get("metadata", {}).get("test_intents", [])
        if intents:
            parts.append(f"intent aligned ({', '.join(intents[:2])})")
        else:
            parts.append("intent aligned (keyword)")

    if not parts:
        parts.append("general cluster/entity relevance")

    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Main re-ranker
# ---------------------------------------------------------------------------

class CandidateReranker:
    """Re-rank retrieved test-case candidates using structural signals.

    Parameters
    ----------
    weights:
        ``RerankerWeights`` instance.  Pass custom weights for domain-specific
        tuning (e.g. raise ``kg_direct_bonus`` if KG coverage is high).
    """

    def __init__(self, weights: Optional[RerankerWeights] = None) -> None:
        self._w = weights or RerankerWeights()

    def rerank(
        self,
        structured_change: Dict[str, Any],
        query_text: str,
        candidates: List[Dict[str, Any]],
        kg_hits: Optional[Dict[str, Any]] = None,
        top_n: int = 5,
    ) -> List[RankedCandidate]:
        """Score, deduplicate, and rank candidates.

        Parameters
        ----------
        structured_change:
            Output of ``ChangeExtractor.extract().to_dict()`` (or a compatible
            dict).  Keys: ``change_kind``, ``cluster``, ``entities``,
            ``conditions``, ``effects``.

        query_text:
            The raw PR/spec change text that produced this structured change.
            Used for lexical similarity comparison.

        candidates:
            List of candidate dicts retrieved from the vector DB.  Expected
            keys per candidate:

            .. code-block:: python

               {
                 "candidate_id": "cand_1",
                 "test_case_id": "TC_OCC_1",
                 "chunk_type":   "intent_summary",   # optional
                 "title":        "...",               # optional
                 "text":         "...",               # full chunk text
                 "score":        0.82,                # optional: original cosine sim
                 "metadata": {
                   "cluster":      "OccupancySensing",  # optional
                   "entities":     ["Occupancy", ...],  # optional
                   "test_intents": ["observe_event"],   # optional
                 }
               }

        kg_hits:
            Optional knowledge-graph result for this change chunk.  Expected
            keys: ``direct_tests`` (list[str]), ``indirect_tests`` (list[str]),
            ``matched_entities`` (list[str]).

        top_n:
            Maximum number of ranked results to return.  Deduplication by
            ``test_case_id`` happens before the top-n cut.

        Returns
        -------
        List[RankedCandidate]
            Sorted by descending ``final_score``.
        """
        if not candidates:
            return []

        # Build lookup sets from KG hits (lower-cased for safety).
        kg_direct: Set[str]   = set()
        kg_indirect: Set[str] = set()
        if kg_hits:
            kg_direct   = {tc.lower() for tc in kg_hits.get("direct_tests", [])}
            kg_indirect = {tc.lower() for tc in kg_hits.get("indirect_tests", [])}

        scored: List[RankedCandidate] = []

        for cand in candidates:
            try:
                rc = self._score_candidate(
                    structured_change, query_text, cand, kg_direct, kg_indirect
                )
                scored.append(rc)
            except Exception as exc:
                cid = cand.get("candidate_id", "?")
                logger.warning("[Reranker] Skipping candidate %s due to error: %s", cid, exc)

        # Deduplicate: keep the highest-scoring chunk per test_case_id.
        best_per_tc: Dict[str, RankedCandidate] = {}
        for rc in scored:
            key = rc.test_case_id.lower()
            if key not in best_per_tc or rc.final_score > best_per_tc[key].final_score:
                best_per_tc[key] = rc

        ranked = sorted(
            best_per_tc.values(),
            key=lambda r: (-round(r.final_score, 2), r.test_case_id),
        )

        logger.debug(
            "[Reranker] %d candidates → %d unique TCs → returning top %d",
            len(scored), len(best_per_tc), min(top_n, len(ranked)),
        )
        return ranked[:top_n]

    # ------------------------------------------------------------------
    # Internal: score a single candidate
    # ------------------------------------------------------------------

    def _score_candidate(
        self,
        structured_change: Dict[str, Any],
        query_text: str,
        candidate: Dict[str, Any],
        kg_direct: Set[str],
        kg_indirect: Set[str],
    ) -> RankedCandidate:
        w = self._w
        meta         = candidate.get("metadata") or {}
        text         = candidate.get("text") or ""
        chunk_type   = candidate.get("chunk_type") or meta.get("chunk_type") or ""
        candidate_cluster  = meta.get("cluster") or meta.get("cluster_name") or ""
        meta_entities      = meta.get("entities") or []
        declared_intents   = meta.get("test_intents") or []
        original_score     = float(candidate.get("score") or candidate.get("retrieval_score") or 0.0)
        test_case_id       = candidate.get("test_case_id") or meta.get("tc_id") or ""

        change_entities = structured_change.get("entities") or []
        change_cluster  = structured_change.get("cluster") or ""
        change_kind     = structured_change.get("change_kind") or "UNKNOWN"

        # ---- Component raw scores (each in [0, 1]) -----------------------

        raw_entity_overlap = _score_entity_overlap(
            change_entities, meta_entities, text
        )
        raw_cluster_match = _score_cluster_match(
            change_cluster, candidate_cluster, text
        )
        raw_cond_effect = _score_condition_effect_overlap(
            structured_change, meta_entities, declared_intents, text
        )
        raw_intent_match = _score_intent_match(
            change_kind, declared_intents, query_text, text
        )
        # KG bonuses: direct takes priority over indirect (mutually exclusive).
        tc_key = test_case_id.lower()
        raw_kg_direct   = 1.0 if tc_key in kg_direct   else 0.0
        raw_kg_indirect = 1.0 if (tc_key in kg_indirect and tc_key not in kg_direct) else 0.0

        raw_lexical   = _score_lexical_similarity(query_text, text)
        raw_chunk     = _score_chunk_type(chunk_type)
        raw_retrieval = min(1.0, original_score)  # cosine sim already 0–1

        # ---- Weighted sum ------------------------------------------------

        breakdown = {
            "entity_overlap":          round(raw_entity_overlap  * w.entity_overlap,          4),
            "cluster_match":           round(raw_cluster_match   * w.cluster_match,            4),
            "condition_effect_overlap": round(raw_cond_effect    * w.condition_effect_overlap, 4),
            "intent_match":            round(raw_intent_match    * w.intent_match,             4),
            "kg_direct_bonus":         round(raw_kg_direct       * w.kg_direct_bonus,          4),
            "kg_indirect_bonus":       round(raw_kg_indirect     * w.kg_indirect_bonus,        4),
            "lexical_similarity":      round(raw_lexical         * w.lexical_similarity,       4),
            "chunk_type_bonus":        round(raw_chunk           * w.chunk_type_bonus,         4),
            "retrieval_score":         round(raw_retrieval       * w.retrieval_score,          4),
        }
        final_score = min(1.0, sum(breakdown.values()))

        reason = _build_reason(breakdown, w, structured_change, candidate)

        return RankedCandidate(
            candidate_id   = candidate.get("candidate_id") or "",
            test_case_id   = test_case_id,
            final_score    = round(final_score, 4),
            score_breakdown = breakdown,
            reason         = reason,
            chunk_type     = chunk_type,
            title          = candidate.get("title") or "",
            text           = text,
            metadata       = meta,
        )


# ---------------------------------------------------------------------------
# Public convenience function
# ---------------------------------------------------------------------------

def rerank_candidates(
    structured_change: Dict[str, Any],
    query_text: str,
    candidates: List[Dict[str, Any]],
    kg_hits: Optional[Dict[str, Any]] = None,
    top_n: int = 5,
    weights: Optional[RerankerWeights] = None,
) -> List[RankedCandidate]:
    """Convenience wrapper — create a ``CandidateReranker`` and call ``rerank``.

    Suitable for one-shot use in pipeline nodes::

        ranked = rerank_candidates(change.to_dict(), chunk.text, vector_hits, kg_hits)
    """
    return CandidateReranker(weights=weights).rerank(
        structured_change=structured_change,
        query_text=query_text,
        candidates=candidates,
        kg_hits=kg_hits,
        top_n=top_n,
    )


# ---------------------------------------------------------------------------
# Example invocation (run as a script for quick smoke-testing)
# ---------------------------------------------------------------------------

def _example() -> None:
    """Minimal smoke-test demonstrating re-ranker usage."""

    structured_change = {
        "chunk_id":    "pr_chunk_12",
        "change_kind": "conditional_behavior_rule",
        "cluster":     "OccupancySensing",
        "entities": [
            {"entity_type": "attribute", "cluster": "OccupancySensing", "name": "Occupancy"},
            {"entity_type": "event",     "cluster": "OccupancySensing", "name": "OccupancyChanged"},
        ],
        "conditions": [
            {"entity_type": "attribute", "cluster": "OccupancySensing",
             "name": "Occupancy", "operator": "==", "value": False},
        ],
        "effects": [
            {"entity_type": "event",     "cluster": "OccupancySensing",
             "name": "OccupancyChanged", "expectation": "shall_not_be_sent"},
        ],
        "old_value":  None,
        "new_value":  None,
        "confidence": 0.82,
        "ambiguous":  False,
    }

    query_text = (
        "When the Occupancy attribute of the OccupancySensing cluster is false, "
        "the OccupancyChanged event shall not be sent."
    )

    candidates = [
        {
            "candidate_id": "cand_1",
            "test_case_id": "TC_OCC_1",
            "chunk_type":   "intent_summary",
            "title":        "If Occupancy is false, OccupancyChanged event shall not be sent",
            "text":         (
                "Test Case ID: TC_OCC_1\n"
                "Verify that when the Occupancy attribute transitions to false, "
                "the OccupancyChanged event is NOT generated by the DUT."
            ),
            "score":        0.88,
            "metadata": {
                "cluster":      "OccupancySensing",
                "entities":     ["Occupancy", "OccupancyChanged"],
                "test_intents": ["validate_behavior_rule", "observe_event"],
            },
        },
        {
            "candidate_id": "cand_2",
            "test_case_id": "TC_OCC_2",
            "chunk_type":   "test_step",
            "title":        "OccupancySensing cluster attribute read",
            "text":         (
                "Read the Occupancy attribute from OccupancySensing cluster. "
                "Verify the returned value is a bitmap8."
            ),
            "score":        0.71,
            "metadata": {
                "cluster":      "OccupancySensing",
                "entities":     ["Occupancy"],
                "test_intents": ["validate_attribute_value"],
            },
        },
        {
            "candidate_id": "cand_3",
            "test_case_id": "TC_OCC_3",
            "chunk_type":   "setup",
            "title":        "OccupancySensing prerequisite setup",
            "text":         "Commission the DUT. Subscribe to all OccupancySensing attributes.",
            "score":        0.65,
            "metadata": {
                "cluster":      "OccupancySensing",
                "entities":     [],
                "test_intents": [],
            },
        },
        {
            "candidate_id": "cand_4",
            "test_case_id": "TC_TSTAT_1",
            "chunk_type":   "intent_summary",
            "title":        "Thermostat event validation",
            "text":         "Verify that the Thermostat cluster emits no spurious events.",
            "score":        0.63,
            "metadata": {
                "cluster":      "Thermostat",
                "entities":     [],
                "test_intents": ["observe_event"],
            },
        },
    ]

    kg_hits = {
        "direct_tests":    ["TC_OCC_1"],
        "indirect_tests":  ["TC_OCC_2"],
        "matched_entities": [
            "ATTRIBUTE::OccupancySensing::Occupancy",
            "EVENT::OccupancySensing::OccupancyChanged",
        ],
    }

    ranked = rerank_candidates(
        structured_change=structured_change,
        query_text=query_text,
        candidates=candidates,
        kg_hits=kg_hits,
        top_n=5,
    )

    print("\n=== Re-ranked candidates ===\n")
    for i, r in enumerate(ranked, start=1):
        print(f"#{i}  {r.test_case_id:15s}  score={r.final_score:.4f}  type={r.chunk_type}")
        print(f"     reason: {r.reason}")
        bd = r.score_breakdown
        non_zero = {k: v for k, v in bd.items() if v > 0}
        print(f"     breakdown: {non_zero}")
        print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    _example()
