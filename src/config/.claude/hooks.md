# Config Module — Hooks

## When you add a new field to an existing dataclass
- Check whether the field needs a `${ENV_VAR}` token in `config.yaml`.
- Add the corresponding `MATTER_RAG__<SECTION>__<KEY>` entry to the env var table in `CLAUDE.md` (root) and `src/config/CLAUDE.md`.
- Run `pytest tests/test_config.py -v` to confirm defaults and YAML round-trips still pass.

## When you add a new config section (new dataclass)
- Add to `models.py`, `AppConfig`, `config_loader.py::load_config()`, `_SECTION_MAP`, and `config/config.yaml`.
- Add a `test_<section>_config_defaults` test in `tests/test_config.py`.
- Update `src/config/CLAUDE.md` table with the new section.

## When you change a field name or type
- Search for all usages across `src/` — the `_build` helper silently drops unknown YAML keys, so a rename will silently fall back to the default without a runtime error.
- Grep for old field name: `grep -r "cfg\.<section>\.<old_field>" src/`
- Update `config/config.yaml` and all call sites simultaneously.

## When you change override precedence logic
- Run `test_override_precedence` and `test_env_var_override` in `tests/test_config.py`.
- Ensure `MATTER_RAG__*` env vars are cleared between test cases to avoid cross-contamination.

## Downstream impact
`AppConfig` is the root object threaded through `PipelineState`. Any structural change touches every node in `src/engine/nodes.py`. Coordinate with engine module changes.
