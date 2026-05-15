"""Tests for GenericChunker and MatterTCChunker."""
import textwrap
import pytest

from src.chunker.base_chunker import GenericChunker
from src.chunker.matter_tc_chunker import (
    IgnoreRule,
    MatterTCChunker,
    TCRecord,
    apply_ignore_rules,
)
from src.loader.base_loader import Document


# ---------------------------------------------------------------------------
# GenericChunker
# ---------------------------------------------------------------------------

def test_generic_chunker_empty():
    c = GenericChunker()
    assert c.chunk("", {}) == []
    assert c.chunk("   ", {}) == []


def test_generic_chunker_short_text():
    c = GenericChunker(chunk_size=100, chunk_overlap=10)
    docs = c.chunk("hello world", {"source": "x"})
    assert len(docs) == 1
    assert docs[0].page_content == "hello world"
    assert docs[0].metadata["chunk_index"] == 0


def test_generic_chunker_overlap():
    c = GenericChunker(chunk_size=50, chunk_overlap=10)
    text = "A" * 200
    docs = c.chunk(text, {})
    assert len(docs) > 1
    # Each chunk except last should be chunk_size chars
    assert len(docs[0].page_content) == 50
    # Overlap: end of chunk 0 should appear at start of chunk 1
    assert docs[0].page_content[-10:] == docs[1].page_content[:10]


def test_generic_chunker_metadata_preserved():
    c = GenericChunker(chunk_size=100, chunk_overlap=0)
    docs = c.chunk("x" * 250, {"path": "a.txt", "section": "intro"})
    for doc in docs:
        assert doc.metadata["path"] == "a.txt"
        assert doc.metadata["section"] == "intro"
    assert docs[0].metadata["chunk_index"] == 0
    assert docs[1].metadata["chunk_index"] == 1


# ---------------------------------------------------------------------------
# MatterTCChunker — non-TC text fallback
# ---------------------------------------------------------------------------

def test_matter_tc_chunker_fallback_no_tc():
    c = MatterTCChunker(chunk_size=100, chunk_overlap=10)
    text = "This is regular AsciiDoc content without any TC headings.\n" * 5
    docs = c.chunk(text, {"path": "spec.adoc"})
    assert len(docs) >= 1
    assert all(isinstance(d, Document) for d in docs)


# ---------------------------------------------------------------------------
# MatterTCChunker — TC detection
# ---------------------------------------------------------------------------

SAMPLE_TC_ADOC = textwrap.dedent("""\
    == TC-OO-2.1 [DUT as Server]

    === Purpose
    Verify that the DUT responds correctly to OnOff commands.

    === PICS
    [PICS.OO.S]
    [PICS.OO.C.00.Rsp]

    === Test Steps
    1. Commission DUT to TH.
    2. Send OnOff command to DUT.
    3. Verify DUT state changes.

    == TC-LV-2.1 [DUT as Server]

    === Purpose
    Verify that the DUT supports Level Control.

    === PICS
    [PICS.LV.S]
    """)


def test_matter_tc_chunker_detects_tc_ids():
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50)
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "TC-OO.adoc"})
    tc_ids = {d.metadata.get("tc_id") for d in docs if d.metadata.get("tc_id")}
    assert "TC-OO-2.1" in tc_ids
    assert "TC-LV-2.1" in tc_ids


def test_matter_tc_chunker_cluster_name():
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50)
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "TC-OO.adoc"})
    oo_docs = [d for d in docs if d.metadata.get("tc_id") == "TC-OO-2.1"]
    assert all(d.metadata.get("cluster_name") == "OO" for d in oo_docs)


def test_matter_tc_chunker_pics_codes():
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50)
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "TC-OO.adoc"})
    oo_docs = [d for d in docs if d.metadata.get("tc_id") == "TC-OO-2.1"]
    all_pics = [code for d in oo_docs for code in (d.metadata.get("pics_codes") or [])]
    assert "OO.S" in all_pics or "OO.C.00.Rsp" in all_pics


def test_matter_tc_chunker_section_types():
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50)
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "TC-OO.adoc"})
    section_types = {d.metadata.get("section_type") for d in docs if d.metadata.get("section_type")}
    # Should have detected purpose and steps at minimum
    assert section_types & {"purpose", "steps", "pics"}


def test_matter_tc_chunker_returns_documents():
    c = MatterTCChunker()
    docs = c.chunk(SAMPLE_TC_ADOC, {})
    assert all(isinstance(d, Document) for d in docs)
    assert all(d.page_content for d in docs)


