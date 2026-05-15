# Matter RAG — FastAPI Debug App

Interactive web application for inspecting the FAISS vector database and NetworkX knowledge
graph produced by the Matter RAG pipeline.

---

## Running

```bash
# From project root — port 9000 (default)
python tests/app/run.py

# Or with uvicorn directly
uvicorn tests.app.main:app --reload --port 9000
```

Open `http://127.0.0.1:9000/` in a browser to see the dashboard.

---

## Endpoints at a Glance

| Method | Path | Returns | Description |
|--------|------|---------|-------------|
| `GET` | `/` | HTML | Dashboard landing page |
| `GET` | `/health` | JSON | Component status (config, vector store, KG) |
| `GET` | `/stats` | JSON | Chunk / KG node breakdowns |
| `POST` | `/query` | JSON | Natural-language search over FAISS + KG |
| `GET` | `/chunks` | JSON | Paginated vector chunks with filters |
| `GET` | `/chunks/{doc_id}` | JSON | Single chunk by doc_id |
| `GET` | `/test-cases` | HTML / JSON | All TEST_CASE KG nodes; filterable + paginated |
| `GET` | `/test-cases/{tc_node_id}` | JSON | Single TC node + KG edges |
| `GET` | `/cluster/{cluster_name}` | HTML / JSON | Cluster summary: schema + requirements + test cases |
| `GET` | `/kg/nodes` | JSON | All KG nodes; filterable by node_type or label |
| `GET` | `/kg/node/{node_id}` | JSON | Single KG node + all neighbours |
| `GET` | `/kg/list` | JSON | Available KG JSON files on disk |
| `GET` | `/kg/graph` | JSON | vis.js-compatible `{nodes, edges}` for the visualizer |
| `GET` | `/kg/viz` | HTML | Interactive force-directed KG visualization |
| `POST` | `/reload` | JSON | Force reload config + vector store + KG from disk |
| `GET` | `/chat` | HTML | React chat UI |
| `POST` | `/api/chat` | JSON | Chat endpoint — LLM reply grounded in FAISS + KG |
| `GET` | `/api/history/{session_id}` | JSON | Message history for a session |
| `DELETE` | `/api/session/{session_id}` | JSON | Delete a session and its history |

---

## Endpoint Details

### `GET /`

Dashboard landing page with an industrial terminal theme.  Shows live counts for:
- KG nodes and edges
- Vector DB chunks
- Test cases
- Load errors

Links to `/kg/viz`, `/test-cases`, `/chat`, and `/stats`.

---

### `GET /health`

Health check for every storage component.  Always returns HTTP 200 so you can see partial state.

```bash
curl http://127.0.0.1:9000/health
```

```json
{
  "status": "ok",
  "components": {
    "config": { "loaded": true },
    "vector_store": {
      "index_file": "data/faiss_index/matter.index",
      "index_exists": true,
      "index_size_mb": 42.1,
      "loaded": true,
      "num_entries": 12480
    },
    "knowledge_graph": {
      "graph_file": "data/knowledge_graph/matter_kg.json",
      "graph_exists": true,
      "loaded": true,
      "num_nodes": 3741,
      "num_edges": 8920
    }
  },
  "errors": []
}
```

---

### `GET /stats`

Detailed statistics about stored data.

```bash
curl http://127.0.0.1:9000/stats
```

Response includes:
- **vector_db** — total entries, breakdown by `source_id`, `doc_type`, `chunk_type`
- **knowledge_graph** — total nodes/edges, breakdown by `node_type`

---

### `POST /query`

Natural-language search over the FAISS vector DB and/or knowledge graph.

```bash
curl -X POST http://127.0.0.1:9000/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "test case for OnOff attribute persistence",
    "top_k": 10,
    "threshold": 0.5,
    "search_vector_db": true,
    "search_kg": true
  }'
```

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | required | Natural-language search query |
| `top_k` | int | `10` | Max results per search source |
| `threshold` | float | `0.5` | Minimum similarity score (0–1) |
| `search_vector_db` | bool | `true` | Include FAISS semantic search |
| `search_kg` | bool | `true` | Include KG entity-based lookup |

**Response:**

