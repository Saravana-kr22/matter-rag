# Config Module

## Purpose
Loads `config/config.yaml` into typed Python dataclasses with layered override support.
Split into two files to keep I/O and data models separate.

## Files

| File | Role |
|---|---|
| `models.py` | All dataclass definitions -- zero I/O, importable standalone |
| `config_loader.py` | `load_config()` + `${VAR}` substitution + env-var + overlay config + `_resolve_relative_paths` helper |

## Key Types

| Class | YAML section | Notable fields |
|---|---|---|
| `LLMConfig` | `llm:` | `provider` ("claude_cli"\|"claude_subprocess"\|"local"\|"lm_studio"\|"gemini"), `model`, `local_model`, `temperature` (default 0.1 -- passed by all providers), `max_tokens`, `max_prompt_chars` (default 80000), `call_log_path`, `lm_studio_url`, `lm_studio_model`, `lm_studio_timeout` (default 3600), `gemini_model`, `gemini_api_key`, `subprocess_timeout` (default 600) |
| `EmbeddingsConfig` | `embeddings:` | `model` (BGE id), `device`, `batch_size`, `normalize`, `cache_dir`, `offline` |
| `DatabaseConfig` | `database:` | `backend` ("faiss"\|"chroma"\|"postgres"\|"docker"), FAISS/Chroma/Postgres/Docker sub-fields |
| `FetcherConfig` | `fetcher:` | `github_token`, `local_extensions`, `github_timeout`, `github_max_retries` |
| `LoaderConfig` | `loader:` | `chunk_size`, `chunk_overlap`, `adoc_section_split` |
| `ChunkerConfig` | `chunker:` | `chunker_type` ("matter_tc"\|"generic"), `chunk_size`, `chunk_overlap`, `ignore_rules` |
| `PipelineConfig` | `pipeline:` | See below |
| `KnowledgeGraphConfig` | `knowledge_graph:` | See below |
| `RerankerConfig` | `reranker:` | `enabled`, plus 9 weighted scoring components |
| `AnalysisConfig` | `analysis:` | `max_llm_calls_per_run` (9999), `dm_dir`, `dm_dirs_additional`, `output_dir`, `tasks`, `sdk_dir`, `sdk_dirs_additional`, `parallel_workers` |
| `LoggingConfig` | `logging:` | `level`, `format` |
| `SpecRepoConfig` | `spec_repo:` | `path`, `url`, `docker_image` |
| `AppConfig` | (root) | All of the above as fields |

## PipelineConfig -- Build-Once Flags

| Field | Default | Meaning |
|---|---|---|
| `build_test_plan_vectors` | `false` | Re-chunk, embed, and save test plan vector DB |
| `build_knowledge_graph` | `false` | Re-build and save the knowledge graph |
| `build_data_model` | `false` | Re-ingest Matter DM XML schema into the KG |
| `rebuild_index` | `false` | Backward-compat alias for `build_test_plan_vectors` |
| `convert_adoc_to_html` | `false` | Run asciidoctor on `.adoc` docs before loading |
| `search_top_k` | `10` | Candidate test cases retrieved per PR chunk |
| `similarity_threshold` | `0.65` | Minimum cosine score to keep a vector result |
| `llm_confidence_threshold` | `0.6` | Rule-based confidence below this -> LLM fallback in `ChangeExtractor` |
| `output_dir` | `"reports"` | Directory for Markdown + JSON reports |
| `logs_dir` | `"logs"` | Base directory for per-run timestamped log folders |
| `system_prompt_skills_file` | `"llm_prompts/matter_spec_skill.md"` | Path to skill file appended verbatim to the LLM system prompt on every run. Edit without rebuilding. Leave file empty to inject nothing. |
| `min_chunk_chars` | `80` | Minimum content length for a `matter_spec_diff` section to be kept as a PR chunk. Raise to `200` to also drop short section-header-rename diffs. |
| `spec_sections` | `[]` | Spec section path prefixes to pull verbatim into 2nd/3rd-pass expand prompt (Tier 2 injection). Set via `--spec-sections` CLI flag. |
| `llm_additional_context` | `""` | Raw domain knowledge appended to 2nd/3rd-pass expand prompt (Tier 3 injection). Set via `--llm-additional-context` CLI flag. |
| `tc_index_path` | `"data/cache/tc_index.json"` | Pre-built TC-ID to adoc file routing index |
| `expand_section_max_chars` | `15000` | Max chars of spec section text injected into the expand prompt |
| `second_pass_expand_cap` | `20` | Max TC expand calls per cluster in second_pass |

## LLMConfig -- Temperature

The `temperature` field (default `0.1`) is now passed by **all** providers:
- `ClaudeSubprocessProvider`: `-t` flag on the `claude --print` command (omitted when temperature is 0)
- `ClaudeProvider`: `temperature=` kwarg in `_complete()`, `stream()`, `complete_with_tools()`
- `GeminiProvider`: `generation_config={"temperature": ...}` in `complete()`, `stream()`, `complete_with_tools()`
- `OllamaProvider`, `LMStudioProvider`: already passed temperature

The `subprocess_timeout` default is `600` seconds.

## LLMConfig -- LM Studio

| Field | Default | Meaning |
|---|---|---|
| `lm_studio_url` | `"http://localhost:1234/v1"` | OpenAI-compatible endpoint |
| `lm_studio_model` | `"qwen3-5.9b"` | Model name as shown in LM Studio |
| `lm_studio_timeout` | `3600` | HTTP timeout in seconds per LLM call |

