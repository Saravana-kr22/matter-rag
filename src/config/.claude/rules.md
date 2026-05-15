# Config Module — Rules

## models.py rules
- `models.py` must contain **only dataclass definitions** — no `import yaml`, no file I/O, no `load_config`.
- Do not use `from __future__ import annotations` in `models.py`; it breaks `isinstance` checks on field types at runtime.
- Every new config section must have a corresponding field in `AppConfig` with `field(default_factory=...)`.
- Default values in dataclasses are the authoritative fallback — do not duplicate them in `config.yaml`.

## config_loader.py rules
- All type coercion uses `yaml.safe_load(value)`, never `int(value)` or `bool(value)` — this handles lists, booleans, and nested structures transparently.
- `${VAR}` substitution must warn (not raise) when a variable is missing, and substitute with `""`.
- The override precedence is strict: explicit dict > env vars > YAML > defaults. Never break this order.
- `_build(cls, raw)` must ignore unknown YAML keys (pass `**{k: v for k, v in raw.items() if k in fields}`) to stay forward-compatible.

## Environment variable rules
- Prefix is `MATTER_RAG__`, separator is `__` (double underscore).
- Section and key names must be **lowercased** in the env var (even if the YAML key is mixed-case).
- Env vars override individual scalar values only — they cannot set nested dicts.

## General rules
- Never read files or call `os.environ` from `models.py`.
- Never store secrets (API keys, passwords) in config.yaml — always use `${ENV_VAR}` tokens.
- `load_config()` must be idempotent — calling it twice with the same args must return equivalent objects.
