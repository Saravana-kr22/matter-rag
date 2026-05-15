"""Semantic PR diff chunker — splits PR changes into meaningful units.

Unlike generic token-based chunking, ``SemanticPRChunker`` groups diff content
by **logical change context**: each chunk represents one coherent change unit
(e.g., all changes to the On/Off cluster's Attributes table, or a set of added
commands to the Door Lock cluster).

Chunking strategy (in priority order):

1. **Matter HTML diff sections** — documents with ``doc_type="matter_spec_diff"``
   are already one section per FetchedDocument (from ``ProcessMatterHtmlDoc``).
   These are passed through as single chunks.

2. **[ADDED/REMOVED/CHANGED] annotated text** — from ProcessMatterHtmlDoc output,
   each annotation block becomes its own chunk context.

3. **Unified diff files** — GitHub PR unified diffs are split at:
   - AsciiDoc section headings (``== Heading``, ``=== Heading``)
   - Table-level diff hunks (groups of ``+|`` / ``-|`` lines)
   - Contiguous hunk groups separated by ``@@`` markers

4. **Plain text** — falls back to paragraph-based splitting (blank-line delimited).

Each output ``Document`` carries enriched metadata:
  - ``semantic_chunk_type``: "matter_diff_section" | "diff_hunk" | "section" | "paragraph"
  - ``cluster``:  cluster name (when detectable)
  - ``section``:  section heading text
  - ``change_types``: list of change annotation types found ("ADDED", "REMOVED", "CHANGED")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from src.fetcher.base_fetcher import FetchedDocument
from src.loader.base_loader import Document

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_ADOC_HEADING_RE  = re.compile(r'^(={1,6})\s+(.+)$', re.MULTILINE)
_DIFF_HUNK_RE     = re.compile(r'^@@[^@]*@@', re.MULTILINE)
_ANNOTATION_RE    = re.compile(r'\[(ADDED|REMOVED|CHANGED):', re.IGNORECASE)
_CLUSTER_RE       = re.compile(r'\b([A-Z][A-Za-z\s/]+)\s+[Cc]luster\b')
_TABLE_ROW_RE     = re.compile(r'^[+\-]?\s*\|')
# Conformance change: M→O, O→M, O[X]→M, M→O[LT], etc.
_CONFORMANCE_CHANGE_RE = re.compile(
    r'\b(M|O|P)\s*(?:\[[^\]]*\])?\s*→\s*(M|O|P)\s*(?:\[[^\]]*\])?'
)
# Access change: R→RW, RW→R, etc.
_ACCESS_CHANGE_RE = re.compile(r'\b(R|RW|RWF|W|V)\s*→\s*(R|RW|RWF|W|V)\b')
# Type change: uint8→uint16, bool→enum8, etc.
_TYPE_CHANGE_RE = re.compile(
    r'\b(uint\d+|int\d+|bool|string|enum\d+|map\d+|single|double|utc)\s*→'
    r'\s*(uint\d+|int\d+|bool|string|enum\d+|map\d+|single|double|utc)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Row change-type classifier
# ---------------------------------------------------------------------------

def _classify_row_change_type(ann_text: str) -> str:
    """Return a semantic change-type label for a table-row annotation string."""
    if ann_text.upper().startswith("[ADDED"):
        return "entity_added"
    if ann_text.upper().startswith("[REMOVED"):
        return "entity_removed"
    if _CONFORMANCE_CHANGE_RE.search(ann_text):
        return "conformance_changed"
    if _ACCESS_CHANGE_RE.search(ann_text):
        return "access_changed"
    if _TYPE_CHANGE_RE.search(ann_text):
        return "type_changed"
    return "row_changed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class SemanticChunk:
    """One logical change unit from a PR diff."""
    content: str
    cluster: str = ""
    section: str = ""
    chunk_type: str = "unknown"   # "matter_diff_section" | "diff_hunk" | "section" | "paragraph"
    change_types: List[str] = field(default_factory=list)  # ["ADDED", "REMOVED", "CHANGED"]
    source_path: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_document(self, chunk_index: int = 0) -> Document:
        meta = {
            **self.metadata,
            "semantic_chunk_type": self.chunk_type,
            "cluster":             self.cluster,
            "section":             self.section,
            "change_types":        self.change_types,
            "chunk_index":         chunk_index,
        }
        return Document(page_content=self.content, metadata=meta)


class SemanticPRChunker:
    """Split PR FetchedDocuments into semantically coherent change chunks.

    Args:
        min_chunk_chars: Discard chunks shorter than this (noise filter).
        max_chunk_chars: Hard cap per chunk; splits long sections at paragraph boundaries.
    """

    def __init__(
        self,
        min_chunk_chars: int = 80,
        max_chunk_chars: int = 6000,
    ) -> None:
        self._min = min_chunk_chars
        self._max = max_chunk_chars

    def chunk(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """Return a list of SemanticChunk objects for *doc*."""
        doc_type = doc.metadata.get("doc_type", "")

        # Already a single diff section from ProcessMatterHtmlDoc
        if doc_type == "matter_spec_diff":
            return self._from_diff_section(doc)

        # GitHub unified diff or plain adoc
        if self._looks_like_unified_diff(doc.content):
            return self._from_unified_diff(doc)

        # AsciiDoc with headings
        if _ADOC_HEADING_RE.search(doc.content):
            return self._from_adoc_sections(doc)

        # Fallback: paragraph splitting
        return self._from_paragraphs(doc)

    def chunk_all_with_log(
        self,
        docs: List[FetchedDocument],
        output_dir: str = "",
        label: str = "pr_chunks",
    ) -> List[SemanticChunk]:
        """Chunk all *docs*, write an ignored-items log, and return all chunks.

        Args:
            docs: Documents to chunk.
            output_dir: When non-empty, write
                ``<output_dir>/<label>_ignored_or_rejected.txt``.
            label: Filename prefix for the log (e.g. ``"pr_chunks"``).
        """
        all_chunks: List[SemanticChunk] = []
        rejected: List[dict] = []

        for doc in docs:
            accepted, doc_rejected = self._chunk_with_rejected(doc)
            all_chunks.extend(accepted)
            rejected.extend(doc_rejected)

        logger.info(
            "[semantic_chunker] %d docs → %d chunks accepted, %d segments rejected (too short)",
            len(docs), len(all_chunks), len(rejected),
        )
        if output_dir and rejected:
            _write_chunker_rejected_log(rejected, output_dir, label)

        return all_chunks

    def _chunk_with_rejected(
        self, doc: FetchedDocument
    ) -> Tuple[List[SemanticChunk], List[dict]]:
        """Return (accepted_chunks, rejected_segments) for a single doc."""
        rejected: List[dict] = []
        chunks = self._chunk_collecting_rejected(doc, rejected)
        return chunks, rejected

    def _chunk_collecting_rejected(
        self, doc: FetchedDocument, rejected: List[dict]
    ) -> List[SemanticChunk]:
        """Dispatch to the right handler, threading ``rejected`` through."""
        doc_type = doc.metadata.get("doc_type", "")
        if doc_type == "matter_spec_diff":
            # Filter near-empty diff sections (section heading renames, bare struct
            # field names, etc.) that have no actionable content for LLM analysis.
            content_stripped = doc.content.strip()
            if len(content_stripped) < self._min:
                rejected.append({
                    "source": doc.path,
                    "section": doc.metadata.get("section_title", ""),
                    "cluster": doc.metadata.get("cluster", ""),
                    "chars": len(content_stripped),
                    "text_preview": content_stripped[:100],
                    "reason": "diff_section_too_short",
                })
                return []
            return self._from_diff_section(doc)
        if self._looks_like_unified_diff(doc.content):
            return self._from_unified_diff_r(doc, rejected)
        if _ADOC_HEADING_RE.search(doc.content):
            return self._from_adoc_sections_r(doc, rejected)
        return self._from_paragraphs_r(doc, rejected)

    # ------------------------------------------------------------------
    # Handlers per content type
    # ------------------------------------------------------------------

    def _from_diff_section(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """A ProcessMatterHtmlDoc section → single chunk or entity-row sub-chunks.

        When a section contains >= 2 table-row annotations (each containing `|`
        column separators), it is split into one sub-chunk per changed row.  This
        prevents a single large attribute table change from producing one noisy chunk
        where every row is piled together — instead each changed entity gets its own
        chunk with the section intro as context prefix.

        Sections with only one annotation, or with paragraph-level annotations (no
        `|`), are returned as a single chunk as before.
        """
        change_types = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(doc.content)]
        # Try entity-row sub-chunking for table change sections
        row_chunks = self._try_entity_row_split(doc)
        if row_chunks:
            return row_chunks
        return [SemanticChunk(
            content=doc.content[:self._max],
            cluster=doc.metadata.get("cluster", ""),
            section=doc.metadata.get("section_title", ""),
            chunk_type="matter_diff_section",
            change_types=sorted(set(change_types)),
            source_path=doc.path,
            metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
        )]

    def _try_entity_row_split(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """Split a diff section into per-entity-row sub-chunks.

        Returns a non-empty list when the section contains >= 2 annotations whose
        content includes `|` column separators (i.e. table rows).  Returns [] when
        not applicable so the caller falls back to a single-chunk path.
        """
        content = doc.content
        ann_spans: List[Tuple[int, int, str]] = []
        for m in _ANNOTATION_RE.finditer(content):
            start = m.start()
            # Walk forward tracking bracket depth to find the matching `]`
            depth = 0
            end = start
            for i in range(start, min(start + 4000, len(content))):
                ch = content[i]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            ann_text = content[start:end]
            ann_spans.append((start, end, ann_text))

        # Keep only annotations that look like table rows (contain | separator)
        table_rows = [(s, e, t) for s, e, t in ann_spans if "|" in t]
        if len(table_rows) < 2:
            return []

        # Intro text = everything before the first annotation (section header / prose)
        intro = content[: ann_spans[0][0]].strip()
        base_meta = {k: v for k, v in doc.metadata.items() if k != "_process_rules"}
        cluster = doc.metadata.get("cluster", "")
        section = doc.metadata.get("section_title", "")

        chunks: List[SemanticChunk] = []
        for _, _, ann_text in table_rows:
            ann_match = _ANNOTATION_RE.match(ann_text)
            ann_type = ann_match.group(1).upper() if ann_match else "CHANGED"
            change_type = _classify_row_change_type(ann_text)
            sub_content = ((intro + "\n\n" + ann_text).strip() if intro else ann_text)
            chunks.append(SemanticChunk(
                content=sub_content[: self._max],
                cluster=cluster,
                section=section,
                chunk_type="entity_row",
                change_types=[ann_type],
                source_path=doc.path,
                metadata={**base_meta, "change_type": change_type, "entity_row": ann_text},
            ))
        return chunks

    def _from_unified_diff(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """Split a unified diff at hunk boundaries, grouping by section context."""
        hunks = _DIFF_HUNK_RE.split(doc.content)
        if not hunks:
            return self._from_paragraphs(doc)

        current_section = doc.metadata.get("section", "")
        current_cluster = self._detect_cluster(doc.content[:500])
        chunks: List[SemanticChunk] = []
        buffer_lines: List[str] = []
        buffer_section = current_section
        buffer_cluster = current_cluster

        def _flush(section: str, cluster: str) -> None:
            text = "\n".join(buffer_lines).strip()
            if len(text) >= self._min:
                change_types = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
                # Also detect from +/-lines
                if any(l.startswith('+') for l in buffer_lines):
                    if "ADDED" not in change_types:
                        change_types.append("ADDED")
                if any(l.startswith('-') for l in buffer_lines):
                    if "REMOVED" not in change_types:
                        change_types.append("REMOVED")
                chunks.append(SemanticChunk(
                    content=text[:self._max],
                    cluster=cluster,
                    section=section,
                    chunk_type="diff_hunk",
                    change_types=sorted(set(change_types)),
                    source_path=doc.path,
                    metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                ))
            buffer_lines.clear()

        for part in hunks:
            for line in part.splitlines():
                # Detect AsciiDoc section headings in the diff
                hdr_m = re.match(r'^[+\-]?\s*(={1,6})\s+(.+)$', line)
                if hdr_m:
                    _flush(buffer_section, buffer_cluster)
                    buffer_section = hdr_m.group(2).strip()
                    buffer_cluster = self._detect_cluster(buffer_section) or buffer_cluster
                buffer_lines.append(line)
                # Split at large tables (flush after 50 table rows)
                if _TABLE_ROW_RE.match(line) and len(buffer_lines) > 50:
                    _flush(buffer_section, buffer_cluster)

        _flush(buffer_section, buffer_cluster)

        if not chunks:
            return self._from_paragraphs(doc)
        return chunks

    def _from_adoc_sections(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """Split at AsciiDoc ``==`` headings."""
        parts = _ADOC_HEADING_RE.split(doc.content)
        # split() returns [pre, level, heading, body, level, heading, body, ...]
        chunks: List[SemanticChunk] = []
        current_cluster = self._detect_cluster(doc.content[:500])

        # pre-heading content
        if parts and parts[0].strip():
            chunks.append(SemanticChunk(
                content=parts[0].strip()[:self._max],
                cluster=current_cluster,
                section="",
                chunk_type="section",
                source_path=doc.path,
                metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
            ))

        i = 1
        while i + 2 <= len(parts):
            heading = parts[i + 1].strip()
            body = parts[i + 2] if i + 2 < len(parts) else ""
            cluster = self._detect_cluster(heading + " " + body[:200]) or current_cluster
            text = f"{'=' * len(parts[i])} {heading}\n{body}".strip()
            if len(text) >= self._min:
                ct = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
                for sub_chunk in self._split_long(text, self._max):
                    chunks.append(SemanticChunk(
                        content=sub_chunk,
                        cluster=cluster,
                        section=heading,
                        chunk_type="section",
                        change_types=sorted(set(ct)),
                        source_path=doc.path,
                        metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                    ))
            i += 3

        return chunks if chunks else self._from_paragraphs(doc)

    def _from_paragraphs(self, doc: FetchedDocument) -> List[SemanticChunk]:
        """Split on blank lines — last resort for unstructured content."""
        paragraphs = re.split(r'\n{2,}', doc.content.strip())
        cluster = self._detect_cluster(doc.content[:500])
        chunks: List[SemanticChunk] = []
        for para in paragraphs:
            text = para.strip()
            if len(text) < self._min:
                continue
            ct = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
            for sub in self._split_long(text, self._max):
                chunks.append(SemanticChunk(
                    content=sub,
                    cluster=cluster,
                    section=doc.metadata.get("section", ""),
                    chunk_type="paragraph",
                    change_types=sorted(set(ct)),
                    source_path=doc.path,
                    metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                ))
        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_unified_diff(text: str) -> bool:
        return bool(_DIFF_HUNK_RE.search(text[:2000]))

    @staticmethod
    def _detect_cluster(text: str) -> str:
        m = _CLUSTER_RE.search(text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _split_long(text: str, max_chars: int) -> List[str]:
        """Split *text* at paragraph boundaries if it exceeds *max_chars*."""
        if len(text) <= max_chars:
            return [text]
        parts = re.split(r'\n{2,}', text)
        chunks: List[str] = []
        buf = ""
        for p in parts:
            if buf and len(buf) + len(p) > max_chars:
                chunks.append(buf.strip())
                buf = p
            else:
                buf = (buf + "\n\n" + p).lstrip()
        if buf.strip():
            chunks.append(buf.strip())
        return chunks or [text[:max_chars]]

    # ------------------------------------------------------------------
    # Rejected-collecting variants of the per-type handlers
    # ------------------------------------------------------------------

    def _from_unified_diff_r(
        self, doc: FetchedDocument, rejected: List[dict]
    ) -> List[SemanticChunk]:
        """Like ``_from_unified_diff`` but appends rejected segments to *rejected*."""
        hunks = _DIFF_HUNK_RE.split(doc.content)
        if not hunks:
            return self._from_paragraphs_r(doc, rejected)

        current_section = doc.metadata.get("section", "")
        current_cluster = self._detect_cluster(doc.content[:500])
        chunks: List[SemanticChunk] = []
        buffer_lines: List[str] = []
        buffer_section = current_section
        buffer_cluster = current_cluster

        def _flush(section: str, cluster: str) -> None:
            text = "\n".join(buffer_lines).strip()
            if len(text) >= self._min:
                change_types = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
                if any(l.startswith('+') for l in buffer_lines):
                    if "ADDED" not in change_types:
                        change_types.append("ADDED")
                if any(l.startswith('-') for l in buffer_lines):
                    if "REMOVED" not in change_types:
                        change_types.append("REMOVED")
                chunks.append(SemanticChunk(
                    content=text[:self._max],
                    cluster=cluster,
                    section=section,
                    chunk_type="diff_hunk",
                    change_types=sorted(set(change_types)),
                    source_path=doc.path,
                    metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                ))
            elif text:
                rejected.append({
                    "source": doc.path,
                    "section": section,
                    "cluster": cluster,
                    "chunk_type": "diff_hunk",
                    "reason": f"too_short ({len(text)} < {self._min} chars)",
                    "text_preview": text[:120],
                })
            buffer_lines.clear()

        for part in hunks:
            for line in part.splitlines():
                hdr_m = re.match(r'^[+\-]?\s*(={1,6})\s+(.+)$', line)
                if hdr_m:
                    _flush(buffer_section, buffer_cluster)
                    buffer_section = hdr_m.group(2).strip()
                    buffer_cluster = self._detect_cluster(buffer_section) or buffer_cluster
                buffer_lines.append(line)
                if _TABLE_ROW_RE.match(line) and len(buffer_lines) > 50:
                    _flush(buffer_section, buffer_cluster)

        _flush(buffer_section, buffer_cluster)

        if not chunks:
            return self._from_paragraphs_r(doc, rejected)
        return chunks

    def _from_adoc_sections_r(
        self, doc: FetchedDocument, rejected: List[dict]
    ) -> List[SemanticChunk]:
        """Like ``_from_adoc_sections`` but appends rejected segments to *rejected*."""
        parts = _ADOC_HEADING_RE.split(doc.content)
        chunks: List[SemanticChunk] = []
        current_cluster = self._detect_cluster(doc.content[:500])

        if parts and parts[0].strip():
            chunks.append(SemanticChunk(
                content=parts[0].strip()[:self._max],
                cluster=current_cluster,
                section="",
                chunk_type="section",
                source_path=doc.path,
                metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
            ))

        i = 1
        while i + 2 <= len(parts):
            heading = parts[i + 1].strip()
            body = parts[i + 2] if i + 2 < len(parts) else ""
            cluster = self._detect_cluster(heading + " " + body[:200]) or current_cluster
            text = f"{'=' * len(parts[i])} {heading}\n{body}".strip()
            if len(text) >= self._min:
                ct = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
                for sub_chunk in self._split_long(text, self._max):
                    chunks.append(SemanticChunk(
                        content=sub_chunk,
                        cluster=cluster,
                        section=heading,
                        chunk_type="section",
                        change_types=sorted(set(ct)),
                        source_path=doc.path,
                        metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                    ))
            elif text:
                rejected.append({
                    "source": doc.path,
                    "section": heading,
                    "cluster": cluster,
                    "chunk_type": "section",
                    "reason": f"too_short ({len(text)} < {self._min} chars)",
                    "text_preview": text[:120],
                })
            i += 3

        return chunks if chunks else self._from_paragraphs_r(doc, rejected)

    def _from_paragraphs_r(
        self, doc: FetchedDocument, rejected: List[dict]
    ) -> List[SemanticChunk]:
        """Like ``_from_paragraphs`` but appends rejected segments to *rejected*."""
        paragraphs = re.split(r'\n{2,}', doc.content.strip())
        cluster = self._detect_cluster(doc.content[:500])
        chunks: List[SemanticChunk] = []
        for para in paragraphs:
            text = para.strip()
            if len(text) < self._min:
                if text:
                    rejected.append({
                        "source": doc.path,
                        "section": doc.metadata.get("section", ""),
                        "cluster": cluster,
                        "chunk_type": "paragraph",
                        "reason": f"too_short ({len(text)} < {self._min} chars)",
                        "text_preview": text[:120],
                    })
                continue
            ct = [m.group(1).upper() for m in _ANNOTATION_RE.finditer(text)]
            for sub in self._split_long(text, self._max):
                chunks.append(SemanticChunk(
                    content=sub,
                    cluster=cluster,
                    section=doc.metadata.get("section", ""),
                    chunk_type="paragraph",
                    change_types=sorted(set(ct)),
                    source_path=doc.path,
                    metadata={k: v for k, v in doc.metadata.items() if k != "_process_rules"},
                ))
        return chunks


# ---------------------------------------------------------------------------
# Module-level log writer
# ---------------------------------------------------------------------------

def _write_chunker_rejected_log(
    rejected: List[dict], output_dir: str, label: str
) -> None:
    """Write rejected/ignored chunk segments to ``<output_dir>/<label>_ignored_or_rejected.txt``."""
    from collections import Counter
    out_path = Path(output_dir) / f"{label}_ignored_or_rejected.txt"
    try:
        reason_counts = Counter(r["reason"].split(" (")[0] for r in rejected)
        lines = [
            f"Semantic Chunker — Ignored / Rejected Segments ({label})",
            "=" * 70,
            f"Total rejected segments: {len(rejected)}",
            "",
            "Summary by reason:",
        ]
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {count:>6d}  {reason}")
        lines += ["", "-" * 70, ""]
        for entry in rejected:
            lines += [
                f"Source  : {entry['source']}",
                f"Section : {entry['section']}",
                f"Cluster : {entry['cluster']}",
                f"Type    : {entry['chunk_type']}",
                f"Reason  : {entry['reason']}",
                f"Preview : {entry['text_preview']}",
                "",
            ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("[semantic_chunker] Rejected log → %s  (%d segments)", out_path, len(rejected))
    except Exception as exc:
        logger.warning("[semantic_chunker] Could not write rejected log: %s", exc)