## LLMConfig -- Gemini

| Field | Default | Meaning |
|---|---|---|
| `gemini_model` | `"gemini-1.5-flash"` | e.g. gemini-2.0-flash, gemini-1.5-pro |
| `gemini_api_key` | `""` | Or set `GEMINI_API_KEY` env var |

## KnowledgeGraphConfig

| Field | Default | Meaning |
|---|---|---|
| `max_depth` | `3` | Max hops when traversing related nodes |
| `relationship_extraction` | `true` | Use LLM to extract cluster/command/attribute entities |
| `backend` | `"local"` | `"local"` (NetworkX in-process) or `"docker"` (HTTP client) |
| `graph_store_path` | `"data/knowledge_graph/matter_kg.json"` | Persisted KG file (local backend) |
| `spec_extractor_workers` | `0` | Parallel workers for spec HTML parsing; 0 = auto (min(docs, cpu_count, 8)) |
| `docker_url` | `"http://localhost:8002"` | Docker KG REST API URL |
| `docker_timeout` | `30` | HTTP timeout in seconds |
| `llm_refinement_enabled` | `false` | Always run LLM refinement when building KG (also triggered by `--build-knowledge-graph-withLLM`) |
| `llm_refinement_max_sections` | `200` | Cost-control: stop after N spec sections |
| `llm_refinement_cache_path` | `"data/knowledge_graph/spec_refiner_cache.json"` | Cache file for refinement results (keyed by content hash) |
| `llm_refinement_provider` | `""` | Override LLM provider for refinement only. `""` = use global `llm:` config; `"local"` = Ollama; `"claude_cli"` / `"claude_subprocess"` = frontier Claude |
| `llm_refinement_local_model` | `""` | Ollama model name when `llm_refinement_provider: local` (e.g. `"llama3.2"`) |

**Using a local LLM for KG refinement** (to avoid frontier API costs):
```yaml
knowledge_graph:
  llm_refinement_provider: local
  llm_refinement_local_model: llama3.2
```
When set, `build_knowledge_graph_node` builds a copy of `LLMConfig` with the override
provider/model and passes it to `LLMSpecRefiner` instead of the global `llm:` config.

## AnalysisConfig

| Field | Default | Meaning |
|---|---|---|
| `max_llm_calls_per_run` | `9999` | Cost-control cap: set low to limit LLM calls |
| `parallel_workers` | `4` | Concurrent LLM calls for PICS/coverage/SDK analysis (1 = sequential) |
| `dm_dir` | `"data/data_model"` | Directory containing Matter DM XML cluster files |
| `dm_dirs_additional` | `[]` | Additional DM XML directories (overlay clusters merged with base) |
| `additional_sources_file` | `""` | Path to additional sources.json (entries appended to base sources at fetch time) |
| `additional_test_plans_dir` | `""` | Additional test plan HTML/adoc directory (merged with base) |
| `additional_spec_dir` | `""` | Additional spec HTML directory (merged with base) |
| `output_dir` | `"reports"` | Directory for analysis HTML/JSON reports |
| `tasks` | `["gaps", "pics"]` | Which analysis tasks to enable |
| `sdk_dir` | `""` | Root of connectedhomeip SDK repo; set to enable SDK coverage analysis |
| `sdk_dirs_additional` | `[]` | Additional SDK code directories (flat or nested; merged with base) |

## SpecRepoConfig

| Field | Default | Meaning |
|---|---|---|
| `path` | `""` | Local clone path (auto-clones if missing) |
| `url` | `"https://github.com/CHIP-Specifications/connectedhomeip-spec.git"` | Spec repo git URL |
| `docker_image` | `"ghcr.io/chip-specifications/chip-documentation:21"` | Docker image for Asciidoctor spec build |

## DatabaseConfig -- Docker Fields

| Field | Default | Used when |
|---|---|---|
| `docker_vector_store_url` | `http://localhost:8001` | `backend: docker` |
| `docker_timeout` | `30` | `backend: docker` |

## Override precedence (highest wins)
1. `overrides` dict passed to `load_config()`
2. `MATTER_RAG__<SECTION>__<KEY>` environment variables
3. `additional_config` overlay YAML file (deep-merged on top of base)
4. Values in `config.yaml`
5. Dataclass field defaults

## `${VAR}` substitution
`${VAR}` tokens in config.yaml are replaced at load time using `os.environ`.
Missing vars produce a `UserWarning` and resolve to `""`.

## `_build(cls, raw_dict)` helper
Maps a raw YAML dict onto a dataclass, ignoring unknown keys.
Type coercion relies on `yaml.safe_load()` -- unaffected by `from __future__ import annotations`.

## Usage
```python
from src.config.config_loader import load_config
cfg = load_config("config/config.yaml")
cfg = load_config(overrides={"llm": {"provider": "local"}})
# Load an overlay config that extends base config with additional sources/paths
cfg = load_config(additional_config="path/to/overlay.yaml")
# Override refinement LLM without changing global LLM
cfg = load_config(overrides={"knowledge_graph": {"llm_refinement_provider": "local", "llm_refinement_local_model": "llama3.2"}})
```

## `_resolve_relative_paths(obj, base_dir)`
Resolves relative paths in an overlay config dict relative to the overlay file's parent directory.
Only applies to string values whose key names contain 'dir', 'path', 'file', or 'url', or whose
values start with './' or '../'. Absolute paths and non-path values are left unchanged.
Called automatically on overlay config dicts loaded via `additional_config`.
