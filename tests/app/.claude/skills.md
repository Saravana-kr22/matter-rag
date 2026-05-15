# tests/app — Skills

## Start the debug server

```bash
# From project root
python tests/app/run.py
# → http://127.0.0.1:9000
```

---

## POST /query — semantic search

```bash
curl -s -X POST http://localhost:9000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"On/Off cluster test cases","top_k":5,"threshold":0.5}' | python3 -m json.tool
```

---

## GET /test-cases — browse test case nodes

```bash
# All TCs
curl http://localhost:9000/test-cases

# Filter by cluster
curl "http://localhost:9000/test-cases?cluster=OccupancySensing&size=20"

# Filter by TC-ID prefix
curl "http://localhost:9000/test-cases?tc_id=TC-OO"
```

---

## POST /api/chat — send a chat message

```bash
# New session (server assigns ID)
curl -s -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Which test cases cover the On/Off cluster?"}' | python3 -m json.tool

# Continue existing session
curl -s -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<sid>","message":"What about level control?"}' | python3 -m json.tool
```

---

## GET /api/history — retrieve session history

```bash
curl http://localhost:9000/api/history/<session_id>
```

---

## POST /reload — hot-reload stores after pipeline run

```bash
# After rebuilding FAISS/KG, reload without restarting the server:
curl -X POST http://localhost:9000/reload
```

---

## Python — query stores directly

```python
# In a script with project root on sys.path:
from tests.app.main import _state, _load_stores
_load_stores()

# Search FAISS
from src.embeddings.embeddings import EmbeddingsModule
embedder = EmbeddingsModule(_state.config.embeddings)
vec = embedder.embed_query("OnOff test")
results = _state.vector_store.search_by_vector(vec, k=5, threshold=0.5)

# Search KG
nodes = _state.kg.search_by_entities("On/Off", max_results=10)
```

---

## React chat UI — session persistence

Session ID is stored in `sessionStorage` under the key `mrq_sid`.  The session
survives page refresh but is isolated per browser tab.

```javascript
// In browser devtools — inspect or reset session:
sessionStorage.getItem("mrq_sid")
sessionStorage.removeItem("mrq_sid")   // forces new session on next message
```
