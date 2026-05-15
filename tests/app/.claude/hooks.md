# tests/app — Change Hooks

## When the pipeline nodes change

If `src/engine/nodes.py` adds/renames `PipelineState` fields:
- Check `pipeline_adapter.py` — it accesses `app_state.vector_store`, `app_state.kg`,
  `app_state.embedder`, `app_state.config`.  No `PipelineState` fields are used directly.
- Update `main.py:_load_stores()` if the KG or vector store load path changes
  (e.g. new config field name like `graph_store_path`).
- Run `pytest tests/` to verify no regressions.

---

## When adding a new REST endpoint

1. Add the route to `main.py` for app-wide endpoints, or `routes/chat.py` for chat
   session endpoints.
2. Update the **Endpoints** table in `.claude/CLAUDE.md`.
3. If the endpoint requires a new Pydantic model, add it near the existing models
   in the same file.
4. If the endpoint accesses a new component (e.g. a new store), add it to the
   `_AppState` class and load it in `_load_stores()`.
5. Add the new component to the `/health` response.

---

## When changing the session store

`SessionStore` in `services/session_store.py` is a thread-safe in-memory store.
If you change field names on `Session` (`__slots__`):
- Update `get_or_create`, `append_message`, `get_messages`, `get_system_prompt`
  in the same file.
- Check `routes/chat.py` — it uses `store.get_or_create`, `store.append_message`,
  `store.get_messages`, `store.get_system_prompt`, `store.delete`.
- Check `pipeline_adapter.py` — it uses `ChatPayload.chat_history` (comes from
  `store.get_messages()` format: `List[{"role": ..., "content": ...}]`).

---

## When updating the chat UI

The full React app lives in `_CHAT_HTML` in `routes/chat.py` (single constant string).
The dashboard lives in `_DASHBOARD_HTML` in `main.py`.

- Use JetBrains Mono for monospace, Syne for display/headings.
- Keep the industrial terminal aesthetic: `--bg:#05060a`, `--cyan:#00e5ff`, sharp corners.
- No rounded `border-radius` on cards or buttons (use 0 or very small).
- Dot-grid texture via `body::before` radial-gradient (preserve it).
- CDN sources used: Google Fonts, unpkg.com (React 18, Babel standalone).
- Test with `/chat` in browser after changes; check the `/` dashboard separately.

---

## When wiring LangGraph or MCP into the adapter

The adapter in `services/pipeline_adapter.py` has three stubs:
- `_invoke_langgraph(payload, prompt, system)` — fill this for LangGraph
- `_invoke_mcp(payload, prompt, system)` — fill this for MCP orchestrator
- `_invoke_rag_retriever(payload)` — fill this for async retriever pipeline

The active fallback (FAISS + LLM) runs only when all stubs return `None`.
Wire a stub by replacing `return None` with the actual invocation; the
priority order (LangGraph → MCP → direct) is enforced in `run_pipeline()`.

---

## When the LLM provider interface changes

`pipeline_adapter.py` calls:
```python
llm = get_llm(app_state.config.llm)
reply = llm.complete(prompt, system=payload.system_prompt)
```

If `complete()` signature changes in `src/llm/llm_provider.py`, update this call site.
The `system=` kwarg is required — without it the system prompt is silently dropped.

---

## When running the test suite

```bash
# From project root
pytest tests/ -v

# Test only the app module
pytest tests/app/ -v
```

The app does not auto-start a server in tests — unit tests mock `_state`.
