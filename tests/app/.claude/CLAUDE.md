# tests/app — FastAPI Debug App

## Purpose
Interactive debug web application for inspecting the FAISS vector database and
NetworkX knowledge graph produced by the Matter RAG pipeline. Provides a
React-based chat UI, REST endpoints for browsing chunks and KG nodes, a
health/stats dashboard, and an interactive KG visualization.

## Running

```bash
# From project root
python tests/app/run.py             # port 9000 (default)
uvicorn tests.app.main:app --reload --port 9000
```

---

## File Layout

```
tests/app/
├── main.py                     # FastAPI app, _AppState singleton, all REST endpoints
├── run.py                      # uvicorn launcher
├── __init__.py
├── routes/
│   ├── chat.py                 # /chat UI + /api/chat, /api/history, /api/session
│   └── __init__.py
└── services/
    ├── session_store.py        # In-memory session store (thread-safe)
    ├── history_builder.py      # Build prompt history for LLM
    ├── pipeline_adapter.py     # Bridge: session → RAG → LLM
    └── __init__.py
```

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard landing page (industrial terminal UI) |
| `GET` | `/health` | Component status (config, vector store, KG) — always HTTP 200 |
| `GET` | `/stats` | Chunk breakdowns by source/type; KG node-type distribution |
| `POST` | `/query` | Natural-language search over FAISS + KG |
| `GET` | `/chunks` | Paginate vector chunks; filter by source, doc_type, chunk_type, tc_id, cluster, text. HTML view (default) or JSON (`?format=json`). |
| `GET` | `/chunks/{doc_id}` | Single chunk by doc_id |
| `GET` | `/test-cases` | **HTML table** of all TEST_CASE KG nodes; filter by cluster or tc_id; paginated. Add `?format=json` for raw JSON. |
| `GET` | `/test-cases/{tc_node_id}` | Single TC node + KG edges |
| `GET` | `/kg/nodes` | All KG nodes; filter by node_type or label |
| `GET` | `/kg/node/{node_id}` | Single KG node + all neighbours |
| `GET` | `/kg/graph` | vis.js-compatible `{nodes, edges}` JSON for the visualizer |
| `GET` | `/kg/viz` | Interactive force-directed KG visualization (vis.js, no build step) |
| `POST` | `/reload` | Force reload config, vector store, KG from disk (invalidates caches) |
| `GET` | `/pipeline` | Pipeline DAG visualization (Mermaid.js) with live status from latest `pipeline_progress.json` |
| `GET` | `/chat` | React chat UI (CDN React 18 + Babel standalone, no build step) |
| `POST` | `/api/chat` | Chat endpoint — returns LLM reply grounded in FAISS + KG |
| `GET` | `/api/history/{session_id}` | Message history for a session |
| `DELETE` | `/api/session/{session_id}` | Delete a session + its history |

---

## `GET /test-cases` — HTML table view

By default this endpoint returns a **styled HTML page** matching the app's dark terminal
theme. It includes:

- **Table columns:** TC ID/Label (clickable link → detail view), Cluster (cyan pill),
  Intents, Source file, Purpose preview (first 120 chars)
- **Filter form:** cluster substring + TC-ID prefix inputs, page-size selector,
  Apply / Clear buttons
- **Pagination:** Prev / Next links with current-page indicator
- **Nav bar links:** ← Dashboard, JSON ↗, KG Viz ↗, → CHAT

### Query parameters

| Param | Default | Description |
|---|---|---|
| `page` | `0` | Zero-based page index |
| `size` | `50` | Results per page (1–500) |
| `cluster` | — | Filter by cluster name substring (case-insensitive) |
| `tc_id` | — | Filter by TC-ID prefix (e.g. `TC-OO`) |
| `format` | — | Set to `json` to return raw JSON instead of HTML |

### Raw JSON format (`?format=json`)

