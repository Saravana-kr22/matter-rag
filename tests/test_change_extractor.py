"""Unit tests for ChangeExtractor — rule-based and LLM fallback paths.

Covers:
- Quality flag detection: [ADDED: Q] → QUIETER_REPORTING_CHANGED (not Nullable)
- Quality flag detection: [REMOVED: N] → NON_VOLATILE_CHANGED
- Conformance column change: [CHANGED: M → O] → CONFORMANCE_CHANGED
- Access column change: [CHANGED: R → RW] → ACCESS_CHANGED
- Data type change: [CHANGED: uint16 → int32] → DATATYPE_CHANGED
- Fallback/default change: [CHANGED: TRUE → FALSE] → FALLBACK_CHANGED
- Priority ordering: Q + conformance in same text → QUIETER_REPORTING_CHANGED wins
- Whole-row ADD_ATTRIBUTE vs column [ADDED: Q] in same section
- LLM fallback: verify correct prompt content (ChangeKind values, quality names, system prompt)
- LLM fallback NOT called when rule-based confidence is high
- Condition text correctness (human-readable strings appended to StructuredChange.conditions)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

from src.processor.change_extractor import (
    ChangeExtractor,
    ChangeKind,
    StructuredChange,
    _LLM_EXTRACTION_PROMPT,
    _LLM_EXTRACTION_SYSTEM,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extractor(llm=None, threshold: float = 0.6) -> ChangeExtractor:
    return ChangeExtractor(llm_provider=llm, confidence_threshold=threshold)


def _make_llm_response(
    change_kind: str = "MODIFY_ATTRIBUTE",
    cluster: str = "On/Off",
    confidence: float = 0.85,
    ambiguous: bool = False,
) -> str:
    """Return a well-formed JSON string that _parse_llm_json can decode."""
    return json.dumps({
        "change_kind": change_kind,
        "cluster": cluster,
        "entities": [],
        "conditions": [],
        "effects": [],
        "old_value": "",
        "new_value": "",
        "confidence": confidence,
        "ambiguous": ambiguous,
    })


# ---------------------------------------------------------------------------
# Quality flag: [ADDED: Q] → QUIETER_REPORTING_CHANGED
# ---------------------------------------------------------------------------

class TestQualityFlagAdded:

    DIFF_TEXT = (
        "0x4001 OnTime uint16 all [ADDED: Q] RW VO LT\n"
        "The OnTime attribute has the Quality flag Q (Quieter Reporting) added."
    )

    def test_change_kind_is_quieter_reporting(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.change_kind == ChangeKind.QUIETER_REPORTING_CHANGED

    def test_not_nullable(self):
        """The old wrong behaviour was to emit NULLABLE_CHANGED — must never happen."""
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert "NULLABLE" not in str(change.change_kind).upper()

    def test_condition_says_quieter_reporting(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert any("Quieter Reporting" in c for c in change.conditions), (
            f"Expected 'Quieter Reporting' in conditions, got {change.conditions}"
        )

    def test_condition_does_not_say_nullable(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        for cond in change.conditions:
            assert "Nullable" not in cond, (
                f"condition contains 'Nullable' (old wrong label): {cond}"
            )

    def test_confidence_is_high(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.confidence >= 0.85

    def test_not_ambiguous(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.ambiguous is False


# ---------------------------------------------------------------------------
# Quality flag: [REMOVED: Q] → QUIETER_REPORTING_CHANGED
# ---------------------------------------------------------------------------

class TestQualityFlagRemoved:

    DIFF_TEXT = (
        "0x4001 OnTime uint16 all [REMOVED: Q] RW VO LT\n"
    )

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["REMOVED"],
        )
        assert change.change_kind == ChangeKind.QUIETER_REPORTING_CHANGED

    def test_condition_says_removed(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["REMOVED"],
        )
        assert any("removed" in c.lower() and "Quieter Reporting" in c
                   for c in change.conditions)


# ---------------------------------------------------------------------------
# Quality flag: [REMOVED: N] → NON_VOLATILE_CHANGED
# ---------------------------------------------------------------------------

class TestNonVolatileRemoved:

    DIFF_TEXT = "0x0010 SomeAttr uint8 M [REMOVED: N] R V\n"

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["REMOVED"],
        )
        assert change.change_kind == ChangeKind.NON_VOLATILE_CHANGED

    def test_condition_text(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["REMOVED"],
        )
        assert any("Non-volatile" in c for c in change.conditions)


# ---------------------------------------------------------------------------
# Conformance column: [CHANGED: M → O] → CONFORMANCE_CHANGED
# ---------------------------------------------------------------------------

class TestConformanceChanged:

    DIFF_TEXT = (
        "0x0001 GlobalSceneControl boolean [CHANGED: M → O] R V\n"
        "Conformance changed from mandatory to optional.\n"
    )

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert change.change_kind == ChangeKind.CONFORMANCE_CHANGED

    def test_condition_text(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert any("M" in c and "O" in c for c in change.conditions), (
            f"Expected 'M → O' in conditions, got {change.conditions}"
        )

    def test_old_new_values(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert change.old_value == "M"
        assert change.new_value == "O"


# ---------------------------------------------------------------------------
# Access column: [CHANGED: R → RW] → ACCESS_CHANGED
# ---------------------------------------------------------------------------

class TestAccessChanged:

    DIFF_TEXT = "0x0000 OnOff boolean M [CHANGED: R → RW] V\n"

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert change.change_kind == ChangeKind.ACCESS_CHANGED

    def test_condition_text(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert any("R" in c and "RW" in c for c in change.conditions)


# ---------------------------------------------------------------------------
# Data type: [CHANGED: uint16 → int32] → DATATYPE_CHANGED
# ---------------------------------------------------------------------------

class TestDatatypeChanged:

    DIFF_TEXT = "0x4002 OffWaitTime [CHANGED: uint16 → int32] M RW VO LT\n"

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert change.change_kind == ChangeKind.DATATYPE_CHANGED

    def test_condition_text(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert any("uint16" in c and "int32" in c for c in change.conditions)


# ---------------------------------------------------------------------------
# Fallback/default: [CHANGED: TRUE → FALSE] → FALLBACK_CHANGED
# ---------------------------------------------------------------------------

class TestFallbackChanged:

    DIFF_TEXT = "0x0000 OnOff boolean M R V [CHANGED: TRUE → FALSE]\n"

    def test_change_kind(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert change.change_kind == ChangeKind.FALLBACK_CHANGED

    def test_condition_text(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        assert any("TRUE" in c.upper() and "FALSE" in c.upper() for c in change.conditions)


# ---------------------------------------------------------------------------
# Priority: Q and conformance change in same text → QUIETER_REPORTING_CHANGED wins
# ---------------------------------------------------------------------------

class TestPriorityQualityOverConformance:

    DIFF_TEXT = (
        "0x4001 OnTime uint16 [CHANGED: M → O] [ADDED: Q] RW VO LT\n"
    )

    def test_quieter_reporting_wins(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED", "ADDED"],
        )
        assert change.change_kind == ChangeKind.QUIETER_REPORTING_CHANGED, (
            f"Expected QUIETER_REPORTING_CHANGED to beat CONFORMANCE_CHANGED, "
            f"got {change.change_kind}"
        )

    def test_both_conditions_recorded(self):
        """Both the Q flag and the conformance change should appear in conditions."""
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED", "ADDED"],
        )
        has_q = any("Quieter Reporting" in c for c in change.conditions)
        has_conf = any("M" in c and "O" in c for c in change.conditions)
        assert has_q, f"Missing Quieter Reporting condition; got {change.conditions}"
        assert has_conf, f"Missing conformance condition; got {change.conditions}"


# ---------------------------------------------------------------------------
# ADD_ATTRIBUTE: whole-row add (no column-level annotation) → ADD_ATTRIBUTE
# ---------------------------------------------------------------------------

class TestWholeRowAddAttribute:

    DIFF_TEXT = "+| 0x0050 | NewAttr | uint8 | M | R V | |"

    def test_add_attribute(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.change_kind == ChangeKind.ADD_ATTRIBUTE

    def test_not_quieter_reporting(self):
        """Whole-row add must NOT be classified as QUIETER_REPORTING_CHANGED."""
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.change_kind != ChangeKind.QUIETER_REPORTING_CHANGED


# ---------------------------------------------------------------------------
# [ADDED: Q] in attribute section → NOT ADD_ATTRIBUTE (it's a column change)
# ---------------------------------------------------------------------------

class TestQualityFlagNotMistokenAsRowAdd:

    DIFF_TEXT = (
        "0x4001 OnTime uint16 all [ADDED: Q] RW VO LT\n"
        "Quality flag Q (Quieter Reporting) added to existing OnTime attribute.\n"
    )

    def test_not_add_attribute(self):
        change = _extractor().extract(
            self.DIFF_TEXT,
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        assert change.change_kind != ChangeKind.ADD_ATTRIBUTE, (
            "[ADDED: Q] for an existing attribute should not produce ADD_ATTRIBUTE"
        )


# ---------------------------------------------------------------------------
# LLM fallback: prompt content verification
# ---------------------------------------------------------------------------

class TestLLMFallbackPrompt:
    """Verify the LLM receives the correct prompt when confidence is low."""

    def _run_with_mock_llm(self, diff_text: str, cluster: str = "On/Off") -> MagicMock:
        mock_llm = MagicMock()
        mock_llm.complete.return_value = _make_llm_response(
            change_kind="MODIFY_ATTRIBUTE",
            cluster=cluster,
            confidence=0.75,
            ambiguous=False,
        )
        # Use a low threshold and ambiguous-by-design text to force LLM path
        extractor = ChangeExtractor(llm_provider=mock_llm, confidence_threshold=0.95)
        extractor.extract(
            diff_text,
            cluster_hint=cluster,
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        return mock_llm

    def test_llm_called_when_low_confidence(self):
        mock_llm = self._run_with_mock_llm(
            "Some vague attribute text with CHANGED annotation but no detail.",
            cluster="On/Off",
        )
        assert mock_llm.complete.called, "LLM should be called for ambiguous low-confidence input"

    def test_llm_prompt_contains_correct_changekind_values(self):
        """The prompt must list QUIETER_REPORTING_CHANGED, not NULLABLE_CHANGED."""
        mock_llm = self._run_with_mock_llm(
            "Vague changed attribute line [CHANGED: some thing → other]",
            cluster="On/Off",
        )
        prompt_used = mock_llm.complete.call_args[0][0]
        assert "QUIETER_REPORTING_CHANGED" in prompt_used, (
            "Prompt must reference QUIETER_REPORTING_CHANGED"
        )
        assert "NULLABLE_CHANGED" not in prompt_used, (
            "Prompt must NOT contain the old NULLABLE_CHANGED value"
        )

    def test_llm_system_prompt_correct(self):
        """System prompt passed as kwarg must match _LLM_EXTRACTION_SYSTEM."""
        mock_llm = self._run_with_mock_llm(
            "Vague changed attribute line [CHANGED: something → else]",
        )
        kwargs = mock_llm.complete.call_args.kwargs
        system_arg = kwargs.get("system", "")
        assert system_arg == _LLM_EXTRACTION_SYSTEM, (
            f"system kwarg mismatch.\n  got: {system_arg!r}\n  expected: {_LLM_EXTRACTION_SYSTEM!r}"
        )

    def test_llm_prompt_contains_cluster(self):
        mock_llm = self._run_with_mock_llm(
            "Vague changed attribute [CHANGED: x → y]",
            cluster="Door Lock",
        )
        prompt_used = mock_llm.complete.call_args[0][0]
        assert "Door Lock" in prompt_used

    def test_llm_prompt_contains_change_text(self):
        snippet = "0x4001 OnTime [CHANGED: special_old → special_new]"
        mock_llm = self._run_with_mock_llm(snippet, cluster="On/Off")
        prompt_used = mock_llm.complete.call_args[0][0]
        assert "special_old" in prompt_used
        assert "special_new" in prompt_used


# ---------------------------------------------------------------------------
# LLM fallback NOT called when confidence is already high
# ---------------------------------------------------------------------------

class TestLLMNotCalledForHighConfidence:

    def test_llm_skipped_for_quality_flag(self):
        """High-confidence Q detection should not trigger LLM."""
        mock_llm = MagicMock()
        extractor = ChangeExtractor(llm_provider=mock_llm, confidence_threshold=0.6)
        extractor.extract(
            "0x4001 OnTime uint16 all [ADDED: Q] RW VO LT",
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["ADDED"],
        )
        mock_llm.complete.assert_not_called()

    def test_llm_skipped_for_conformance(self):
        mock_llm = MagicMock()
        extractor = ChangeExtractor(llm_provider=mock_llm, confidence_threshold=0.6)
        extractor.extract(
            "0x0001 GlobalSceneControl boolean [CHANGED: M → O] R V",
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        mock_llm.complete.assert_not_called()

    def test_llm_skipped_for_datatype(self):
        mock_llm = MagicMock()
        extractor = ChangeExtractor(llm_provider=mock_llm, confidence_threshold=0.6)
        extractor.extract(
            "0x4002 OffWaitTime [CHANGED: uint16 → int32] M RW VO LT",
            cluster_hint="On/Off",
            section_hint="Attributes",
            change_types_hint=["CHANGED"],
        )
        mock_llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# _LLM_EXTRACTION_PROMPT template sanity
# ---------------------------------------------------------------------------

class TestPromptTemplate:
    """Verify the prompt template itself has the correct quality names."""

    def test_prompt_template_has_quieter_reporting_changekind(self):
        rendered = _LLM_EXTRACTION_PROMPT.format(
            cluster="On/Off",
            change_text="test diff text",
        )
        assert "QUIETER_REPORTING_CHANGED" in rendered

    def test_prompt_template_no_nullable_changekind(self):
        rendered = _LLM_EXTRACTION_PROMPT.format(
            cluster="On/Off",
            change_text="test diff text",
        )
        assert "NULLABLE_CHANGED" not in rendered