```json
{
  "query": "...",
  "vector_results": [
    {
      "doc_id": "test_plans_local::TC-OO-2.1::full",
      "score": 0.823,
      "page_content": "TC-OO-2.1 ...",
      "metadata": { "tc_id": "TC-OO-2.1", "cluster": "On/Off", "chunk_type": "full" }
    }
  ],
  "kg_results": [
    {
      "node_id": "TC::TC-OO-2.1",
      "node_type": "TEST_CASE",
      "label": "TC-OO-2.1",
      "properties": { ... }
    }
  ]
}
```

---

### `GET /chunks`

Browse vector DB chunks with optional filters.  All filters are case-insensitive substring matches.

```bash
# First page of all chunks
curl "http://127.0.0.1:9000/chunks?page=0&size=20"

# Filter by cluster
curl "http://127.0.0.1:9000/chunks?source=on_off&doc_type=test_plan"

# Filter by text content
curl "http://127.0.0.1:9000/chunks?contains=persistence&chunk_type=procedure"
```

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `page` | `0` | Zero-based page index |
| `size` | `20` | Results per page (1–200) |
| `source` | — | Filter by `source_id` or `path` substring |
| `doc_type` | — | Filter by `doc_type` (e.g. `test_plan`, `spec`, `pr_change`) |
| `chunk_type` | — | Filter by chunk type (`full`, `intent_summary`, `procedure`, `setup`) |
| `contains` | — | Text substring filter on `page_content` |

---

### `GET /chunks/{doc_id}`

Retrieve a single chunk by its exact `doc_id`.

```bash
curl "http://127.0.0.1:9000/chunks/test_plans_local::TC-OO-2.1::full"
```

---

### `GET /test-cases`

List all `TEST_CASE` nodes from the knowledge graph.  Returns a styled HTML table by default.
Add `?format=json` for raw JSON.

```bash
# HTML table view
open "http://127.0.0.1:9000/test-cases"

# Filter by cluster
open "http://127.0.0.1:9000/test-cases?cluster=On%2FOff"

# Filter by TC-ID prefix
open "http://127.0.0.1:9000/test-cases?tc_id=TC-OO"

# Raw JSON
curl "http://127.0.0.1:9000/test-cases?format=json&cluster=Door+Lock"
```

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `page` | `0` | Zero-based page index |
| `size` | `50` | Results per page (1–500) |
| `cluster` | — | Filter by cluster name substring (case-insensitive) |
| `tc_id` | — | Filter by TC-ID prefix (e.g. `TC-OO`) |
| `format` | — | Set to `json` to return raw JSON |

**JSON response shape:**

```json
{
  "total": 734,
  "page": 0,
  "size": 50,
  "pages": 15,
  "test_cases": [
    {
      "node_id": "TC::TC-OO-2.1",
      "node_type": "TEST_CASE",
      "label": "TC-OO-2.1",
      "properties": {
        "cluster": "On/Off",
        "intents": ["read_attribute", "verify_persistence"],
        "purpose": "Verify OnOff attribute persistence across power cycles",
        "pics_codes": ["OO.S.A0000"]
      }
    }
  ]
}
```

---

### `GET /test-cases/{tc_node_id}`

Get a single TC node and all its KG edges.

```bash
# Use the label (TC-ID), not the full node_id with prefix
curl "http://127.0.0.1:9000/test-cases/TC-OO-2.1"
```

Returns the node properties and a list of edges (source, target, edge_type).

---

### `GET /cluster/{cluster_name}`

One-page summary for a Matter cluster.  Returns HTML by default; add `?format=json` for raw JSON.

**Sections:**
- **Schema** — ATTRIBUTE / COMMAND / EVENT / FEATURE nodes from DM XML
- **Requirements** — REQUIREMENT / BEHAVIOR_RULE nodes linked to this cluster
- **Test Cases** — TEST_CASE nodes that target this cluster

```bash
# HTML summary page
open "http://127.0.0.1:9000/cluster/On/Off%20Cluster"

# Raw JSON
curl "http://127.0.0.1:9000/cluster/Door%20Lock%20Cluster?format=json"
```

---

### `GET /kg/nodes`

Browse all KG nodes.  Filterable by `node_type` or `label` substring.