```json
{
  "total": 1234,
  "page": 0,
  "size": 50,
  "pages": 25,
  "test_cases": [
    {
      "node_id": "TC::TC-OO-2.1",
      "node_type": "TEST_CASE",
      "label": "TC-OO-2.1",
      "properties": { "cluster": "On/Off", "intents": ["functional"], ... }
    }
  ]
}
```

---

## `_AppState` Singleton (`main.py`)

```python
class _AppState:
    config        = None   # AppConfig loaded from config/config.yaml
    vector_store  = None   # FAISSStore (loaded at startup via store.load())
    kgs           = {}     # Dict[str, NetworkX DiGraph] — keyed by source name
    embedder      = None   # EmbeddingsModule (lazy — first /query or /api/chat call)
    load_errors: List[str] = []
    _kg_nt_cache: Dict[str, Dict[str, str]] = {}    # source → {node_id: node_type_str}
    _kg_degree_sorted: Dict[str, List[str]]  = {}   # source → degree-sorted node_id list
```

`_kg_nt_cache` and `_kg_degree_sorted` are built lazily on first `/kg/graph` call per source
and invalidated on `/reload`. They eliminate repeated `_ndata_to_dict()` calls (O(N) each)
that previously caused slow page loads on large KGs.

Loaded once during FastAPI `lifespan` via `_load_stores()`.
Embedder loaded lazily via `_get_embedder()` (sets `HF_HUB_OFFLINE` if model cache exists).

---

## KG Visualization (`/kg/viz` + `/kg/graph`)

### `GET /kg/graph` — Data endpoint

Returns `{"nodes": [...], "edges": [...], "center": str|null}` for vis.js.

Query parameters:

| Param | Default | Description |
|---|---|---|
| `source` | `merged` | Which KG to query: `merged`, `test_plan`, `data_model`, `spec`, or any key in `_state.kgs` |
| `center` | — | Node ID (or label substring) to center a subgraph around |
| `hops` | `2` | Hops from center (1–4) |
| `node_type` | — | Filter: `CLUSTER`, `ATTRIBUTE`, `COMMAND`, `EVENT`, `FEATURE`, `REQUIREMENT`, `BEHAVIOR_RULE`, `TEST_CASE`, `SECTION`, `PR_CHANGE` |
| `cluster` | — | Case-insensitive cluster name substring filter |
| `limit` | `150` | Max nodes (1–2000) |

**Default view (no filters):** Uses degree-sorted cache; returns top nodes excluding SECTION
document-hub nodes (e.g. `allclusters.html` with degree 8k+). Expands to include direct
neighbors of every CLUSTER node, capped at `limit` additional nodes sorted by degree.

**Center mode:** Returns the N-hop ego-graph (undirected) around the specified node.

**vis node payload** — each node object includes:
```json
{
  "id": "TC::TC-OO-2.1",
  "label": "TC-OO-2.1",
  "color": "#a5d6a7",
  "size": 12,
  "node_type": "TEST_CASE",
  "cluster": "On/Off",
  "tc_id": "TC-OO-2.1",
  "purpose": "Verify OnOff attribute persistence...",
  "intents": "read_attribute, verify_persistence",
  "content": "Verify OnOff attribute persistence..."
}
```

**Sub-graph note:** The `test_plan` sub-graph contains only TEST_CASE and SECTION nodes —
no CLUSTER nodes — so edges to clusters are absent. Use `merged` to see TC→CLUSTER edges.
The viz shows a status hint when `edges=0` and `nodes>0`.

### `GET /kg/viz` — Interactive page

Force-directed graph using vis.js Network (CDN, no build step). Features:

- **Color-coded nodes** by NodeType (see color table below)
- **Node size** proportional to degree (clamped 8–30)
- **Labels** with dark semi-transparent background halo for legibility on all zoom levels
- **DOM-element tooltips** (hover): label, type, degree, node ID, cluster, tc_id
- **Click a node** → sidebar shows: ID, type, degree, cluster (cyan), tc_id, intents, purpose/content
- **Toolbar:** Source selector, Center, Hops, Type filter, Cluster filter, Limit, Fit button, Physics ON/OFF toggle
- **Physics toggle:** Physics is **disabled by default**. On every `load()` call, a brief
  200-iteration stabilization pass runs automatically (spreading nodes to prevent overlap),
  then physics freezes via the `stabilizationIterationsDone` event. The "Physics ON" button
  re-enables live simulation for manual exploration.
