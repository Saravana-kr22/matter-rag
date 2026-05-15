# Processor Module — Rules

## DocumentProcessor rules
- `process()` must return a **new** `FetchedDocument` — never mutate `doc.content` in-place.
- Original `metadata` must be preserved; only `_processed` and `_rules_applied` keys are added.
- Rules are applied in order: global rules first, then per-source rules.
- `apply_to` extension filter uses `doc.extension` (the computed `Path(path).suffix.lower()` property).
- If `apply_to` is absent or `None`, the rule applies to all document types.

## Rule validation rules
- Unknown `type` values must raise `ValueError` with the unknown type name — never silently skip.
- `strip_regex` with `scope: "line"` applies `re.sub(..., flags=re.MULTILINE)` — pattern must match a full line.
- `strip_block_between` is inclusive (removes the start and end lines themselves).
- `strip_first_lines` / `strip_last_lines` with `count=0` are no-ops (not errors).

## `.ignore_rules.json` rules
- The file is optional — `DocumentProcessor` returns documents unchanged when the file is absent.
- `_comment` keys in the JSON are ignored at runtime (they are documentation only).
- Rules must not reference external files or make network calls.

## Independence rules
- This module must import **only** `src.fetcher.base_fetcher.FetchedDocument` from the rest of `src/`.
- No imports from `src.loader`, `src.engine`, `src.config`, or any fetcher source class.
- This independence is what allows the module to be tested and replaced without cascading changes.

## No side effects
- `DocumentProcessor.__init__` reads the rules file once; subsequent `process()` calls are stateless.
- `process()` must not write files, make network requests, or modify global state.
