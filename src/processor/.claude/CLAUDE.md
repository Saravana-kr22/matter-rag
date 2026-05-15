# Processor Module

## Purpose
Transform raw `FetchedDocument` objects before they reach the loader and embedding stages.
Contains three distinct responsibilities:

1. **Rule-based text cleaning** (`document_processor.py`) — strip noise, normalize whitespace
2. **Matter spec diff HTML expansion** (`matter_html_processor.py`) — expand diff HTML into annotated-text chunks
3. **Canonical schema extraction** (`matter_schema_extractor.py`) — extract entity tables from diff HTML
4. **Semantic PR chunking** (`semantic_chunker.py`) — split PR diffs into coherent change units
5. **Structured change extraction** (`change_extractor.py`) — classify and extract change records from PR chunks

## Files

| File | Class(es) | Role |
|---|---|---|
| `document_processor.py` | `DocumentProcessor` | Rule engine — loads JSON rule files, applies rules in order |
| `matter_html_processor.py` | `ProcessMatterHtmlDoc` | Expand Matter spec diff HTML → one FetchedDocument per diff section |
| `matter_schema_extractor.py` | `MatterSchemaExtractor` | Parse entity tables (attributes/commands/events/features) from diff HTML |
| `semantic_chunker.py` | `SemanticPRChunker`, `SemanticChunk` | Split PR/spec docs into coherent semantic chunks |
| `change_extractor.py` | `ChangeExtractor`, `StructuredChange`, `ChangeKind` | Classify and extract structured change records from PR chunks |
| `html_semantic_parser.py` | `parse_spec()`, `parse_test_plan()`, `parse_file()` | Semantic HTML→JSON parser for AsciiDoc-generated HTML — strips all CSS/JS noise |

---

## DocumentProcessor

Rule engine applied by `process_documents_node` to all fetched documents.

Rules are loaded from two sources (both applied):
1. `.ignore_rules.json` (project root) — global rules for every document
2. `doc.metadata["_process_rules"]` — per-source rules set by each fetcher

```python
processor = DocumentProcessor(".ignore_rules.json")
cleaned_doc = processor.process(doc, source_rules=doc.metadata.get("_process_rules", []))
```

Returns a new `FetchedDocument` with `_processed: True` and `_rules_applied: <count>` added to metadata.

### Rule Types

| type | Behaviour |
|---|---|
| `strip_regex` | Remove lines matching `pattern` (`scope: "line"`) |
| `strip_block_between` | Remove lines between `start_pattern` and `end_pattern` (inclusive) |
| `strip_first_lines` | Remove first `count` lines |
| `strip_last_lines` | Remove last `count` lines |
| `normalize_whitespace` | Collapse 3+ consecutive blank lines to 1 |
| `replace_regex` | Replace `pattern` with `replacement` |

All rules support optional `apply_to: [".adoc", ".md"]` extension filter and `comment` field.

---

## ProcessMatterHtmlDoc

Expands a Matter spec diff HTML file into one `FetchedDocument` per diff section.
Called by `process_documents_node` on docs with `metadata["matter_diff"] = True`.

```python
proc = ProcessMatterHtmlDoc(cluster_filter="On/Off", section_filter="Attributes")
sections = proc.process(fetched_doc)   # List[FetchedDocument]
```

Each output doc has metadata: `doc_type="matter_spec_diff"`, `cluster`, `section_title`,
`section_level`, `is_new_section`, `source_html`.
Content contains `[ADDED: ...]`, `[REMOVED: ...]`, `[CHANGED: old → new]` annotations.

**`cluster_filter` behavior**: when set, `_collect_sections` finds the h3-headed `sect2`
div matching the cluster name as the root. It then includes **both** that root section
itself and all its descendant `sectN` divs. The root must be explicitly included via
`div in cluster_roots` — a div cannot be its own ancestor, so the cluster intro section
would otherwise be silently dropped (bug fixed).

---

## MatterSchemaExtractor

Parses the original (un-expanded) Matter spec diff HTML to extract canonical entity tables
with diff_status per entity row. Called by `build_matter_schema_node`.

```python
extractor = MatterSchemaExtractor()
schema = extractor.extract(html_content)
# schema = {"clusters": [{"name": "On/Off", "diff_status": "changed",
#   "attributes": [{"id": "0x0000", "name": "OnOff", "diff_status": "unchanged", ...}],
#   "commands": [...], "events": [...], "features": [...]}]}
```

`diff_status` values: `"added"`, `"removed"`, `"changed"`, `"unchanged"`.
Table type is classified by heading keywords (primary) + column header patterns (fallback).

---

## SemanticPRChunker

Splits PR `FetchedDocument` objects into semantically coherent `SemanticChunk` objects
instead of arbitrary token windows. Called by `chunk_pr_node`.

