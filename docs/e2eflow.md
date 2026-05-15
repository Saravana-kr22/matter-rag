# End-to-End Flow — Matter RAG Pipeline

This document explains the complete data flow from input HTML to final test case report,
including chunking, embedding, vector search, knowledge graph, reranking, LLM prompt
assembly, and multi-pass TC generation.

---

## 1. Input Processing

### Document Fetching (`fetch_documents_node`)

Input sources (configured in `sources.json`):
- **PR diff HTML** (`--input-doc` or `PR_URL`) → `pr_documents`
- **Test plan HTML** (allclusters.html, index.html) → `test_plan_fetched`
- **Spec HTML** (matter spec) → `spec_fetched`
- **DM XML** (cluster definitions) → `data_model_fetched`

### Document Processing (`process_documents_node`)

Applies `.ignore_rules.json` text cleaning, then expands Matter diff HTML:
- `ProcessMatterHtmlDoc` finds sections with `<ins class="diff-new">`, `<del class="diff-old">`, `<ins class="diff-chg">`
- Each changed section becomes one `FetchedDocument` with annotated text:
  - `[ADDED: new text here]`
  - `[REMOVED: deleted text here]`
  - `[CHANGED: old text → new text]`

---

## 2. Test Plan Chunking and Embedding

### TC-Aware Chunking (`vector_chunk_gen.py`)

Each of the 858 test cases produces up to **4 specialized chunks**:

| Chunk Type | Content | Retrieval Purpose |
|---|---|---|
| `full` | TC-ID + title + cluster + purpose + prerequisites + all steps + outcomes | Broad match — any query about this TC |
| `intent_summary` | TC-ID + title + cluster + intents + entity_refs (dense, ~200 chars) | Entity/intent match — "find TCs that read OnTime" |
| `procedure` | Numbered procedure steps only | Step-level match — "find TCs that invoke ResetCounts" |
| `setup` | Prerequisites + test environment + DUT type | Environment match — "find server DUT TCs" |

**Splitting oversized chunks:**

BGE-large-en-v1.5 has a 512-token limit (~1450 chars). When `full` or `procedure` chunks
exceed this, they're split at sentence boundaries with a TC context prefix:
`[TC-OO-2.1] Attributes — On/Off Cluster (part 2/3)`

**Output:** 5,711 total chunks from 858 TCs.

### Embedding (`src/embeddings/embeddings.py`)

| Property | Value |
|---|---|
| Model | `BAAI/bge-large-en-v1.5` |
| Dimensions | 1024 |
| Max tokens | 512 |
| Device | Apple Silicon MPS GPU (fallback: CPU) |
| Batch size | 64 |
| Normalization | L2 normalized (enables cosine via inner product) |
| Total vectors | 5,711 |
| Embedding time | ~30 seconds on MPS |

### FAISS Index Storage (`src/database/faiss_store.py`)

| Property | Value |
|---|---|
| Index type | `IndexFlatIP` (inner product on L2-normalized vectors = cosine) |
| Index file | `data/faiss_index/index.faiss` (22 MB) |
| Metadata file | `data/faiss_index/metadata.json` (11 MB) |
| Metadata per chunk | tc_id, cluster, intents, entity_refs, chunk_type, source_doc, mode |

---

## 3. Knowledge Graph Construction

### Data Sources → Node Types

| Source | Nodes Created | Count |
|---|---|---|
| DM XML | CLUSTER, ATTRIBUTE, COMMAND, EVENT, FEATURE | 2,260 |
| Spec HTML | REQUIREMENT, BEHAVIOR_RULE, SECTION, PROTOCOL_AREA | 20,149 |
| Test plan HTML | TEST_CASE | 858 |
| Pipeline (virtual) | PROMPT_SECTION, BEHAVIOR, PICS_ITEM | 14 |
| **Total** | | **23,281 nodes** |

### Edge Types and Counts

