# LLM Module — Rules

## Interface contract
- All providers (`ClaudeSubprocessProvider`, `ClaudeProvider`, `OllamaProvider`) must implement `complete(prompt, system=None) -> str` and `stream(prompt, system=None) -> Generator[str, None, None]`.
- `complete()` must return the **full** response as a single string — never a list or generator.
- `stream()` must yield string **chunks** — callers join them for the full response.
- `LoggingLLMProvider` must implement the same interface and delegate all calls to the wrapped provider.

## ClaudeSubprocessProvider rules
- System prompt must be embedded as `<system>\n{system}\n</system>\n\n{prompt}` — do not use `--append-system-prompt` (that flag is invalid).
- Subprocess stdin/stdout must use text mode (not bytes).
- Non-zero return code from `claude` CLI must raise `RuntimeError` with stderr content.
- Timeout is 300 seconds — do not make it configurable without updating CLAUDE.md.

## Credentials rules
- Never log, print, or store API keys.
- `ANTHROPIC_API_KEY` and `OLLAMA_HOST` must be read from environment variables — never hardcoded.
- If the API key is missing for `claude_cli` provider, raise a clear `ValueError` at `get_llm()` time (not at first call).

## Error handling rules
- Network errors (timeouts, connection refused) must be raised — never silently return empty strings.
- Rate limit errors (HTTP 429) should be re-raised with the original error message.
- Do not implement retry logic inside the LLM module — callers decide retry strategy.

## LoggingLLMProvider rules
- Must not alter the prompt, response, or any provider behaviour.
- Logging failures (e.g. disk full) must not interrupt the LLM call — catch and log a warning.
- `call_log_path` parent directory must be created if it does not exist.

## Provider rules
- `get_llm(config)` must accept a `LLMConfig` and return an object satisfying the interface.
- New providers must pass through `temperature` and equivalent `max_tokens` parameters.
- `get_llm()` wraps the returned provider with `LoggingLLMProvider` when `config.call_log_path` is non-empty.

## Prompt rules
- The `system` parameter should always be passed as a separate channel (system message or embedded tag) — never prepended to `prompt`.
- Do not truncate prompts inside the LLM module — callers are responsible for prompt length.