- **Layout:** `improvedLayout: true` + `randomSeed: 42` for consistent positioning across reloads

---

## Chat Architecture

### Agentic (MCP tool-use) path — `provider: claude_cli`

When `provider: claude_cli` is configured (`ClaudeProvider`), `POST /api/chat` uses the
agentic tool-use path in `services/mcp_chat.py`.  A single LLM call with tools attached
replaces the old two-step plan + dispatch:

```
POST /api/chat
    → routes/chat.py: detect supports_tool_use(llm) == True
    → services/mcp_chat.run_mcp_chat(user_message, system, history, app_state, run_ctx)
        → ClaudeProvider.complete_with_tools(
              messages, system, tools=KG_TOOLS,
              tool_executor=partial(execute_kg_tool, kg, vs, emb, cfg),
          )
          # Tool-use loop (up to 8 iterations):
          #   model decides which tools to call
          #   execute_kg_tool() dispatches to KG / FAISS methods
          #   results fed back; model continues
          # Loop exits when stop_reason != "tool_use"
    → session_store.append_message(sid, "assistant", reply)
    → ChatResponse(session_id, reply)
```

**Why this is better than the old two-step path:**
- Multi-intent: "list TCs for On/Off AND find gaps for Toggle" → single turn
- Multi-hop: model can call `search_vector_db` then correlate with `find_entity_coverage`
- No planner overhead: the model is the planner — one LLM turn instead of two
- Extensible: add new tools in `kg_tools.py` without touching routing logic

### Classic two-step path — fallback for other providers

When `provider` is `claude_subprocess`, `local`, or `lm_studio`, the `/api/chat` endpoint
falls back to the original graph-based pipeline:

```
POST /api/chat
    → routes/chat.py: supports_tool_use(llm) == False
    → services/pipeline_adapter.run_pipeline(payload, _state, run_ctx)
        → chat_graph (3 nodes):
              search_test_plan_vector_db_node   # FAISS top-5
              search_knowledge_graph_node       # LLM call 1: query planner
              analyze_chunks_with_llm_node      # LLM call 2: response
    → session_store.append_message(sid, "assistant", reply)
    → ChatResponse(session_id, reply)
```

### KG Tools (`services/kg_tools.py`)

Six tools exposed to the LLM in agentic mode:

| Tool | KG method | Use when |
|---|---|---|
| `get_test_cases_for_cluster` | `kg.get_test_cases_for_cluster()` | "list TCs for X", counts |
| `find_entity_coverage` | `kg.find_entity_coverage()` | entity-level coverage check |
| `find_requirements_and_coverage` | `kg.find_requirements_and_coverage()` | spec gaps |
| `get_cluster_dependencies` | `kg.get_cluster_dependencies()` | cross-cluster relationships |
| `search_kg_by_keywords` | `kg.search_by_entities()` | broad KG keyword search |
| `search_vector_db` | `FAISSSearch.search()` | semantic / step-level search |

Tool definitions follow the Anthropic tool-use JSON schema.  `execute_kg_tool()` dispatches
by tool name; failures are caught and returned as informative error strings (the loop continues).

### `ClaudeProvider.complete_with_tools()`

Added to `src/llm/llm_provider.py`.  Runs the Anthropic tool-use loop:
- Takes `messages`, `system`, `tools`, `tool_executor`, `max_iterations=8`
- On `stop_reason == "tool_use"`: calls `tool_executor(name, input)` for each block, appends results, continues
- On `stop_reason != "tool_use"`: returns collected text blocks as the final reply
- Safety: if `max_iterations` reached, strips tools and asks the model to conclude

### Selecting the chat path

`routes/chat.py` probes the configured LLM at request time:
```python
_probe_llm = get_llm(copy.copy(_state.config.llm))
_use_mcp = supports_tool_use(_probe_llm)   # True only for ClaudeProvider
```