```bash
# All TEST_CASE nodes
curl "http://127.0.0.1:9000/kg/nodes?node_type=TEST_CASE&page=0&size=50"

# Label substring search
curl "http://127.0.0.1:9000/kg/nodes?label=OnOff"
```

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `page` | `0` | Zero-based page index |
| `size` | `50` | Results per page (1–500) |
| `node_type` | — | Filter: `CLUSTER`, `ATTRIBUTE`, `COMMAND`, `EVENT`, `FEATURE`, `REQUIREMENT`, `BEHAVIOR_RULE`, `TEST_CASE`, `SECTION`, `PR_CHANGE` |
| `label` | — | Label substring filter (case-insensitive) |

---

### `GET /kg/node/{node_id}`

Get a single KG node and all its neighbours (one hop).

The `{node_id}` parameter is resolved with fallback:
1. Exact match (e.g. `TC-OO-2.1`, `CLUSTER::Low Power Cluster`)
2. Case-insensitive exact match
3. Prefix match (shortest candidate wins)
4. Substring match (shortest candidate wins)

```bash
# TEST_CASE nodes — stored without namespace prefix, use TC-ID directly
curl "http://127.0.0.1:9000/kg/node/TC-OO-2.1"

# CLUSTER nodes — stored as "CLUSTER::<label>", URL-encode special chars
curl "http://127.0.0.1:9000/kg/node/CLUSTER::Low%20Power%20Cluster"

# Substring match works too — pass any unique substring of the label
curl "http://127.0.0.1:9000/kg/node/Low%20Power%20Cluster"
curl "http://127.0.0.1:9000/kg/node/On%2FFF%20Cluster"
```

Returns node properties and a list of `{neighbour_id, neighbour_label, edge_type, direction}` entries.

---

### `GET /kg/list`

List all KG JSON files available on disk (useful to see what sub-graphs exist).

```bash
curl http://127.0.0.1:9000/kg/list
```

---

### `GET /kg/graph`

Returns vis.js-compatible `{"nodes": [...], "edges": [...]}` JSON for the graph visualizer.
Used by `/kg/viz` internally but also useful for building custom visualizations.

```bash
# Default view (top ~150 nodes by degree)
curl "http://127.0.0.1:9000/kg/graph"

# Center on a specific node, 2 hops
curl "http://127.0.0.1:9000/kg/graph?center=TC-OO-2.1&hops=2"

# Filter to TEST_CASE nodes only
curl "http://127.0.0.1:9000/kg/graph?node_type=TEST_CASE&limit=200"

# Filter by cluster
curl "http://127.0.0.1:9000/kg/graph?cluster=on/off&limit=100"

# Use a specific sub-graph source
curl "http://127.0.0.1:9000/kg/graph?source=test_plan"
```

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `source` | `merged` | KG to query: `merged`, `test_plan`, `data_model`, `spec`, or any key in the loaded KG dict |
| `center` | — | Node ID or label substring to center a subgraph around |
| `hops` | `2` | Hops from center node (1–4) |
| `node_type` | — | Filter to a single node type |
| `cluster` | — | Cluster name substring filter (case-insensitive) |
| `limit` | `150` | Max nodes to return (1–2000) |

**vis.js node payload:**

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
  "intents": "read_attribute, verify_persistence"
}
```

---

### `GET /kg/viz`

Interactive force-directed graph visualization powered by vis.js (no build step required).

```
open http://127.0.0.1:9000/kg/viz
```

**Features:**
- Color-coded nodes by NodeType (see color table below)
- Node size proportional to graph degree (clamped 8–30)
- Hover tooltip: label, type, degree, node ID
- Click a node: sidebar shows full properties (ID, type, cluster, tc_id, intents, purpose)
- **Toolbar controls:** Source selector, Center node, Hops, Type filter, Cluster filter, Limit slider, Fit button, Physics ON/OFF toggle
- Physics disabled by default — 200-iteration stabilization pass runs once on load, then freezes

**NodeType colors:**

| NodeType | Color |
|----------|-------|
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

---

### `POST /reload`

Force reload of config, vector store, and KG from disk.  Invalidates all caches.
Useful after rebuilding the pipeline without restarting the server.

```bash
curl -X POST http://127.0.0.1:9000/reload
```

```json
{
  "status": "ok",
  "errors": []
}
```

---

### `GET /chat`

React chat UI (CDN React 18 + Babel standalone, no build step).

```
open http://127.0.0.1:9000/chat
```

Type any Matter protocol question.  The backend runs an LLM query planner to decide the
best KG retrieval strategy (list all test cases, entity coverage, requirement lookup,
cluster dependency traversal, or general keyword search), retrieves context from FAISS
and the KG, then answers using the LLM.

---

### `POST /api/chat`

Programmatic chat endpoint.

```bash
curl -X POST http://127.0.0.1:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "my-session-1",
    "message": "How many test cases cover the On/Off cluster?",
    "include_context": false
  }'
