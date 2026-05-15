# Chunker Module — Rules

## Import rules
- **Never** import `Document` at module top-level in chunker files — always do it lazily inside `chunk()` to prevent circular imports.
- Import `BaseChunker` only from `src.chunker.base_chunker`, not from `__init__` inside the chunker package itself.

## BaseChunker contract
- `chunk(text, metadata)` must return `[]` for empty or whitespace-only `text`.
- Every returned `Document` must copy `metadata` (do not mutate the caller's dict) and add `chunk_index`.
- Do not raise exceptions for valid inputs — return an empty list for edge cases.

## MatterTCChunker rules
- `_TC_HEADING_RE` must stay anchored with `re.MULTILINE` and match `^== TC-` (exactly two `=` signs) to avoid false positives on sub-headings.
- `_PICS_RE` character class must include `.` (`[A-Z0-9_.]+`) — PICS codes contain dots (e.g. `OO.C.00.Rsp`).
- Ignore rules are applied **before** TC splitting — never after.
- `MatterTCChunker` must accept both `IgnoreRule` objects and plain `dict` entries in `ignore_rules` to support YAML config round-trips.

## IgnoreRule rules
- `match` must be one of `"contains"`, `"startswith"`, `"exact"`, `"regex"` — raise `ValueError` otherwise.
- `scope` must be one of `"line"`, `"paragraph"`, `"block"` — raise `ValueError` otherwise.
- Default behaviour is **case-insensitive** (`case_sensitive=False`).
- After applying rules, collapse 3+ consecutive blank lines to 2 to keep text clean.

## Testing rules
- Unit-test every new `IgnoreRule` scope + match combination.
- Test that TC metadata (`tc_id`, `cluster_name`, `pics_codes`) survives ignore-rule stripping.
- Use `textwrap.dedent` for inline AsciiDoc fixtures to avoid indentation artifacts.
