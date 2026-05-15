# Setup LM Studio as the LLM provider

Follow these steps to configure the Matter RAG pipeline to run with a local LM Studio model (e.g. Qwen3-5.9B) instead of Claude.

## Step 1 — Install dependencies

`openai` is already in `requirements.txt` so a fresh `pip install -r requirements.txt` covers it. If you skipped that step, install it now:

```bash
pip install openai>=1.0.0
```

## Step 2 — Start LM Studio local server

1. Open **LM Studio**
2. Load the model you want to use (e.g. **Qwen3-5.9B**)
3. Go to the **Local Server** tab (left sidebar)
4. Click **Start Server** — the server starts at `http://localhost:1234`

Verify it is running:
```bash
curl http://localhost:1234/v1/models
```
You should see a JSON list containing your loaded model.

## Step 3 — Update config.yaml

Edit `config/config.yaml` and set the `llm:` section:

```yaml
llm:
  provider: lm_studio
  lm_studio_url: http://localhost:1234/v1
  lm_studio_model: qwen3-5.9b   # must match the model name shown in LM Studio exactly
  temperature: 0.1
  max_tokens: 4096
```

> **Important**: the `lm_studio_model` value must match the model identifier shown in LM Studio's model list (check `curl http://localhost:1234/v1/models` to see the exact ID).

## Step 4 — Run the pipeline

```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc data/input_doc/appclusters_diff.html
```

Or for a single cluster:
```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc data/input_doc/appclusters_diff.html --cluster "On/Off"
```

## To switch back to Claude

```yaml
llm:
  provider: claude_subprocess   # uses local claude CLI
```

## Supported providers summary

| `provider` value | Backed by | Key config fields |
|---|---|---|
| `claude_subprocess` | Local `claude` CLI (default) | — |
| `claude_cli` | Anthropic Python SDK | `model`, `ANTHROPIC_API_KEY` env var |
| `local` | Ollama | `local_model` |
| `lm_studio` | LM Studio local server | `lm_studio_url`, `lm_studio_model` |
