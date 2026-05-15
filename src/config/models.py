"""Config dataclasses — one per YAML section of config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal


@dataclass
class LLMConfig:
    provider: Literal["claude_cli", "local", "claude_subprocess", "lm_studio", "gemini"] = "claude_cli"
    model: str = "claude-sonnet-4-6"
    local_model: str = "llama3.2"
    temperature: float = 0.1
    max_tokens: int = 4096
    call_log_path: str = "logs/llm_calls.jsonl"  # "" to disable call logging
    subprocess_timeout: int = 600               # seconds before claude subprocess is killed
    # LM Studio settings (provider: lm_studio)
    lm_studio_url: str = "http://localhost:1234/v1"   # LM Studio OpenAI-compatible endpoint
    lm_studio_model: str = "qwen3-5.9b"              # model name as shown in LM Studio
    lm_studio_timeout: int = 3600                     # HTTP timeout in seconds per LLM call
    # Google Gemini settings (provider: gemini)
    gemini_model: str = "gemini-1.5-flash"            # e.g. gemini-2.0-flash, gemini-1.5-pro
    gemini_api_key: str = ""                          # or set GEMINI_API_KEY env var
    max_prompt_chars: int = 80_000                    # 0 = no limit; truncate prompt sections if exceeded


@dataclass
class EmbeddingsConfig:
    model: str = "BAAI/bge-small-en-v1.5"
    device: str = "cpu"
    batch_size: int = 32
    normalize: bool = True
    cache_dir: str = "models"          # local directory to cache downloaded models
    offline: bool = False              # True → never contact HuggingFace (model must already be cached)


@dataclass
class DatabaseConfig:
    backend: str = "faiss"                             # "faiss" | "chroma" | "postgres" | "docker"
    # FAISS
    faiss_index_path: str = "data/faiss_index/matter.index"
    metadata_path: str = "data/faiss_index/metadata.json"
    index_type: str = "IndexFlatIP"
    # ChromaDB
    chroma_persist_dir: str = "data/chroma"
    chroma_collection: str = "matter_tc"
    # PostgreSQL + pgvector
    postgres_url: str = ""             # e.g. postgresql://user:pass@localhost/matterdb
    postgres_table: str = "matter_embeddings"
    # Docker vector store service
    docker_vector_store_url: str = "http://localhost:8001"
    docker_timeout: int = 30           # HTTP request timeout in seconds


@dataclass
class FetcherConfig:
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    default_repo: str = "project-chip/connectedhomeip"
    github_timeout: int = 60          # seconds per request; increase on slow networks
    github_max_retries: int = 3       # retry count on timeout / 5xx errors
    local_extensions: List[str] = field(
        default_factory=lambda: [".adoc", ".pdf", ".csv", ".md", ".txt"]
    )


@dataclass
class LoaderConfig:
    chunk_size: int = 1000
    chunk_overlap: int = 200
    adoc_section_split: bool = True


@dataclass
class ChunkerConfig:
    """Controls which chunker is used and its parameters."""
    chunker_type: str = "matter_tc"   # "matter_tc" | "generic"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    ignore_rules: List[dict] = field(default_factory=list)
    # Each dict must have "pattern" and may have "match", "scope", "case_sensitive".
    # Example:
    #   - {pattern: "Copyright", scope: paragraph, match: contains}
    #   - {pattern: "NOTE:", scope: line, match: startswith}
    #   - {pattern: "^\\s*//", scope: line, match: regex}


@dataclass
class PipelineConfig:
    rebuild_index: bool = False           # kept for backward compat; use build_test_plan_vectors
    build_test_plan_vectors: bool = False # True → re-chunk, embed and save test plan vector DB
    build_knowledge_graph: bool = False   # True → rebuild and save the knowledge graph
    build_data_model: bool = False        # True → re-ingest Matter DM XML into the KG
    convert_adoc_to_html: bool = False    # True → run asciidoctor on .adoc docs in process stage
    search_top_k: int = 10
    similarity_threshold: float = 0.65
    llm_confidence_threshold: float = 0.6  # chunks below this confidence use LLM for extraction
    output_dir: str = "reports"
    logs_dir: str = "logs"                # base directory for per-run log folders
    tc_index_path: str = "data/cache/tc_index.json"  # pre-built TC-ID → adoc file routing index
    system_prompt_skills_file: str = "llm_prompts/matter_spec_skill.md"  # extra LLM instructions
    # Spec section context injection for TC expand prompt (Tiers 2 & 3)
    # Tier 2: comma-separated section path prefixes to include verbatim
    #   e.g. "11.7.1.8,11.7.2.2" pulls those spec sections + their ancestors into expand prompt
    spec_sections: List[str] = field(default_factory=list)
    # Tier 3: raw domain knowledge appended to the expand prompt as-is
    #   e.g. "Use KID=0x0000000000000001; verify EXT-X-SESSION-KEY before EXT-X-MAP"
    llm_additional_context: str = ""
    # Max chars of spec section text injected into each TC expand prompt.
    # 0 = no limit (include complete section text — best quality, higher token cost).
    expand_section_max_chars: int = 15000
    second_pass_expand_cap: int = 20        # max TC expand calls per cluster in second_pass


@dataclass
class RerankerConfig:
    """Controls the candidate re-ranking stage between vector retrieval and LLM analysis.

    Set ``enabled: false`` in config.yaml to skip re-ranking entirely and pass raw
    vector results (in cosine-similarity order) directly to the LLM.

    When enabled, each component raw score (0–1) is multiplied by its weight and
    summed to produce the final candidate score (clamped to 1.0).
    """
    enabled: bool = True                    # false → skip re-ranking, use raw vector order

    # Per-component scoring weights (see src/search/reranker.py RerankerWeights)
    entity_overlap: float           = 0.25  # matched entity names from structured change
    cluster_match: float            = 0.15  # cluster name (exact / token-level)
    condition_effect_overlap: float = 0.15  # both sides of a behaviour rule covered
    intent_match: float             = 0.15  # test intent alignment with ChangeKind
    kg_direct_bonus: float          = 0.20  # KG direct edge to this test case
    kg_indirect_bonus: float        = 0.08  # KG 2-hop link to this test case
    lexical_similarity: float       = 0.08  # token overlap with PR/spec change text
    chunk_type_bonus: float         = 0.05  # chunk type preference (intent_summary > setup)
    retrieval_score: float          = 0.04  # original cosine similarity passthrough


@dataclass
class KnowledgeGraphConfig:
    max_depth: int = 3
    relationship_extraction: bool = True
    backend: str = "local"             # "local" | "docker"
    docker_url: str = "http://localhost:8002"
    docker_timeout: int = 30           # HTTP request timeout in seconds
    graph_store_path: str = "data/knowledge_graph/matter_kg.json"  # persisted KG file
    spec_extractor_workers: int = 0    # parallel workers for spec HTML parsing; 0 = auto (cpu_count, max 8)
    # LLM-assisted spec refinement (optional — adds cross-cluster edges via LLM over spec text)
    llm_refinement_enabled: bool = False
    llm_refinement_max_sections: int = 200
    llm_refinement_cache_path: str = "data/knowledge_graph/spec_refiner_cache.json"
    # Override LLM provider for refinement — set to "local" to use Ollama instead of a frontier
    # model and avoid per-token costs.  When empty (""), the global llm: config is used.
    llm_refinement_provider: str = ""        # "" | "local" | "claude_cli" | "claude_subprocess"
    llm_refinement_local_model: str = ""     # Ollama model name when llm_refinement_provider=local
    # Spec sections to consolidate into PROMPT_SECTION nodes during KG build.
    # Each entry has path_prefix (substring matched against section_path in the KG)
    # and label (shown in the system prompt header and used as the node ID suffix).
    # Add any spec section here — no code changes required.
    prompt_sections: List[dict] = field(default_factory=lambda: [
        {"path_prefix": "7. Data Model Specification > 7.3. Conformance", "label": "Conformance"},
        {"path_prefix": "7. Data Model Specification > 7.6. Access",      "label": "Access"},
        {"path_prefix": "7. Data Model Specification > 7.7. Other Qualities", "label": "Other Qualities"},
    ])


@dataclass
class LoggingConfig:
    level: str = "VERBOSE"  # VERBOSE=5 < DEBUG=10 < INFO=20; override with "INFO" in production
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


@dataclass
class AnalysisConfig:
    """Config for proactive quality-check analysis pipelines (PICS + coverage gaps)."""
    max_llm_calls_per_run: int = 9999        # cost-control cap; set low to limit LLM calls
    dm_dir: str = "data/data_model"         # primary DM XML directory
    dm_dirs_additional: List[str] = field(default_factory=list)  # additional DM XML dirs (overlay clusters)
    output_dir: str = "reports"             # directory for analysis HTML/JSON reports
    tasks: List[str] = field(default_factory=lambda: ["gaps", "pics"])  # which tasks to run
    sdk_dir: str = ""                       # root of connectedhomeip SDK repo
    sdk_dirs_additional: List[str] = field(default_factory=list)  # additional SDK code directories (flat or nested)
    parallel_workers: int = 4               # concurrent LLM calls for analysis (1 = sequential)
    additional_sources_file: str = ""       # additional sources.json (entries appended to base)
    additional_test_plans_dir: str = ""     # additional test plan HTML/adoc dir (merged with base)
    additional_spec_dir: str = ""           # additional spec HTML dir (merged with base)


@dataclass
class SpecRepoConfig:
    path: str = ""                          # local clone path (auto-clones if missing)
    url: str = "https://github.com/CHIP-Specifications/connectedhomeip-spec.git"
    docker_image: str = "ghcr.io/chip-specifications/chip-documentation:21"


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    fetcher: FetcherConfig = field(default_factory=FetcherConfig)
    loader: LoaderConfig = field(default_factory=LoaderConfig)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    knowledge_graph: KnowledgeGraphConfig = field(default_factory=KnowledgeGraphConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    spec_repo: SpecRepoConfig = field(default_factory=SpecRepoConfig)
