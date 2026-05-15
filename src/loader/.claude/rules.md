# Loader Module — Rules

## Document rules
- `page_content` must always be a non-empty string — loaders must filter out empty chunks before returning.
- `metadata` must include `chunk_index` (int) and `path` (str) at minimum.
- `metadata` must be a plain JSON-serialisable dict — no numpy types, no custom classes.
- Never mutate the `metadata` dict passed into a chunker — always copy it before adding keys.

## BaseDocumentLoader rules
- Every loader must accept an injected `BaseChunker` in its `__init__`.
- `load()` must never raise for valid (but empty or unreadable) content — return `[]` and log a warning.
- `supported_extensions()` must return lowercase extensions with leading dots (e.g. `[".adoc"]`).

## AdocLoader rules
- Section splitting (`adoc_section_split=True`) must preserve the heading text as the first line of each section — do not strip it.
- If section splitting is disabled, pass the full document text to the chunker as one block.

## CSVLoader rules
- Rows are converted to prose strings (e.g. `"key: value; key: value"`) before chunking — never embed raw CSV bytes.
- Empty rows must be skipped.

## PDFLoader rules
- PDFLoader must use `metadata["absolute_path"]` from `FetchedDocument` to re-open the binary file — `FetchedDocument.content` may be a partial text extraction.
- If `absolute_path` is not available, fall back to `content` as plain text.

## Factory rules
- `DocumentLoaderFactory` must not perform any I/O at construction time.
- Unknown extensions fall back to `TextLoader` — never raise `KeyError`.
- `chunker_map` overrides take precedence over defaults derived from `chunker_config`.
