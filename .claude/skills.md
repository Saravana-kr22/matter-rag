# Skills & Capabilities

## Module Skills

### ConfigLoader
- Reads `config/config.yaml`
- Returns typed `AppConfig` object with full validation
- Supports env-var substitution (`${VAR}`) and `MATTER_RAG__<SECTION>__<KEY>` env-var overrides
- Override precedence: explicit dict > env vars > YAML > dataclass defaults

### Fetcher (pluggable multi-source)
- **sources.json-driven**: loads source definitions from project root; no code changes needed to add sources
- **github_pr**: Fetches PR diff via GitHub REST API (`/pulls/{n}/files`), stores unified diff
- **github_repo**: Downloads full files from a GitHub repo tree path; saves to disk
- **github_tag_diff**: Compares PR head vs a base tag using GitHub compare API
- **local_folder**: Recursively walks a directory, filters by extension
- **url**: Fetches any HTTP/HTTPS URL; `format="matter_diff"` triggers expansion
- **csv**: Converts CSV rows to prose `FetchedDocument` objects
- **matter_xml**: Parses CSA DevX DM XML → structured schema FetchedDocument per cluster
- All fetchers return `List[FetchedDocument(path, content, metadata)]`

### DocumentProcessor
- Loads global rules from `.ignore_rules.json` + per-source rules from doc metadata
- Rule types: `strip_regex`, `strip_block_between`, `strip_first_lines`, `strip_last_lines`, `normalize_whitespace`, `replace_regex`
- `apply_to` field restricts rules to specific file extensions

### DocumentLoader
- **PDF**: `pypdf` — extracts text page by page
- **AsciiDoc (.adoc)**: section-split + TC-aware chunking via `MatterTCChunker`
- **CSV**: `pandas` — row-by-row or column-aware chunking
- **HTML**: semantic parser strips CSS/JS noise, retains content structure
- Output: `List[Document]` with `page_content` + rich metadata

### EmbeddingsModule
- Model: `BAAI/bge-large-en-v1.5` (1024-dim, configurable)
- Batch encoding with `sentence-transformers`
- Device: `mps` (Apple Silicon GPU), `cuda`, or `cpu`
- `offline: true` in config prevents HuggingFace model downloads on each run

### VectorStore (FAISS)
- `add_documents(docs, embeddings)` — stores vectors + metadata
- `save(path)` / `load(path)` — persist/restore index
- Backed by `faiss.IndexFlatIP` (inner product / cosine similarity)
- Also supports ChromaDB, PostgreSQL+pgvector, Docker backends

### FAISSSearch + CandidateReranker
- `search(query, k, threshold)` → `List[SearchResult]`
- `CandidateReranker`: re-scores top-K FAISS hits using 9 structural signals
  (entity overlap, KG edges, cluster match, condition/effect overlap, intent match,
  lexical similarity, chunk type, cosine score)

### KnowledgeGraphBuilder
- Nodes: `CLUSTER`, `ATTRIBUTE`, `COMMAND`, `EVENT`, `FEATURE`, `REQUIREMENT`,
  `BEHAVIOR_RULE`, `TEST_CASE`, `PR_CHANGE`, `SECTION`
- Search: `search_by_structured_change()`, `search_by_entities()`, `get_test_cases_for_cluster()`
- Persisted: `kg.export_json()` / `kg.load_from_json()` (NetworkX DiGraph)
- Optional LLM spec-refinement pass: adds cross-cluster DEPENDS_ON + REFERENCES edges

### LLMProvider
| Config Value | Implementation | Notes |
|---|---|---|
| `claude_subprocess` | Local `claude` CLI via subprocess | Default; uses corporate SSO auth, no API key needed |
| `claude_cli` | `anthropic.Anthropic` SDK | Requires `ANTHROPIC_API_KEY` |
| `local` | `ollama` Python client | Requires local Ollama server |
| `lm_studio` | `openai` SDK → LM Studio server | Requires LM Studio running at `lm_studio_url` |

`LoggingLLMProvider` wraps any provider to log every call (prompt, response, timing) to JSONL.

Run `/setup-lm-studio` in Claude Code for step-by-step LM Studio setup instructions.

### Engine (LangGraph Pipeline)
Nodes in the `StateGraph` (16 nodes):
1. `fetch_documents_node` — load sources from `sources.json`; route by role
2. `process_documents_node` — apply text-cleaning rules
3. `ingest_data_model_node` — write `data_model_schema.json`; pass DM docs through
4. `build_matter_schema_node` — extract canonical entity tables from spec diff HTML
5. `chunk_embed_test_plans_node` — build or load test plan vector DB (once)
6. `chunk_pr_node` — chunk PR + spec docs
7. `extract_pr_changes_node` — rule-based + LLM structured change extraction
8. `build_knowledge_graph_node` — build or load KG (once)
9. `search_test_plan_vector_db_node` — top-K FAISS hits per PR chunk
10. `search_knowledge_graph_node` — KG entity/structure search per PR chunk
11. `analyze_with_llm_node` — 1 LLM call per PR chunk (reranker + prompt + parse)
12. `write_adoc_updates_node` — write TC updates back to source `.adoc` files
13. `write_updated_testplan_node` — write per-cluster updated test plan `.adoc` files
14. `cluster_review_node` — cluster-level LLM audit pass; writes `cluster_review_<ts>.md`
15. `generate_report_node` — write Markdown + JSON report
16. `cleanup_node` — always last; release GPU memory, log run summary

Each run writes logs to `logs/ghpr_analysis_<YYYYMMDD_HHMMSS>/`.

### Analysis Pipelines
- **Coverage gaps** (`run_coverage_analysis.py`): finds spec REQUIREMENTs with no TEST_CASE
- **PICS validation** (`run_pics_analysis.py`): finds wrong/missing PICS codes in TCs
- **SDK coverage** (`run_sdk_coverage_analysis.py`): spec REQUIREMENTs vs SDK implementation

---

## Report Output
The pipeline report generates:
- **New test cases needed**: PR changes with no matching test case (with suggested TC skeleton)
- **Update candidates**: existing TCs that need updating (with suggested changes)
- **Cluster review**: symmetry gaps, missing test types, duplicate coverage audit
- **Coverage map**: which PR sections are covered by which TCs
- Format: HTML report + JSON sidecar + optional updated `.adoc` test plan files
