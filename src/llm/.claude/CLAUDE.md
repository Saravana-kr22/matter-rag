# LLM Module

## Purpose
Provider abstraction for LLM completions. Supports `claude_subprocess` (local Claude CLI), `claude_cli` (Anthropic SDK -- default), `local` (Ollama), `lm_studio` (LM Studio OpenAI-compatible local server), and `gemini` (Google Gemini).
All providers expose a three-method interface (`complete`, `stream`, `complete_with_tools`) so the rest of the pipeline is provider-agnostic.
`LoggingLLMProvider` transparently wraps any provider to log every call to JSONL, TXT, and HTML files.

## Temperature
All providers pass `config.temperature` (default `0.1`) to the underlying API:
- **ClaudeSubprocessProvider**: `-t` flag added to the `claude --print` command (omitted when temperature is 0)
- **ClaudeProvider**: `temperature=self.config.temperature` in `_complete()`, `stream()`, and `complete_with_tools()` kwargs
- **GeminiProvider**: `generation_config={"temperature": self.config.temperature}` in `complete()`, `stream()`, and `complete_with_tools()`
- **OllamaProvider**, **LMStudioProvider**: already passed temperature (no change)

## Files

| File | Class(es) | Role |
|---|---|---|
| `llm_provider.py` | `ClaudeSubprocessProvider`, `ClaudeProvider`, `OllamaProvider`, `LMStudioProvider`, `GeminiProvider`, `LoggingLLMProvider`, `get_llm()` | Factory + provider implementations |
| `call_logger.py` | `LLMCallLogger`, `log_parse_error()` | Per-call log dispatcher: writes `.jsonl`, `.txt`, and `.html` (dark-terminal, collapsible panes, live auto-refresh). Module-level `log_parse_error()` records JSON parse failures in the HTML log. |

## LLMInterface (informal protocol)
```python
llm.complete(prompt: str, system: str | None = None) -> str
llm.stream(prompt: str, system: str | None = None) -> Generator[str, None, None]
llm.complete_with_tools(messages: list, system: str | None = None, tools: list | None = None, tool_executor=None, max_iterations: int = 8) -> str
```

## Providers

### ClaudeSubprocessProvider (`provider: claude_subprocess`)
- Calls `claude --print --output-format text` via `subprocess.Popen`
- `-t` flag only passed when `temperature > 0` (omitted at 0.0 for CLI compatibility)
- Prompt is piped to stdin; response is read from stdout
- No API key needed -- uses whatever auth the local `claude` CLI has (corporate SSO / keyring)
- System prompt embedded as `<system>\n...\n</system>\n\n{prompt}` in the user message
- `claude` binary must be on `PATH`; timeout: `config.subprocess_timeout` (default 600 seconds)

### ClaudeProvider (`provider: claude_cli`) -- **default**
- Uses `anthropic.Anthropic()` client
- Auth order: `ANTHROPIC_API_KEY` env var -> `apiKeyHelper` script in `~/.claude/settings.json`
- Automatic token refresh: retries once on 401/auth errors after re-running `apiKeyHelper`
- Configurable: `model` (e.g. `claude-sonnet-4-6`), `max_tokens`
- `_complete()`, `stream()`, and `complete_with_tools()` all pass `temperature=self.config.temperature` in kwargs
- `complete_with_tools()` converts OpenAI-format tool defs to Anthropic format internally

### OllamaProvider (`provider: local`)
- Uses `ollama` Python client
- Connects to local Ollama server (default `http://localhost:11434`)
- Set `OLLAMA_HOST` env var to override the server URL
- Configurable: `local_model` (e.g. `llama3.2`), `temperature`
- `complete_with_tools()` uses Ollama's native function-calling API (Llama 3.1+, Mistral, Qwen2.5, etc.)

### LMStudioProvider (`provider: lm_studio`)
- Uses `openai` Python SDK pointed at LM Studio's local OpenAI-compatible server
- Default base URL: `http://localhost:1234/v1` (configurable via `lm_studio_url`)
- Model name must match the identifier shown in LM Studio (`lm_studio_model`, e.g. `qwen3-5.9b`)
- HTTP timeout: `lm_studio_timeout` (default 3600 seconds)
- Start LM Studio -> load model -> Local Server tab -> Start Server
- Run `/setup-lm-studio` in Claude Code for step-by-step setup instructions
- `complete_with_tools()` uses OpenAI-compatible function-calling protocol

### GeminiProvider (`provider: gemini`)
- Uses `google.generativeai` SDK
- Auth: `GEMINI_API_KEY` env var or `config.llm.gemini_api_key`
- Configurable: `gemini_model` (default `gemini-1.5-flash`)
- `complete()`, `stream()`, and `complete_with_tools()` pass `generation_config={"temperature": self.config.temperature}`
- `complete_with_tools()` converts OpenAI-format tool defs to Gemini `FunctionDeclaration` objects internally