```python
chunker = SemanticPRChunker(min_chunk_chars=80, max_chunk_chars=6000)
chunks = chunker.chunk(fetched_doc)   # List[SemanticChunk]
doc = chunk.to_document(chunk_index=0)  # → loader.Document with enriched metadata

# Batch variant — writes rejected log automatically:
all_chunks = chunker.chunk_all_with_log(
    docs,
    output_dir=run_dir,     # write <run_dir>/pr_chunks_ignored_or_rejected.txt
    label="pr_chunks",      # filename prefix
)
```

`chunk_all_with_log()` collects segments shorter than `min_chunk_chars` (80 chars) into a
rejected list with their source path, section, cluster, and text preview.  The log file is
written only when `output_dir` is non-empty and at least one segment was rejected.

### Chunking Strategy (priority order)
1. **matter_spec_diff** docs — sections < `min_chunk_chars` are rejected and logged; qualifying sections are passed through as single chunks (already semantic), or split per entity row when ≥ 2 table-row annotations are present.
2. **Unified diff** — split at `@@` hunk boundaries + AsciiDoc section headings + table row groups
3. **AsciiDoc headings** — split at `== / === / ====` headings
4. **Paragraphs** — blank-line splitting (last resort)

### SemanticChunk metadata
`semantic_chunk_type`, `cluster`, `section`, `change_types` (`["ADDED", "REMOVED", "CHANGED"]`), `chunk_index`

---

## ChangeExtractor

Classifies each PR chunk into a typed `StructuredChange` record.
Used by `extract_pr_changes_node`. Two-pass strategy:

1. **Rule-based** — regex + `[ADDED/REMOVED/CHANGED]` annotations → high-confidence result without LLM
2. **LLM fallback** — when `ambiguous=True` and `confidence < threshold`, LLM is called once per chunk

```python
extractor = ChangeExtractor(llm_provider=llm, confidence_threshold=0.6)
change = extractor.extract(text, cluster_hint="On/Off", section_hint="Attributes",
                            change_types_hint=["ADDED"])
record = change.to_dict()
```

### ChangeKind taxonomy
`ADD/REMOVE/MODIFY_CLUSTER`, `ADD/REMOVE/MODIFY_ATTRIBUTE`, `ADD/REMOVE/MODIFY_COMMAND`,
`ADD/REMOVE/MODIFY_EVENT`, `ADD/REMOVE/MODIFY_FEATURE`, `ADD/REMOVE/MODIFY_REQUIREMENT`,
`MODIFY_BEHAVIOR`, `UNKNOWN`

**Attribute-level quality/property change kinds** (detected from `[ADDED/REMOVED: Q/N]` and `[CHANGED: ...]` annotations):
`QUIETER_REPORTING_CHANGED` (Q flag — Quieter Reporting, not Nullable),
`NON_VOLATILE_CHANGED` (N flag),
`CONFORMANCE_CHANGED`, `ACCESS_CHANGED`, `DATATYPE_CHANGED`, `CONSTRAINT_CHANGED`, `FALLBACK_CHANGED`

### StructuredChange fields
`change_kind`, `cluster`, `entities` (list of `{type, name, id}`), `conditions`, `effects`,
`old_value`, `new_value`, `confidence` (0–1), `ambiguous`, `source_text`

---

## HtmlSemanticParser (`html_semantic_parser.py`)

Parses AsciiDoc-generated HTML into clean structured JSON, stripping all presentational
noise — `<style>`, `<script>`, inline CSS, navigation chrome — and retaining only
semantic content (headings, paragraphs, lists, tables, notes/admonitions).

### API

```python
from src.processor.html_semantic_parser import parse_spec, parse_test_plan, parse_file

# Generic spec document — returns GenericDocument schema (sections + chunks)
doc = parse_spec(html_str, doc_id="appclusters")

# Test-plan document — detects [TC-*] headings, returns TestPlanDocument schema
tc_doc = parse_test_plan(html_str, doc_id="allclusters")

# Auto-detect based on presence of [TC-*] headings
result = parse_file(html_str, doc_id="somefile")
```

### Output schemas

**`GenericDocument`** (from `parse_spec`):
```json
{
  "doc_id": "...",
  "title": "...",
  "sections": [
    {
      "heading": "...",
      "level": 2,
      "chunks": [
        {"type": "paragraph", "text": "...", "section_path": [...]}
      ]
    }
  ]
}
```

**`TestPlanDocument`** (from `parse_test_plan`):
```json
{
  "doc_id": "...",
  "title": "...",
  "test_cases": [
    {
      "tc_id": "TC-OO-2.1",
      "title": "...",
      "chunks": [...]
    }
  ]
}
```

### What is stripped

- `<style>`, `<script>`, `<noscript>`, `<svg>`, `<template>` tags (entire subtree)
- AsciiDoc boilerplate IDs: `header`, `footer`, `toc`, `toctitle`, `preamble`
- Revision history, copyright, table-of-contents sections (by title pattern)
- All CSS Google Fonts URL fragments (e.g. `family=Open+Sans:...`)
- `<link rel="stylesheet">` tags

### Visited-ID guard

The parser tracks `visited_ids` to avoid processing the same DOM node twice.
This prevents duplicate content from nested structures or back-references.
