# Chunker Module — Hooks

## When you edit base_chunker.py
- Run `pytest tests/test_chunker.py -k GenericChunker` to verify sliding-window behaviour.
- Check that `chunk_index` is still set on every returned `Document`.

## When you edit matter_tc_chunker.py
- Run the full chunker test suite: `pytest tests/test_chunker.py -v`
- Verify `_TC_HEADING_RE` still matches `TC-OO-2.1`, `TC-LV-3.10`, `TC-MCORE-1.1` but not `=== TC-OO-2.1` (sub-headings).
- Verify `_PICS_RE` matches `[PICS.OO.S]`, `[PICS.OO.C.00.Rsp]`, `[PICS.MCORE.C.00.01.Rsp]`.

## When you change IgnoreRule
- Re-run all `test_ignore_*` tests.
- Verify `IgnoreRule.from_dict` round-trips through a YAML-style dict without data loss.

## When you add a new chunker class
- Add it to `__init__.py` `__all__`.
- Add at least: empty-input test, overlap test, metadata-propagation test.
- Register it in `DocumentLoaderFactory` if it should be the default for any extension.

## When you change chunk_size / chunk_overlap defaults
- Update `config/config.yaml` `chunker.chunk_size` / `chunker.chunk_overlap` to match.
- Check that `test_generic_chunker_overlap` still passes with the new defaults.

## Downstream impact
Changing chunker output directly affects:
- `src/embeddings/` — number of vectors to embed
- `src/database/` — vector store size and metadata keys
- `src/engine/nodes.py` — `_format_test_cases` reads `tc_id`, `cluster_name`, `pics_codes`, `section_type`

Notify the team when adding new mandatory metadata keys.