| Edge Type | Count | Meaning |
|---|---|---|
| BELONGS_TO_PROTOCOL_AREA | 62,913 | Node belongs to a protocol area hierarchy |
| verifies_requirement | 61,753 | TC verifies a spec requirement |
| belongs_to | 8,021 | REQ belongs to a SECTION |
| implements | 6,411 | REQ implements/references an entity |
| references | 4,794 | Generic reference edge |
| reads | 1,989 | TC reads an attribute |
| tests_command | 1,441 | TC tests a command |
| tests | 1,384 | TC tests a cluster |
| HAS_ATTRIBUTE | 1,138 | Cluster has an attribute |
| depends_on | 579 | Cross-cluster dependency |
| HAS_COMMAND | 472 | Cluster has a command |
| HAS_FEATURE | 354 | Cluster has a feature |
| alias_of | 14 | Derived cluster → base cluster |
| **Total** | | **152,445 edges** |

### Requirement Extraction (Rule Engine)

The rule engine scans spec text for normative keywords:
```
SHALL, SHALL NOT, MUST, MUST NOT, is required, is prohibited, may not
```

Each normative sentence is classified into a `RequirementType`:
- `timing_requirement` — "within N seconds", "timeout"
- `state_transition_rule` — "transition from X to Y", "reset to 0"
- `value_constraint` — "range", "between", "at least"
- `conditional_behavior_rule` — "if X then SHALL Y"
- `cross_entity_dependency` — references entities from other clusters
- `entity_definition_changed` — defines what an entity IS/DOES
- `general_normative_requirement` — any other SHALL/MUST

### TC → Entity Edge Creation

For each TC, the rule engine creates typed edges based on intents + entity_refs:

```
READ_ATTRIBUTE intent + ATTRIBUTE entity → "reads" edge
WRITE_ATTRIBUTE intent + ATTRIBUTE entity → "writes" edge
INVOKE_COMMAND intent + COMMAND entity → "tests_command" edge
OBSERVE_EVENT intent + EVENT entity → "observes_event" edge
```

### Spec Record Linking (3-Tier)

TCs are linked to spec requirements via `_link_spec_records`:

| Tier | Method | Precision |
|---|---|---|
| Tier 1 | Entity-name match against spec_index | Highest — matches by specific attribute/command name |
| Tier 1.5 | Extract entity names from procedure step text | High — "TH reads OverrunCount" → links to OverrunCount REQs |
| Tier 2 | Section-path keyword matching | Medium — TC text keywords match requirement section headings |
| Tier 3 | Cluster-level fallback | Lowest — all requirements for the TC's cluster |

All tiers apply unconditional cluster filtering via `spec_id_to_cluster` reverse map to prevent cross-cluster bleeding.

---

## 4. PR Chunk Processing

### Semantic PR Chunking (`chunk_pr_node`)

The PR diff HTML is split into semantic chunks by the `SemanticPRChunker`:
- Split at section headings (h3/h4/h5 boundaries)
- Each chunk = one spec section with its diff annotations
- Chunks below `min_chunk_chars` (80) are rejected
- Metadata: cluster, section_title, change_types, section_level

### Structured Change Extraction (`extract_pr_changes_node`)

For each PR chunk, the `ChangeExtractor` classifies the change:
- **Rule-based** (first pass): regex on `[ADDED/REMOVED/CHANGED]` annotations
- **LLM fallback**: when confidence < threshold, asks LLM to classify

Output per chunk:
```json
{
  "change_kind": "MODIFY_ATTRIBUTE",
  "cluster": "On/Off Cluster",
  "entities": [{"type": "attribute", "name": "OnTime", "id": "0x4001"}],
  "change_summary": "Added Quieter Reporting quality for OnTime",
  "confidence": 0.85
}
```

### Batch Packing (`_pack_cluster_chunks_into_batches`)

PR chunks are grouped by cluster and packed into LLM-sized batches:
- Sorted by section depth (overview sections first)
- Greedy-packed until diff budget reached
- Diff budget derived from `config.llm.max_prompt_chars` minus section overhead
- Default: ~12K chars of diff content per batch

---

