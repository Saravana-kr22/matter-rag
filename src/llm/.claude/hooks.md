# LLM Module — Hooks

## When you add a new LLM provider
1. Create a new class implementing `complete()` and `stream()`.
2. Add a branch in `get_llm()` keyed on a new `provider` string.
3. Add provider config fields to `LLMConfig` in `models.py` (with sensible defaults).
4. Add YAML block to `config/config.yaml` under `llm:`.
5. Update the Providers table in `CLAUDE.md`.
6. Add test in `tests/test_llm.py` using a mocked client.
7. Update `LLMConfig.provider` Literal in `models.py` to include the new value.

## When you change the LLMInterface (add a method)
- Update **all** provider classes: `ClaudeSubprocessProvider`, `ClaudeProvider`, `OllamaProvider`.
- Update `LoggingLLMProvider` to delegate the new method.
- Update the interface description in `CLAUDE.md`.
- Search callers: `grep -r "get_llm\|llm\.complete\|llm\.stream" src/` — update all call sites.

## When you update the Claude model name default
- Update `LLMConfig.model` default in `models.py`.
- Update `config/config.yaml` `llm.model`.
- Update the Providers table in `CLAUDE.md`.

## When you change temperature or token defaults
- Update `LLMConfig` defaults in `models.py`.
- Update `config/config.yaml` defaults and comments.

## When you change LoggingLLMProvider log format
- The JSONL schema change affects any tooling that parses `logs/llm_calls.jsonl`.
- Update the "Inspect LLM call log" example in `skills.md`.
- Update `commands.md` log format example if the schema changes.

## Downstream impact
`get_llm()` is called only in `src/engine/nodes.py::analyze_node`. Changes to the interface require updating that node.
`LoggingLLMProvider` is transparent — downstream code never sees it directly.
