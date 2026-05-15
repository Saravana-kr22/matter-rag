"""Vector chunk generator — produces embedding-ready chunks from TestCaseRecords.

Generates up to 4 chunk types per TestCaseRecord:

    full            — complete TC text (title + all subsections joined)
    intent_summary  — title + purpose + detected intents + extracted step actions (short, dense signal)
    procedure       — numbered procedure steps only
    setup           — prerequisites + test environment setup only

Chunks are skipped when the underlying text is empty.  When ``output_dir`` is
provided, TCs that produce zero chunks are written to
``<output_dir>/vector_chunks_ignored_or_rejected.txt`` for debugging.

Each VectorChunkRecord carries rich metadata so that FAISS results can be
decoded back to the TC without hitting the graph:
    tc_id, cluster, chunk_type, intents, mode, source_doc, entity_refs
"""

from __future__ import annotations

import logging
import re as _re
from pathlib import Path
from typing import List

from src.knowledge_graph.schema import (
    TestCaseRecord,
    VectorChunkRecord,
    VectorChunkType,
)

logger = logging.getLogger(__name__)

# BGE-large-en-v1.5 has a 512-token max input (~2048 chars).  Use a conservative
# character limit so that each sub-chunk fits comfortably within one embedding
# window.  The context header added to each part consumes ~100-150 chars.
# Matter content averages ~3.8 chars/token (not 4.0) due to short identifiers.
# Target: 1450 body + ~150 header ≈ 1600 total / 3.8 ≈ 421 tokens (within 512 limit).
_MAX_CHUNK_CHARS = 1450


# ---------------------------------------------------------------------------
# Chunk splitting for oversized full / procedure chunks
# ---------------------------------------------------------------------------