## 5. Retrieval (Search Phase)

### Vector Search (`search_test_plan_vector_db_node`)

For each PR chunk batch:
1. Encode PR chunk text with BGE (with instruction prefix for queries)
2. FAISS inner-product search → top-k candidates (default k=10)
3. Filter by `similarity_threshold` (0.65)
4. Return `List[SearchResult]` with score + full metadata

### KG Entity Search (`search_knowledge_graph_node`)

For each PR chunk with a structured change record:
1. Look up the changed entity node ID: `ATTRIBUTE::On/Off::OnTime`
2. Build 2-hop undirected ego-graph around that node
3. Collect all TEST_CASE nodes within 2 hops
4. Also collect REQUIREMENT nodes (for Section R)
5. Return up to 10 nodes sorted by relevance

### Reranking (`src/search/reranker.py`)

FAISS candidates are re-scored using 9 weighted dimensions:

| Signal | Weight | What it measures |
|---|---|---|
| Entity overlap | 0.25 | TC entity_refs ∩ PR change entities |
| KG direct bonus | 0.20 | KG also found this TC via graph traversal |
| Cluster match | 0.15 | TC from same cluster as change |
| Condition/effect overlap | 0.15 | TC conditions match change conditions |
| Intent match | 0.15 | TC intent (read/write/invoke) matches change type |
| KG indirect bonus | 0.08 | TC shares a requirement with a KG-found TC |
| Lexical similarity | 0.08 | Jaccard keyword overlap |
| Chunk type bonus | 0.05 | Prefer "full" chunks over fragments |
| Retrieval score | 0.04 | Original FAISS cosine score |

Ties within 0.01 are broken by TC-ID for determinism.

---

## 6. LLM Prompt Assembly (Pass 1)

### System Prompt (~24K chars, ~7K tokens)

| Component | Chars | Source |
|---|---|---|
| Skill file (TC naming, PICS format, coverage rules, boundary testing) | ~17,000 | `llm_prompts/matter_test_coverage_and_structure.md` |
| PROMPT_SECTION nodes (spec sections 7.3/7.6/7.7) | ~5,000 | KG PROMPT_SECTION nodes |
| Additional context (if `--llm-additional-context` provided) | varies | Per-pass from directory/file |

### User Prompt Sections (~40K chars, ~12K tokens typical)

| Section | Content | Typical Size | Source |
|---|---|---|---|
| **Change JSON** | Structured change record (cluster, entities, change_kind) | 500 chars | `extract_pr_changes_node` |
| **PR Diff** | Annotated diff text with `[ADDED/REMOVED/CHANGED]` markers | 3,000-12,000 chars | `chunk_pr_node` → `_prepare_diff_content()` |
| **Section S** | OLD spec text for changed sections (before the PR) | 200-2,000 chars | HTML diff `<del>` markers |
| **Section T** | Full surrounding spec prose for the changed section | 200-2,000 chars | KG SECTION nodes |
| **Section R** | Spec requirements linked to the changed entities | 500-3,000 chars | KG REQUIREMENT nodes via entity cross-reference |
| **Section X** | Surrounding cluster context (sibling sections) | 200-1,000 chars | KG neighbor traversal |
| **Section A** | Top-K reranked FAISS hits (TC summaries) | 2,000-5,000 chars | FAISS → reranker |
| **Section B** | KG entity-linked TCs and requirements | 100-2,000 chars | `search_by_structured_change()` |
| **Section C** | All existing TC-IDs for this cluster (flat list) | 100-500 chars | `_collect_cluster_tc_nodes()` |
| **Section D** | Full TC content for all cluster TCs (steps, outcomes) | 5,000-50,000 chars | KG TC nodes with full adoc_section |
| **Task** | JSON output format instructions + rules | 6,600 chars | Hardcoded template |

### Total Prompt Size (typical)

| Component | Chars | Est. Tokens |
|---|---|---|
| System prompt | 24,000 | ~7,000 |
| User prompt | 40,000 | ~12,000 |
| **Total** | **64,000** | **~19,000** |

