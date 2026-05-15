# Processor Module — Hooks

## When you add a new rule type
1. Add a `_apply_<type>(content: str, rule: dict) -> str` method to `DocumentProcessor`.
2. Add the new `type` key to the `_apply()` dispatch in `document_processor.py`.
3. Raise `ValueError` in the default/unknown branch so bad rule types are caught early.
4. Document the new type and its required keys in `CLAUDE.md` Rule Types table.
5. Add an example to `.ignore_rules.json` comments if it's generally useful.
6. Add test in `tests/test_processor.py`.

## When you change `.ignore_rules.json`
- Changes take effect immediately on the next pipeline run — no code changes needed.
- Verify with a quick smoke test:
  ```bash
  python -c "
  from src.fetcher.base_fetcher import FetchedDocument
  from src.processor.document_processor import DocumentProcessor
  doc = FetchedDocument('test.adoc', 'Copyright 2024\nReal content')
  print(DocumentProcessor('.ignore_rules.json').process(doc).content)
  "
  ```

## When you change FetchedDocument (in base_fetcher.py)
- Update `process()` if it accesses any new/renamed metadata keys.
- The processor reads: `doc.content`, `doc.extension`, `doc.path`, `doc.metadata`.

## When you add per-source rules in sources.json
- Per-source rules follow the same schema as `.ignore_rules.json` rules.
- They run **after** global rules — use them for source-specific cleanup (e.g. strip diff markers from PR patches).
- They are stored by each fetcher in `doc.metadata["_process_rules"]` and read by `process_node`.

## Downstream impact
`process_node` runs between `fetch_node` and `load_node`. Changes to how content is cleaned can affect:
- `load_node` chunk quality (e.g. fewer blank lines = better chunk boundaries)
- `embed_node` embedding quality (shorter, cleaner text → better vectors)
- `analyze_node` LLM context quality (less noise = more focused analysis)