def _split_chunk(
    chunk: VectorChunkRecord,
    tc_id: str,
    title: str,
    cluster: str,
) -> List[VectorChunkRecord]:
    """Split an oversized chunk into sub-chunks of ~_MAX_CHUNK_CHARS each.

    Splits at paragraph boundaries (double newline) first; if a single
    paragraph still exceeds the limit it is further split at sentence
    boundaries ('. ').  Each sub-chunk is prefixed with a TC context
    header so that the embedding captures the TC identity.

    Returns the original chunk unchanged when its text fits within the
    limit.
    """
    text = chunk.text
    if len(text) <= _MAX_CHUNK_CHARS:
        return [chunk]

    # --- Phase 1: split by paragraphs, accumulate into bins ---------------
    paragraphs = text.split('\n\n')
    sub_texts: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for para in paragraphs:
        # If a single paragraph exceeds the limit, split it by sentences
        if len(para) > _MAX_CHUNK_CHARS:
            # Flush anything accumulated so far
            if current_parts:
                sub_texts.append('\n\n'.join(current_parts))
                current_parts = []
                current_len = 0
            # Split the oversized paragraph at sentence boundaries
            sentences = para.split('. ')
            sent_parts: List[str] = []
            sent_len = 0
            for sent in sentences:
                piece = sent if not sent_parts else '. ' + sent
                if sent_len + len(piece) > _MAX_CHUNK_CHARS and sent_parts:
                    sub_texts.append(''.join(sent_parts))
                    sent_parts = [sent]
                    sent_len = len(sent)
                else:
                    sent_parts.append(piece)
                    sent_len += len(piece)
            if sent_parts:
                sub_texts.append(''.join(sent_parts))
            continue

        if current_len + len(para) + 2 > _MAX_CHUNK_CHARS and current_parts:
            sub_texts.append('\n\n'.join(current_parts))
            current_parts = [para]
            current_len = len(para)
        else:
            current_parts.append(para)
            current_len += len(para) + 2  # +2 for '\n\n'

    if current_parts:
        sub_texts.append('\n\n'.join(current_parts))

    # Edge case: splitting produced nothing (shouldn't happen)
    if not sub_texts:
        return [chunk]

    # If splitting resulted in a single chunk, just return the original
    if len(sub_texts) == 1:
        return [chunk]

    total = len(sub_texts)
    header = f"[{tc_id}] {title} — {cluster}"
    result: List[VectorChunkRecord] = []

    for i, part_text in enumerate(sub_texts, 1):
        prefixed = f"{header} (part {i}/{total})\n\n{part_text}"
        part_meta = dict(chunk.metadata)
        part_meta["chunk_part"] = i
        part_meta["chunk_total"] = total
        new_chunk = VectorChunkRecord(
            chunk_id=f"{chunk.chunk_id}::part{i}",
            tc_id=chunk.tc_id,
            chunk_type=chunk.chunk_type,
            text=prefixed,
            metadata=part_meta,
        )
        result.append(new_chunk)

    logger.debug(
        "[vector_chunk_gen] Split %s into %d parts (original %d chars)",
        chunk.chunk_id, total, len(text),
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_vector_chunks(
    test_case_records: List[TestCaseRecord],
    output_dir: str = "",
) -> List[VectorChunkRecord]:
    """Generate embedding-ready VectorChunkRecord objects from TestCaseRecords.

    Args:
        test_case_records: TC records produced by ``test_plan_extractor``.
        output_dir: When non-empty, write a debug log of TCs that produced no
            chunks to ``<output_dir>/vector_chunks_ignored_or_rejected.txt``.
    """
    import time
    total = len(test_case_records)
    logger.info("[vector_chunk_gen] ── Stage: Generate Vector Chunks ── %d test cases", total)
    t0 = time.time()

    chunks: List[VectorChunkRecord] = []
    ignored: List[dict] = []          # TCs that produced zero chunks
    split_count = 0                   # chunks created by oversized splitting
    _PROGRESS_EVERY = max(1, total // 10)   # log every ~10%

    for idx, tc in enumerate(test_case_records, 1):
        tc_chunks, tc_skipped = _chunks_for_tc_with_skipped(tc)

        # Split oversized full / procedure chunks into embedding-friendly parts
        final_tc_chunks: List[VectorChunkRecord] = []
        for chunk in tc_chunks:
            if chunk.chunk_type in (VectorChunkType.FULL, VectorChunkType.PROCEDURE):
                parts = _split_chunk(chunk, tc.id, tc.title, tc.cluster)
                if len(parts) > 1:
                    split_count += len(parts) - 1  # count the extra chunks added
                final_tc_chunks.extend(parts)
            else:
                final_tc_chunks.append(chunk)

        chunks.extend(final_tc_chunks)
        if not final_tc_chunks:
            ignored.append({
                "tc_id": tc.id,
                "title": tc.title,
                "cluster": tc.cluster,
                "source_doc": tc.source_doc,
                "skipped_types": tc_skipped,
                "reason": "all chunk types empty (no text in any of: full, intent_summary, procedure, setup)",
            })
        if idx % _PROGRESS_EVERY == 0 or idx == total:
            pct = int(idx / total * 100)
            logger.info(
                "[vector_chunk_gen] Progress %3d%%  (%d/%d TCs)  chunks_so_far=%d",
                pct, idx, total, len(chunks),
            )

    elapsed = time.time() - t0
    from collections import Counter
    type_counts = Counter(c.chunk_type.value for c in chunks)
    logger.info(
        "[vector_chunk_gen] ── Done ── %d chunks  full=%d  intent_summary=%d  procedure=%d  "
        "setup=%d  ignored_tcs=%d  split_extra=%d  (%.1fs)",
        len(chunks),
        type_counts.get("full", 0),
        type_counts.get("intent_summary", 0),
        type_counts.get("procedure", 0),
        type_counts.get("setup", 0),
        len(ignored),
        split_count,
        elapsed,
    )

    if output_dir and ignored:
        _write_ignored_log(ignored, output_dir)

    return chunks


# ---------------------------------------------------------------------------
# Per-TC chunk builders
# ---------------------------------------------------------------------------

def _chunks_for_tc(tc: TestCaseRecord) -> List[VectorChunkRecord]:
    """Backward-compatible wrapper — returns chunks only."""
    chunks, _ = _chunks_for_tc_with_skipped(tc)
    return chunks


def _chunks_for_tc_with_skipped(
    tc: TestCaseRecord,
) -> tuple[List[VectorChunkRecord], List[str]]:
    """Return (chunks_produced, skipped_chunk_types)."""
    base_meta = {
        "tc_id":      tc.id,
        "cluster":    tc.cluster,
        "mode":       tc.mode.value,
        "intents":    [i.value for i in tc.intents],
        "entity_refs": tc.entity_refs,
        "source_doc": tc.source_doc,
    }

    results: List[VectorChunkRecord] = []
    skipped: List[str] = []

    # ── full ─────────────────────────────────────────────────────────────────
    full_text = _build_full_text(tc)
    if full_text.strip():
        results.append(VectorChunkRecord(
            chunk_id=f"{tc.id}::full",
            tc_id=tc.id,
            chunk_type=VectorChunkType.FULL,
            text=full_text,
            metadata={**base_meta, "chunk_type": VectorChunkType.FULL},
        ))
    else:
        skipped.append("full")

    # ── intent_summary ───────────────────────────────────────────────────────
    summary_text = _build_intent_summary(tc)
    if summary_text.strip():
        results.append(VectorChunkRecord(
            chunk_id=f"{tc.id}::intent_summary",
            tc_id=tc.id,
            chunk_type=VectorChunkType.INTENT_SUMMARY,
            text=summary_text,
            metadata={**base_meta, "chunk_type": VectorChunkType.INTENT_SUMMARY},
        ))
    else:
        skipped.append("intent_summary")

    # ── procedure ────────────────────────────────────────────────────────────
    procedure_text = _build_procedure_text(tc)
    if procedure_text.strip():
        results.append(VectorChunkRecord(
            chunk_id=f"{tc.id}::procedure",
            tc_id=tc.id,
            chunk_type=VectorChunkType.PROCEDURE,
            text=procedure_text,
            metadata={**base_meta, "chunk_type": VectorChunkType.PROCEDURE},
        ))
    else:
        skipped.append("procedure")

    # ── setup ────────────────────────────────────────────────────────────────
    setup_text = _build_setup_text(tc)
    if setup_text.strip():
        results.append(VectorChunkRecord(
            chunk_id=f"{tc.id}::setup",
            tc_id=tc.id,
            chunk_type=VectorChunkType.SETUP,
            text=setup_text,
            metadata={**base_meta, "chunk_type": VectorChunkType.SETUP},
        ))
    else:
        skipped.append("setup")

    return results, skipped


# ---------------------------------------------------------------------------
# Rejected / ignored log
# ---------------------------------------------------------------------------

def _write_ignored_log(ignored: List[dict], output_dir: str) -> None:
    """Write TCs that produced zero vector chunks to a debug log file."""
    out_path = Path(output_dir) / "vector_chunks_ignored_or_rejected.txt"
    try:
        lines = [
            "Vector Chunk Generator — Ignored / Rejected Test Cases",
            "=" * 70,
            f"Total TCs producing no chunks: {len(ignored)}",
            "",
            "These TCs had empty text in all 4 chunk types (full / intent_summary /",
            "procedure / setup).  Common causes:",
            "  - TC extracted with no content (parsing failed)",
            "  - TC record has only tc_id + title, all body fields empty",
            "  - Source doc format not supported by html_semantic_parser",
            "",
            "-" * 70,
        ]
        for entry in ignored:
            lines += [
                f"TC-ID      : {entry['tc_id']}",
                f"Title      : {entry['title']}",
                f"Cluster    : {entry['cluster']}",
                f"Source     : {entry['source_doc']}",
                f"Skipped    : {', '.join(entry['skipped_types'])}",
                f"Reason     : {entry['reason']}",
                "",
            ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("[vector_chunk_gen] Ignored TC log → %s  (%d entries)", out_path, len(ignored))
    except Exception as exc:
        logger.warning("[vector_chunk_gen] Could not write ignored log: %s", exc)


# ---------------------------------------------------------------------------
# Text assembly helpers
# ---------------------------------------------------------------------------

def _build_full_text(tc: TestCaseRecord) -> str:
    parts = []
    if tc.id:
        parts.append(f"Test Case: {tc.id}")
    if tc.title:
        parts.append(f"Title: {tc.title}")
    if tc.cluster:
        parts.append(f"Cluster: {tc.cluster}")
    if tc.purpose:
        parts.append(f"Purpose: {tc.purpose}")
    if tc.prerequisites:
        parts.append(f"Prerequisites: {tc.prerequisites}")
    if tc.setup:
        parts.append(f"Setup: {tc.setup}")
    if tc.procedure_steps:
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(tc.procedure_steps))
        parts.append(f"Procedure:\n{steps}")
    if tc.expected_outcomes:
        outcomes = "\n".join(f"  - {o}" for o in tc.expected_outcomes)
        parts.append(f"Expected Outcomes:\n{outcomes}")
    # fallback to all_text if we have nothing structured
    if len(parts) <= 2 and tc.all_text:
        parts.append(tc.all_text)
    return "\n\n".join(parts)


def _build_intent_summary(tc: TestCaseRecord) -> str:
    intents_str = ", ".join(i.value for i in tc.intents) if tc.intents else "unknown"
    entities_str = ", ".join(
        ref.split("::")[-1] for ref in tc.entity_refs[:5]
    ) if tc.entity_refs else ""

    parts = [f"[{tc.id}] {tc.title}"]
    if tc.cluster:
        parts.append(f"Cluster: {tc.cluster}")
    parts.append(f"Intents: {intents_str}")
    if entities_str:
        parts.append(f"Entities: {entities_str}")
    if tc.purpose:
        parts.append(f"Purpose: {tc.purpose}")
    step_actions = _extract_step_actions(tc.procedure_steps)
    if step_actions:
        parts.append(f"Step actions: {', '.join(step_actions)}")
    return "\n".join(parts)


def _build_procedure_text(tc: TestCaseRecord) -> str:
    if not tc.procedure_steps:
        return ""
    header = f"[{tc.id}] {tc.title} — Procedure"
    steps = "\n".join(f"{i+1}. {s}" for i, s in enumerate(tc.procedure_steps))
    return f"{header}\n{steps}"


def _build_setup_text(tc: TestCaseRecord) -> str:
    parts = [f"[{tc.id}] {tc.title} — Setup"]
    if tc.prerequisites:
        parts.append(f"Prerequisites: {tc.prerequisites}")
    if tc.setup:
        parts.append(f"Test Environment: {tc.setup}")
    if tc.dut_type:
        parts.append(f"DUT Type: {tc.dut_type}")
    if len(parts) == 1:
        return ""   # no setup info — skip this chunk type
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Step action extractor (for intent_summary keyword augmentation)
# ---------------------------------------------------------------------------

# Strip leading step numbers: "1. ", "2) ", "(a) "
_STEP_PREFIX_RE = _re.compile(r'^\s*(?:\d+[.)]\s*|[a-zA-Z][.)]\s*)')

# Title Case 2+-word runs like "Factory Reset", "Door Lock", "Operational Certificate"
_TITLE_CASE_RE = _re.compile(r'(?:[A-Z][a-z]+\s+)+[A-Z][a-z]+')

# Common filler words that add no signal as a leading action word
_TRIVIAL = frozenset({
    "th", "dut", "the", "a", "an", "to", "of", "and", "or", "is", "are",
    "be", "in", "on", "at", "by", "if", "it", "as", "for", "from", "that",
    "this", "with", "its", "not", "no", "so",
})

# Protocol/technology terms that appear in step text in mixed-case or lower-case form
# and would be missed by the Title-Case and all-caps passes.
_PROTOCOL_TERMS = frozenset({
    "ble", "bluetooth", "mdns", "wifi", "wi-fi", "thread", "ipv4", "ipv6",
    "tcp", "udp", "ip", "dns", "nfc", "qr", "qrcode", "commissioning",
    "joiner", "commissioner", "operational", "fabric", "attestation",
    "onboarding", "pairing", "discriminator", "passcode", "timer",
    "timeout", "expiry", "expiration", "heartbeat", "subscription",
    "broadcast", "multicast", "unicast", "discovery", "advertisement",
    "advertising", "scanning", "joining", "rejoining",
    "setupcode", "ssid", "password", "dataset",
})


def _extract_step_actions(procedure_steps: List[str]) -> List[str]:
    """Extract unique action phrases from procedure steps for keyword-biased embedding.

    Three passes:
    0. All-caps words (≥3 chars) + protocol term whitelist — catches BLE, TCP, UDP,
       mdns, wifi, thread, timer, joiner, etc. regardless of capitalisation.
    1. Title Case runs — catches named operations like "Factory Reset", "Door Lock".
    2. First 2 meaningful words per step — catches verbs like "commission dut",
       "send command", "read attribute".

    Returns up to 20 lowercase deduplicated phrases.
    """
    if not procedure_steps:
        return []

    seen: set = set()
    out: List[str] = []

    def _add(phrase: str) -> None:
        phrase = phrase.strip().lower()
        if phrase and phrase not in seen and len(phrase) > 3:
            seen.add(phrase)
            out.append(phrase)

    # Pass 0 — all-caps acronyms (BLE, TCP, UDP, MDNS …) + protocol whitelist
    _ALLCAPS_RE = _re.compile(r'\b[A-Z]{3,}\b')
    for step in procedure_steps:
        # All-caps words ≥ 3 chars (skip DUT/TH which are test-infra noise)
        for word in _ALLCAPS_RE.findall(step):
            if word not in {"DUT", "TH", "THE", "AND", "FOR", "NOT"}:
                _add(word)
        # Protocol terms present anywhere in the step (case-insensitive)
        step_lower = step.lower()
        for term in _PROTOCOL_TERMS:
            if term in step_lower:
                _add(term)

    # Pass 1 — Title Case multi-word phrases anywhere in the step text
    for step in procedure_steps:
        for match in _TITLE_CASE_RE.findall(step):
            _add(match)

    # Pass 2 — first 2 meaningful words of each step after stripping numbering
    for step in procedure_steps:
        text = _STEP_PREFIX_RE.sub("", step, count=1).strip()
        words = [
            w.lower().strip(".,();:")
            for w in text.split()
            if w.lower().strip(".,();:").isalpha()
            and w.lower().strip(".,();:") not in _TRIVIAL
            and len(w) > 2
        ]
        if len(words) >= 2:
            _add(f"{words[0]} {words[1]}")
        elif words:
            _add(words[0])

    return out[:20]