### Prompt Safety Cap

If total prompt exceeds `config.llm.max_prompt_chars` (auto-detected from model context):
1. Section D (full TC content) is truncated first
2. Warning logged with truncation amount
3. For Claude 200K: cap is ~420K chars (never triggered in practice)
4. For 64K models: cap is ~134K chars (may truncate Section D for large clusters)

---

## 7. Multi-Pass LLM Analysis

### Pass 1: Per-Chunk Analysis (`analyze_chunks_with_llm_node`)

**1 LLM call per batch** (batches grouped by cluster).

LLM receives the full prompt (Sections S/T/R/X/A/B/C/D + diff + task) and returns:

```json
{
  "change_summary": "Added Quieter Reporting quality to OnTime attribute",
  "impacted_entities": ["OnTime", "OffWaitTime"],
  "missing_tests": [
    {
      "tc_id": "TC-OO-3.4",
      "title": "OnTime Quieter Reporting Subscription Verification",
      "cluster": "On/Off Cluster",
      "test_type": "unit",
      "justification": "No existing TC verifies suppressed reporting..."
    }
  ],
  "update_candidates": [
    {
      "tc_id": "TC-OO-2.1",
      "reason": "Add step to verify OnTime reporting threshold behavior"
    }
  ]
}
```

After all batches: `_deduplicate_missing_tc_ids()` resolves TC number collisions.

### Pass 2: Cluster Review (`cluster_review_node`)

**1 LLM call per cluster** that had Pass 1 results.

Audits the full per-cluster picture:
- Symmetry gaps (server test exists but no client test)
- Missing test types (no negative test, no boundary test)
- Duplicate coverage across TCs

### Pass 3: Holistic TC Generation (`second_pass_tc_gen_node`)

Two parts:

**Part A — Consolidation** (1 LLM call per triggered cluster):
- Dedup-only: removes duplicate TC proposals from Pass 1
- No gap-filling — just cleanup

**Part B — Coverage Gap TCs** (when `--no-coverage-gaps` is NOT set):
- Queries KG for REQUIREMENT nodes with no TC coverage
- **Outline call**: 1 LLM call to propose TC outlines for uncovered requirements
- **Expand calls**: 1 LLM call per TC to generate full adoc procedure

Trigger conditions:
- `pass1_missing > 5` → always triggers (many gaps found)
- `existing_count < 5 AND is_pr_relevant` → triggers for sparse clusters touched by this PR

### Pass 4: Human Outline Expand (`human_outline_expand_node`)

Only runs when `--third-pass-expand /path/to/outline.json` is provided.
Re-expands a human-modified outline JSON into full TC adoc. No-op otherwise.

---

## 8. Output Generation

### TC Deduplication

`_deduplicate_missing_tc_ids()` assigns deterministic TC numbers:
- Sort all proposed TCs by title text (alphabetical)
- Assign sequential minor version numbers (2.14, 2.15, 2.16...)
- Same titles always get same numbers regardless of LLM generation order

### Report Generation (`generate_report_node`)

Output files in `reports/matter_rag_reports_<timestamp>/`:

| File | Content |
|---|---|
| `test_coverage_final_<ts>.html` | HTML report with Section 1 (PR-driven) + Section 2 (coverage gaps) |
| `test_coverage_pass1_<ts>.html` | Pass 1 results only (before consolidation) |
| `report_data_<ts>.json` | Machine-readable: missing_tests, update_candidates, coverage_gap_tests |
| `llm_calls.html` | All LLM prompts/responses with collapsible panes |
| `llm_generated_adocs/*.adoc` | Per-cluster generated TC adoc files |

### Adoc Writing (`write_adoc_updates_node` + `write_updated_testplan_node`)

- Writes TC updates back to source `.adoc` files using `tc_index.json` routing
- Writes per-cluster updated test plan files to `reports/updated_testplans_<ts>/`

---

## 9. LLM Call Summary