`supports_tool_use()` checks for the `complete_with_tools` callable (only `ClaudeProvider`
has it).  This means no config flag is needed — switching `provider: claude_cli` in
`config.yaml` automatically enables the agentic path.

### Chat intent examples (agentic path)

| Question | Tools called |
|---|---|
| "How many test cases cover On/Off?" | `get_test_cases_for_cluster("On/Off")` |
| "Does the Toggle command have coverage?" | `find_entity_coverage("On/Off", "command", "Toggle")` |
| "What timing requirements exist for Door Lock?" | `find_requirements_and_coverage("Door Lock", ["timing"])` |
| "What clusters depend on On/Off cluster?" | `get_cluster_dependencies("On/Off", "incoming_depends_on")` |
| "Show me TCs for AC and coverage gaps for Thermostat" | `get_test_cases_for_cluster("AC")` + `find_requirements_and_coverage("Thermostat")` |

---

## File Layout

```
tests/app/
├── main.py                     # FastAPI app, _AppState singleton, all REST endpoints
├── run.py                      # uvicorn launcher
├── __init__.py
├── routes/
│   ├── chat.py                 # /chat UI + /api/chat, /api/history, /api/session
│   └── __init__.py
└── services/
    ├── session_store.py        # In-memory session store (thread-safe)
    ├── history_builder.py      # Build prompt history for LLM
    ├── pipeline_adapter.py     # Bridge: session → RAG graph (classic two-step path)
    ├── mcp_chat.py             # Agentic tool-use chat (claude_cli provider only)
    ├── kg_tools.py             # KG/FAISS tool definitions + execute_kg_tool()
    └── __init__.py
```

---

| Model | Fields |
|---|---|
| `QueryRequest` | `query: str`, `top_k: int = 10`, `threshold: float = 0.5`, `search_vector_db: bool`, `search_kg: bool` |
| `ChatRequest` | `session_id: Optional[str]`, `message: str`, `include_context: bool = False` |
| `ChatResponse` | `session_id: str`, `reply: str`, `context: Optional[str]` |
| `HistoryResponse` | `session_id: str`, `messages: list`, `system_prompt: str` |
| `ReloadResponse` | `status: str`, `errors: List[str]` |

---

## Key Helper Functions (`main.py`)

| Function | Purpose |
|---|---|
| `_ndata_to_dict(node_id, ndata)` | Convert raw networkx node data to dict — handles both `obj`-wrapped and flat storage |
| `_node_to_dict(node)` | Convert `GraphNode` object to dict |
| `_entry_to_dict(entry)` | Convert vector store entry to dict |
| `_search_result_to_dict(r)` | Convert `SearchResult` to dict |

---

## Session Store (`services/session_store.py`)

Thread-safe in-memory store backed by a `threading.Lock`.

```python
from tests.app.services.session_store import store

session = store.get_or_create(session_id)  # creates if None/missing
store.append_message(sid, "user", "hello")
msgs = store.get_messages(sid)             # List[{"role": ..., "content": ...}]
sysp = store.get_system_prompt(sid)        # MATTER_SYSTEM_PROMPT constant
store.delete(sid)                          # returns True if existed
```

The `MATTER_SYSTEM_PROMPT` constant instructs the LLM to answer as a Matter protocol
expert, ground answers in RAG context, and identify test coverage gaps.

---

## NodeType Colors (KG Visualizer)

| NodeType | Color |
|---|---|
| CLUSTER | `#00bcd4` cyan |
| ATTRIBUTE | `#ce93d8` violet |
| COMMAND | `#ffb74d` amber |
| EVENT | `#ef9a9a` red |
| FEATURE | `#80cbc4` teal |
| REQUIREMENT | `#fff176` yellow |
| BEHAVIOR_RULE | `#ffe082` amber-light |
| TEST_CASE | `#a5d6a7` green |
| SECTION | `#90caf9` blue |
| PR_CHANGE | `#f48fb1` pink |