# ---------------------------------------------------------------------------
# IgnoreRule — unit tests for apply_ignore_rules
# ---------------------------------------------------------------------------

def test_ignore_line_contains():
    text = "Line one.\nNOTE: ignore me.\nLine three."
    rules = [IgnoreRule(pattern="NOTE:", scope="line", match="startswith")]
    result = apply_ignore_rules(text, rules)
    assert "NOTE:" not in result
    assert "Line one." in result
    assert "Line three." in result


def test_ignore_line_exact():
    text = "keep this\nremove this exactly\nkeep this too"
    rules = [IgnoreRule(pattern="remove this exactly", scope="line", match="exact")]
    result = apply_ignore_rules(text, rules)
    assert "remove this exactly" not in result
    assert "keep this" in result


def test_ignore_line_regex():
    text = "// comment line\nnormal line\n// another comment"
    rules = [IgnoreRule(pattern=r"^\s*//", scope="line", match="regex")]
    result = apply_ignore_rules(text, rules)
    assert "//" not in result
    assert "normal line" in result


def test_ignore_paragraph_contains():
    text = "First paragraph.\n\nCopyright (c) 2024 ACME Corp. All rights reserved.\nLicensed under...\n\nThird paragraph."
    rules = [IgnoreRule(pattern="Copyright", scope="paragraph", match="contains")]
    result = apply_ignore_rules(text, rules)
    assert "Copyright" not in result
    assert "First paragraph." in result
    assert "Third paragraph." in result


def test_ignore_block_contains():
    text = "Good line.\n\nNOTE: start of block\ncontinuation of block\n\nGood line again."
    rules = [IgnoreRule(pattern="NOTE:", scope="block", match="startswith")]
    result = apply_ignore_rules(text, rules)
    assert "NOTE:" not in result
    assert "Good line." in result
    assert "Good line again." in result


def test_ignore_case_insensitive_default():
    text = "copyright notice here\nkeep this"
    rules = [IgnoreRule(pattern="COPYRIGHT", scope="line", match="contains")]
    result = apply_ignore_rules(text, rules)
    assert "copyright" not in result
    assert "keep this" in result


def test_ignore_case_sensitive():
    text = "copyright notice here\nCOPYRIGHT NOTICE\nkeep this"
    rules = [IgnoreRule(pattern="COPYRIGHT", scope="line", match="contains", case_sensitive=True)]
    result = apply_ignore_rules(text, rules)
    assert "COPYRIGHT NOTICE" not in result
    assert "copyright notice here" in result   # lowercase kept, not matched


def test_ignore_rule_from_dict():
    d = {"pattern": "NOTE:", "match": "startswith", "scope": "line"}
    rule = IgnoreRule.from_dict(d)
    assert rule.pattern == "NOTE:"
    assert rule.match == "startswith"
    assert rule.scope == "line"
    assert rule.case_sensitive is False


def test_ignore_rule_invalid_match():
    with pytest.raises(ValueError, match="match"):
        IgnoreRule(pattern="x", match="fuzzy")


def test_ignore_rule_invalid_scope():
    with pytest.raises(ValueError, match="scope"):
        IgnoreRule(pattern="x", scope="word")


def test_ignore_rules_applied_before_tc_split():
    """License paragraph must be stripped even when it precedes TC headings."""
    text = textwrap.dedent("""\
        Copyright (c) 2024 CSA. All rights reserved.
        This spec is confidential.

        == TC-OO-2.1 [DUT as Server]

        === Purpose
        Verify OnOff cluster.
        """)
    rules = [IgnoreRule(pattern="Copyright", scope="paragraph", match="contains")]
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50, ignore_rules=rules)
    docs = c.chunk(text, {})
    combined = " ".join(d.page_content for d in docs)
    assert "Copyright" not in combined
    # TC content must still be present
    assert any(d.metadata.get("tc_id") == "TC-OO-2.1" for d in docs)


def test_ignore_rules_accepts_dicts():
    """MatterTCChunker should accept raw dicts from YAML config."""
    rules = [{"pattern": "NOTE:", "match": "startswith", "scope": "line"}]
    c = MatterTCChunker(ignore_rules=rules)
    text = "NOTE: skip this\n== TC-OO-2.1\n\n=== Purpose\nVerify."
    docs = c.chunk(text, {})
    combined = " ".join(d.page_content for d in docs)
    assert "NOTE:" not in combined