| Stage | Calls | Prompt Size | Purpose |
|---|---|---|---|
| Extract PR changes (LLM fallback) | 0-N (only when rule-based confidence < 0.6) | ~2K chars | Classify change type |
| Pass 1 (analyze_chunks_with_llm) | 1 per batch | ~64K chars | Identify missing/update TCs |
| Pass 2 (cluster_review) | 1 per cluster | ~8-20K chars | Audit symmetry + coverage |
| Pass 3 consolidation | 1 per triggered cluster | ~15-25K chars | Dedup TCs |
| Pass 3 coverage gap outline | 1 per cluster with gaps | ~10-15K chars | Propose gap TC outlines |
| Pass 3 coverage gap expand | 1 per gap TC | ~8-12K chars | Generate full adoc per TC |

Typical PR touching 1 cluster: **4-8 LLM calls total**.
Large PR touching 5 clusters with coverage gaps: **20-40 LLM calls total**.

---

## 10. Configuration Reference

### Key Config Fields

```yaml
llm:
  provider: claude_subprocess    # claude_subprocess | claude_cli | local | lm_studio | gemini
  temperature: 0.0              # 0.0 = most deterministic
  max_prompt_chars: 0           # 0 = auto-detect from model context window
  subprocess_timeout: 600       # seconds per LLM call

pipeline:
  search_top_k: 10             # FAISS candidates per query
  similarity_threshold: 0.65   # minimum cosine to keep a FAISS result
  min_chunk_chars: 80          # reject PR diff sections shorter than this
  system_prompt_skills_file: llm_prompts/matter_test_coverage_and_structure.md

embeddings:
  model: BAAI/bge-large-en-v1.5
  device: mps                  # mps | cuda | cpu

analysis:
  max_llm_calls_per_run: 0     # 0 = unlimited
  parallel_workers: 4          # concurrent LLM calls for PICS/coverage analysis
```

### Context Window Auto-Detection

At startup, `get_llm()` queries the model's context window:
- Minimum: 64K tokens (pipeline rejects smaller models)
- Auto-sets: `max_prompt_chars = context_tokens * 3.5 chars/token * 60% budget`
- 40% reserved for model response output
- Claude 200K → 420K chars budget (never constrains in practice)
- 128K model → 268K chars budget
- 64K model → 134K chars budget (Section D may be truncated for large clusters)

---

## 11. Pipeline DAG

```
fetch_documents → process_documents → ingest_data_model → build_matter_schema
  → chunk_embed_test_plans → chunk_pr → extract_pr_changes → build_knowledge_graph
      │
      ├─ [no PR chunks] → cleanup → END
      │
      └─ [PR chunks present]
          → search_test_plan_vector_db → search_knowledge_graph
            → analyze_chunks_with_llm (Pass 1)
              → cluster_review (Pass 2)
                → second_pass_tc_gen (Pass 3)
                  → human_outline_expand (Pass 4)
                    → write_adoc_updates → write_updated_testplan
                      → generate_report → cleanup → END
```

18 nodes total. Conditional routing after `build_knowledge_graph` based on whether PR chunks exist.

---

## 12. Analysis Pipelines (Separate from PR Analysis)

### Coverage Analysis (`run_coverage_analysis.py`)

5-node pipeline. Per cluster:
- Phase 1: Classify all requirements as covered/partial/uncovered (1 LLM call)
- Phase 2: Generate gap descriptions for uncovered/partial (1 LLM call)
- Supports parallel workers (`analysis.parallel_workers`)

### PICS Validation (`run_pics_analysis.py`)

6-node pipeline. Per cluster:
- Deterministic pre-pass: wrong_side, non_existent, dut_type_mismatch (no LLM)
- LLM pass: missing_feature_pics, missing_protocol_pics, step_pics_mismatch
- Supports parallel workers

### SDK Coverage (`run_sdk_coverage_analysis.py`)

6-node pipeline. Cross-checks spec requirements against SDK implementation code.
Reads `.cpp/.h` files from `{sdk_dir}/src/app/clusters/`.
