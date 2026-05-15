# LLM Module — Skills

## Get LLM from config (auto-wrapped with logging)
```python
from src.llm.llm_provider import get_llm
from src.config.config_loader import load_config
cfg = load_config()
llm = get_llm(cfg.llm)   # LoggingLLMProvider if call_log_path is set
```

## Non-streaming completion
```python
response = llm.complete(
    "Analyze this PR change",
    system="You are a senior Matter test engineer.",
)
print(response)
```

## Streaming completion
```python
for chunk in llm.stream("Explain this test step"):
    print(chunk, end="", flush=True)
```

## Use claude_subprocess (corporate CLI, no API key needed)
```yaml
# config.yaml
llm:
  provider: claude_subprocess
  max_tokens: 4096
```
Requires `claude` CLI on PATH with corporate SSO auth.

## Switch to local Ollama
```yaml
# config.yaml
llm:
  provider: local
  local_model: llama3.2
  temperature: 0.0
```

## Switch to LM Studio (Qwen3 or any local model)
```yaml
# config.yaml
llm:
  provider: lm_studio
  lm_studio_url: http://localhost:1234/v1
  lm_studio_model: qwen3-5.9b   # must match model name shown in LM Studio
  temperature: 0.1
  max_tokens: 4096
```
Start LM Studio → load model → Local Server tab → Start Server. Run `/setup-lm-studio` for full instructions.

## Switch Claude model
```yaml
llm:
  provider: claude_cli
  model: claude-opus-4-6   # higher quality
  max_tokens: 8192
```

## Inspect LLM call log
```python
import json
with open("logs/llm_calls.jsonl") as f:
    for line in f:
        c = json.loads(line)
        status = "OK" if c["success"] else f"FAILED: {c['error']}"
        print(f"#{c['call_id']} {c['duration_s']:.1f}s {status}")
```

## Disable LLM call logging
```yaml
llm:
  call_log_path: ""   # empty string disables LoggingLLMProvider wrapper
```

## Test with a mock LLM (unit tests)
```python
class MockLLM:
    def complete(self, prompt, system=None):
        return "## Missing Test Cases\n1. TC for new attribute\n"
    def stream(self, prompt, system=None):
        yield "mock response"
```