```

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | string | required | User question |
| `session_id` | string | auto-generated | Conversation session ID (pass back for multi-turn) |
| `include_context` | bool | `false` | If true, returns the RAG context string in the response |

**Response:**

```json
{
  "session_id": "my-session-1",
  "reply": "The On/Off Cluster has 7 test cases: TC-OO-2.1 ...",
  "context": null
}
```

The `session_id` is stable — pass it back in subsequent requests to continue the conversation.

**Example multi-turn session:**

```bash
# First turn
RESP=$(curl -s -X POST http://127.0.0.1:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "List all test cases for Door Lock cluster"}')
SID=$(echo $RESP | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# Second turn (same session)
curl -X POST http://127.0.0.1:9000/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SID\", \"message\": \"Which of those test cases cover the LockUnlatch command?\"}"
```

---

### `GET /api/history/{session_id}`

Retrieve the full message history for a session.

```bash
curl "http://127.0.0.1:9000/api/history/my-session-1"
```

```json
{
  "session_id": "my-session-1",
  "messages": [
    { "role": "user", "content": "List all test cases for Door Lock cluster" },
    { "role": "assistant", "content": "The Door Lock cluster has 42 test cases ..." }
  ],
  "system_prompt": "You are a Matter protocol expert..."
}
```

---

### `DELETE /api/session/{session_id}`

Delete a session and its message history.

```bash
curl -X DELETE "http://127.0.0.1:9000/api/session/my-session-1"
```

```json
{ "deleted": true }
```

---

## Chat — How It Works

```
POST /api/chat
    → session_store.get_or_create(session_id)
    → session_store.append_message(sid, "user", message)
    → run_pipeline(payload, _state)
        → _plan_chat_query()          ← 1 LLM call: returns structured query plan
              {intent, cluster, entity_type, entity_name, traverse, keywords}
        → KG dispatcher (by intent):
              "list_test_cases"    → get_test_cases_for_cluster()  (no top-k cap)
              "entity_coverage"    → find_entity_coverage()
              "requirement_lookup" → find_requirements_and_coverage()
              "graph_traversal"    → get_cluster_dependencies()
              "general_qa"         → search_by_keywords()
        → FAISS top-5 semantic search (in parallel)
        → build_prompt_with_history()
        → LLM.complete()              ← answers grounded in KG + FAISS context
    → session_store.append_message(sid, "assistant", reply)
    → ChatResponse(session_id, reply)
```

**Chat intent examples:**

| Question | Intent | KG method called |
|----------|--------|-----------------|
| "How many test cases cover On/Off cluster?" | `list_test_cases` | `get_test_cases_for_cluster()` |
| "List all TC-OO test cases" | `list_test_cases` | `get_test_cases_for_cluster()` |
| "Does the Toggle command have test coverage?" | `entity_coverage` | `find_entity_coverage()` |
| "Is the Breadcrumb attribute tested?" | `entity_coverage` | `find_entity_coverage()` |
| "What are the timing requirements for Door Lock?" | `requirement_lookup` | `find_requirements_and_coverage()` |
| "What clusters depend on On/Off cluster?" | `graph_traversal` | `get_cluster_dependencies(incoming)` |
| "What clusters does Scenes depend on?" | `graph_traversal` | `get_cluster_dependencies(outgoing)` |
| "How does Matter commissioning work?" | `general_qa` | `search_by_keywords()` |

---

## Interactive API Docs

FastAPI auto-generates interactive API docs:

- **Swagger UI:** `http://127.0.0.1:9000/docs`
- **ReDoc:** `http://127.0.0.1:9000/redoc`

---

## File Layout

```
tests/app/
├── main.py                     # FastAPI app, _AppState singleton, all REST endpoints
├── run.py                      # uvicorn launcher (port 9000)
├── README.md                   # this file
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