## LLMCallLogger (call_logger.py)
- Writes three output files derived from `call_log_path`:
  - `<stem>.jsonl` -- one JSON object per line (machine-readable, backward compat)
  - `<stem>.txt` -- human-readable text with call delimiters and request/response blocks
  - `<stem>.html` -- dark-terminal HTML with collapsible panes, pass filter toolbar, live auto-refresh (5s poll)
- Seeds from existing JSONL on init so HTML accumulates correctly across `get_llm()` re-instantiations within a run
- `log_parse_error(context, response_preview)` -- module-level function; records JSON parse failures as orange PARSE ERROR blocks in the HTML log
- `next_call_label` -- set via `LoggingLLMProvider.set_next_label()` to tag calls with pass/phase labels (shown in HTML pane headers)
- HTML pass filter: buttons for All, Pass 1, Pass 2 (Consolidation), Pass 2 (Expand), Cluster Review, Chat, Other

## LoggingLLMProvider (transparent wrapper)
- Wraps any provider without changing any call site
- Automatically applied by `get_llm()` when a log path is available (per-run `log_dir` or global `config.call_log_path`)
- Delegates to `LLMCallLogger` for 3-file output
- Logs `==>` / `<==` markers to Python logging showing call metadata:
  ```
  ==> call #3 | ClaudeProvider | system_len=512 prompt_len=2048
  <== call #3 | 4.2s | response_len=891
  ```
- On failure: retries once after 2s delay, then logs error and raises
- `set_next_label(label)` -- tag the next call with a descriptive label for the HTML log

## Factory
```python
from src.llm.llm_provider import get_llm
llm = get_llm(cfg.llm)                         # uses config.call_log_path
llm = get_llm(cfg.llm, log_dir="logs/run_1")   # per-run log dir (llm_calls.* in that dir)
response = llm.complete("Summarize this TC", system="You are a test engineer.")
```

`get_llm()` accepts an optional `log_dir` parameter. When provided, LLM call logs are written
to `<log_dir>/llm_calls.jsonl` (and `.txt`/`.html`) so parallel pipeline runs each get isolated
call logs. Falls back to `config.call_log_path` when `log_dir` is omitted.

## Config
```yaml
llm:
  provider: claude_cli          # "claude_cli" | "claude_subprocess" | "local" | "lm_studio" | "gemini"
  model: claude-sonnet-4-6      # Claude model (claude_cli / claude_subprocess)
  local_model: llama3.2         # Ollama model (local only)
  temperature: 0.1              # passed by ALL providers; -t flag omitted at 0.0 for subprocess
  max_tokens: 4096
  max_prompt_chars: 80000       # auto-adjusted downward from model context window at startup
  call_log_path: logs/llm_calls.jsonl   # set to "" to disable call logging
  subprocess_timeout: 600       # seconds before claude subprocess is killed
  # LM Studio settings (lm_studio only)
  lm_studio_url: http://localhost:1234/v1   # LM Studio local server endpoint
  lm_studio_model: qwen3-5.9b              # must match model name shown in LM Studio
  lm_studio_timeout: 3600                  # HTTP timeout in seconds per LLM call
  # Gemini settings (gemini only)
  gemini_model: gemini-1.5-flash           # e.g. gemini-2.0-flash, gemini-1.5-pro
  gemini_api_key: ""                       # or set GEMINI_API_KEY env var
```

## Provider summary

| `provider` | Backed by | Key fields | Requires |
|---|---|---|---|
| `claude_cli` | Anthropic Python SDK | `model` | `ANTHROPIC_API_KEY` or `apiKeyHelper` |
| `claude_subprocess` | Local `claude` CLI | `subprocess_timeout` | `claude` on PATH |
| `local` | Ollama | `local_model` | Ollama running locally |
| `lm_studio` | LM Studio (OpenAI API) | `lm_studio_url`, `lm_studio_model`, `lm_studio_timeout` | LM Studio server started |
| `gemini` | Google Gemini SDK | `gemini_model`, `gemini_api_key` | `GEMINI_API_KEY` env var |

## Context Window Detection

All providers expose a `context_window` property (tokens):
- **ClaudeProvider**: queries `client.models.retrieve()`, falls back to lookup table
- **ClaudeSubprocessProvider**: static lookup table by model name (200K for Claude models)
- **OllamaProvider**: `ollama.show()` -> model_info context fields
- **LMStudioProvider**: `client.models.list()` -> context_window/context_length
- **GeminiProvider**: `genai.get_model()` -> input_token_limit

`get_llm()` enforces minimum 64K tokens at startup (raises `RuntimeError` if below).
Auto-sets `config.max_prompt_chars = int(context * 3.5 * 0.60)` when config value is 0.
Falls back to 80K chars if detection fails (context_window returns 0).