def test_no_ignore_rules_unchanged():
    """Without rules, text must pass through unmodified."""
    c = MatterTCChunker(chunk_size=500, chunk_overlap=50)
    text = "== TC-OO-2.1\n\n=== Purpose\nVerify."
    docs = c.chunk(text, {})
    assert any(d.metadata.get("tc_id") == "TC-OO-2.1" for d in docs)


# ---------------------------------------------------------------------------
# Primary chunk — TCRecord structured output
# ---------------------------------------------------------------------------

FULL_TC_ADOC = textwrap.dedent("""\
    == TC-PAVST-2.2 Verify reading CurrentConnections attribute over transports

    === Purpose
    This test case verifies the allocation of the PushAV Transport.

    === PICS
    [PICS.PAVST.S]
    [PICS.AVSM.S]

    === Preconditions
    * DUT (Camera) has been commissioned to TH
    * TH can communicate with DUT

    === Required devices
    * TH (Test Harness Controller)
    * DUT (PushAVStreamTransport-enabled device)

    === Device Topology
    TH and DUT are on the same fabric.

    === Setup
    {comDutTH}.

    === Test Steps

    |===
    | Step | Directions | Expected result

    | 1
    | TH Reads CurrentConnections attribute from the DUT.
    | Verify the number of PushAV Connections is 0.

    | 2
    | TH sends AllocatePushTransport command.
    | Verify DUT returns SUCCESS and a ConnectionID.

    |===

    === Notes
    NOTE: This test case is informative.
    """)


def test_primary_chunk_present():
    """Every TC must yield exactly one primary chunk."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "push_av_stream_transport.adoc"})
    primaries = [d for d in docs if d.metadata.get("chunk_type") == "primary"]
    assert len(primaries) == 1
    assert primaries[0].metadata["tc_id"] == "TC-PAVST-2.2"


def test_tc_record_is_dict():
    """Primary chunk must carry a tc_record dict."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "push_av_stream_transport.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    rec = primary.metadata["tc_record"]
    assert isinstance(rec, dict)
    # Required top-level keys
    for key in ("test_case_id", "title", "source_file", "purpose", "pics",
                "preconditions", "required_devices", "device_topology",
                "test_setup", "test_steps", "notes", "entities", "test_intents"):
        assert key in rec, f"Missing key: {key}"


def test_tc_record_test_case_id():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "push_av_stream_transport.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert primary.metadata["tc_record"]["test_case_id"] == "TC-PAVST-2.2"


def test_tc_record_source_file():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "push_av_stream_transport.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert primary.metadata["tc_record"]["source_file"] == "push_av_stream_transport.adoc"


def test_tc_record_purpose():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert "PushAV Transport" in primary.metadata["tc_record"]["purpose"]


def test_tc_record_pics():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    pics = primary.metadata["tc_record"]["pics"]
    assert "PAVST.S" in pics
    assert "AVSM.S" in pics


def test_tc_record_preconditions():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    precond = primary.metadata["tc_record"]["preconditions"]
    assert isinstance(precond, list)
    assert len(precond) == 2
    assert any("commissioned" in p for p in precond)


def test_tc_record_required_devices():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    devices = primary.metadata["tc_record"]["required_devices"]
    assert isinstance(devices, list)
    assert len(devices) == 2
    names = {d["name"] for d in devices}
    assert "TH" in names
    assert "DUT" in names
    # descriptions parsed from "(…)" pattern
    th = next(d for d in devices if d["name"] == "TH")
    assert "Test Harness Controller" in th["description"]


def test_tc_record_device_topology():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert "same fabric" in primary.metadata["tc_record"]["device_topology"]


def test_tc_record_test_setup():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert "comDutTH" in primary.metadata["tc_record"]["test_setup"]


def test_tc_record_test_steps_table():
    """Test steps parsed from AsciiDoc table format."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    steps = primary.metadata["tc_record"]["test_steps"]
    assert isinstance(steps, list)
    assert len(steps) == 2
    assert steps[0]["step_no"] == 1
    assert "CurrentConnections" in steps[0]["text"]
    assert "0" in steps[0]["expected"]
    assert steps[1]["step_no"] == 2
    assert "AllocatePushTransport" in steps[1]["text"]


def test_tc_record_notes():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    notes = primary.metadata["tc_record"]["notes"]
    assert isinstance(notes, list)
    assert len(notes) >= 1
    assert any("informative" in n for n in notes)


def test_tc_record_entities_and_intents_empty():
    """entities and test_intents are reserved for the KG pipeline — must be empty lists."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    assert primary.metadata["tc_record"]["entities"] == []
    assert primary.metadata["tc_record"]["test_intents"] == []


