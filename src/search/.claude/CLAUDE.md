# Search Module

## Purpose
Semantic similarity search over any `BaseVectorStore` backend using precomputed embeddings.
`FAISSSearch` is the primary class but is now fully backend-agnostic — it delegates all vector
math to `store.search_by_vector()`.

## Files

| File | Class(es) | Role |
|---|---|---|
| `faiss_search.py` | `FAISSSearch`, `SearchResult` (re-export) | Search orchestration |
| `reranker.py` | `CandidateReranker`, `RerankerWeights`, `RankedCandidate` | Structural re-ranking of FAISS candidates using entity/cluster/KG signals |

## SearchResult
Defined in `src.database.base_store`; re-exported here for backward compatibility.
```python
@dataclass
class SearchResult:
    score: float        # cosine similarity 0.0–1.0
    doc_id: str
    page_content: str
    metadata: dict      # tc_id, cluster_name, pics_codes, section_type, path, …
    rank: int
```

## FAISSSearch API

| Method | Input | Output |
|---|---|---|
| `search(query, k, threshold)` | text query | `List[SearchResult]` |
| `search_by_vector(vec, k, threshold)` | precomputed `np.ndarray` | `List[SearchResult]` |
| `batch_search(queries, k, threshold)` | list of text queries | `List[List[SearchResult]]` |

## Backend-agnostic design
`FAISSSearch` works with any `BaseVectorStore`:
```python
store = create_vector_store(cfg.database)   # faiss | chroma | postgres
searcher = FAISSSearch(store, embedder)
results = searcher.search("OnOff cluster test", k=10, threshold=0.65)
```

## threshold
Minimum cosine similarity score. Results below are excluded.
Pipeline default: `pipeline.similarity_threshold = 0.65`.
Range: `0.0` (keep all) to `1.0` (exact match only).

---

## CandidateReranker (`reranker.py`)

Sits between FAISS retrieval and LLM analysis.  Re-scores top-K FAISS candidates
using structural signals the embedding model cannot distinguish.

### Scoring components (default weights)

| Component | Weight | Signal |
|---|---|---|
| `entity_overlap` | 0.25 | Exact entity names from structured change record |
| `kg_direct_bonus` | 0.20 | KG has a direct edge to this TC |
| `cluster_match` | 0.15 | Cluster name (exact + token-level) |
| `condition_effect_overlap` | 0.15 | Both sides of a behaviour rule covered |
| `intent_match` | 0.15 | Test intents align with `ChangeKind` |
| `kg_indirect_bonus` | 0.08 | KG has a 2-hop edge to this TC |
| `lexical_similarity` | 0.08 | Dice-coefficient token overlap with PR/spec text |
| `chunk_type_bonus` | 0.05 | `intent_summary` > `test_step` > `setup` |
| `retrieval_score` | 0.04 | Original cosine similarity (tiebreaker) |

### Deterministic sorting

The final sort in `rerank_candidates` uses `key=lambda r: (-round(r.final_score, 2), r.test_case_id)`.
This ensures deterministic ordering within 0.01 score bands — candidates with near-identical
scores are sorted alphabetically by TC ID, so results are stable across runs.

### Usage

```python
from src.search.reranker import CandidateReranker, rerank_candidates

# One-shot convenience
ranked = rerank_candidates(
    structured_change = change.to_dict(),  # from ChangeExtractor
    query_text        = pr_chunk.text,
    candidates        = vector_results,    # list[dict] from FAISS
    kg_hits           = kg_result,         # optional {"direct_tests": [...], ...}
    top_n             = 5,
)
for r in ranked:
    print(f"{r.final_score:.3f}  {r.test_case_id}  {r.reason}")
```

### RankedCandidate fields

`candidate_id`, `test_case_id`, `final_score`, `score_breakdown` (dict), `reason` (string),
`chunk_type`, `title`, `text`, `metadata`

### kg_hits format

```python
kg_hits = {
    "direct_tests":    ["TC_OCC_1"],      # TCs with direct KG edge to change entities
    "indirect_tests":  ["TC_OCC_2"],      # TCs with 2-hop KG path
    "matched_entities": ["ATTRIBUTE::OccupancySensing::Occupancy"],
}
```

Direct and indirect bonuses are mutually exclusive — direct takes priority.
