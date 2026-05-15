# Matter RAG Pipeline — Complete Project Flow

> **Purpose:** End-to-end call-flow reference for debugging, onboarding, and tracing.
> Every node, every file artifact, every LLM call, every log line explained.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Full Pipeline DAG](#2-full-pipeline-dag)
3. [Node-by-Node Call Flow](#3-node-by-node-call-flow)
   - [Node 0 — CLI Entry](#node-0--cli-entry-scriptsrun_pipelinepy)
   - [Node 1 — fetch_documents_node](#node-1--fetch_documents_node)
   - [Node 2 — process_documents_node](#node-2--process_documents_node)
   - [Node 3 — ingest_data_model_node](#node-3--ingest_data_model_node)
   - [Node 4 — build_matter_schema_node](#node-4--build_matter_schema_node)
   - [Node 5 — chunk_embed_test_plans_node](#node-5--chunk_embed_test_plans_node)
   - [Node 6 — chunk_pr_node](#node-6--chunk_pr_node)
   - [Node 7 — extract_pr_changes_node](#node-7--extract_pr_changes_node)
   - [Node 8 — build_knowledge_graph_node](#node-8--build_knowledge_graph_node)
   - [Node 9 — search_test_plan_vector_db_node](#node-9--search_test_plan_vector_db_node)
   - [Node 10 — search_knowledge_graph_node](#node-10--search_knowledge_graph_node)
   - [Node 11 — analyze_with_llm_node](#node-11--analyze_with_llm_node)
   - [Node 12 — write_adoc_updates_node](#node-12--write_adoc_updates_node)
   - [Node 13 — generate_report_node](#node-13--generate_report_node)
   - [Node 14 — cleanup_node](#node-14--cleanup_node)
4. [LLM Call Analysis](#4-llm-call-analysis)
5. [File Artifacts Per Run](#5-file-artifacts-per-run)
6. [Logging Structure](#6-logging-structure)
7. [Build-Once Control Flags](#7-build-once-control-flags)
8. [Operating Modes](#8-operating-modes)
9. [Debugging Guide](#9-debugging-guide)

---

## 1. Architecture Overview

```
sources.json / CLI args
        │
        ▼
  run_pipeline.py          ← CLI entry point
        │
        ▼
  MatterRAGPipeline        ← LangGraph StateGraph orchestrator
        │
        ▼
  PipelineState (dict)     ← shared state passed through all nodes
        │
        ├── Fetch layer    (nodes 1–2)  data ingestion + cleaning
        ├── Schema layer   (nodes 3–4)  Matter DM XML + spec schema
        ├── Index layer    (node 5)     vector DB build-or-load (KB pipeline)
        ├── PR layer       (nodes 6–7) chunk PR + extract structured changes
        ├── KG layer       (node 8)    knowledge graph build-or-load (KB pipeline)
        ├── Search layer   (nodes 9–10) vector + KG retrieval per PR chunk
        ├── Analysis layer (node 11)   LLM analysis (1 call per PR chunk)
        ├── Output layer   (nodes 12–13) write .adoc patches + report
        └── Cleanup layer  (node 14)   GPU/memory release + run summary
```

**Key data types:**

| Type | Module | Description |
|---|---|---|
| `FetchedDocument` | `src/fetcher/base_fetcher.py` | Raw doc from any source (path, content, metadata) |
| `Document` | `src/loader/base_loader.py` | Chunked text unit ready for embedding |
| `SearchResult` | `src/database/base_store.py` | Vector search hit (score, doc_id, page_content, metadata) |
| `GraphNode` | `src/knowledge_graph/base_graph.py` | KG node (node_id, node_type, name, metadata) |
| `PipelineState` | `src/engine/nodes.py` | TypedDict — the shared mutable state dict |

---

## 2. Full Pipeline DAG

```
CLI: run_pipeline.py
        │
        ▼
[1] fetch_documents_node
        │  reads: sources.json + env vars (PR_URL, GITHUB_TOKEN)
        │  writes: pr_documents, test_plan_fetched, spec_fetched, data_model_fetched
        ▼
[2] process_documents_node
        │  reads: pr_documents, test_plan_fetched, spec_fetched, data_model_fetched
        │  applies: .ignore_rules.json + per-source rules
        │  expands: matter_diff HTML → 1 FetchedDocument per diff section
        │  writes: pr_documents (expanded), spec_diff_html (originals before expansion)
        ▼
[3] ingest_data_model_node
        │  reads: data_model_fetched (Matter DM XML → FetchedDocuments from MatterXMLFetcher)
        │  writes: <run_dir>/data_model_schema.json  (inspection file)
        │  passes through: data_model_fetched unchanged (used by KG node later)
        ▼
[4] build_matter_schema_node
        │  reads: spec_diff_html (original HTML before expansion)
        │  extracts: entity tables (attributes/commands/events/features) with diff_status
        │  writes: <run_dir>/matter_schema.json  (inspection file)
        │  writes: state["matter_schema"]
        ▼
[5] chunk_embed_test_plans_node
        │  reads: test_plan_fetched
        │  always: chunk docs → test_plan_chunks  (cheap, needed for KG)
        │  IF build_test_plan_vectors OR no FAISS index on disk:
        │      embed test_plan_chunks with BGE model → save FAISS index + metadata.json
        │  ELSE:
        │      store.load() ← restores FAISS index from disk (fast)
        │  writes: test_plan_chunks, vector_store
        ▼
[6] chunk_pr_node
        │  reads: pr_documents (may be 158 matter_spec_diff sections)
        │  chunks via SemanticPRChunker:
        │      matter_spec_diff → 1 chunk per section (pass-through)
        │      unified diff    → split at @@ hunk boundaries
        │      .adoc           → split at == headings
        │      fallback        → blank-line paragraphs
        │  reads: spec_fetched → spec_chunks (for KG)
        │  applies: cluster_filter (drops off-cluster chunks)
        │  writes: pr_chunks, spec_chunks
        ▼
[7] extract_pr_changes_node
        │  reads: pr_chunks (one per PR diff section)
        │  for each chunk:
        │      ChangeExtractor (rule-based) → StructuredChange
        │      IF confidence < 0.6 AND ambiguous → LLM fallback call  ← LLM CALL (occasional)
        │  writes: pr_changes (list of structured change records)
        │  writes: <run_dir>/pr_changes.json  (inspection file)
        ▼
[8] build_knowledge_graph_node
        │  IF build_knowledge_graph OR no KG JSON file on disk:
        │      add_data_model_documents(data_model_fetched) → CLUSTER/ATTR/CMD nodes
        │      add_spec_documents(spec_chunks)              → REQUIREMENT/SECTION nodes
        │      add_test_plan_documents(test_plan_chunks)    → TEST_CASE/SECTION nodes
        │      extract_matter_entities(all chunks)
        │      export_json → data/knowledge_graph/matter_kg.json
        │  ELSE:
        │      load_from_json(matter_kg.json)  ← fast warm load
        │  ALWAYS: add_pr_documents(pr_chunks) → PR_CHANGE nodes (transient, not saved)
        │  writes: knowledge_graph
        │
        ├── [no pr_chunks] → cleanup_node → END  (build-only / --index-only run)
        │
        └── [pr_chunks present] →
                ▼
[9] search_test_plan_vector_db_node
        │  reads: pr_chunks, vector_store
        │  for each pr_chunk:
        │      embed chunk text as query (BGE model)
        │      FAISS.search(query_vec, k=10, threshold=0.65)
        │      → top-K SearchResult objects
        │  writes: search_results  { "pr_0": [SearchResult,...], "pr_1": [...], ... }
        ▼
[10] search_knowledge_graph_node
        │  reads: pr_chunks, pr_changes, knowledge_graph
        │  for each pr_chunk:
        │      IF structured change record available:
        │          kg.search_by_structured_change(cluster, entity_type, entity_name)
        │          fallback to kg.search_by_entities() if no hits
        │      ELSE:
        │          kg.search_by_entities(chunk text)  ← regex entity extraction
        │  writes: graph_results  { "pr_0": [GraphNode,...], ... }
        ▼
[11] analyze_with_llm_node                              ← LLM CALL: 1 per PR chunk
        │  reads: pr_chunks, pr_changes, search_results, graph_results
        │  for each pr_chunk:
        │      rerank vector hits (RerankerWeights, 9 scoring components)
        │      build prompt:
        │          Section A — re-ranked vector search hits (test plan candidates)
        │          Section B — KG entity-matched nodes
        │          Change record JSON (from extract_pr_changes_node)
        │          PR diff text (first 4000 chars of chunk)
        │      llm.complete(prompt)  ← LOGGED with ==> / <==
        │      parse JSON from response
        │  writes: analysis_results, missing_tests, update_candidates
        ▼
[12] write_adoc_updates_node
        │  reads: analysis_results, search_results
        │  for each analysis result with update_candidates:
        │      patch existing TC in source .adoc file
        │  for each analysis result with missing_tests:
        │      append new TC section to cluster's update file
        │  writes: reports/adoc_updates_<ts>/  (patched .adoc files)
        │  writes: adoc_output_paths
        ▼
[13] generate_report_node
        │  reads: missing_tests, update_candidates, analysis_results, pr_changes
        │  writes: reports/<run_ts>/report.md  (Markdown summary)
        │  writes: reports/<run_ts>/report.json (structured JSON)
        │  writes: report_path
        ▼
[14] cleanup_node
        │  releases GPU memory (MPS/CUDA) + gc.collect()
        │  logs one-line run summary
        ▼
       END
```

---

## 3. Node-by-Node Call Flow

### Node 0 — CLI Entry (`scripts/run_pipeline.py`)

```
python scripts/run_pipeline.py [args]
    │
    ├── Pre-import: read config.yaml with stdlib yaml
    │   IF embeddings.offline: true → os.environ["HF_HUB_OFFLINE"] = "1"
    │   (must be set before ANY import — huggingface_hub reads this at import time)
    │
    ├── parse_args() → argparse.Namespace
    │   --pr-url / --pr-number    GitHub PR to analyse
    │   --input-doc FILE          Local HTML or .adoc to analyse (alternative to PR)
    │   --cluster NAME            Limit to one cluster (partial match, case-insensitive)
    │   --build-test-plan-vectors Rebuild and save FAISS vector DB (run once)
    │   --build-knowledge-graph   Rebuild and save KG (run once)
    │   --build-data-model        Re-ingest DM XML into KG
    │   --index-only              Alias: all 3 build flags = True, no PR required
    │   --compare-only            Alias: all 3 build flags = False (use cache)
    │   --output DIR              Report output directory
    │   --log-level LEVEL         VERBOSE | DEBUG | INFO | WARNING | ERROR
    │
    ├── load_config(args.config) → AppConfig
    ├── configure_pipeline_logging(config) → run_dir
    ├── MatterRAGPipeline(config)
    └── pipeline.run(pr_url, input_doc, cluster_filter, build_*, output_dir)
```

**Relevant log file:** `master.log` (everything), console (INFO and above)

---

### Node 1 — `fetch_documents_node`

```
state["pr_url"] or state["input_doc"]
state["config"]
    │
    ├── load_sources("sources.json")
    │   for each source in sources:
    │       create_fetcher(source_cfg) → BaseFetcher subclass
    │       fetcher.fetch() → List[FetchedDocument]
    │       route by role:
    │           role="pr"         → pr_documents
    │           role="test_plan"  → test_plan_fetched   (default)
    │           role="spec"       → spec_fetched
    │           role="data_model" → data_model_fetched
    │
    ├── Fetcher types and what they produce:
    │   github_pr        → unified diff patches (.patch text per file)
    │   github_repo      → full file downloads saved to local_save_dir
    │   github_tag_diff  → compare API diff (base tag → PR head)
    │   local_folder     → recursive rglob("*"), UTF-8 read
    │   url              → HTTP GET; HTML stripped or preserved per format=
    │   csv              → each row → 1 FetchedDocument (prose)
    │   matter_xml       → each cluster → 1 FetchedDocument + metadata["schema"]
    │
    ├── --input-doc handling (local HTML/adoc instead of GitHub PR):
    │   reads file content, sets metadata["matter_diff"]=True for .html/.htm
    │   routes to pr_documents
    │
    └── Writes to state:
        pr_documents        List[FetchedDocument]
        test_plan_fetched   List[FetchedDocument]
        spec_fetched        List[FetchedDocument]
        data_model_fetched  List[FetchedDocument]
```

**Log line to watch:**
```
[fetch_documents_node] Total: pr=1  test_plan=184  spec=0  data_model=4
```

---

### Node 2 — `process_documents_node`

```
pr_documents, test_plan_fetched, spec_fetched, data_model_fetched
    │
    ├── DocumentProcessor(".ignore_rules.json")
    │   applies global rules to EVERY document:
    │       strip_regex        → remove matching lines/blocks
    │       normalize_whitespace → collapse 3+ blank lines to 1
    │       replace_regex      → text substitutions
    │   applies per-source rules from doc.metadata["_process_rules"]
    │
    ├── _maybe_convert_adoc_to_html(doc)  [if convert_adoc_to_html: true in config]
    │   subprocess: asciidoctor -b html5 → .html
    │
    ├── _expand_matter_html(docs)
    │   for each doc with extension .html/.htm AND metadata["matter_diff"]=True:
    │       ProcessMatterHtmlDoc(cluster_filter, section_filter)
    │       .process(doc) → List[FetchedDocument]  ← one per diff section
    │       each output doc:
    │           doc_type   = "matter_spec_diff"
    │           cluster    = "11.7. Push AV Stream Transport Cluster"
    │           section_title = "11.7.1. CMAF Ingestion"
    │           content    = "[CHANGED: old → new] ... plain text ..."
    │
    ├── EXAMPLE: appclusters_diff.html
    │   1 HTML file → ProcessMatterHtmlDoc → 158 FetchedDocuments
    │   each = one spec diff section with [CHANGED/ADDED/REMOVED] annotations
    │
    ├── spec_diff_html_originals captured BEFORE expansion
    │   (needed by build_matter_schema_node to parse entity tables)
    │
    └── _write_matter_diff_inspection(pr_docs + spec_docs, run_dir)
        writes: <run_dir>/matter_diff_sections.json
        { "total_sections": 158, "sections": [{ "index":0, "cluster":"...", ... }] }
```

**Log lines to watch:**
```
[process_documents_node] Expanded Matter HTML diff appclusters_diff.html → 158 sections
[process_documents_node] Processed: PR=158  test_plan=184  spec=0  (convert_adoc_to_html=False)
```

---

### Node 3 — `ingest_data_model_node`

```
data_model_fetched  (from MatterXMLFetcher: each cluster = 1 FetchedDocument)
    │
    ├── For each doc: read doc.metadata["schema"]
    │   schema = {
    │     "cluster_name": "On/Off",
    │     "cluster_id": "0x0006",
    │     "revision": "6",
    │     "attributes": [{"id":"0x0000","name":"OnOff","type":"boolean",...}],
    │     "commands": [...], "events": [...], "features": [...]
    │   }
    │
    ├── Writes inspection file: <run_dir>/data_model_schema.json
    │
    └── Passes data_model_fetched through (unchanged, consumed by KG node)
```

**Log line to watch:**
```
[ingest_data_model_node] Data model: 4 clusters, 0 errors
```
**If you see "No data_model documents — skipping":** Check that `sources.json` has an entry
with `"type": "matter_xml", "role": "data_model"` and that `data/data_model/` contains `.xml` files.

---

### Node 4 — `build_matter_schema_node`

```
spec_diff_html  (original HTML before section expansion)
    │
    ├── MatterSchemaExtractor().extract(html_content)
    │   Parses entity tables directly from the diff HTML:
    │       <table class="tableblock ..."> → attributes / commands / events / features
    │       diff_status per row: "added" | "removed" | "changed" | "unchanged"
    │
    ├── Output:
    │   state["matter_schema"] = {
    │     "clusters": [{
    │       "name": "On/Off", "diff_status": "changed",
    │       "attributes": [{"id":"0x0000","name":"OnOff","diff_status":"unchanged",...}],
    │       "commands": [...], "events": [...], "features": [...]
    │     }]
    │   }
    │
    └── Writes: <run_dir>/matter_schema.json
```

**If no matter_diff HTML in state:** node skips with a log message; `matter_schema` stays empty.

---

### Node 5 — `chunk_embed_test_plans_node`

```
test_plan_fetched, spec_fetched, data_model_fetched  (all available in state)
    │
    ├── create_vector_store(config.database)  (FAISS by default)
    │
    ├── build_flag resolution:
    │   1. state["build_test_plan_vectors"]         (from --build-test-plan-vectors CLI)
    │   2. config.pipeline.build_test_plan_vectors  (from config.yaml)
    │   3. config.pipeline.rebuild_index            (backward-compat alias)
    │   4. AUTO: FAISS index file absent on disk    (first-run self-heal)
    │
    ├── IF build_flag = True:
    │   KnowledgeBaseBuilder().build(
    │       data_model_docs=data_model_fetched,
    │       spec_docs=spec_fetched,
    │       test_plan_docs=test_plan_fetched,
    │       output_dir=run_dir,             ← write rejected/ignored logs here
    │       max_workers=spec_extractor_workers,  ← parallel spec HTML parsing
    │   )
    │   → KnowledgeBase with kb.vector_chunks  List[VectorChunkRecord]
    │
    │   Spec extraction runs in parallel (ProcessPoolExecutor):
    │     spec_extractor_workers=0 → auto (min(doc_count, cpu_count, 8))
    │     Writes: <run_dir>/spec_extractor_rejected_records.txt  (if any rejected)
    │     Writes: <run_dir>/vector_chunks_ignored_or_rejected.txt (if any TCs empty)
    │
    │   Vector chunk types per TestCaseRecord (up to 4):
    │     full           — title + purpose + prerequisites + procedure + outcomes
    │     intent_summary — TC-ID + title + cluster + intents + entity refs (dense)
    │     procedure      — numbered steps only
    │     setup          — prerequisites + test environment + DUT type
    │
    │   Each chunk carries metadata: tc_id, cluster, intents, entity_refs, chunk_type
    │   (stored in FAISS sidecar JSON for rich decode at search time)
    │
    │   Convert VectorChunkRecord → Document (page_content=text, metadata=...)
    │   → test_plan_chunks  List[Document]
    │
    │   EmbeddingsModule(config.embeddings)
    │       SentenceTransformer("BAAI/bge-large-en-v1.5")
    │       model.encode(texts, batch_size=64, show_progress_bar=True)
    │   store.add_documents(test_plan_chunks, embeddings)
    │   store.save()
    │       data/faiss_index/matter.index    (binary FAISS IndexFlatIP)
    │       data/faiss_index/metadata.json   (chunk metadata sidecar)
    │
    │   state["built_knowledge_base"] = kb  ← cached for build_knowledge_graph_node
    │   (avoids running KnowledgeBaseBuilder.build() twice)
    │
    └── IF build_flag = False:
        store.load()  ← reads matter.index + metadata.json from disk (fast, no GPU)
        test_plan_chunks = []  (not needed on warm run — KG loaded from JSON cache)
```

**Log lines to watch:**
```
[chunk_embed_test_plans_node] KB pipeline produced 47832 rich TC chunks (full/intent_summary/procedure/setup per TC)
[chunk_embed_test_plans_node] Embedding 47832 chunks...
[chunk_embed_test_plans_node] Vector DB saved (47832 vectors)
```
vs warm run:
```
[chunk_embed_test_plans_node] Loading existing vector DB...
[chunk_embed_test_plans_node] Loaded vector DB (47832 vectors)
```

---

### Node 6 — `chunk_pr_node`

This is where the 158 HTML diff sections become 158 PR chunks.

```
pr_documents  (158 FetchedDocuments with doc_type="matter_spec_diff")
    │
    ├── SemanticPRChunker(min_chunk_chars=80, max_chunk_chars=6000)
    │   .chunk_all_with_log(pr_docs, output_dir=run_dir, label="pr_chunks")
    │   Collects rejected segments (< 80 chars) for all docs at once.
    │   Writes: <run_dir>/pr_chunks_ignored_or_rejected.txt  (if any rejected)
    │
    │   Per document, dispatches to:
    │       _from_diff_section(doc)
    │       → exactly 1 SemanticChunk per section (already semantic)
    │       chunk_type = "matter_diff_section"
    │       cluster    = "11.7. Push AV Stream Transport Cluster"
    │       change_types = ["CHANGED", "ADDED", ...]
    │
    │   CASE 2: GitHub unified diff (@@-markers present)
    │       _from_unified_diff(doc)
    │       → N chunks split at @@ boundaries + AsciiDoc headings + table row groups
    │       chunk_type = "diff_hunk"
    │
    │   CASE 3: AsciiDoc with == headings
    │       _from_adoc_sections(doc)
    │       → N chunks split at == headings
    │       chunk_type = "section"
    │
    │   CASE 4: fallback
    │       _from_paragraphs(doc)
    │       → N chunks split at blank lines
    │       chunk_type = "paragraph"
    │
    ├── 158 sections × 1 chunk each = 158 pr_chunks (matter_spec_diff case)
    │
    ├── cluster_filter applied AFTER chunking:
    │   pr_chunks = [c for c in pr_chunks if _chunk_matches_cluster(c, cluster_filter)]
    │   --cluster "Push AV" → keeps only ~15 of 158 chunks
    │
    ├── spec_fetched → spec_chunks (same chunking, doc_type="spec")
    │
    └── Writes: pr_chunks, spec_chunks
```

**Log lines to watch:**
```
[chunk_pr_node] cluster_filter='Push AV': 158 → 15 PR chunks
[chunk_pr_node] PR chunks: 158  spec chunks: 0
```

---

### Node 7 — `extract_pr_changes_node`

```
pr_chunks  (158 Document objects)
    │
    ├── ChangeExtractor(llm_provider=None, confidence_threshold=0.6)  ← rule-based only
    │   for each chunk:
    │       regex scan for [ADDED/REMOVED/CHANGED] annotations
    │       detect cluster, entities (attributes/commands/events/features by name pattern)
    │       classify ChangeKind: ADD_ATTRIBUTE | MODIFY_COMMAND | MODIFY_BEHAVIOR | ...
    │       compute confidence: 0.0–1.0
    │
    ├── IF change.ambiguous AND confidence < 0.6:
    │   ChangeExtractor(llm_provider=get_llm(config.llm))  ← LLM FALLBACK
    │   llm.complete(classification_prompt)
    │   → re-classifies ambiguous changes  ← OCCASIONAL LLM CALL
    │
    ├── Output per chunk (StructuredChange.to_dict()):
    │   {
    │     "change_kind": "MODIFY_ATTRIBUTE",
    │     "cluster": "On/Off",
    │     "entities": [{"type": "attribute", "name": "OnOff", "id": "0x0000"}],
    │     "conditions": ["when device is on"],
    │     "effects": ["attribute value = TRUE"],
    │     "old_value": "...", "new_value": "...",
    │     "confidence": 0.91,
    │     "ambiguous": false,
    │     "pr_chunk_index": 0,
    │     "pr_path": "TC-OO-2.1.adoc"
    │   }
    │
    ├── Per-chunk log (NEW — added for visibility):
    │   chunk 1/158 | 11.7.1.adoc | cluster='Push AV' kind=MODIFY_REQUIREMENT
    │               confidence=0.87 entities=[CMAFIngest] method=rule-based
    │
    └── Writes:
        state["pr_changes"]          List[dict]
        <run_dir>/pr_changes.json    inspection file
```

**Log line to watch:**
```
[extract_pr_changes_node] Extracted 158 change records (3 via LLM fallback)
```

---

### Node 8 — `build_knowledge_graph_node`

```
data_model_fetched, pr_chunks  (+ optional cache: state["built_knowledge_base"])
    │
    ├── build_flag resolution:
    │   1. state["build_knowledge_graph"]
    │   2. config.pipeline.build_knowledge_graph
    │   3. AUTO: data/knowledge_graph/matter_kg.json absent on disk
    │
    ├── IF build_flag = True  (first run or --build-knowledge-graph):
    │   create_knowledge_graph(config.knowledge_graph) → MatterKGBuilder (NetworkX DiGraph)
    │
    │   DOUBLE-BUILD AVOIDANCE:
    │   IF state["built_knowledge_base"] is set:
    │       kb = state["built_knowledge_base"]  ← reuse KB from chunk_embed_test_plans_node
    │       (avoids running KnowledgeBaseBuilder.build() twice when both build flags are True)
    │   ELSE:
    │       kb = KnowledgeBaseBuilder().build(
    │           data_model_docs=data_model_fetched,
    │           spec_docs=[],         (spec_fetched passed via chunk_embed if available)
    │           test_plan_docs=[],    (already processed in node 5)
    │       )
    │
    │   _import_graph_bundle(kg, kb.graph)
    │       ← bridges GraphBundle (new typed KB output) → MatterKGBuilder._graph (NetworkX)
    │       Maps GraphNodeType.name → NodeType enum (e.g. "TEST_CASE" → NodeType.TEST_CASE)
    │       Edge types stored as strings (compatible with load_from_json + FastAPI app)
    │       Only copies nodes/edges where both endpoints exist (guards orphan edges)
    │
    │   kg.export_json(data/knowledge_graph/matter_kg.json)
    │       ← saves all sub-graphs (spec, test-plan, DM schema); excludes transient PR nodes
    │
    │   Debug dumps per source: <run_dir>/kg_debug/{spec,test_plan}/<source_id>.json
    │
    ├── IF build_flag = False (warm run):
    │   kg.load_from_json(data/knowledge_graph/matter_kg.json)
    │   ← restores DiGraph from JSON; fast, no GPU/embedding needed
    │
    ├── ALWAYS (build or load):
    │   kg.add_pr_documents(pr_chunks)
    │   → PR_CHANGE nodes (transient — not saved to JSON)
    │   edges: PR_CHANGE─AFFECTS→ATTRIBUTE, PR_CHANGE─AFFECTS→CLUSTER, ...
    │
    └── [conditional route]:
        IF pr_chunks empty → cleanup_node → END  (--index-only or build-only run)
        IF pr_chunks present → continue to node 9
```

**Log lines to watch:**
```
[build_knowledge_graph_node] No KG file at data/knowledge_graph/matter_kg.json — building.
[build_knowledge_graph_node] Data model sub-graph ingested (4 docs)
[build_knowledge_graph_node] Spec sub-graph ingested (0 chunks)
[build_knowledge_graph_node] Test plan sub-graph ingested (11776 chunks)
[build_knowledge_graph_node] KG saved to data/knowledge_graph/matter_kg.json (2340 nodes, 5821 edges)
```
vs warm run:
```
[build_knowledge_graph_node] Loading existing KG from data/knowledge_graph/matter_kg.json
[build_knowledge_graph_node] KG loaded (2340 nodes, 5821 edges)
```

---

### Node 9 — `search_test_plan_vector_db_node`

```
pr_chunks (158), vector_store (loaded FAISS index)
    │
    ├── EmbeddingsModule(config.embeddings)  ← BGE model loads HERE on warm runs
    │   (lazy load — only triggered on first embed_query() call)
    │   Loading weights: 391/391  ← this is normal; reads from disk cache, no download
    │
    ├── FAISSSearch(store, embedder)
    │
    ├── for each pr_chunk (i of 158):
    │   searcher.search(
    │       chunk.page_content,  ← embed as query (BGE query prefix applied)
    │       k=config.pipeline.search_top_k,       default: 10
    │       threshold=config.pipeline.similarity_threshold  default: 0.65
    │   )
    │   → List[SearchResult]  (up to 10 results, cosine score ≥ 0.65)
    │
    │   Per-chunk log (NEW):
    │   chunk 1/158 | 11.7.1. CMAF Ingestion → 6 hits (top score: 0.742)
    │
    ├── Batches: progress bar here = embedding 158 PR chunks as queries
    │   (small, ~few seconds; NOT re-embedding the full 11776-chunk test plan index)
    │
    └── Writes: search_results  {"pr_0": [SearchResult,...], "pr_157": [...]}
```

**What "top score: 0.742" means:** cosine similarity between the PR diff section text and the
nearest test case chunk in the vector index. Scores below `similarity_threshold` (0.65) are
discarded. A score near 1.0 means the test case is almost identical to the PR change text.

---

### Node 10 — `search_knowledge_graph_node`

```
pr_chunks (158), pr_changes (158 structured records), knowledge_graph
    │
    ├── Build changes_by_idx: { pr_chunk_index → change_record }
    │
    ├── for each pr_chunk (i of 158):
    │   change_rec = changes_by_idx[i]
    │
    │   PATH A — structured change available (usual case):
    │       for each entity in change_rec["entities"][:3]:
    │           kg.search_by_structured_change(cluster, entity_type, entity_name)
    │           → TEST_CASE nodes directly linked to this entity
    │       if no hits: fallback to PATH B
    │
    │   PATH B — regex entity extraction:
    │       kg.search_by_entities(chunk.page_content)
    │       → regex scans for cluster/attribute/command names in chunk text
    │       → TEST_CASE nodes reachable within 2 hops of matched entities
    │
    │   Per-chunk log (NEW):
    │   chunk 1/158 | ... | cluster='Push AV' entities=[CMAFIngest] → 3 KG matches (structured)
    │   chunk 5/158 | ... → 0 KG matches (entity regex)
    │
    └── Writes: graph_results  {"pr_0": [GraphNode,...], ...}
```

---

### Node 11 — `analyze_with_llm_node`

**This is where the bulk of LLM calls happen. 1 call per PR chunk = up to 158 calls.**

```
pr_chunks (158), pr_changes, search_results, graph_results
    │
    ├── get_llm(config.llm) → LoggingLLMProvider(ClaudeSubprocessProvider)
    │
    ├── for each pr_chunk (i of 158):
    │
    │   1. Re-rank vector hits (if config.reranker.enabled = true):
    │      RerankerWeights (9 components):
    │          entity_overlap           0.25  (matched entity names)
    │          cluster_match            0.15  (cluster name overlap)
    │          condition_effect_overlap 0.15  (condition+effect coverage)
    │          intent_match             0.15  (ChangeKind vs TC intent)
    │          kg_direct_bonus          0.20  (KG has direct edge to this TC)
    │          kg_indirect_bonus        0.08  (KG has 2-hop link)
    │          lexical_similarity       0.08  (token overlap)
    │          chunk_type_bonus         0.05  (prefer intent_summary chunks)
    │          retrieval_score          0.04  (raw cosine, tiebreaker)
    │      → ranked List[RankedCandidate]  (re-ordered, trimmed to top_k)
    │
    │   2. Build prompt:
    │      _STRUCTURED_ANALYSIS_SYSTEM  (system prompt: test engineer persona)
    │      _STRUCTURED_ANALYSIS_PROMPT.format(
    │          change_json  = JSON of structured change record
    │          path         = chunk.metadata["path"]
    │          content      = chunk.page_content[:4000]  ← first 4000 chars of diff
    │          test_cases   = _format_ranked_test_cases(ranked)   ← Section A
    │          graph_context = _format_graph_results(graph_hits)  ← Section B
    │      )
    │
    │   3. llm.complete(prompt, system=_STRUCTURED_ANALYSIS_SYSTEM)
    │      ← LoggingLLMProvider wraps this:
    │         ==> call #N | ClaudeSubprocessProvider | system_len=X prompt_len=Y
    │             <first 400 chars of prompt>
    │         <== call #N | Xs | response_len=Z
    │             <first 400 chars of response>
    │
    │   4. _parse_structured_response(response, ...)
    │      extracts JSON from LLM markdown response:
    │      {
    │        "change_summary": "...",
    │        "impacted_entities": [{"type":"attribute","name":"OnOff","cluster":"On/Off"}],
    │        "coverage": {
    │           "direct_tests": ["TC-OO-2.1"],
    │           "indirect_tests": [],
    │           "missing": false
    │        },
    │        "recommendation": {
    │           "action": "update_existing | add_new | none",
    │           "details": "..."
    │        },
    │        "reasoning": "..."
    │      }
    │
    │   Per-chunk log (NEW):
    │   [analyze_with_llm_node] LLM call 1/158 | 11.7.1. CMAF Ingestion |
    │       change=MODIFY_REQUIREMENT entities=[CMAFIngest] | tc_candidates=6 kg_hits=3
    │   [analyze_with_llm_node] call 1/158 → action=update_existing missing=0 updates=1
    │
    └── Writes:
        analysis_results    List[dict]  (one per chunk)
        missing_tests       List[dict]  (chunks where action="add_new")
        update_candidates   List[dict]  (chunks where action="update_existing")
```

**Cost estimate at 158 chunks:**

| Provider | ~Time/call | Total time | Notes |
|---|---|---|---|
| `claude_subprocess` | 8–15s | 20–40 min | Local Claude CLI; depends on model |
| `claude_cli` (API) | 3–8s | 8–21 min | Anthropic API; rate-limited |
| `local` (Ollama) | varies | varies | Depends on hardware |

**To reduce LLM calls:** use `--cluster "Push AV"` to limit chunks to one cluster.

---

### Node 12 — `write_adoc_updates_node`

```
analysis_results, search_results
    │
    ├── create_updater(".adoc") → ADocUpdater
    │
    ├── For each analysis result:
    │   update_candidates → patch existing TC section in source .adoc file
    │   missing_tests     → append new TC section to cluster's update file
    │
    └── Writes: reports/adoc_updates_<YYYYMMDD_HHMMSS>/ (patched .adoc files)
        state["adoc_output_paths"]
```

---

### Node 13 — `generate_report_node`

```
missing_tests, update_candidates, analysis_results, pr_changes, run_dir
    │
    ├── Markdown report: reports/<run_ts>/report.md
    │   Sections: Summary → Missing Tests → Update Candidates → Coverage Analysis
    │
    └── JSON report: reports/<run_ts>/report.json
        { "summary": {...}, "missing_tests": [...], "update_candidates": [...] }
        ▼
[14] cleanup_node → END
```

---

### Node 14 — `cleanup_node`

**Always the last node on both terminal paths** (after `generate_report_node` on the full path,
and after `build_knowledge_graph_node` on build-only paths).

```
state (pass-through — no state modifications)
    │
    ├── GPU memory release:
    │   torch.mps.empty_cache()   (Apple Silicon MPS)
    │   torch.cuda.empty_cache()  (NVIDIA CUDA)
    │   (no-op if torch not installed or no GPU)
    │
    ├── gc.collect()  ← reclaim BGE model memory and large numpy arrays
    │
    ├── Log one-line run summary:
    │   [cleanup_node] Pipeline complete — fatal=False errors=0 pr_chunks=158
    │       kg=19914/42483 missing_TCs=12 update_TCs=34 report=reports/...
    │
    └── If errors recorded: log first 5 errors at WARNING level
```

**Note on LLM context:** All three providers (`ClaudeSubprocessProvider`, `ClaudeProvider`,
`OllamaProvider`) are completely stateless — each call is an independent single-turn request.
There is no accumulated conversation history to clear between runs or between chunks.

---

## 4. LLM Call Analysis

There are **two places** where LLM calls happen:

| Node | When | Frequency | Purpose |
|---|---|---|---|
| `extract_pr_changes_node` | `confidence < 0.6 AND ambiguous` | Occasional (a few per run) | Classify ambiguous change types |
| `analyze_with_llm_node` | **Every** PR chunk | 1 call × N chunks | Full test coverage analysis |

**With `appclusters_diff.html` (158 sections, no `--cluster` filter):**
```
extract_pr_changes: ~3–10 LLM calls  (only ambiguous chunks)
analyze_with_llm:   158 LLM calls    (one per section)
TOTAL:              ~160–170 LLM calls
```

**With `--cluster "Push AV Stream Transport"` (~15 sections):**
```
analyze_with_llm:   ~15 LLM calls
TOTAL:              ~15–20 LLM calls  (~10x faster)
```

**In `llm.log` you will see for every call:**
```
2026-04-12 10:23:01 [INFO] src.llm.llm_provider: ==> call #1 | ClaudeSubprocessProvider | system_len=312 prompt_len=3842
    ## PR Change
    **File**: 11.7.1. CMAF Ingestion
    ...
2026-04-12 10:23:09 [INFO] src.llm.llm_provider: <== call #1 | 8.3s | response_len=612
    ```json
    {"change_summary": "CMAF Ingestion section numbering updated", ...}
```

Full prompt + response for every call is also in `logs/llm_calls.jsonl` (one JSON object per line).

---

## 5. File Artifacts Per Run

Every run creates a timestamped directory: `logs/matter_rag_pipeline_<MMDDYYYY_HHMMSS>/`

| File | Written by | Contents |
|---|---|---|
| `master.log` | logging_config | Every log record from every module |
| `engine.log` | src.engine.* | Node entry/exit, routing decisions |
| `fetcher.log` | src.fetcher.* | Source fetch counts, errors |
| `processor.log` | src.processor.* | Rules applied, sections expanded |
| `loader.log` | src.loader.* | Chunks created per document |
| `embeddings.log` | src.embeddings.* | Model load, encode batch sizes |
| `database.log` | src.database.* | FAISS save/load, vector counts |
| `search.log` | src.search.* | Query scores, result counts |
| `knowledge_graph.log` | src.knowledge_graph.* | Node/edge counts per sub-graph |
| `llm.log` | src.llm.* | `==>` / `<==` with prompt+response previews |
| `config.log` | src.config.* | Config loading details |
| `matter_diff_sections.json` | process_documents_node | All 158 extracted diff sections |
| `data_model_schema.json` | ingest_data_model_node | DM XML cluster schemas |
| `matter_schema.json` | build_matter_schema_node | Entity tables with diff_status |
| `pr_changes.json` | extract_pr_changes_node | Structured change record per chunk |
| `kg_debug/spec/<id>.json` | build_knowledge_graph_node | Per-source KG snapshot (spec) |
| `kg_debug/test_plan/<id>.json` | build_knowledge_graph_node | Per-source KG snapshot (TPs) |
| `spec_extractor_rejected_records.txt` | KnowledgeBaseBuilder (spec_extractor stage) | Sentences filtered as non-normative — reason summary table + per-entry detail |
| `vector_chunks_ignored_or_rejected.txt` | KnowledgeBaseBuilder (vector_chunk_gen stage) | TestCaseRecords with all 4 chunk types empty |
| `pr_chunks_ignored_or_rejected.txt` | chunk_pr_node (semantic_chunker) | PR diff segments < 80 chars that were discarded |

**Persistent artifacts** (survive across runs, reused on warm runs):

| File | Written by | Reused when |
|---|---|---|
| `data/faiss_index/matter.index` | chunk_embed_test_plans_node | `build_test_plan_vectors=False` |
| `data/faiss_index/metadata.json` | chunk_embed_test_plans_node | Always paired with above |
| `data/knowledge_graph/matter_kg.json` | build_knowledge_graph_node | `build_knowledge_graph=False` |
| `logs/llm_calls.jsonl` | LoggingLLMProvider | Appended each run (never overwritten) |

**Report output** (configured via `config.pipeline.output_dir`, default `reports/`):

| File | Contents |
|---|---|
| `reports/<ts>/report.md` | Markdown: missing tests + update candidates |
| `reports/<ts>/report.json` | Structured JSON: same data |
| `reports/adoc_updates_<ts>/*.adoc` | Patched test plan .adoc files |

---

## 6. Logging Structure

### Log levels

| Level | Value | When to use |
|---|---|---|
| `VERBOSE` | 5 | State diffs between every node, all function args |
| `DEBUG` | 10 | Detailed internals (chunk counts, scores, entity names) |
| `INFO` | 20 | **Default** — node enter/exit, LLM calls, key counts |
| `WARNING` | 30 | Skipped steps, fallbacks, recoverable errors |
| `ERROR` | 40 | Failed LLM calls, parse errors, missing files |

Set in `config/config.yaml`:
```yaml
logging:
  level: VERBOSE   # most detail — use INFO for production
```

Or override at CLI:
```bash
python scripts/run_pipeline.py --log-level DEBUG ...
```

### The `@log_node` decorator (all 14 nodes use this)

At `INFO` level:
```
[fetch_documents_node] starting
[fetch_documents_node] done
```

At `VERBOSE` level:
```
[fetch_documents_node] ENTER — state: {config=AppConfig(...), pr_url=str[52]: 'https://github.com/...'}
[fetch_documents_node] EXIT  — updates: {pr_documents=list[1], test_plan_fetched=list[184], ...}
```

### LLM call trace in `llm.log`

```
==> call #1 | ClaudeSubprocessProvider | system_len=312 prompt_len=3842
    ## PR Change
    **File**: 11.7.1. CMAF Ingestion
    [first 400 chars of prompt ...]

<== call #1 | 8.3s | response_len=612
    ```json
    {"change_summary": "...", "recommendation": {"action": "update_existing"} ...}
```

Full call content (every prompt + every response) is in `logs/llm_calls.jsonl`:
```json
{"call_id": 1, "ts": "2026-04-12T10:23:01Z", "provider": "ClaudeSubprocessProvider",
 "prompt_len": 3842, "prompt": "...", "system": "...", "response": "...",
 "duration_s": 8.3, "success": true}
```

---

## 7. Build-Once Control Flags

| Flag | CLI | config.yaml | Default | Effect when True |
|---|---|---|---|---|
| `build_test_plan_vectors` | `--build-test-plan-vectors` | `pipeline.build_test_plan_vectors` | `false` | Re-chunk, embed, save FAISS index |
| `build_knowledge_graph` | `--build-knowledge-graph` | `pipeline.build_knowledge_graph` | `false` | Re-build, save KG JSON |
| `build_data_model` | `--build-data-model` | `pipeline.build_data_model` | `false` | Re-ingest DM XML into KG |

**Auto-build (first-run self-heal):**
- If `data/faiss_index/matter.index` does not exist → `build_test_plan_vectors` auto-enabled
- If `data/knowledge_graph/matter_kg.json` does not exist → `build_knowledge_graph` auto-enabled

**Shortcuts:**
```bash
--index-only    # all 3 build flags = True, no PR required (build the caches)
--compare-only  # all 3 build flags = False (use caches, fast analysis)
```

---

## 8. Operating Modes

### Full run (first time — builds everything)
```
fetch → process → ingest_dm → schema → chunk_embed_tp (BUILD) → chunk_pr
    → extract_changes → build_kg (BUILD) → search_vdb → search_kg
    → analyze_llm (158 calls) → write_adoc → report → cleanup → END
```

### Warm run (caches exist, `--compare-only`)
```
fetch → process → ingest_dm → schema → chunk_embed_tp (LOAD, fast)
    → chunk_pr → extract_changes → build_kg (LOAD, fast)
    → search_vdb → search_kg → analyze_llm (158 calls) → write_adoc → report → cleanup → END
```

### Index-only run (`--index-only`, no PR)
```
fetch → process → ingest_dm → schema → chunk_embed_tp (BUILD)
    → chunk_pr (no pr_docs → pr_chunks=[]) → extract_changes
    → build_kg (BUILD) → [route: no pr_chunks] → cleanup → END
```

### Single-cluster run (fast analysis)
```
python scripts/run_pipeline.py \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV Stream Transport" \
  --compare-only
# ~15 LLM calls instead of 158
```

---

## 9. Debugging Guide

### "ingest_data_model_node: No data_model documents — skipping"

`sources.json` is missing an active data_model entry.
```json
{
  "id": "matter_data_model",
  "type": "matter_xml",
  "role": "data_model",
  "path": "data/data_model"
}
```
Also verify `data/data_model/` contains `.xml` files.

---

### "build_knowledge_graph_node: No KG file — building" every run

`data/knowledge_graph/matter_kg.json` is absent.
Run once with `--build-knowledge-graph` (or `--index-only`) to build and save it.

---

### Re-embedding test plans every run

Check FAISS index exists:
```bash
ls -lh data/faiss_index/matter.index
```
If missing, run: `--build-test-plan-vectors` once. The file should persist across runs.
If it exists but still rebuilds, check `config.pipeline.build_test_plan_vectors` in `config.yaml`.

---

### HuggingFace network request on every run

`config/config.yaml`:
```yaml
embeddings:
  offline: true  # prevents GET https://huggingface.co/api/models/... on every run
```

---

### 158 LLM calls taking too long

Use `--cluster` to limit to one cluster per run:
```bash
python scripts/run_pipeline.py \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "Push AV" \
  --compare-only
```

Or filter by confidence — chunks where `extract_pr_changes_node` returned `action=none`
will still reach `analyze_with_llm_node`. Future optimization: skip LLM call if no vector
hits above threshold AND no KG matches.

---

### "NameError: name 'run_dir' is not defined" in build_knowledge_graph_node

Fixed — `run_dir = state.get("run_dir", "")` was missing from the function's top.

---

### Tracing a specific LLM call

1. Find call number in `engine.log`:
   ```
   [analyze_with_llm_node] LLM call 42/158 | 11.7.42. Some Section | ...
   ```
2. Open `logs/llm_calls.jsonl`, find `"call_id": 42`
3. Or scan `llm.log` for `==> call #42`

---

### Reading the vector search scores

High score (≥ 0.85) = strong semantic match between PR diff and existing test case.
Low score (0.65–0.75) = weak match — test case retrieved but may not be truly related.
No results = no test case in the vector DB covers this change at all (potential gap).

---

*Generated from code reading on 2026-04-12. Update when nodes are added or renamed.*