# ---------------------------------------------------------------------------
# Secondary chunks
# ---------------------------------------------------------------------------

def test_secondary_chunks_present():
    """Expect secondary chunks for purpose, pics, preconditions, test_steps, etc."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    chunk_types = {d.metadata.get("chunk_type") for d in docs}
    assert "primary"       in chunk_types
    assert "purpose"       in chunk_types
    assert "pics"          in chunk_types
    assert "preconditions" in chunk_types
    assert "test_steps"    in chunk_types
    assert "notes"         in chunk_types


def test_secondary_chunks_inherit_tc_id():
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    for d in docs:
        if d.metadata.get("chunk_type") not in ("preamble", None):
            assert d.metadata.get("tc_id") == "TC-PAVST-2.2", (
                f"chunk_type={d.metadata.get('chunk_type')} missing tc_id"
            )


def test_secondary_chunks_backward_compat_section_type():
    """Secondary chunks must expose the old section_type key for downstream consumers."""
    c = MatterTCChunker()
    docs = c.chunk(FULL_TC_ADOC, {"path": "p.adoc"})
    by_type = {d.metadata["chunk_type"]: d.metadata.get("section_type") for d in docs if "chunk_type" in d.metadata}
    assert by_type["purpose"]       == "purpose"
    assert by_type["pics"]          == "pics"
    assert by_type["preconditions"] == "env"
    assert by_type["test_steps"]    == "steps"
    assert by_type["primary"]       == "primary"


# ---------------------------------------------------------------------------
# Test steps — numbered list format
# ---------------------------------------------------------------------------

NUMBERED_STEPS_TC = textwrap.dedent("""\
    == TC-OO-3.1 Verify OnOff with numbered steps

    === Purpose
    Verify basic OnOff behavior.

    === Test Steps
    1. TH commissions DUT to TH.
       Expected: DUT joins the fabric successfully.
    2. TH sends On command.
       Expected: Verify DUT turns on and OnOff attribute reads TRUE.
    3. TH sends Off command.
    """)


def test_numbered_steps_parsed():
    c = MatterTCChunker()
    docs = c.chunk(NUMBERED_STEPS_TC, {"path": "oo.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary")
    steps = primary.metadata["tc_record"]["test_steps"]
    assert len(steps) == 3
    assert steps[0]["step_no"] == 1
    assert "commissions" in steps[0]["text"].lower()
    assert "fabric" in steps[0]["expected"].lower()
    assert steps[1]["step_no"] == 2
    assert "TRUE" in steps[1]["expected"] or "true" in steps[1]["expected"].lower()


# ---------------------------------------------------------------------------
# Multiple TCs — one primary each
# ---------------------------------------------------------------------------

def test_two_tcs_two_primaries():
    c = MatterTCChunker()
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "oo.adoc"})
    primaries = [d for d in docs if d.metadata.get("chunk_type") == "primary"]
    assert len(primaries) == 2
    ids = {p.metadata["tc_id"] for p in primaries}
    assert ids == {"TC-OO-2.1", "TC-LV-2.1"}


def test_primary_page_content_contains_tc_id():
    """Primary page_content is used for embedding — must contain TC id."""
    c = MatterTCChunker()
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "oo.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary"
                   and d.metadata.get("tc_id") == "TC-OO-2.1")
    assert "TC-OO-2.1" in primary.page_content


def test_primary_page_content_contains_purpose():
    c = MatterTCChunker()
    docs = c.chunk(SAMPLE_TC_ADOC, {"path": "oo.adoc"})
    primary = next(d for d in docs if d.metadata.get("chunk_type") == "primary"
                   and d.metadata.get("tc_id") == "TC-OO-2.1")
    assert "OnOff" in primary.page_content


# ---------------------------------------------------------------------------
# TCRecord.to_dict() schema
# ---------------------------------------------------------------------------

def test_tc_record_to_dict_keys():
    record = TCRecord(
        test_case_id="TC-TEST-1.1",
        title="Test title",
        pics=["A.S"],
        test_steps=[{"step_no": 1, "text": "Do X", "expected": "Verify Y", "pics": []}],
    )
    d = record.to_dict()
    expected_keys = {
        "test_case_id", "title", "source_file", "section_group", "category",
        "purpose", "pics", "preconditions", "required_devices", "device_topology",
        "test_setup", "test_steps", "notes", "entities", "test_intents",
    }
    assert set(d.keys()) == expected_keys
    assert d["test_case_id"] == "TC-TEST-1.1"
    assert d["pics"] == ["A.S"]
    assert d["entities"] == []
