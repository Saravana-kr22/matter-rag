# tests/app — Rules

## Late imports to avoid circular dependencies

`routes/chat.py` imports from `tests.app.main` at module level for `ChatPayload` and
`run_pipeline`.  But `main.py` imports `chat_router` from `routes/chat.py`.

**Rule**: never add a top-level import of `_state` or `_get_embedder` in `chat.py`.
Use a late import inside the handler function:

```python
@router.post("/api/chat")
async def chat(req: ChatRequest):
    from tests.app.main import _state, _get_embedder  # ← late import here
    ...
```

---

## `_get_embedder()` lazy loading

The BGE model is CPU/MPS-heavy (~2 GB).  It must NOT be loaded at startup alongside
the FAISS store — load it on the first actual query.  Always call `_get_embedder()`
before any embedding operations, never access `_state.embedder` directly.

---

## `_ndata_to_dict()` for KG node data

Never use `node.node_type` directly on raw networkx `ndata` dict — the node may be
stored as a `GraphNode` under `ndata["obj"]` or as flat keys depending on the builder
version.  Always use `_ndata_to_dict(node_id, ndata)` which handles both layouts.

---

## `_state` access in routes

All routes access `_state` (the `_AppState` singleton from `main.py`).  Check
`_state.config is None` at the top of any handler that requires config.  Return HTTP
503 if a required component is not loaded — never let a `NoneType` error escape.

---

## `/health` always returns HTTP 200

The `/health` endpoint is used by the dashboard frontend to determine display status.
It must never return a 4xx/5xx even when components are not loaded.  All error
information is encoded inside the JSON response body (`errors`, `components[x].error`).

---

## Vector store entries: use `_entries` for stat scanning

`_state.vector_store._entries` is the raw in-memory list of `VectorEntry` objects.
Access it only for read-only iteration (stats, filters, pagination).  Never mutate it.

---

## KG graph: use `_state.kg._graph` only for traversal

`_state.kg._graph` is the underlying `networkx.DiGraph`.  Use the KG's public API
(`get_all_test_cases`, `search_by_entities`) when possible.  Direct graph traversal
is acceptable only for edge inspection (`out_edges`, `in_edges`, `neighbors`).

---

## HTML templates: no build step

All React UI is served as inline HTML with CDN React 18 + `@babel/standalone` for
JSX transpilation in the browser.  Do not introduce npm, webpack, or any build
artifacts.  Keep the full template in a single `_CHAT_HTML` / `_DASHBOARD_HTML`
constant in the respective Python file.

---

## Thread safety: always use `session_store` methods

The `SessionStore` uses `threading.Lock` internally.  Never access `_sessions` dict
directly.  All reads and writes must go through `store.get_or_create()`,
`store.append_message()`, `store.get_messages()`, etc.
