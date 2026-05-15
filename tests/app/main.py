"""Matter RAG — Debug FastAPI application.

Provides a web interface for inspecting the FAISS vector database and
NetworkX knowledge graph built by the pipeline.

Run from the project root::

    uvicorn tests.app.main:app --reload --port 8080

Or use the bundled launcher::

    python tests/app/run.py

Endpoints
---------
GET  /health          — check index files exist + report sizes
GET  /stats           — detailed DB and KG statistics
POST /query           — natural-language search over vector DB + KG
GET  /chunks          — paginate through stored chunks with optional filters
GET  /chunks/{doc_id} — fetch a single chunk by doc_id
GET  /test-cases      — list all TestCase nodes in the knowledge graph
GET  /test-cases/{id} — get a specific TC node + its KG neighbours
GET  /cluster/{name}  — cluster summary: DM schema + requirements + test cases
GET  /kg/nodes        — paginate all KG nodes (filterable by node_type)
GET  /kg/node/{id}    — get a KG node + immediate neighbours
GET  /kg/graph        — vis.js-compatible subgraph JSON (center/hops/type/cluster/limit)
GET  /kg/viz          — interactive force-directed KG visualization (vis.js)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path when running as `python tests/app/run.py`
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Apple Silicon / OpenBLAS thread safety (must be before faiss/numpy imports)
# ---------------------------------------------------------------------------
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Pre-read config to set HF_HUB_OFFLINE before any HuggingFace import.
# Mirrors the same pattern in scripts/run_pipeline.py.
try:
    import yaml as _yaml
    _cfg_path = _PROJECT_ROOT / "config" / "config.yaml"
    _raw = _yaml.safe_load(_cfg_path.read_text())
    if _raw.get("embeddings", {}).get("offline", False):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    del _yaml, _cfg_path, _raw
except Exception:
    pass

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel

# Chat router (imported after sys.path is set)
from tests.app.routes.chat import router as chat_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App-level singletons (loaded once at startup)
# ---------------------------------------------------------------------------

class _AppState:
    config = None
    vector_store = None      # FAISSStore (loaded)
    kg = None                # MatterKGBuilder (loaded from JSON) — merged KG
    kgs: Dict[str, Any] = {}  # per-source KGs: "data_model", "spec", "test_plan" (lazy-loaded)
    embedder = None          # EmbeddingsModule (lazy — loaded on first query)
    load_errors: List[str] = []

    # Per-source KG caches populated lazily by get_kg_graph() — invalidated on reload.
    # _kg_nt_cache:      source_key → {node_id: node_type_upper}
    # _kg_degree_sorted: source_key → [node_id, ...] sorted by degree desc, SECTION excluded
    _kg_nt_cache: Dict[str, Dict[str, str]] = {}
    _kg_degree_sorted: Dict[str, List[str]] = {}


_state = _AppState()


# ---------------------------------------------------------------------------
# Lifespan: load config + stores at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _install_run_aware_handler()
    _load_stores()
    yield


def _install_run_aware_handler() -> None:
    """Attach RunAwareFileHandler to the root logger (idempotent).

    The handler silently drops records when no RunContext is active (e.g. startup
    log lines).  When a chat request is in flight, it routes every src.* and
    tests.app.* log record into the per-request run directory.
    """
    from src.engine.run_context import RunAwareFileHandler
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RunAwareFileHandler):
            return  # already installed
    handler = RunAwareFileHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # src.llm, src.database, src.search, src.embeddings must be at DEBUG so their
    # per-result details reach the per-run log files (llm.log, database.log,
    # vector_db_search.log, kg_search.log, embeddings.log).
    # The root console handler stays at INFO so debug records never appear in the terminal.
    for _debug_mod in ("src.llm", "src.database", "src.search", "src.search.vector", "src.search.kg", "src.embeddings"):
        logging.getLogger(_debug_mod).setLevel(logging.DEBUG)
    logger.info("RunAwareFileHandler installed")


def _load_stores() -> None:
    """Load config, FAISS store and KG from disk.  Errors are stored, not raised."""
    _state.load_errors = []
    # Invalidate per-source KG caches so get_kg_graph() rebuilds them after reload
    _state.kgs = {}
    _state._kg_nt_cache = {}
    _state._kg_degree_sorted = {}

    try:
        from src.config.config_loader import load_config
        config_path = _PROJECT_ROOT / "config" / "config.yaml"
        _state.config = load_config(str(config_path))
        logger.info("Config loaded from %s", config_path)
    except Exception as exc:
        _state.load_errors.append(f"Config load failed: {exc}")
        logger.error("Config load failed: %s", exc)
        return

    # ---- Vector store ----
    try:
        from src.database.vector_store import create_vector_store
        store = create_vector_store(_state.config.database)
        store.load()
        _state.vector_store = store
        logger.info("Vector store loaded: %d entries", store.size)
    except Exception as exc:
        _state.load_errors.append(f"Vector store load failed: {exc}")
        logger.warning("Vector store load failed: %s", exc)

    # ---- Knowledge graph ----
    try:
        from src.knowledge_graph.graph_factory import create_knowledge_graph
        kg = create_knowledge_graph(_state.config.knowledge_graph)
        kg_path = getattr(_state.config.knowledge_graph, "graph_store_path",
                          "data/knowledge_graph/matter_kg.json")
        kg_dir = (_PROJECT_ROOT / kg_path).parent
        _candidate_components = [
            kg_dir / f"{_src}_kg.json"
            for _src in ("data_model", "spec", "test_plan", "spec_llm")
        ]
        _available_components = [p for p in _candidate_components if p.exists()]
        if _available_components:
            logger.info(
                "Loading KG from %d component file(s): %s",
                len(_available_components),
                ", ".join(p.name for p in _available_components),
            )
            kg.load_from_components([str(p) for p in _available_components])
        else:
            kg.load_from_json(str(_PROJECT_ROOT / kg_path))
        _state.kg = kg
        logger.info("KG loaded: %d nodes, %d edges", kg.num_nodes, kg.num_edges)
    except Exception as exc:
        _state.load_errors.append(f"KG load failed: {exc}")
        logger.warning("KG load failed: %s", exc)


def _get_embedder():
    """Lazy-load the embeddings model on first call.

    Forces offline mode when the model files are already cached locally — avoids
    proxy / firewall errors from HuggingFace update checks.
    """
    if _state.embedder is None and _state.config is not None:
        from pathlib import Path as _Path
        import huggingface_hub as _hf_hub

        # Use cached model if available — don't trigger a network round-trip
        cache_dir = _Path(_state.config.embeddings.cache_dir).expanduser().resolve()
        model_name = _state.config.embeddings.model
        # Check for any cached snapshot directory for this model
        slug = model_name.replace("/", "--")
        model_cache = cache_dir / f"models--{slug}"
        if model_cache.exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")

        from src.embeddings.embeddings import EmbeddingsModule
        _state.embedder = EmbeddingsModule(_state.config.embeddings)
        logger.info("Embeddings model loaded")
    return _state.embedder


def _get_source_kg(source: str):
    """Lazy-load a per-source sub-graph KG, caching it in ``_state.kgs``.

    ``source`` must be one of ``"data_model"``, ``"spec"``, ``"test_plan"``,
    or ``"merged"`` (returns the main ``_state.kg``).

    Returns the loaded ``MatterKGBuilder`` or raises ``HTTPException(503)``
    if the file is missing or can't be loaded.
    """
    if source == "merged" or not source:
        if _state.kg is None:
            raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")
        return _state.kg

    if source in _state.kgs:
        return _state.kgs[source]

    if _state.config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")

    kg_path_base = getattr(_state.config.knowledge_graph, "graph_store_path",
                           "data/knowledge_graph/matter_kg.json")
    sub_path = _PROJECT_ROOT / Path(kg_path_base).parent / f"{source}_kg.json"

    if not sub_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Sub-graph file not found: {sub_path.name}. "
                   "Re-run the pipeline with --build-knowledge-graph to generate it.",
        )

    from src.knowledge_graph.graph_factory import create_knowledge_graph
    kg = create_knowledge_graph(_state.config.knowledge_graph)
    try:
        kg.load_from_json(str(sub_path))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load {sub_path.name}: {exc}")

    _state.kgs[source] = kg
    logger.info("Sub-graph '%s' loaded: %d nodes, %d edges", source, kg.num_nodes, kg.num_edges)
    return kg


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Matter RAG — Debug API",
    description=(
        "Inspect the FAISS vector database and knowledge graph built by the "
        "Matter RAG pipeline.  Use /query to test retrieval, /chunks to browse "
        "stored documents, /test-cases to inspect TC coverage, and /chat to open "
        "the interactive Matter expert chat UI."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(chat_router)


# ---------------------------------------------------------------------------
# Access log — writes every request/response line to logs/app_access.log
# ---------------------------------------------------------------------------

def _setup_access_log() -> logging.Logger:
    """Create a dedicated file logger for HTTP access records."""
    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app_access.log"

    _logger = logging.getLogger("matter_rag.access")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False  # don't echo to root logger

    if not _logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        _logger.addHandler(handler)

    return _logger


_access_log = _setup_access_log()


class _AccessLogMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, and duration for every HTTP request."""

    async def dispatch(self, request: Request, call_next):
        t0 = time.monotonic()
        # Log incoming request (before processing)
        client = request.client.host if request.client else "-"
        _access_log.info(
            "→ %s %s  client=%s",
            request.method, request.url.path, client,
        )
        try:
            response = await call_next(request)
            duration_ms = (time.monotonic() - t0) * 1000
            _access_log.info(
                "← %s %s  status=%d  %.0fms",
                request.method, request.url.path, response.status_code, duration_ms,
            )
            return response
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            _access_log.error(
                "✗ %s %s  error=%s  %.0fms",
                request.method, request.url.path, exc, duration_ms,
            )
            raise


app.add_middleware(_AccessLogMiddleware)


# ---------------------------------------------------------------------------
# GET /  — dashboard landing page
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Matter RAG // Debug Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:     #05060a;
      --surf:   #0c0e14;
      --card:   #0f1119;
      --border: #1b1e2a;
      --bh:     #2a2e3e;
      --text:   #cdd2e8;
      --muted:  #50566e;
      --cyan:   #00e5ff;
      --cdim:   rgba(0,229,255,.10);
      --orange: #ff6830;
      --green:  #2de08a;
      --amber:  #f5a623;
      --violet: #a78bfa;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { font-size: 14px; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'JetBrains Mono', monospace;
      min-height: 100vh;
      overflow-x: hidden;
    }
    /* dot-grid texture */
    body::before {
      content: ''; position: fixed; inset: 0; z-index: 0; pointer-events: none;
      background-image: radial-gradient(circle, #1a1d28 1px, transparent 1px);
      background-size: 26px 26px; opacity: .35;
    }
    /* top accent line */
    body::after {
      content: ''; position: fixed; top: 0; left: 0; right: 0; height: 1px; z-index: 200;
      background: linear-gradient(90deg, transparent 0%, var(--cyan) 50%, transparent 100%);
    }
    .z1 { position: relative; z-index: 1; }

    /* NAV */
    nav {
      position: sticky; top: 0; z-index: 50;
      border-bottom: 1px solid var(--border);
      background: rgba(5,6,10,.92); backdrop-filter: blur(14px);
      height: 52px; padding: 0 2rem;
      display: flex; align-items: center; gap: 1.25rem;
    }
    .logo-hex {
      width: 28px; height: 28px; flex-shrink: 0;
      clip-path: polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);
      background: var(--cdim); border: 1px solid var(--cyan);
      display: flex; align-items: center; justify-content: center;
      font-family: 'Syne', sans-serif; font-size: 11px; font-weight: 800;
      color: var(--cyan); letter-spacing: 0;
    }
    .nav-name { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 13px;
      letter-spacing: .08em; text-transform: uppercase; color: #fff; }
    .nav-slash { color: var(--muted); }
    .nav-sub { font-size: 11px; color: var(--muted); }
    .nav-r { margin-left: auto; display: flex; align-items: center; gap: 1.25rem; }
    .pill {
      font-size: 10px; padding: 3px 10px; border: 1px solid var(--border);
      color: var(--muted); letter-spacing: .06em; transition: all .3s;
    }
    .pill.ok     { border-color: rgba(45,224,138,.35); color: var(--green); }
    .pill.deg    { border-color: rgba(245,166,35,.35);  color: var(--amber); }
    .pill.off    { border-color: rgba(255,104,48,.35);  color: var(--orange); }
    .nav-a { font-size: 11px; color: var(--muted); text-decoration: none;
      letter-spacing: .05em; transition: color .2s; }
    .nav-a:hover { color: var(--cyan); }
    .nav-a.hi { color: var(--cyan); }

    /* HEADER */
    header { max-width: 1200px; margin: 0 auto; padding: 3.5rem 2rem 2.5rem; }
    .eyebrow {
      font-size: 10px; letter-spacing: .2em; text-transform: uppercase;
      color: var(--cyan); margin-bottom: .75rem;
      display: flex; align-items: center; gap: 8px;
    }
    .eyebrow::before { content:''; display:block; width:20px; height:1px; background:var(--cyan); }
    h1 {
      font-family: 'Syne', sans-serif; font-weight: 800;
      font-size: clamp(1.9rem, 4vw, 3rem);
      color: #fff; letter-spacing: -.025em; line-height: 1.08; margin-bottom: .75rem;
    }
    h1 em { font-style: normal; color: var(--cyan); }
    .sub { color: var(--muted); font-size: 12px; line-height: 1.85; max-width: 540px; }

    /* STATS */
    .stats {
      max-width: 1200px; margin: 0 auto 3rem; padding: 0 2rem;
      display: grid; grid-template-columns: repeat(4,1fr);
      gap: 1px; background: var(--border); border: 1px solid var(--border);
    }
    @media(max-width:600px){ .stats { grid-template-columns: repeat(2,1fr); } }
    .stat {
      background: var(--card); padding: 1.5rem 1.75rem; position: relative; overflow: hidden;
      transition: background .2s;
    }
    .stat:hover { background: #121420; }
    .stat::before {
      content:''; position:absolute; top:0; left:0; right:0; height:2px;
      opacity:0; transition:opacity .3s;
    }
    .stat:nth-child(1)::before { background: var(--cyan); }
    .stat:nth-child(2)::before { background: var(--violet); }
    .stat:nth-child(3)::before { background: var(--green); }
    .stat:nth-child(4)::before { background: var(--amber); }
    .stat:hover::before { opacity: 1; }
    .stat-lbl { font-size:10px; letter-spacing:.15em; text-transform:uppercase;
      color:var(--muted); margin-bottom:.5rem; }
    .stat-val {
      font-family:'Syne',sans-serif; font-weight:700;
      font-size:2.1rem; letter-spacing:-.025em; line-height:1;
    }
    .stat:nth-child(1) .stat-val { color:var(--cyan); }
    .stat:nth-child(2) .stat-val { color:var(--violet); }
    .stat:nth-child(3) .stat-val { color:var(--green); }
    .stat:nth-child(4) .stat-val { color:var(--amber); }
    .stat-unit { font-size:10px; color:var(--muted); margin-top:5px; }

    /* MODULES */
    .mods-wrap { max-width:1200px; margin:0 auto 2.5rem; padding:0 2rem; }
    .sec-lbl {
      font-size:10px; letter-spacing:.2em; text-transform:uppercase; color:var(--muted);
      padding-bottom:.85rem; border-bottom:1px solid var(--border); margin-bottom:1.5rem;
      display:flex; justify-content:space-between; align-items:center;
    }
    .mods {
      display:grid; grid-template-columns:repeat(3,1fr);
      gap:1px; background:var(--border); border:1px solid var(--border);
    }
    @media(max-width:700px){ .mods { grid-template-columns:1fr; } }
    @media(min-width:501px) and (max-width:700px){ .mods { grid-template-columns:repeat(2,1fr); } }
    .mod {
      background:var(--card); padding:1.75rem; text-decoration:none;
      display:flex; flex-direction:column; gap:.9rem; position:relative; overflow:hidden;
      transition:background .2s;
    }
    .mod:hover { background:#13151f; }
    .mod.feat { background:#0a0d18; }
    .mod.feat:hover { background:#0d1020; }
    /* bottom glow line on hover */
    .mod::after {
      content:''; position:absolute; bottom:0; left:0; right:0; height:1px;
      background:linear-gradient(90deg,transparent,var(--cyan),transparent);
      opacity:0; transition:opacity .3s;
    }
    .mod:hover::after { opacity:.5; }
    .mod-num { font-size:10px; color:var(--muted); letter-spacing:.08em; }
    .mod-icon {
      width:34px; height:34px; border:1px solid var(--border);
      display:flex; align-items:center; justify-content:center;
      transition:border-color .2s, background .2s;
    }
    .mod:hover .mod-icon, .mod.feat .mod-icon {
      border-color:rgba(0,229,255,.4); background:var(--cdim);
    }
    .mod-icon svg { width:15px; height:15px; color:var(--muted); transition:color .2s; }
    .mod:hover .mod-icon svg, .mod.feat .mod-icon svg { color:var(--cyan); }
    .mod-title {
      font-family:'Syne',sans-serif; font-weight:700; font-size:13px;
      color:#e4e6f4; margin-bottom:3px;
    }
    .mod.feat .mod-title { color:#fff; }
    .mod-desc { font-size:11px; color:var(--muted); line-height:1.75; }
    .mod-ep {
      font-size:10px; color:var(--muted); letter-spacing:.04em;
      margin-top:auto; display:flex; justify-content:space-between; align-items:center;
    }
    .mod.feat .mod-ep { color:var(--cyan); }
    .mod-arr { opacity:0; transition:opacity .2s,transform .2s; color:var(--cyan); font-size:13px; }
    .mod:hover .mod-arr { opacity:1; transform:translateX(3px); }

    /* TERMINAL HEALTH */
    .health-wrap { max-width:1200px; margin:0 auto 5rem; padding:0 2rem; }
    .term-box { border:1px solid var(--border); background:var(--surf); }
    .term-hdr {
      padding:.55rem 1rem; border-bottom:1px solid var(--border);
      display:flex; align-items:center; gap:8px;
    }
    .term-dots { display:flex; gap:5px; }
    .term-dots span { width:9px; height:9px; border-radius:50%; }
    .term-dots span:nth-child(1){ background:#ff5f57; }
    .term-dots span:nth-child(2){ background:#febc2e; }
    .term-dots span:nth-child(3){ background:#28c840; }
    .term-ttl { font-size:10px; color:var(--muted); margin-left:4px; letter-spacing:.05em; }
    .term-body { padding:1.25rem 1.5rem; min-height:110px; }
    .tl { font-size:11px; line-height:2.1; display:flex; align-items:baseline; gap:10px; }
    .tp { color:var(--cyan); user-select:none; }
    .tk { color:#dde0ee; min-width:120px; }
    .tok { color:var(--green); }
    .twn { color:var(--amber); }
    .ter { color:var(--orange); }
    .tdim { color:var(--muted); }
    .terr-block {
      margin-top:.6rem; padding:.55rem 1rem;
      border-left:2px solid var(--orange); font-size:11px;
      color:var(--orange); background:rgba(255,104,48,.06);
    }

    /* ANIMATIONS */
    @keyframes fsup { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
    .anim { opacity:0; animation: fsup .45s ease forwards; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
    .cur { animation: blink 1.1s step-end infinite; color:var(--cyan); }
  </style>
</head>
<body><div class="z1">

<nav>
  <div class="logo-hex">M</div>
  <span class="nav-name">Matter RAG</span>
  <span class="nav-slash">/</span>
  <span class="nav-sub">debug console</span>
  <div class="nav-r">
    <span id="hbadge" class="pill">● init</span>
    <a href="/docs" target="_blank" class="nav-a">API_DOCS&nbsp;↗</a>
    <a href="/chat" class="nav-a hi">→ CHAT</a>
  </div>
</nav>

<header class="anim" style="animation-delay:.05s">
  <div class="eyebrow">pipeline.debug</div>
  <h1>RAG <em>Debug</em><br>Console</h1>
  <p class="sub">Inspect vector embeddings, traverse the knowledge graph,<br>
  query test case coverage, and run the Matter expert chat.</p>
</header>

<section class="stats anim" style="animation-delay:.12s">
  <div class="stat">
    <div class="stat-lbl">vector chunks</div>
    <div id="sc" class="stat-val">—</div>
    <div class="stat-unit">faiss entries</div>
  </div>
  <div class="stat">
    <div class="stat-lbl">KG nodes</div>
    <div id="sn" class="stat-val">—</div>
    <div class="stat-unit">networkx nodes</div>
  </div>
  <div class="stat">
    <div class="stat-lbl">KG edges</div>
    <div id="se" class="stat-val">—</div>
    <div class="stat-unit">directed edges</div>
  </div>
  <div class="stat">
    <div class="stat-lbl">test cases</div>
    <div id="st" class="stat-val">—</div>
    <div class="stat-unit">tc nodes</div>
  </div>
</section>

<section class="mods-wrap anim" style="animation-delay:.2s">
  <div class="sec-lbl"><span>system.modules</span><span id="clk" class="cur">_</span></div>
  <div class="mods">

    <a href="/chat" class="mod feat">
      <span class="mod-num">01 // primary</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
            d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-3 3v-3z"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">RAG Chat</div>
        <div class="mod-desc">Interactive Matter expert. Queries are grounded via FAISS similarity search and knowledge graph entity retrieval in real-time.</div>
      </div>
      <div class="mod-ep"><span>GET /chat</span><span class="mod-arr">→</span></div>
    </a>

    <a href="/docs#/default/query_query_post" class="mod">
      <span class="mod-num">02</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-4.35-4.35M17 11A6 6 0 115 11a6 6 0 0112 0z"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">Semantic Query</div>
        <div class="mod-desc">POST natural-language questions to search FAISS and KG simultaneously. Returns ranked hits with similarity scores.</div>
      </div>
      <div class="mod-ep"><span>POST /query</span><span class="mod-arr">→</span></div>
    </a>

    <a href="/test-cases" class="mod">
      <span class="mod-num">03</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
            d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 12l2 2 4-4"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">Test Cases</div>
        <div class="mod-desc">List all TEST_CASE nodes from the knowledge graph. Filter by cluster name or TC-ID prefix.</div>
      </div>
      <div class="mod-ep"><span>GET /test-cases</span><span class="mod-arr">→</span></div>
    </a>

    <a href="/chunks" class="mod">
      <span class="mod-num">04</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h8"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">Vector Chunks</div>
        <div class="mod-desc">Browse raw document chunks in FAISS. Filter by source, doc_type, chunk_type, or content substring.</div>
      </div>
      <div class="mod-ep"><span>GET /chunks</span><span class="mod-arr">→</span></div>
    </a>

    <a href="/kg/nodes" class="mod">
      <span class="mod-num">05</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/>
          <path stroke-linecap="round" d="M12 7v4M12 11l-5 6M12 11l5 6"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">Knowledge Graph</div>
        <div class="mod-desc">Explore KG nodes — clusters, spec sections, test cases, entities. Filter by type or label substring.</div>
      </div>
      <div class="mod-ep"><span>GET /kg/nodes</span><span class="mod-arr">→</span></div>
    </a>

    <a href="/stats" class="mod">
      <span class="mod-num">06</span>
      <div class="mod-icon">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
            d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
        </svg>
      </div>
      <div>
        <div class="mod-title">Statistics</div>
        <div class="mod-desc">Node-type distributions, chunk breakdowns by source and doc-type, metadata key inventory.</div>
      </div>
      <div class="mod-ep"><span>GET /stats</span><span class="mod-arr">→</span></div>
    </a>

  </div>
</section>

<section class="health-wrap anim" style="animation-delay:.3s">
  <div class="sec-lbl"><span>system.health</span></div>
  <div class="term-box">
    <div class="term-hdr">
      <div class="term-dots"><span></span><span></span><span></span></div>
      <span class="term-ttl">health_check — matter-rag-debug-api</span>
    </div>
    <div class="term-body" id="hbody">
      <div class="tl"><span class="tp">$</span><span class="tdim">fetching component status…</span></div>
    </div>
  </div>
</section>

</div><!-- z1 -->
<script>
(function(){
  const clk = document.getElementById('clk');
  const tick = () => { clk.textContent = new Date().toTimeString().slice(0,8); };
  tick(); setInterval(tick, 1000);
})();

function animNum(el, target) {
  const t = parseInt(target, 10);
  if (isNaN(t)) return;
  const steps = 28, dur = 700;
  let s = 0;
  const iv = setInterval(() => {
    s++;
    el.textContent = Math.round(t * s / steps).toLocaleString();
    if (s >= steps) { el.textContent = t.toLocaleString(); clearInterval(iv); }
  }, dur / steps);
}

async function boot() {
  try {
    const h = await fetch('/health').then(r => r.json());
    const b = document.getElementById('hbadge');
    if (h.status === 'ok')     { b.textContent='● operational'; b.className='pill ok'; }
    else                        { b.textContent='● degraded';    b.className='pill deg'; }

    let html = '';
    Object.entries(h.components || {}).forEach(([k, v]) => {
      const ok  = v.loaded !== false && !v.error;
      const cls = ok ? 'tok' : 'ter';
      const st  = ok ? 'ONLINE' : 'OFFLINE';
      const det = v.num_entries != null
        ? `<span class="tdim">(${v.num_entries.toLocaleString()} entries)</span>`
        : v.num_nodes != null
        ? `<span class="tdim">(${v.num_nodes.toLocaleString()} nodes / ${(v.num_edges||0).toLocaleString()} edges)</span>`
        : v.error
        ? `<span class="ter">${v.error.slice(0,90)}</span>` : '';
      html += `<div class="tl"><span class="tp">$</span><span class="tk">${k}</span><span class="${cls}">${st}</span>&nbsp;${det}</div>`;
    });
    if (h.errors?.length)
      html += h.errors.map(e => `<div class="terr-block">ERR: ${e}</div>`).join('');
    document.getElementById('hbody').innerHTML = html ||
      '<div class="tl"><span class="tp">$</span><span class="tdim">no components</span></div>';
  } catch(e) {
    const b = document.getElementById('hbadge');
    b.textContent = '● offline'; b.className = 'pill off';
    document.getElementById('hbody').innerHTML = '<div class="terr-block">Cannot reach API server</div>';
  }

  try {
    const s = await fetch('/stats').then(r => r.json());
    const vs = s.vector_store, kg = s.knowledge_graph;
    if (vs?.total_entries != null) animNum(document.getElementById('sc'), vs.total_entries);
    if (kg?.total_nodes  != null) animNum(document.getElementById('sn'), kg.total_nodes);
    if (kg?.total_edges  != null) animNum(document.getElementById('se'), kg.total_edges);
    const tc = kg?.by_node_type?.TEST_CASE || kg?.by_node_type?.TestCase || kg?.by_node_type?.test_case;
    if (tc) animNum(document.getElementById('st'), tc);
    else fetch('/test-cases?size=1&format=json').then(r=>r.json()).then(d=>{
      if (d.total!=null) animNum(document.getElementById('st'), d.total);
    }).catch(()=>{});
  } catch(e) {}
}
boot();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    """Serve the Matter RAG debug console landing page."""
    return HTMLResponse(content=_DASHBOARD_HTML)


@app.get("/pipeline", response_class=HTMLResponse, summary="Pipeline DAG visualization")
def pipeline_viz():
    """Render the 18-node pipeline DAG using Mermaid.js with live status from the latest run."""
    import glob as _glob
    import json as _json

    # Try to load latest pipeline_progress.json for status coloring
    progress_nodes: dict = {}
    progress_files = sorted(_glob.glob("logs/ghpr_analysis_*/pipeline_progress.json"))
    if progress_files:
        try:
            progress = _json.loads(Path(progress_files[-1]).read_text())
            for entry in progress.get("completed_nodes", []):
                progress_nodes[entry.get("node", "")] = entry.get("status", "done")
        except Exception:
            pass

    def _cls(node_name: str) -> str:
        if node_name in progress_nodes:
            return ":::done"
        return ""

    mermaid = f"""
graph TD
    A[fetch_documents]{_cls("fetch_documents")} --> B[process_documents]{_cls("process_documents")}
    B --> C[ingest_data_model]{_cls("ingest_data_model")}
    C --> D[build_matter_schema]{_cls("build_matter_schema")}
    D --> E[chunk_embed_test_plans]{_cls("chunk_embed_test_plans")}
    E --> F[chunk_pr]{_cls("chunk_pr")}
    F --> G[extract_pr_changes]{_cls("extract_pr_changes")}
    G --> H[build_knowledge_graph]{_cls("build_knowledge_graph")}
    H -->|pr_chunks present| I[search_test_plan_vector_db]{_cls("search_test_plan_vector_db")}
    H -->|no PR given| R[cleanup]{_cls("cleanup")}
    H -->|PR but 0 chunks| Q[generate_report]{_cls("generate_report")}
    I --> J[search_knowledge_graph]{_cls("search_knowledge_graph")}
    J --> K[analyze_chunks_with_llm<br>Pass 1]{_cls("analyze_chunks_with_llm")}
    K --> L[cluster_review<br>Pass 2]{_cls("cluster_review")}
    L --> M[second_pass_tc_gen<br>Pass 3]{_cls("second_pass_tc_gen")}
    M --> N[human_outline_expand<br>Pass 4]{_cls("human_outline_expand")}
    N --> O[write_adoc_updates]{_cls("write_adoc_updates")}
    O --> P[write_updated_testplan]{_cls("write_updated_testplan")}
    P --> Q
    Q --> R
    R --> S((END))

    classDef done fill:#2e7d32,stroke:#4caf50,color:#fff
    classDef default fill:#263238,stroke:#546e7a,color:#e0e0e0

    style A fill:#00695c,stroke:#4db6ac,color:#fff
    style H fill:#4a148c,stroke:#ce93d8,color:#fff
    style K fill:#e65100,stroke:#ff9800,color:#fff
    style L fill:#e65100,stroke:#ff9800,color:#fff
    style M fill:#e65100,stroke:#ff9800,color:#fff
    style Q fill:#1565c0,stroke:#64b5f6,color:#fff
    style R fill:#37474f,stroke:#78909c,color:#fff
    style S fill:#1b5e20,stroke:#66bb6a,color:#fff
"""

    node_descriptions = {
        "fetch_documents": "Load sources from sources.json + --input-doc; route by role",
        "process_documents": "Apply .ignore_rules.json text cleaning; expand matter_diff HTML",
        "ingest_data_model": "Write data_model_schema.json; pass DM XML docs through",
        "build_matter_schema": "Extract canonical entity tables from spec diff HTML",
        "chunk_embed_test_plans": "Build or load FAISS vector DB from test plan chunks",
        "chunk_pr": "Chunk PR docs into pr_chunks; chunk spec docs; apply cluster filter",
        "extract_pr_changes": "Rule-based + LLM fallback structured change extraction",
        "build_knowledge_graph": "Build or load KG from DM XML + spec + test plans",
        "search_test_plan_vector_db": "Per-chunk FAISS top-k search",
        "search_knowledge_graph": "Per-chunk KG entity search via structured change records",
        "analyze_chunks_with_llm": "Pass 1: bin-pack chunks, rerank, assemble prompt (S/T/R/X/A/B/C/D), 1 LLM call per batch",
        "cluster_review": "Pass 2: per-cluster LLM audit for symmetry gaps and missing test types",
        "second_pass_tc_gen": "Pass 3: consolidation dedup + coverage gap outline/expand",
        "human_outline_expand": "Pass 4: re-expand human-edited outline (--third-pass-expand); no-op otherwise",
        "write_adoc_updates": "Write TC updates back to source .adoc files via tc_index.json",
        "write_updated_testplan": "Write per-cluster updated .adoc test plans to reports/",
        "generate_report": "Write Markdown + JSON + HTML report",
        "cleanup": "Release GPU/MPS memory, GC, log run summary",
    }

    desc_rows = "\n".join(
        f'<tr><td style="color:#80cbc4;font-weight:bold">{n}</td><td>{d}</td></tr>'
        for n, d in node_descriptions.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Pipeline DAG — Matter RAG</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ background:#1a1a1a; color:#e0e0e0; font-family:monospace; margin:0; padding:20px; }}
  h1 {{ color:#80cbc4; margin-bottom:4px; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:20px; }}
  .mermaid {{ background:#252525; border:1px solid #333; border-radius:8px; padding:20px; margin:20px 0; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; margin-top:20px; }}
  th {{ background:#252525; color:#80cbc4; padding:8px 12px; text-align:left; border-bottom:2px solid #333; }}
  td {{ padding:6px 12px; border-bottom:1px solid #2a2a2a; }}
  tr:hover td {{ background:#202020; }}
  .nav {{ margin-bottom:16px; font-size:13px; }}
  .nav a {{ color:#80cbc4; text-decoration:none; margin-right:16px; }}
  .nav a:hover {{ text-decoration:underline; }}
  .legend {{ display:flex; gap:16px; margin:12px 0; font-size:12px; }}
  .legend span {{ padding:3px 10px; border-radius:10px; }}
</style>
</head>
<body>
<div class="nav">
  <a href="/">&larr; Dashboard</a>
  <a href="/kg/viz">KG Viz &rarr;</a>
  <a href="/test-cases">Test Cases &rarr;</a>
  <a href="/chat">Chat &rarr;</a>
</div>
<h1>Pipeline DAG — Matter RAG</h1>
<div class="sub">18-node LangGraph pipeline | run_ghpr_analysis.py</div>

<div class="legend">
  <span style="background:#00695c;color:#fff">Entry</span>
  <span style="background:#4a148c;color:#fff">Conditional routing</span>
  <span style="background:#e65100;color:#fff">LLM passes</span>
  <span style="background:#1565c0;color:#fff">Report</span>
  <span style="background:#37474f;color:#fff">Cleanup</span>
</div>

<div class="mermaid">
{mermaid}
</div>

<h2>Node Descriptions</h2>
<table>
<tr><th>Node</th><th>Description</th></tr>
{desc_rows}
</table>

<script>
mermaid.initialize({{
  startOnLoad: true,
  theme: 'dark',
  themeVariables: {{
    primaryColor: '#263238',
    primaryTextColor: '#e0e0e0',
    lineColor: '#546e7a',
    primaryBorderColor: '#546e7a',
  }}
}});
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# Pydantic models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str
    top_k: int = 10
    threshold: float = 0.5
    search_vector_db: bool = True
    search_kg: bool = True


class ReloadResponse(BaseModel):
    status: str
    errors: List[str]


# ---------------------------------------------------------------------------
# Helper: serialise GraphNode to dict
# ---------------------------------------------------------------------------

def _node_to_dict(node) -> Dict[str, Any]:
    return {
        "node_id":   node.node_id,
        "node_type": node.node_type.name if hasattr(node.node_type, "name") else str(node.node_type),
        "label":     node.label,
        "properties": node.properties,
    }


def _resolve_node_id(graph, query: str) -> Optional[str]:
    """Resolve a user-supplied node ID string to an actual graph node ID.

    Resolution priority (highest → lowest):
    1. Exact match (case-sensitive) — returned immediately.
    2. Case-insensitive exact match.
    3. Prefix match: candidates where the node ID starts with the query
       (case-insensitive), sorted by length ascending (shortest = most specific).
    4. Substring match anywhere in the node ID, sorted by length ascending.

    Returns ``None`` if no match is found.

    The previous fallback used ``matches[0]`` from unsorted iteration, which caused
    e.g. ``CLUSTER::OnOff`` to resolve to
    ``FEATURE::Device Energy Management Mode Cluster::OnOff`` because the string
    ``cluster::onoff`` appears at the tail of the longer node ID.
    """
    if query in graph:
        return query

    q = query.lower()

    # Case-insensitive exact match
    for nid in graph.nodes:
        if nid.lower() == q:
            return nid

    # Prefix match — prefer shorter (more specific) candidates
    prefix_matches = sorted(
        [nid for nid in graph.nodes if nid.lower().startswith(q)],
        key=len,
    )
    if prefix_matches:
        return prefix_matches[0]

    # Substring match — prefer shorter candidates
    sub_matches = sorted(
        [nid for nid in graph.nodes if q in nid.lower()],
        key=len,
    )
    return sub_matches[0] if sub_matches else None


def _ndata_to_dict(node_id: str, ndata: dict) -> Dict[str, Any]:
    """Convert raw networkx node data (which may store a GraphNode under 'obj') to a dict."""
    obj = ndata.get("obj")
    if obj is not None:
        return {
            "node_id":   obj.node_id,
            # Use .name (e.g. "TEST_CASE", "BEHAVIOR_RULE") not .value ("TestCase", "BehaviorRule")
            # so it matches the _COLOR map keys and node_type filter comparisons.
            "node_type": obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type),
            "label":     obj.label,
            "properties": {
                k: (str(v) if len(str(v)) > 200 else v)
                for k, v in obj.properties.items()
            },
        }
    # Flat storage fallback (rare — load_from_json always sets obj)
    nt = ndata.get("node_type", "")
    # Normalize CamelCase enum values ("BehaviorRule") → SCREAMING_SNAKE ("BEHAVIOR_RULE")
    try:
        from src.knowledge_graph.base_graph import NodeType as _NT
        nt = _NT(nt).name
    except (ValueError, ImportError):
        pass
    return {
        "node_id":   node_id,
        "node_type": nt,
        "label":     ndata.get("label", node_id),
        "properties": {k: v for k, v in ndata.items() if k not in ("node_type", "label")},
    }


def _entry_to_dict(entry) -> Dict[str, Any]:
    return {
        "doc_id":       entry.doc_id,
        "page_content": entry.page_content,
        "metadata":     entry.metadata,
    }


def _search_result_to_dict(r) -> Dict[str, Any]:
    return {
        "score":        round(r.score, 4),
        "rank":         r.rank,
        "doc_id":       r.doc_id,
        "page_content": r.page_content,
        "metadata":     r.metadata,
    }


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health", summary="Check index files and database health")
def health():
    """Return the status of every storage component.

    Checks file existence, on-disk sizes, and in-memory entry counts.
    Returns HTTP 200 even when some components are unavailable so you
    can see partial state.
    """
    result: Dict[str, Any] = {"status": "ok", "components": {}, "errors": list(_state.load_errors)}

    # ---- Config ----
    result["components"]["config"] = {
        "loaded": _state.config is not None,
    }

    # ---- FAISS index ----
    if _state.config:
        idx_path = Path(_PROJECT_ROOT / _state.config.database.faiss_index_path)
        meta_path = Path(_PROJECT_ROOT / _state.config.database.metadata_path)
        result["components"]["vector_store"] = {
            "index_file":    str(idx_path),
            "index_exists":  idx_path.exists(),
            "index_size_mb": round(idx_path.stat().st_size / 1e6, 2) if idx_path.exists() else None,
            "meta_file":     str(meta_path),
            "meta_exists":   meta_path.exists(),
            "loaded":        _state.vector_store is not None,
            "num_entries":   _state.vector_store.size if _state.vector_store else None,
        }

    # ---- KG JSON ----
    if _state.config:
        kg_path_rel = getattr(_state.config.knowledge_graph, "graph_store_path",
                              "data/knowledge_graph/matter_kg.json")
        kg_path = _PROJECT_ROOT / kg_path_rel
        result["components"]["knowledge_graph"] = {
            "kg_file":       str(kg_path),
            "kg_exists":     kg_path.exists(),
            "kg_size_mb":    round(kg_path.stat().st_size / 1e6, 2) if kg_path.exists() else None,
            "loaded":        _state.kg is not None,
            "num_nodes":     _state.kg.num_nodes if _state.kg else None,
            "num_edges":     _state.kg.num_edges if _state.kg else None,
        }

    if _state.load_errors:
        result["status"] = "degraded"

    return result


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

@app.get("/stats", summary="Detailed statistics about stored data")
def stats():
    """Return chunk counts, node type distributions, and metadata key summary."""
    out: Dict[str, Any] = {}

    # ---- Vector store stats ----
    if _state.vector_store is None:
        out["vector_store"] = {"error": "not loaded"}
    else:
        from collections import Counter
        entries = _state.vector_store._entries
        source_counts: Counter = Counter()
        chunk_type_counts: Counter = Counter()
        doc_type_counts: Counter = Counter()
        meta_keys: Counter = Counter()

        for e in entries:
            source_counts[e.metadata.get("source_id", e.metadata.get("path", "unknown"))] += 1
            chunk_type_counts[e.metadata.get("chunk_type", "unknown")] += 1
            doc_type_counts[e.metadata.get("doc_type", "unknown")] += 1
            for k in e.metadata:
                meta_keys[k] += 1

        out["vector_store"] = {
            "total_entries":       len(entries),
            "by_source":           dict(source_counts.most_common(20)),
            "by_chunk_type":       dict(chunk_type_counts.most_common()),
            "by_doc_type":         dict(doc_type_counts.most_common()),
            "metadata_keys_found": list(meta_keys.keys()),
        }

    # ---- KG stats ----
    if _state.kg is None:
        out["knowledge_graph"] = {"error": "not loaded"}
    else:
        from collections import Counter
        node_type_counts: Counter = Counter()
        for node_id, ndata in _state.kg._graph.nodes(data=True):
            obj = ndata.get("obj")
            if obj is not None:
                nt = obj.node_type.value if hasattr(obj.node_type, "value") else str(obj.node_type)
            else:
                nt = str(ndata.get("node_type", "unknown"))
            node_type_counts[nt] += 1

        edge_type_counts: Counter = Counter()
        for u, v, edata in _state.kg._graph.edges(data=True):
            et = edata.get("edge_type", "unknown")
            edge_type_counts[et.value if hasattr(et, "value") else str(et)] += 1

        out["knowledge_graph"] = {
            "total_nodes":       _state.kg.num_nodes,
            "total_edges":       _state.kg.num_edges,
            "by_node_type":      dict(node_type_counts.most_common()),
            "by_edge_type":      dict(edge_type_counts.most_common()),
        }

    return out


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------

@app.post("/query", summary="Natural-language search over vector DB and KG")
def query(req: QueryRequest):
    """Search for test cases and spec content matching a natural-language query.

    Searches both the FAISS vector database (semantic similarity) and the
    NetworkX knowledge graph (entity-based lookup) and returns merged results.

    Example body::

        {
          "query": "is there a test case for device discovery",
          "top_k": 10,
          "threshold": 0.5
        }
    """
    if _state.config is None:
        raise HTTPException(status_code=503, detail="Config not loaded — check /health")

    vector_results: List[Dict] = []
    kg_results: List[Dict] = []

    # ---- Vector DB search ----
    if req.search_vector_db:
        if _state.vector_store is None:
            vector_results = [{"error": "vector store not loaded"}]
        else:
            try:
                embedder = _get_embedder()
                query_vec = embedder.embed_query(req.query)
                raw = _state.vector_store.search_by_vector(
                    query_vec,
                    k=req.top_k,
                    threshold=req.threshold,
                )
                vector_results = [_search_result_to_dict(r) for r in raw]
            except Exception as exc:
                vector_results = [{"error": str(exc)}]

    # ---- KG search ----
    if req.search_kg:
        if _state.kg is None:
            kg_results = [{"error": "knowledge graph not loaded"}]
        else:
            try:
                nodes = _state.kg.search_by_entities(req.query, max_results=req.top_k)
                kg_results = [_node_to_dict(n) for n in nodes]
            except Exception as exc:
                kg_results = [{"error": str(exc)}]

    # ---- Summary ----
    found_tc_ids = {
        r["metadata"].get("tc_id") or r["metadata"].get("test_case_id") or r["metadata"].get("heading", "")
        for r in vector_results
        if isinstance(r.get("metadata"), dict)
    }
    found_tc_ids |= {
        n["node_id"] for n in kg_results
        if n.get("node_type") == "TestCase"
    }
    found_tc_ids.discard("")

    return {
        "query":          req.query,
        "top_k":          req.top_k,
        "threshold":      req.threshold,
        "vector_results": vector_results,
        "kg_results":     kg_results,
        "summary": {
            "vector_hits": len(vector_results),
            "kg_hits":     len(kg_results),
            "test_case_ids_found": sorted(found_tc_ids),
        },
    }


# ---------------------------------------------------------------------------
# GET /chunks
# ---------------------------------------------------------------------------

@app.get("/chunks", summary="Browse stored vector DB chunks with optional filters")
def list_chunks(
    request: Request,
    page: int = Query(0, ge=0, description="Page number (0-based)"),
    size: int = Query(20, ge=1, le=200, description="Page size"),
    source: Optional[str] = Query(None, description="Filter by source_id substring"),
    doc_type: Optional[str] = Query(None, description="Filter by doc_type (pr_change, spec, test_plan …)"),
    chunk_type: Optional[str] = Query(None, description="Filter by chunk_type (full, intent_summary, procedure, setup)"),
    tc_id: Optional[str] = Query(None, description="Filter by tc_id prefix (e.g. TC-OO, TC-DRLK-2.1)"),
    cluster: Optional[str] = Query(None, description="Filter by cluster name substring"),
    contains: Optional[str] = Query(None, description="Filter by text substring in page_content"),
    format: Optional[str] = Query(None, description="Set to 'json' for raw JSON; default is HTML"),
):
    """Return paginated chunk entries from the FAISS metadata store.

    All filters are case-insensitive substring matches.  Use ``/stats`` to see
    available values for source, doc_type, and chunk_type.
    """
    if _state.vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store not loaded — check /health")

    entries = _state.vector_store._entries

    # Apply filters
    if source:
        src_lower = source.lower()
        entries = [e for e in entries
                   if src_lower in (e.metadata.get("source_id", "") or "").lower()
                   or src_lower in (e.metadata.get("path", "") or "").lower()]
    if doc_type:
        dt_lower = doc_type.lower()
        entries = [e for e in entries
                   if dt_lower in (e.metadata.get("doc_type", "") or "").lower()]
    if chunk_type:
        ct_lower = chunk_type.lower()
        entries = [e for e in entries
                   if ct_lower in (e.metadata.get("chunk_type", "") or "").lower()]
    if tc_id:
        tc_lower = tc_id.lower()
        entries = [e for e in entries
                   if tc_lower in (e.metadata.get("tc_id", "") or "").lower()]
    if cluster:
        cl_lower = cluster.lower()
        entries = [e for e in entries
                   if cl_lower in (e.metadata.get("cluster", "") or "").lower()]
    if contains:
        c_lower = contains.lower()
        entries = [e for e in entries if c_lower in e.page_content.lower()]

    total = len(entries)
    page_entries = entries[page * size: (page + 1) * size]

    # JSON response
    if format == "json":
        return {
            "total":   total,
            "page":    page,
            "size":    size,
            "pages":   (total + size - 1) // size,
            "chunks":  [_entry_to_dict(e) for e in page_entries],
        }

    # HTML response (default)
    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows = ""
    for e in page_entries:
        meta = e.metadata or {}
        content_preview = e.page_content[:200] + ("…" if len(e.page_content) > 200 else "")
        tc = meta.get("tc_id", "")
        cl = meta.get("cluster", "")
        ct = meta.get("chunk_type", "")
        intents = ", ".join(meta.get("intents", [])) if isinstance(meta.get("intents"), list) else str(meta.get("intents", ""))
        entity_refs = ", ".join(meta.get("entity_refs", [])) if isinstance(meta.get("entity_refs"), list) else str(meta.get("entity_refs", ""))
        rows += (
            f"<tr>"
            f"<td><a href='/chunks/{e.doc_id}' style='color:#4fc3f7'>{_esc(e.doc_id)}</a></td>"
            f"<td><span style='color:#ce93d8'>{_esc(tc)}</span></td>"
            f"<td><span style='color:#00bcd4'>{_esc(cl)}</span></td>"
            f"<td><span style='color:#ffb74d'>{_esc(ct)}</span></td>"
            f"<td style='font-size:0.8em;color:#aaa'>{_esc(intents)}</td>"
            f"<td style='font-size:0.8em;color:#aaa'>{_esc(entity_refs)}</td>"
            f"<td style='font-size:0.8em;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{_esc(content_preview)}</td>"
            f"</tr>\n"
        )

    total_pages = (total + size - 1) // size
    prev_link = f"?page={page-1}&size={size}" if page > 0 else ""
    next_link = f"?page={page+1}&size={size}" if page < total_pages - 1 else ""
    # Preserve filters in pagination links
    filter_params = ""
    for k, v in [("source", source), ("doc_type", doc_type), ("chunk_type", chunk_type),
                 ("tc_id", tc_id), ("cluster", cluster), ("contains", contains)]:
        if v:
            filter_params += f"&{k}={v}"
    if prev_link:
        prev_link += filter_params
    if next_link:
        next_link += filter_params

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Chunks Browser</title>
<style>
body{{background:#1e1e1e;color:#d4d4d4;font-family:monospace;padding:20px}}
h1{{color:#4fc3f7}}
a{{color:#4fc3f7;text-decoration:none}}
table{{border-collapse:collapse;width:100%;font-size:0.85em}}
th{{background:#2d2d2d;color:#fff;padding:6px 8px;text-align:left;position:sticky;top:0}}
td{{border-bottom:1px solid #333;padding:5px 8px;vertical-align:top}}
tr:hover{{background:#2a2a2a}}
.filters{{background:#252525;padding:12px;border-radius:4px;margin:10px 0}}
.filters input,.filters select{{background:#333;color:#d4d4d4;border:1px solid #555;padding:4px 8px;margin:0 8px 0 4px;border-radius:3px}}
.filters button{{background:#4fc3f7;color:#000;border:none;padding:5px 14px;border-radius:3px;cursor:pointer;font-weight:bold}}
.nav{{margin:10px 0;display:flex;gap:16px;align-items:center}}
.nav a{{background:#333;padding:4px 10px;border-radius:3px}}
.badge{{display:inline-block;background:#333;padding:2px 8px;border-radius:3px;font-size:0.8em}}
</style></head><body>
<h1>Vector DB Chunks ({total} total)</h1>
<div class="nav">
<a href="/">Dashboard</a>
<a href="/test-cases">Test Cases</a>
<a href="/kg/viz">KG Viz</a>
<a href="/chunks?format=json&page={page}&size={size}{filter_params}">JSON</a>
</div>
<div class="filters">
<form method="get">
<label>TC-ID: <input name="tc_id" value="{tc_id or ''}" placeholder="TC-OO"></label>
<label>Cluster: <input name="cluster" value="{cluster or ''}" placeholder="On/Off"></label>
<label>Chunk Type: <input name="chunk_type" value="{chunk_type or ''}" placeholder="full"></label>
<label>Contains: <input name="contains" value="{contains or ''}" placeholder="text search"></label>
<label>Size: <input name="size" value="{size}" size="3"></label>
<button type="submit">Filter</button>
<a href="/chunks" style="margin-left:10px;color:#888">Clear</a>
</form>
</div>
<div class="nav">
{"<a href='" + prev_link + "'>Prev</a>" if prev_link else "<span style='color:#555'>Prev</span>"}
<span class="badge">Page {page+1}/{total_pages}</span>
{"<a href='" + next_link + "'>Next</a>" if next_link else "<span style='color:#555'>Next</span>"}
</div>
<table>
<tr><th>Doc ID</th><th>TC-ID</th><th>Cluster</th><th>Chunk Type</th><th>Intents</th><th>Entity Refs</th><th>Content Preview</th></tr>
{rows}
</table>
</body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# GET /chunks/{doc_id}
# ---------------------------------------------------------------------------

@app.get("/chunks/{doc_id}", summary="Get a single chunk by doc_id")
def get_chunk(doc_id: str):
    """Return the full chunk record for a given ``doc_id`` (e.g. ``doc_000042``)."""
    if _state.vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store not loaded — check /health")

    entry = _state.vector_store.get_by_id(doc_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"doc_id '{doc_id}' not found")

    return _entry_to_dict(entry)


# ---------------------------------------------------------------------------
# GET /test-cases
# ---------------------------------------------------------------------------

@app.get("/test-cases", summary="List all TestCase nodes in the knowledge graph")
def list_test_cases(
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
    cluster: Optional[str] = Query(None, description="Filter by cluster name substring"),
    tc_id: Optional[str] = Query(None, description="Filter by TC id substring e.g. TC-OO"),
    format: Optional[str] = Query(None, description="'json' to return raw JSON"),
):
    """Return all TEST_CASE nodes from the knowledge graph, with optional filters.

    Returns an HTML table by default. Add ``?format=json`` for raw JSON.
    """
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    all_tcs = _state.kg.get_all_test_cases()

    if cluster:
        c_lower = cluster.lower()
        all_tcs = [n for n in all_tcs
                   if c_lower in n.label.lower()
                   or c_lower in str(n.properties.get("cluster", "")).lower()
                   or c_lower in str(n.properties.get("path", "")).lower()
                   or any(c_lower in rc.lower() for rc in n.properties.get("related_clusters", []))]
    if tc_id:
        t_lower = tc_id.lower()
        all_tcs = [n for n in all_tcs
                   if t_lower in n.node_id.lower() or t_lower in n.label.lower()]

    total = len(all_tcs)
    page_nodes = all_tcs[page * size: (page + 1) * size]
    pages = (total + size - 1) // size

    if format == "json":
        return {
            "total": total,
            "page":  page,
            "size":  size,
            "pages": pages,
            "test_cases": [_node_to_dict(n) for n in page_nodes],
        }

    # ── HTML view ────────────────────────────────────────────────────────────
    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    filter_params = ""
    if cluster:
        filter_params += f'&cluster={_esc(cluster)}'
    if tc_id:
        filter_params += f'&tc_id={_esc(tc_id)}'

    prev_link = f"/test-cases?page={page-1}&size={size}{filter_params}" if page > 0 else "#"
    next_link = f"/test-cases?page={page+1}&size={size}{filter_params}" if page < pages - 1 else "#"

    rows_html = ""
    for n in page_nodes:
        p = n.properties
        tc_label = _esc(n.label)
        cluster_val = _esc(p.get("cluster", "—"))
        source = _esc(p.get("source_doc", p.get("path", "—")).split("/")[-1])
        purpose = _esc(str(p.get("purpose", p.get("content", "")))[:120])
        intents = _esc(", ".join(p.get("intents", [])) if isinstance(p.get("intents"), list) else str(p.get("intents", "—")))
        node_id_esc = _esc(n.node_id)
        detail_url = f"/test-cases/{n.node_id}"
        viz_url = _esc(f"/kg/viz?center={n.node_id}&hops=2&source=merged")
        rows_html += f"""
        <tr>
          <td><a href="{_esc(detail_url)}" class="tc-link">{tc_label}</a></td>
          <td><span class="pill-cluster">{cluster_val}</span></td>
          <td class="muted">{intents}</td>
          <td class="muted">{source}</td>
          <td class="purpose">{purpose}</td>
          <td style="text-align:center"><a href="{viz_url}" class="kg-link" title="View in KG graph" target="_blank">⬡</a></td>
        </tr>"""

    showing_from = page * size + 1
    showing_to   = min((page + 1) * size, total)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Test Cases // Matter RAG</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:#05060a; --surf:#0c0e14; --card:#0f1119; --border:#1b1e2a;
      --text:#cdd2e8; --muted:#50566e; --cyan:#00e5ff; --green:#2de08a;
      --amber:#f5a623; --violet:#a78bfa; --orange:#ff6830;
    }}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    html{{font-size:14px}}
    body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;min-height:100vh}}
    body::before{{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
      background-image:radial-gradient(circle,#1a1d28 1px,transparent 1px);
      background-size:26px 26px;opacity:.35}}
    body::after{{content:'';position:fixed;top:0;left:0;right:0;height:1px;z-index:200;
      background:linear-gradient(90deg,transparent 0%,var(--cyan) 50%,transparent 100%)}}
    .z1{{position:relative;z-index:1}}
    nav{{position:sticky;top:0;z-index:50;border-bottom:1px solid var(--border);
      background:rgba(5,6,10,.92);backdrop-filter:blur(14px);
      padding:.7rem 2rem;display:flex;align-items:center;gap:10px}}
    .logo-hex{{width:28px;height:28px;background:var(--cyan);clip-path:polygon(50% 0%,93% 25%,93% 75%,50% 100%,7% 75%,7% 25%);
      display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px;color:#05060a}}
    .nav-name{{font-weight:600;color:#fff;font-size:13px}}
    .nav-slash{{color:var(--muted)}}
    .nav-sub{{color:var(--muted);font-size:12px}}
    .nav-r{{margin-left:auto;display:flex;gap:1.2rem;align-items:center}}
    .nav-a{{font-size:11px;color:var(--muted);text-decoration:none;letter-spacing:.05em}}
    .nav-a:hover{{color:var(--cyan)}}
    .wrap{{max-width:1400px;margin:0 auto;padding:2rem 2rem 5rem}}
    .page-hdr{{margin-bottom:1.5rem}}
    .eyebrow{{font-size:10px;color:var(--cyan);letter-spacing:.15em;text-transform:uppercase;margin-bottom:.4rem}}
    h1{{font-family:'Syne',sans-serif;font-size:1.8rem;color:#fff}}
    .sub{{font-size:12px;color:var(--muted);margin-top:.4rem}}
    .toolbar{{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:1.5rem;align-items:center}}
    .toolbar input{{background:var(--surf);border:1px solid var(--border);color:var(--text);
      padding:.45rem .9rem;font-family:inherit;font-size:12px;outline:none;min-width:220px}}
    .toolbar input:focus{{border-color:var(--cyan)}}
    .toolbar button{{background:var(--cyan);color:#05060a;border:none;padding:.45rem 1.1rem;
      font-family:inherit;font-size:12px;font-weight:600;cursor:pointer}}
    .toolbar button:hover{{opacity:.85}}
    .toolbar a.btn{{background:var(--surf);color:var(--muted);border:1px solid var(--border);
      padding:.4rem .9rem;font-size:11px;text-decoration:none;letter-spacing:.04em}}
    .toolbar a.btn:hover{{border-color:var(--cyan);color:var(--cyan)}}
    .meta{{font-size:11px;color:var(--muted);margin-bottom:.9rem}}
    .meta span{{color:var(--cyan)}}
    table{{width:100%;border-collapse:collapse;font-size:12px}}
    th{{text-align:left;padding:.6rem 1rem;border-bottom:1px solid var(--border);
      font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;
      background:var(--surf)}}
    td{{padding:.65rem 1rem;border-bottom:1px solid var(--border);vertical-align:top}}
    tr:hover td{{background:rgba(255,255,255,.025)}}
    .tc-link{{color:var(--cyan);text-decoration:none;font-weight:500}}
    .tc-link:hover{{text-decoration:underline}}
    .pill-cluster{{background:rgba(0,229,255,.08);color:var(--cyan);
      padding:.15rem .55rem;font-size:10px;border:1px solid rgba(0,229,255,.18)}}
    .muted{{color:var(--muted)}}
    .purpose{{color:#9ea5bf;max-width:380px;word-break:break-word}}
    .kg-link{{color:#00bcd4;text-decoration:none;font-size:16px;line-height:1}}
    .kg-link:hover{{color:#00e5ff}}
    .pager{{display:flex;gap:.75rem;align-items:center;margin-top:1.5rem;font-size:12px;color:var(--muted)}}
    .pager a{{color:var(--cyan);text-decoration:none;padding:.3rem .8rem;border:1px solid rgba(0,229,255,.25)}}
    .pager a.disabled{{pointer-events:none;opacity:.3}}
    .pager .cur-page{{color:#fff}}
  </style>
</head>
<body><div class="z1">
<nav>
  <div class="logo-hex">M</div>
  <span class="nav-name">Matter RAG</span>
  <span class="nav-slash">/</span>
  <span class="nav-sub">test cases</span>
  <div class="nav-r">
    <a href="/" class="nav-a">← Dashboard</a>
    <a href="/test-cases?format=json&page={page}&size={size}{filter_params}" class="nav-a">JSON&nbsp;↗</a>
    <a href="/kg/viz" class="nav-a">KG Viz&nbsp;↗</a>
    <a href="/chat" class="nav-a hi">→ CHAT</a>
  </div>
</nav>

<div class="wrap">
  <div class="page-hdr">
    <div class="eyebrow">knowledge graph</div>
    <h1>Test Cases</h1>
    <p class="sub">TEST_CASE nodes extracted from test plan documents. Click a row to inspect KG neighbours.</p>
  </div>

  <form class="toolbar" method="get" action="/test-cases">
    <input name="cluster" placeholder="Filter by cluster…" value="{_esc(cluster or '')}"/>
    <input name="tc_id"   placeholder="Filter by TC-ID…"   value="{_esc(tc_id or '')}"/>
    <input name="size"    placeholder="Page size"           value="{size}" style="min-width:90px;max-width:100px"/>
    <button type="submit">Apply</button>
    <a href="/test-cases" class="btn">Clear</a>
  </form>

  <div class="meta">
    Showing <span>{showing_from}–{showing_to}</span> of <span>{total}</span> test cases
    &nbsp;·&nbsp; page <span>{page + 1}</span> / <span>{max(pages, 1)}</span>
  </div>

  <table>
    <thead>
      <tr>
        <th>TC ID / Label</th>
        <th>Cluster</th>
        <th>Intents</th>
        <th>Source file</th>
        <th>Purpose (preview)</th>
        <th style="text-align:center">KG</th>
      </tr>
    </thead>
    <tbody>{rows_html}
    </tbody>
  </table>

  <div class="pager">
    <a href="{prev_link}" class="{'disabled' if page == 0 else ''}">← Prev</a>
    <span class="cur-page">Page {page + 1} / {max(pages, 1)}</span>
    <a href="{next_link}" class="{'disabled' if page >= pages - 1 else ''}">Next →</a>
  </div>
</div>
</div>
</body>
</html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# GET /test-cases/{tc_node_id}
# ---------------------------------------------------------------------------

@app.get("/test-cases/{tc_node_id:path}", summary="Get a TestCase node + its KG neighbours")
def get_test_case(tc_node_id: str):
    """Return a single TestCase node and all its immediate KG neighbours.

    Use URL-encoded ``tc_node_id`` if it contains special characters.
    The ``node_id`` value comes from the ``/test-cases`` listing.
    """
    if not tc_node_id or not tc_node_id.strip():
        raise HTTPException(status_code=400, detail="tc_node_id is required (e.g. /test-cases/TC-OO-2.1)")

    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    graph = _state.kg._graph
    if tc_node_id not in graph:
        resolved = _resolve_node_id(graph, tc_node_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"Node '{tc_node_id}' not found")
        tc_node_id = resolved

    ndata = graph.nodes[tc_node_id]
    node = _ndata_to_dict(tc_node_id, ndata)

    # Outgoing edges
    out_edges = []
    for _, tgt, edata in graph.out_edges(tc_node_id, data=True):
        tgt_ndata = graph.nodes.get(tgt, {})
        tgt_dict = _ndata_to_dict(tgt, tgt_ndata)
        et = edata.get("edge_type", "")
        out_edges.append({
            "edge_type":    et.value if hasattr(et, "value") else str(et),
            "target_id":    tgt,
            "target_type":  tgt_dict["node_type"],
            "target_label": tgt_dict["label"],
        })

    # Incoming edges
    in_edges = []
    for src, _, edata in graph.in_edges(tc_node_id, data=True):
        src_ndata = graph.nodes.get(src, {})
        src_dict = _ndata_to_dict(src, src_ndata)
        et = edata.get("edge_type", "")
        in_edges.append({
            "edge_type":   et.value if hasattr(et, "value") else str(et),
            "source_id":   src,
            "source_type": src_dict["node_type"],
            "source_label": src_dict["label"],
        })

    return {
        "node":           node,
        "outgoing_edges": out_edges,
        "incoming_edges": in_edges,
        "degree":         graph.degree(tc_node_id),
    }



# ---------------------------------------------------------------------------
# GET /nodes  — quick summary of CLUSTER and TEST_CASE nodes
# ---------------------------------------------------------------------------

@app.get("/nodes", summary="List CLUSTER and TEST_CASE nodes (HTML or JSON)")
def list_nodes(
    node_type: Optional[str] = Query(None, description="Filter: CLUSTER or TEST_CASE (default: both)"),
    cluster: Optional[str] = Query(None, description="Filter TEST_CASEs by cluster substring"),
    label: Optional[str] = Query(None, description="Label substring filter"),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
    format: Optional[str] = Query(None, description="Set to 'json' for raw JSON"),
):
    """List CLUSTER and TEST_CASE nodes from the knowledge graph.

    Without filters both types are shown together.  Use ``?node_type=CLUSTER``
    or ``?node_type=TEST_CASE`` to narrow.  Returns styled HTML by default;
    add ``?format=json`` for raw JSON.
    """
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    graph = _state.kg._graph
    target_types = {"CLUSTER", "TEST_CASE"}
    if node_type:
        nt_upper = node_type.upper()
        if nt_upper in target_types:
            target_types = {nt_upper}
        # allow partial like "cluster" or "tc"
        elif "CLUSTER" in nt_upper or nt_upper in ("CL",):
            target_types = {"CLUSTER"}
        elif "TEST" in nt_upper or nt_upper in ("TC",):
            target_types = {"TEST_CASE"}
        else:
            target_types = {nt_upper}

    nodes = []
    for nid, ndata in graph.nodes(data=True):
        d = _ndata_to_dict(nid, ndata)
        nt = d["node_type"]
        if nt not in target_types:
            continue
        lbl = d["label"]
        if label and label.lower() not in lbl.lower():
            continue
        if cluster and nt == "TEST_CASE":
            c_val = str(d.get("properties", {}).get("cluster", "")).lower()
            if cluster.lower() not in c_val:
                continue
        nodes.append({
            "node_id":   nid,
            "node_type": nt,
            "label":     lbl,
            "degree":    graph.degree(nid),
            "cluster":   d.get("properties", {}).get("cluster", "") if nt == "TEST_CASE" else "",
        })

    nodes.sort(key=lambda n: (n["node_type"], n["label"]))
    total = len(nodes)
    pages = (total + size - 1) // size
    page_nodes = nodes[page * size: (page + 1) * size]

    if format == "json":
        return {
            "total": total,
            "page": page,
            "size": size,
            "pages": pages,
            "nodes": page_nodes,
        }

    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # Build HTML
    _COLOR = {
        "CLUSTER":   "#00bcd4",
        "TEST_CASE": "#a5d6a7",
    }

    filter_qparts = []
    if node_type:
        filter_qparts.append(f"node_type={node_type}")
    if cluster:
        filter_qparts.append(f"cluster={cluster}")
    if label:
        filter_qparts.append(f"label={label}")
    if size != 50:
        filter_qparts.append(f"size={size}")

    def _page_url(p: int) -> str:
        parts = filter_qparts + [f"page={p}"]
        return f"/nodes?{'&'.join(parts)}"

    rows_html = ""
    for n in page_nodes:
        color = _COLOR.get(n["node_type"], "#aaa")
        cl_pill = f'<span class="pill-cluster">{_esc(n["cluster"])}</span>' if n["cluster"] else "—"
        detail_url = f"/kg/node/{_esc(n['node_id'])}"
        viz_url = f"/kg/viz?center={_esc(n['label'])}&hops=2"
        rows_html += f"""
        <tr>
          <td><span class="pill-nt" style="background:{color};color:#111;">{_esc(n["node_type"])}</span></td>
          <td><a href="{_esc(detail_url)}" class="tc-link">{_esc(n["label"])}</a></td>
          <td>{cl_pill}</td>
          <td style="text-align:right;">{n["degree"]}</td>
          <td><a href="{viz_url}" class="tc-link" target="_blank">viz ↗</a></td>
        </tr>"""

    prev_link = f'<a href="{_page_url(page-1)}" class="page-btn">← Prev</a>' if page > 0 else '<span class="page-btn disabled">← Prev</span>'
    next_link = f'<a href="{_page_url(page+1)}" class="page-btn">Next →</a>' if page + 1 < pages else '<span class="page-btn disabled">Next →</span>'

    filter_params = "&".join(filter_qparts)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>KG Nodes — Matter RAG</title>
  <style>
    body{{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;margin:0;padding:20px;}}
    h1{{color:#58a6ff;font-size:1.4em;margin-bottom:4px;}}
    .sub{{color:#8b949e;font-size:.85em;margin-bottom:16px;}}
    .nav{{margin-bottom:18px;}}
    .nav a{{color:#58a6ff;text-decoration:none;margin-right:16px;font-size:.85em;}}
    .filter-form{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;align-items:center;}}
    .filter-form input,.filter-form select{{background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:5px 8px;border-radius:4px;font-family:inherit;font-size:.85em;}}
    .filter-form button{{background:#238636;color:#fff;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:.85em;}}
    .filter-form .clear-btn{{background:#21262d;color:#8b949e;border:1px solid #30363d;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:.85em;text-decoration:none;}}
    table{{width:100%;border-collapse:collapse;font-size:.82em;}}
    th{{background:#161b22;color:#8b949e;padding:8px 10px;text-align:left;border-bottom:1px solid #30363d;}}
    td{{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:middle;}}
    tr:hover td{{background:#161b22;}}
    .pill-nt{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.78em;font-weight:bold;}}
    .pill-cluster{{background:#0d2137;color:#58a6ff;border:1px solid #1f6feb;padding:1px 7px;border-radius:10px;font-size:.8em;}}
    .tc-link{{color:#58a6ff;text-decoration:none;}}
    .tc-link:hover{{text-decoration:underline;}}
    .pagination{{margin-top:14px;display:flex;gap:10px;align-items:center;font-size:.83em;}}
    .page-btn{{background:#21262d;color:#8b949e;border:1px solid #30363d;padding:4px 12px;border-radius:4px;cursor:pointer;text-decoration:none;}}
    .page-btn:not(.disabled):hover{{background:#30363d;}}
    .page-btn.disabled{{opacity:.4;cursor:default;}}
    .summary{{color:#8b949e;font-size:.82em;}}
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">← Dashboard</a>
    <a href="/test-cases">Test Cases ↗</a>
    <a href="/kg/viz">KG Viz ↗</a>
    <a href="/nodes?{filter_params}&format=json" target="_blank">JSON ↗</a>
    <a href="/chat">→ CHAT</a>
  </div>
  <h1>KG Nodes</h1>
  <div class="sub">Showing CLUSTER and TEST_CASE nodes — {total} total</div>

  <form class="filter-form" method="get" action="/nodes">
    <select name="node_type">
      <option value="">All types</option>
      <option value="CLUSTER" {"selected" if node_type and "CLUSTER" in node_type.upper() else ""}>CLUSTER</option>
      <option value="TEST_CASE" {"selected" if node_type and "TEST" in node_type.upper() else ""}>TEST_CASE</option>
    </select>
    <input name="cluster" type="text" placeholder="Cluster filter" value="{_esc(cluster or '')}">
    <input name="label"   type="text" placeholder="Label filter"   value="{_esc(label or '')}">
    <select name="size">
      {"".join(f'<option value="{s}" {"selected" if s == size else ""}>{s} per page</option>' for s in [25,50,100,200])}
    </select>
    <button type="submit">Apply</button>
    <a href="/nodes" class="clear-btn">Clear</a>
  </form>

  <table>
    <thead><tr><th>Type</th><th>Label / Node ID</th><th>Cluster</th><th>Degree</th><th></th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>

  <div class="pagination">
    {prev_link}
    <span class="summary">Page {page+1} of {max(1,pages)} ({total} nodes)</span>
    {next_link}
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# GET /kg/nodes
# ---------------------------------------------------------------------------

@app.get("/kg/nodes", summary="Browse all knowledge graph nodes")
def list_kg_nodes(
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=500),
    node_type: Optional[str] = Query(None, description="Filter by node type e.g. TestCase, Cluster"),
    label: Optional[str] = Query(None, description="Filter by label substring"),
):
    """Return all nodes in the knowledge graph, optionally filtered by type or label."""
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    graph = _state.kg._graph
    nodes = []
    for node_id, ndata in graph.nodes(data=True):
        d = _ndata_to_dict(node_id, ndata)
        nt = d["node_type"]
        lbl = d["label"]
        if node_type and node_type.lower() not in nt.lower():
            continue
        if label and label.lower() not in lbl.lower():
            continue
        nodes.append({
            "node_id":   node_id,
            "node_type": nt,
            "label":     lbl,
            "degree":    graph.degree(node_id),
        })

    total = len(nodes)
    return {
        "total": total,
        "page":  page,
        "size":  size,
        "pages": (total + size - 1) // size,
        "nodes": nodes[page * size: (page + 1) * size],
    }


# ---------------------------------------------------------------------------
# GET /kg/node/{node_id}
# ---------------------------------------------------------------------------

@app.get("/kg/node/{node_id:path}", summary="Get a KG node + its immediate neighbours")
def get_kg_node(node_id: str):
    """Return a single KG node with full properties and all adjacent edges + neighbours."""
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    graph = _state.kg._graph
    if node_id not in graph:
        resolved = _resolve_node_id(graph, node_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        node_id = resolved

    ndata = graph.nodes[node_id]
    node = _ndata_to_dict(node_id, ndata)
    node["degree"] = graph.degree(node_id)

    neighbours = []
    for nbr in graph.neighbors(node_id):
        nbr_ndata = graph.nodes.get(nbr, {})
        nbr_dict = _ndata_to_dict(nbr, nbr_ndata)
        edata = graph.edges.get((node_id, nbr), {})
        et = edata.get("edge_type", "")
        neighbours.append({
            "node_id":   nbr,
            "node_type": nbr_dict["node_type"],
            "label":     nbr_dict["label"],
            "edge_type": et.value if hasattr(et, "value") else str(et),
            "direction": "out",
        })
    for pred in graph.predecessors(node_id):
        pred_ndata = graph.nodes.get(pred, {})
        pred_dict = _ndata_to_dict(pred, pred_ndata)
        edata = graph.edges.get((pred, node_id), {})
        et = edata.get("edge_type", "")
        neighbours.append({
            "node_id":   pred,
            "node_type": pred_dict["node_type"],
            "label":     pred_dict["label"],
            "edge_type": et.value if hasattr(et, "value") else str(et),
            "direction": "in",
        })

    return {"node": node, "neighbours": neighbours}


# ---------------------------------------------------------------------------
# GET /kg/list  — available KG files
# ---------------------------------------------------------------------------

@app.get("/kg/list", summary="List available knowledge graph files")
def list_kg_files():
    """Return all KG JSON files available in the knowledge graph directory.

    Response::

        {
          "available": ["merged", "data_model", "spec", "test_plan"],
          "files": {"merged": "matter_kg.json", "data_model": "data_model_kg.json", ...}
        }
    """
    if _state.config is None:
        raise HTTPException(status_code=503, detail="Config not loaded")

    kg_path_base = Path(getattr(_state.config.knowledge_graph, "graph_store_path",
                                "data/knowledge_graph/matter_kg.json"))
    kg_dir = _PROJECT_ROOT / kg_path_base.parent

    available: List[str] = []
    files: Dict[str, str] = {}

    merged_path = _PROJECT_ROOT / kg_path_base
    if merged_path.exists():
        available.append("merged")
        files["merged"] = kg_path_base.name

    for src in ("data_model", "spec", "test_plan"):
        sub = kg_dir / f"{src}_kg.json"
        if sub.exists():
            available.append(src)
            files[src] = sub.name

    return {"available": available, "files": files}


# ---------------------------------------------------------------------------
# GET /kg/graph  — vis.js-compatible graph data (filtered subgraph)
# ---------------------------------------------------------------------------

@app.get("/kg/graph", summary="Return graph data (nodes+edges) for visualization")
def get_kg_graph(
    center: Optional[str] = Query(None, description="Node ID to center the subgraph on"),
    hops: int = Query(2, ge=1, le=4, description="Number of hops from center node"),
    node_type: Optional[str] = Query(None, description="Filter by NodeType (e.g. CLUSTER, TEST_CASE)"),
    cluster: Optional[str] = Query(None, description="Filter by cluster name (case-insensitive substring)"),
    limit: int = Query(150, ge=1, le=2000, description="Max nodes to return"),
    source: Optional[str] = Query(None, description="KG source: merged (default), data_model, spec, test_plan"),
):
    """Return a vis.js-compatible {nodes, edges} payload for the KG visualizer.

    When *center* is given, returns the N-hop ego-graph around that node.
    Without *center*, returns the highest-degree nodes up to *limit*.
    Additional *node_type*, *cluster*, and *source* filters narrow the result further.
    """
    active_kg = _get_source_kg(source or "merged")
    graph = active_kg._graph
    source_key = source or "merged"

    # --- build / reuse per-source node-type + degree-sorted caches ---
    # These are computed once per source on first request and reused until reload.
    if source_key not in _state._kg_nt_cache:
        nt_map: Dict[str, str] = {}
        for nid, ndata in graph.nodes(data=True):
            obj = ndata.get("obj")
            if obj is not None:
                nt_map[nid] = obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type)
            else:
                raw_nt = ndata.get("node_type", "")
                try:
                    from src.knowledge_graph.base_graph import NodeType as _NT
                    raw_nt = _NT(raw_nt).name
                except (ValueError, ImportError, AttributeError):
                    pass
                nt_map[nid] = str(raw_nt)
        _state._kg_nt_cache[source_key] = nt_map
        # Degree-sorted list: exclude SECTION hubs, sort by degree descending
        _state._kg_degree_sorted[source_key] = sorted(
            (nid for nid, nt in nt_map.items() if nt.upper() != "SECTION"),
            key=lambda n: graph.degree(n),
            reverse=True,
        )
        logger.info("KG cache built for source=%r: %d nodes", source_key, len(nt_map))

    nt_cache = _state._kg_nt_cache[source_key]

    # --- resolve candidate node set ---
    if center:
        if center not in graph:
            resolved = _resolve_node_id(graph, center)
            if resolved is None:
                raise HTTPException(status_code=404, detail=f"Center node '{center}' not found")
            center = resolved
        import networkx as nx
        # Build filtered undirected graph for ego-graph traversal.
        # Two exclusions prevent neighbourhood explosion:
        # 1. BELONGS_TO_PROTOCOL_AREA — hub nodes like PROTOCOL_AREA::General have 1300+
        #    edges and would pull in the entire graph at 2 hops.
        # 2. Foreign-CLUSTER → non-CLUSTER edges — a `depends_on` edge from the center
        #    cluster to a sibling cluster (e.g. RVC Run Mode → On/Off) would otherwise
        #    pull all of that sibling's TCs/REQs into hop-2.  We keep the inter-CLUSTER
        #    edge itself (so the dependency is visible at hop-1) but treat every foreign
        #    CLUSTER node as a terminal stop in the traversal.
        def _is_cluster_node(nid: str) -> bool:
            return nt_cache.get(nid, "").upper() == "CLUSTER"

        filtered_g = nx.Graph(
            (u, v, d) for u, v, d in graph.to_undirected(as_view=True).edges(data=True)
            if d.get("edge_type", "") != "BELONGS_TO_PROTOCOL_AREA"
            # Drop edges where a FOREIGN cluster node (not the center) touches a
            # non-cluster node — prevents sibling cluster neighborhoods from flooding in.
            and not (
                (_is_cluster_node(u) and u != center and not _is_cluster_node(v))
                or (_is_cluster_node(v) and v != center and not _is_cluster_node(u))
            )
        )
        subg = nx.ego_graph(filtered_g, center, radius=hops)
        candidate_ids = set(subg.nodes)

        # When the center is a CLUSTER node:
        #  a) Strip non-adjacent CLUSTER nodes that crept in via shared test-case paths.
        #  b) Supplement with ALL nodes whose cluster property matches — ensures every
        #     TEST_CASE, REQUIREMENT, ATTRIBUTE, COMMAND, EVENT, FEATURE for this cluster
        #     is present regardless of hop distance or limit.
        center_ndata = graph.nodes.get(center, {})
        center_obj = center_ndata.get("obj")
        is_cluster_center = (
            center_obj is not None
            and getattr(getattr(center_obj, "node_type", None), "name", "") == "CLUSTER"
        )
        if is_cluster_center:
            direct_nbrs = set(graph.predecessors(center)) | set(graph.successors(center))
            # TCs that have an explicit TESTS or TESTS_COMMAND edge to this cluster.
            # Using direct_nbrs (all edge types) was too broad — it included TCs
            # reachable via REQUIREMENT/SECTION bridges, causing foreign TC bleed.
            tc_direct_tests = {
                src for src, _, edata in graph.in_edges(center, data=True)
                if str(getattr(edata.get("edge_type"), "value", edata.get("edge_type", ""))).lower()
                   in ("tests", "tests_command")
                and nt_cache.get(src, "").upper() == "TEST_CASE"
            }
            cname_lower = center_obj.label.lower()
            _CLUSTER_OWN_TYPES = {"ATTRIBUTE", "COMMAND", "EVENT", "FEATURE",
                                   "REQUIREMENT", "BEHAVIOR_RULE", "TEST_CASE"}
            for nid, ndata in graph.nodes(data=True):
                obj = ndata.get("obj")
                if obj is None:
                    continue
                nt = getattr(getattr(obj, "node_type", None), "name", "")
                if nt in _CLUSTER_OWN_TYPES:
                    node_cluster = str(obj.properties.get("cluster", "")).lower()
                    if node_cluster and cname_lower in node_cluster:
                        candidate_ids.add(nid)
                elif nt == "CLUSTER" and nid != center and nid not in direct_nbrs:
                    # Remove non-adjacent foreign CLUSTERs (noise via shared TCs)
                    candidate_ids.discard(nid)
            # Remove foreign TEST_CASE nodes — TCs whose primary cluster doesn't match
            # the center arrive via TESTS edges (related_clusters) or REQUIREMENT bridges.
            # The viz should show this cluster's own TCs only; cross-cluster TCs are
            # listed on the /cluster/ detail page instead.
            def _tc_primary_cluster(nid: str) -> str:
                obj = graph.nodes.get(nid, {}).get("obj")
                return str(obj.properties.get("cluster", "")).lower() if obj else ""

            candidate_ids = {
                nid for nid in candidate_ids
                if nt_cache.get(nid, "").upper() != "TEST_CASE"
                or cname_lower in _tc_primary_cluster(nid)
                # Also keep foreign TCs with an explicit TESTS edge to this cluster —
                # they genuinely exercise it (e.g. call OpenCommissioningWindow).
                or nid in tc_direct_tests
            }
        candidate_ids = list(candidate_ids)
    else:
        is_cluster_center = False
        # Use cached sorted list; when node_type filter is set, include SECTION nodes too
        if node_type and node_type.upper() == "SECTION":
            candidate_ids = sorted(graph.nodes, key=lambda n: graph.degree(n), reverse=True)
        else:
            candidate_ids = _state._kg_degree_sorted[source_key]

    # --- apply node_type / cluster filters ---
    # When centered on a CLUSTER node, bypass the vis limit so the property scan's
    # complete set of TCs / attributes / etc. is never truncated.
    effective_limit = 2000 if is_cluster_center else limit

    selected: list[str] = []
    total_matching = 0  # count of candidates passing filters (before limit)
    for nid in candidate_ids:
        nt = nt_cache.get(nid, "")
        if node_type and nt.upper() != node_type.upper():
            continue
        if cluster:
            # Cheap check: only call _ndata_to_dict when cluster filter is active
            ndata = graph.nodes.get(nid, {})
            nd = _ndata_to_dict(nid, ndata)
            lbl = nd.get("label", "").lower()
            cid = nid.lower()
            props_cluster = nd.get("properties", {}).get("cluster", "").lower()
            if cluster.lower() not in lbl and cluster.lower() not in cid and cluster.lower() not in props_cluster:
                continue
        total_matching += 1
        if len(selected) < effective_limit:
            selected.append(nid)

    # --- neighbor expansion: include direct neighbors of CLUSTER nodes ---
    # Capped at `limit` additional nodes (prefer highest-degree neighbors) to prevent
    # blowup when many CLUSTERs are in the selection — each cluster can have 50-200 children.
    if not center:
        selected_set: set[str] = set(selected)
        expansion_candidates: List[tuple] = []
        for nid in list(selected):
            if nt_cache.get(nid, "").upper() == "CLUSTER":
                for nbr in list(graph.predecessors(nid)) + list(graph.successors(nid)):
                    if nbr not in selected_set:
                        expansion_candidates.append((graph.degree(nbr), nbr))
                        selected_set.add(nbr)  # deduplicate
        # Sort by degree and take up to `limit` expansion nodes
        expansion_candidates.sort(reverse=True)
        selected = selected + [nbr for _, nbr in expansion_candidates[:limit]]

    selected_set = set(selected)

    # --- NodeType → color map (vis.js color strings) ---
    _COLOR = {
        "CLUSTER":       "#00bcd4",   # cyan
        "ATTRIBUTE":     "#ce93d8",   # violet
        "COMMAND":       "#ffb74d",   # amber
        "EVENT":         "#ef9a9a",   # red-200
        "FEATURE":       "#80cbc4",   # teal
        "REQUIREMENT":   "#fff176",   # yellow
        "BEHAVIOR_RULE": "#ffe082",   # amber-light
        "TEST_CASE":     "#a5d6a7",   # green
        "SECTION":       "#90caf9",   # blue-200
        "PR_CHANGE":     "#f48fb1",   # pink
    }

    vis_nodes = []
    for nid in selected:
        ndata = graph.nodes.get(nid, {})
        nd = _ndata_to_dict(nid, ndata)
        nt = nd.get("node_type", "SECTION")
        deg = graph.degree(nid)
        props = nd.get("properties", {})
        # Pick the best content field for display: purpose → normative_text → content
        display_content = (
            props.get("purpose") or props.get("normative_text") or props.get("content") or ""
        )
        intents = props.get("intents", [])
        vis_nodes.append({
            "id":       nid,
            "label":    nd.get("label", nid)[:60],
            "color":    _COLOR.get(nt.upper(), "#b0bec5"),
            "size":     max(8, min(30, 8 + deg)),
            "node_type": nt,
            "cluster":  props.get("cluster", ""),
            "tc_id":    props.get("tc_id", ""),
            "purpose":  display_content[:200],
            "intents":  ", ".join(intents) if isinstance(intents, list) else str(intents or ""),
            "content":  display_content[:200],
        })

    # --- edges: only include edges where both endpoints are in selected set ---
    vis_edges = []
    seen_edges: set[tuple] = set()
    for src, dst, edata in graph.edges(data=True):
        if src not in selected_set or dst not in selected_set:
            continue
        key = (src, dst)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        et = edata.get("edge_type", "")
        et_str = et.value if hasattr(et, "value") else str(et)
        edge_obj: dict = {
            "from":  src,
            "to":    dst,
            "label": et_str,
            "arrows": "to",
        }
        # Highlight cross-cluster dependency edges so they stand out
        if et_str == "depends_on":
            edge_obj["color"] = {"color": "#ff5252", "highlight": "#ff1744"}
            edge_obj["dashes"] = True
            edge_obj["width"] = 2
        vis_edges.append(edge_obj)

    return {"nodes": vis_nodes, "edges": vis_edges, "center": center, "total": total_matching}


# ---------------------------------------------------------------------------
# GET /kg/viz  — interactive vis.js visualization page
# ---------------------------------------------------------------------------

_KG_VIZ_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Matter KG Visualizer</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: monospace; background: #1a1a2e; color: #e0e0e0; display: flex;
         flex-direction: column; height: 100vh; overflow: hidden; }
  #toolbar { display: flex; gap: 10px; padding: 12px 16px; background: #16213e;
             border-bottom: 2px solid #0f3460; flex-wrap: wrap; align-items: center; }
  #toolbar label { font-size: 14px; color: #90caf9; font-weight: bold; }
  #toolbar input, #toolbar select { background: #0f3460; color: #e0e0e0;
             border: 1px solid #1a4a8a; border-radius: 4px; padding: 5px 8px;
             font-family: monospace; font-size: 14px; }
  #toolbar button { background: #0f3460; color: #90caf9; border: 1px solid #1a4a8a;
             border-radius: 4px; padding: 6px 14px; cursor: pointer; font-size: 14px; font-weight: bold; }
  #toolbar button:hover { background: #1a4a8a; }
  #main { display: flex; flex: 1; overflow: hidden; }
  #network { flex: 1; }

  /* ── Sidebar ── */
  #sidebar { width: 340px; background: #16213e; border-left: 1px solid #0f3460;
             display: flex; flex-direction: column; overflow: hidden; font-size: 12px; }
  .sb-section { display: flex; flex-direction: column; overflow: hidden; }
  .sb-section.grow { flex: 1; min-height: 0; }
  .sb-header { color: #90caf9; font-size: 12px; font-weight: bold; padding: 7px 10px 5px;
               border-bottom: 1px solid #0f3460; display: flex; align-items: center;
               justify-content: space-between; flex-shrink: 0; }
  .sb-header .sb-search { background: #0f3460; border: 1px solid #1a4a8a; border-radius: 3px;
               color: #e0e0e0; font-family: monospace; font-size: 11px; padding: 2px 6px;
               width: 130px; }
  .sb-divider { border: none; border-top: 1px solid #0f3460; flex-shrink: 0; }

  /* ── Node tree ── */
  #node-tree { overflow-y: auto; flex: 1; padding: 4px 0; }
  .tree-group { margin-bottom: 1px; }
  .tree-group-header { display: flex; align-items: center; gap: 5px; padding: 5px 8px;
               cursor: pointer; user-select: none; border-radius: 3px; }
  .tree-group-header:hover { background: #1a3050; }
  .tree-arrow { font-size: 9px; color: #555; width: 10px; flex-shrink: 0;
               transition: transform 0.15s; display: inline-block; }
  .tree-arrow.open { transform: rotate(90deg); color: #90caf9; }
  .leg-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .tree-type-name { flex: 1; }
  .tree-count { color: #556; font-size: 11px; }
  .tree-group-body { display: none; padding: 2px 0 2px 24px; }
  .tree-group-body.open { display: block; }
  .tree-node-row { padding: 3px 8px 3px 4px; border-radius: 3px; cursor: pointer;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
               display: block; }
  .tree-node-row:hover { background: #1a3050; color: #e0e0e0; }
  .tree-node-row.selected { background: #0d3060; color: #90caf9; border-left: 2px solid #90caf9;
               padding-left: 2px; }
  .tree-empty { color: #444; font-size: 11px; padding: 6px 10px; font-style: italic; }

  /* ── Detail panel ── */
  #detail-section { flex-shrink: 0; max-height: 42%; overflow-y: auto; }
  .prop { margin-bottom: 4px; padding: 0 10px; }
  .prop .k { color: #80cbc4; }
  .prop .v { color: #e0e0e0; word-break: break-all; }
  #detail-title { padding: 7px 10px 4px; }
  #detail { padding-bottom: 8px; }

  /* ── Legend strip ── */
  #legend-strip { display: flex; flex-wrap: wrap; gap: 4px; padding: 5px 8px;
               border-bottom: 1px solid #0f3460; flex-shrink: 0; }
  .leg { display: flex; align-items: center; gap: 4px; font-size: 10px; color: #aaa;
         padding: 3px 7px; border-radius: 4px; cursor: pointer; user-select: none;
         border: 1px solid transparent; transition: background 0.15s, border-color 0.15s; }
  .leg:hover { background: rgba(255,255,255,0.07); color: #e0e0e0; }
  .leg.active { background: rgba(255,255,255,0.13); border-color: rgba(255,255,255,0.25); color: #fff; }
  .leg-label { font-weight: 500; }

  #status { font-size: 11px; color: #aaa; padding-left: 8px; }
  #node-count { color: #80cbc4; font-size: 12px; margin-left: 4px; }
</style>
</head>
<body>
<div id="toolbar">
  <label>KG:</label>
  <select id="kgsource" onchange="load()">
    <option value="test_plan">test_plan</option>
    <option value="spec">spec</option>
    <option value="data_model">data_model</option>
    <option value="merged">merged (all — large)</option>
  </select>
  <label>Center node:</label>
  <input id="center" type="text" placeholder="node_id or label" size="28">
  <label>Hops:</label>
  <select id="hops"><option>1</option><option selected>2</option><option>3</option></select>
  <label>Type:</label>
  <select id="ntype">
    <option value="">All</option>
    <option>CLUSTER</option><option>ATTRIBUTE</option><option>COMMAND</option>
    <option>EVENT</option><option>FEATURE</option><option>REQUIREMENT</option>
    <option>BEHAVIOR_RULE</option><option>TEST_CASE</option><option>SECTION</option>
    <option>PR_CHANGE</option>
  </select>
  <label>Cluster:</label>
  <input id="cluster" type="text" placeholder="e.g. OnOff" size="14">
  <label>Limit:</label>
  <input id="limit" type="number" value="150" min="10" max="2000" size="6">
  <span id="node-count"></span>
  <button onclick="load()">Load</button>
  <button onclick="resetView()">Fit</button>
  <button id="physbtn" onclick="togglePhysics()">Physics ON</button>
  <span id="status">—</span>
</div>
<div id="main">
  <div id="network"></div>
  <div id="sidebar">

    <!-- Legend strip -->
    <div id="legend-strip"></div>

    <!-- Node browser -->
    <div class="sb-section grow">
      <div class="sb-header">
        Node Browser
        <input class="sb-search" id="tree-search" type="text" placeholder="filter nodes…"
               oninput="filterTree(this.value)" autocomplete="off">
      </div>
      <div id="node-tree"><div class="tree-empty">Load a graph to browse nodes.</div></div>
    </div>

    <hr class="sb-divider">

    <!-- Detail panel -->
    <div class="sb-section" id="detail-section">
      <div class="sb-header" id="detail-title">Click a node to inspect</div>
      <div id="detail"></div>
    </div>

  </div>
</div>
<script>
const COLORS = {
  CLUSTER:"#00bcd4", ATTRIBUTE:"#ce93d8", COMMAND:"#ffb74d",
  EVENT:"#ef9a9a", FEATURE:"#80cbc4", REQUIREMENT:"#fff176",
  BEHAVIOR_RULE:"#ffe082", TEST_CASE:"#a5d6a7", SECTION:"#90caf9",
  PR_CHANGE:"#f48fb1"
};
const TYPE_ORDER = ["CLUSTER","ATTRIBUTE","COMMAND","EVENT","FEATURE",
                    "REQUIREMENT","BEHAVIOR_RULE","TEST_CASE","SECTION","PR_CHANGE"];

function esc(s) {
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// Build legend strip — each item is clickable to filter by type
const ls = document.getElementById("legend-strip");
TYPE_ORDER.forEach(k => {
  const c = COLORS[k] || "#b0bec5";
  const span = document.createElement("span");
  span.className = "leg";
  span.dataset.type = k;
  span.title = `Click to filter graph by ${k}`;
  span.innerHTML = `<span class="leg-dot" style="background:${c}"></span><span class="leg-label">${k}</span>`;
  span.addEventListener("click", () => filterByType(k));
  ls.appendChild(span);
});

function filterByType(type) {
  const ntype = document.getElementById("ntype");
  // Toggle: clicking the active type clears the filter
  if (ntype.value === type) {
    ntype.value = "";
  } else {
    ntype.value = type;
  }
  _syncLegendActive();
  load().then(() => {
    if (ntype.value) scrollToTypeInTree(ntype.value);
  });
}

function _syncLegendActive() {
  const active = document.getElementById("ntype").value;
  document.querySelectorAll("#legend-strip .leg").forEach(l => {
    l.classList.toggle("active", l.dataset.type === active);
  });
}

function scrollToTypeInTree(type) {
  const group = document.querySelector(`.tree-group[data-type="${CSS.escape(type)}"]`);
  if (!group) return;
  const body = group.querySelector(".tree-group-body");
  const arrow = group.querySelector(".tree-arrow");
  if (body && !body.classList.contains("open")) {
    body.classList.add("open");
    if (arrow) arrow.classList.add("open");
  }
  group.scrollIntoView({ block: "start", behavior: "smooth" });
}

// DOM element tooltip
function makeTooltip(n) {
  const el = document.createElement("div");
  el.style.cssText = "background:#1e2d4e;padding:6px 10px;border-radius:4px;font-size:12px;" +
                     "color:#e0e0e0;max-width:320px;line-height:1.5";
  el.innerHTML =
    `<b style="color:#90caf9">${esc(n.label)}</b><br>` +
    `<span style="color:#80cbc4">type:</span> ${esc(n.node_type)}&nbsp;&nbsp;` +
    `<span style="color:#80cbc4">deg:</span> ${n.size}<br>` +
    (n.cluster ? `<span style="color:#80cbc4">cluster:</span> ${esc(n.cluster)}<br>` : "") +
    (n.tc_id && n.tc_id !== n.label ? `<span style="color:#80cbc4">tc_id:</span> ${esc(n.tc_id)}<br>` : "") +
    `<span style="color:#666;font-size:10px">${esc(n.id)}</span>`;
  return el;
}

// ── vis.js network ────────────────────────────────────────────────────────────
const container = document.getElementById("network");
const options = {
  physics: { enabled: false, stabilization: { iterations: 300, fit: true },
             barnesHut: { gravitationalConstant: -18000, springLength: 220, springConstant: 0.04,
                          damping: 0.3, avoidOverlap: 0.5 } },
  nodes: { shape: "dot", font: { color: "#e0e0e0", size: 12,
             background: "rgba(26,26,46,0.75)", strokeWidth: 0 }, borderWidth: 1 },
  edges: { color: { color: "#334" }, font: { color: "#888", size: 9 },
           smooth: { type: "dynamic" } },
  interaction: { hover: true, tooltipDelay: 200 },
  layout: { improvedLayout: true, randomSeed: 42 }
};
let network = new vis.Network(container, { nodes: [], edges: [] }, options);
let _physicsOn = false;

function togglePhysics() {
  _physicsOn = !_physicsOn;
  network.setOptions({ physics: { enabled: _physicsOn } });
  document.getElementById("physbtn").textContent = _physicsOn ? "Physics OFF" : "Physics ON";
  document.getElementById("physbtn").style.color = _physicsOn ? "#a5d6a7" : "#90caf9";
}

// ── Detail panel ─────────────────────────────────────────────────────────────
function showDetail(node) {
  document.getElementById("detail-title").textContent = node.label;
  let html =
    `<div class="prop"><span class="k">id: </span><span class="v">${esc(node.id)}</span></div>` +
    `<div class="prop"><span class="k">type: </span><span class="v">${esc(node.node_type)}</span></div>` +
    `<div class="prop"><span class="k">degree: </span><span class="v">${node.size}</span></div>`;
  if (node.cluster) html += `<div class="prop"><span class="k">cluster: </span><span class="v" style="color:#00e5ff">${esc(node.cluster)}</span></div>`;
  if (node.tc_id && node.tc_id !== node.label) html += `<div class="prop"><span class="k">tc_id: </span><span class="v">${esc(node.tc_id)}</span></div>`;
  if (node.intents) html += `<div class="prop"><span class="k">intents: </span><span class="v">${esc(node.intents)}</span></div>`;
  if (node.purpose) html += `<div class="prop" style="margin-top:6px"><span class="k">purpose: </span><br><span class="v">${esc(node.purpose)}</span></div>`;
  document.getElementById("detail").innerHTML = html;
}

// ── Select a node by ID (from tree or graph click) ───────────────────────────
function selectNodeById(nodeId) {
  const node = (window._nodes || []).find(n => n.id === nodeId);
  if (!node) return;

  // Highlight in graph
  network.selectNodes([nodeId]);
  network.focus(nodeId, { scale: 1.5, animation: { duration: 400, easingFunction: "easeInOutQuad" } });

  // Update detail panel
  showDetail(node);

  // Sync tree highlight
  document.querySelectorAll(".tree-node-row.selected").forEach(r => r.classList.remove("selected"));
  // CSS.escape handles special chars in node IDs
  const row = document.querySelector(`.tree-node-row[data-nid="${CSS.escape(nodeId)}"]`);
  if (row) {
    row.classList.add("selected");
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    // Make sure the parent group is open
    const body = row.closest(".tree-group-body");
    if (body && !body.classList.contains("open")) {
      body.classList.add("open");
      const arrow = body.previousElementSibling?.querySelector(".tree-arrow");
      if (arrow) arrow.classList.add("open");
    }
  }
}

// Click on graph node → select + show in tree
network.on("click", function(params) {
  if (!params.nodes.length) return;
  selectNodeById(params.nodes[0]);
});

// ── Build node browser tree ───────────────────────────────────────────────────
function buildTree(nodes, autoExpand) {
  const groups = {};
  nodes.forEach(n => { (groups[n.node_type] = groups[n.node_type] || []).push(n); });

  const tree = document.getElementById("node-tree");
  tree.innerHTML = "";

  if (!nodes.length) {
    tree.innerHTML = "<div class='tree-empty'>No nodes loaded.</div>";
    return;
  }

  TYPE_ORDER.forEach(type => {
    const typeNodes = groups[type];
    if (!typeNodes || typeNodes.length === 0) return;
    typeNodes.sort((a, b) => a.label.localeCompare(b.label));

    const color = COLORS[type] || "#b0bec5";
    const groupDiv = document.createElement("div");
    groupDiv.className = "tree-group";
    groupDiv.dataset.type = type;

    // Group header — click toggles expansion
    const header = document.createElement("div");
    header.className = "tree-group-header";
    header.innerHTML =
      `<span class="tree-arrow">▶</span>` +
      `<span class="leg-dot" style="background:${color}"></span>` +
      `<span class="tree-type-name">${esc(type)}</span>` +
      `<span class="tree-count">${typeNodes.length}</span>`;

    const body = document.createElement("div");
    body.className = "tree-group-body";

    header.addEventListener("click", () => {
      const open = body.classList.toggle("open");
      header.querySelector(".tree-arrow").classList.toggle("open", open);
    });

    // Node rows
    typeNodes.forEach(n => {
      const row = document.createElement("div");
      row.className = "tree-node-row";
      row.dataset.nid = n.id;
      row.dataset.label = n.label.toLowerCase();
      row.textContent = n.label;
      row.title = n.id;
      row.addEventListener("click", () => selectNodeById(n.id));
      body.appendChild(row);
    });

    groupDiv.appendChild(header);
    groupDiv.appendChild(body);
    tree.appendChild(groupDiv);
  });

  // Auto-expand: always when in center mode or total nodes ≤ 80; always when only 1 group
  const groupCount = tree.querySelectorAll(".tree-group").length;
  if (autoExpand || nodes.length <= 80 || groupCount === 1) {
    tree.querySelectorAll(".tree-group-body").forEach(b => b.classList.add("open"));
    tree.querySelectorAll(".tree-arrow").forEach(a => a.classList.add("open"));
  }
}

// ── Filter tree by search text ───────────────────────────────────────────────
function filterTree(q) {
  q = q.toLowerCase().trim();
  document.querySelectorAll(".tree-group").forEach(group => {
    let visibleInGroup = 0;
    group.querySelectorAll(".tree-node-row").forEach(row => {
      const match = !q || row.dataset.label.includes(q);
      row.style.display = match ? "" : "none";
      if (match) visibleInGroup++;
    });
    // Show/hide the whole group; auto-expand when filtering
    group.style.display = visibleInGroup === 0 ? "none" : "";
    if (q && visibleInGroup > 0) {
      const body = group.querySelector(".tree-group-body");
      const arrow = group.querySelector(".tree-arrow");
      body.classList.add("open");
      arrow.classList.add("open");
    }
  });
}

// ── Load graph data ───────────────────────────────────────────────────────────
async function load() {
  const kgsource = document.getElementById("kgsource").value;
  const center   = document.getElementById("center").value.trim();
  const hops     = document.getElementById("hops").value;
  const ntype    = document.getElementById("ntype").value;
  const cluster  = document.getElementById("cluster").value.trim();
  const limit    = document.getElementById("limit").value;

  let url = `/kg/graph?hops=${hops}&limit=${limit}&source=${encodeURIComponent(kgsource)}`;
  if (center)  url += `&center=${encodeURIComponent(center)}`;
  if (ntype)   url += `&node_type=${encodeURIComponent(ntype)}`;
  if (cluster) url += `&cluster=${encodeURIComponent(cluster)}`;

  document.getElementById("status").textContent = "Loading…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || resp.statusText); }
    const data = await resp.json();
    window._nodes = data.nodes;

    const visNodes = data.nodes.map(n => ({
      id: n.id, label: n.label,
      title: makeTooltip(n),
      color: { background: COLORS[n.node_type] || "#b0bec5", border: "#555" },
      size: n.size,
    }));
    const visEdges = data.edges.map((e,i) => ({ ...e, id: `e${i}` }));

    network.setData({ nodes: new vis.DataSet(visNodes), edges: new vis.DataSet(visEdges) });
    document.getElementById("node-count").textContent = `/ ${data.total} nodes`;

    let statusText = `${data.nodes.length} nodes, ${data.edges.length} edges` +
      (data.center ? ` (center: ${data.center})` : "");
    if (data.edges.length === 0 && data.nodes.length > 0) {
      statusText += " — no edges (sub-graph only; use 'merged' or set a Center node)";
    }
    document.getElementById("status").textContent = statusText;

    // Build node browser — auto-expand when a center node is set (subgraph view)
    buildTree(data.nodes, !!center);
    // Clear search box and detail panel
    document.getElementById("tree-search").value = "";
    document.getElementById("detail-title").textContent = "Click a node to inspect";
    document.getElementById("detail").innerHTML = "";

    // Sync legend active state after load
    _syncLegendActive();

    // Brief stabilization pass then freeze
    _physicsOn = false;
    document.getElementById("physbtn").textContent = "Physics ON";
    document.getElementById("physbtn").style.color = "#90caf9";
    document.getElementById("status").textContent = "Laying out… " + statusText;
    network.setOptions({ physics: { enabled: true } });
    network.once("stabilizationIterationsDone", function() {
      network.setOptions({ physics: { enabled: false } });
      network.fit();
      document.getElementById("status").textContent = statusText;
    });
  } catch(e) {
    document.getElementById("status").textContent = "Error: " + e.message;
  }
}

function resetView() { network.fit(); }

// ── Init: populate KG selector then auto-load ─────────────────────────────────
async function init() {
  try {
    const resp = await fetch("/kg/list");
    if (resp.ok) {
      const data = await resp.json();
      const sel = document.getElementById("kgsource");
      sel.innerHTML = "";
      const labels = { merged: "merged (all — large)", data_model: "data_model",
                       spec: "spec", test_plan: "test_plan" };
      (data.available || ["merged"]).forEach(src => {
        const opt = document.createElement("option");
        opt.value = src; opt.textContent = labels[src] || src;
        sel.appendChild(opt);
      });
      // Apply source from URL param if present and available
      const availSet = new Set(data.available || []);
      availSet.add("merged"); // merged is always loadable
      if (window._urlSource && availSet.has(window._urlSource)) {
        sel.value = window._urlSource;
        load();
        return;
      }
      const preferred = ["test_plan", "spec", "data_model"];
      const available = data.available || [];
      const pick = preferred.find(s => available.includes(s));
      if (pick) { sel.value = pick; load(); }
      else {
        document.getElementById("status").textContent =
          "Only merged KG available (large). Set a Center node or Type filter, then click Load.";
      }
    } else { load(); }
  } catch(_) { load(); }
}

// Read URL params on page open (support direct links like /kg/viz?center=X&hops=2)
(function applyUrlParams() {
  const p = new URLSearchParams(window.location.search);
  if (p.get("center"))     document.getElementById("center").value  = p.get("center");
  if (p.get("hops"))       document.getElementById("hops").value    = p.get("hops");
  if (p.get("node_type"))  document.getElementById("ntype").value   = p.get("node_type");
  if (p.get("cluster"))    document.getElementById("cluster").value = p.get("cluster");
  if (p.get("limit"))      document.getElementById("limit").value   = p.get("limit");
  if (p.get("source")) {
    const sel = document.getElementById("kgsource");
    // set after init populates options, handled inside init
    window._urlSource = p.get("source");
  } else if (p.get("center")) {
    // Center node specified without an explicit source — default to merged KG so
    // all node types (CLUSTER, ATTRIBUTE, COMMAND, TEST_CASE …) are visible.
    window._urlSource = "merged";
  }
})();

init();
</script>
</body>
</html>
"""


@app.get("/kg/viz", response_class=HTMLResponse, summary="Interactive KG visualization")
def get_kg_viz():
    """Serve an interactive vis.js force-directed graph of the knowledge graph.

    Use the toolbar controls to:
    - Set a **center** node ID (or label substring) and **hops** to explore a neighbourhood
    - Filter by **node type** (CLUSTER, TEST_CASE, …) or **cluster** name
    - Adjust **limit** to control how many nodes are loaded
    - Click any node to inspect its properties in the side panel
    """
    if _state.kg is None:
        return HTMLResponse(
            "<html><body style='font-family:monospace;background:#1a1a2e;color:#ef9a9a;padding:2rem'>"
            "<h2>Knowledge graph not loaded</h2>"
            "<p>Run the pipeline first or check <a href='/health' style='color:#90caf9'>/health</a>.</p>"
            "</body></html>",
            status_code=503,
        )
    return HTMLResponse(_KG_VIZ_HTML)


# ---------------------------------------------------------------------------
# GET /cluster/{name}  — cluster summary: schema + requirements + test cases
# ---------------------------------------------------------------------------

@app.get("/cluster/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/cluster", response_class=HTMLResponse,
         summary="List all Matter clusters in the knowledge graph")
def list_clusters():
    """Directory page: all CLUSTER nodes as clickable links."""
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    graph = _state.kg._graph
    clusters = []
    for nid, ndata in graph.nodes(data=True):
        obj = ndata.get("obj")
        if obj is None:
            continue
        nt = obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type)
        if nt == "CLUSTER":
            attrs_count = sum(
                1 for _, tgt, _ in graph.out_edges(obj.node_id, data=True)
                if (graph.nodes.get(tgt, {}).get("obj") and
                    (lambda o: o.node_type.name if hasattr(o.node_type, "name") else str(o.node_type))(
                        graph.nodes[tgt]["obj"]) == "ATTRIBUTE")
            )
            tc_count = sum(
                1 for _, ndata2 in graph.nodes(data=True)
                if ndata2.get("obj") and
                   (lambda o: o.node_type.name if hasattr(o.node_type, "name") else str(o.node_type))(ndata2["obj"]) == "TEST_CASE" and
                   obj.label.lower() in str(ndata2["obj"].properties.get("cluster", "")).lower()
            )
            clusters.append({
                "name": obj.label,
                "node_id": obj.node_id,
                "code": obj.properties.get("code", ""),
                "revision": obj.properties.get("revision", ""),
                "pics_code": obj.properties.get("pics_code", ""),
                "url": f"/cluster/{obj.label}",
            })

    clusters.sort(key=lambda c: c["name"])

    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    rows_html = ""
    for c in clusters:
        code_s    = _esc(c["code"])
        rev_s     = _esc(c["revision"])
        pics_s    = _esc(c["pics_code"])
        name_esc  = _esc(c["name"])
        url_esc   = _esc(c["url"])
        node_id_esc = _esc(c["node_id"])
        viz_url   = _esc(f"/kg/viz?center={c['node_id']}&hops=2")
        rows_html += f"""
        <tr>
          <td><a href="{url_esc}" class="cl-link">{name_esc}</a></td>
          <td class="mono muted">{code_s}</td>
          <td class="mono muted">{pics_s}</td>
          <td class="muted center">{rev_s}</td>
          <td class="center"><a href="{viz_url}" class="kg-link" title="View in KG graph">⬡</a></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>Matter Clusters // Matter RAG</title>
  <style>
    body  {{ background:#05060a; color:#cdd2e8; font-family:'JetBrains Mono',monospace; margin:0; padding:24px; }}
    h1    {{ color:#00e5ff; margin-bottom:4px; font-size:1.4rem; letter-spacing:.04em; }}
    .sub  {{ color:#50566e; font-size:13px; margin-bottom:20px; }}
    .bar  {{ display:flex; gap:16px; margin-bottom:18px; }}
    .bar a{{ color:#00e5ff; font-size:13px; text-decoration:none; }}
    .bar a:hover{{ text-decoration:underline; }}
    input {{ background:#0c0e14; border:1px solid #1b1e2a; color:#cdd2e8; padding:6px 10px;
             border-radius:4px; font-family:inherit; font-size:13px; width:260px; }}
    input:focus {{ outline:none; border-color:#00e5ff; }}
    table {{ border-collapse:collapse; width:100%; font-size:13px; }}
    th    {{ background:#0c0e14; color:#00e5ff; padding:8px 14px; text-align:left;
             border-bottom:2px solid #1b1e2a; position:sticky; top:0; }}
    td    {{ padding:7px 14px; border-bottom:1px solid #0f1119; }}
    tr:hover td {{ background:#0c0e14; }}
    .cl-link {{ color:#2de08a; text-decoration:none; font-weight:500; }}
    .cl-link:hover {{ text-decoration:underline; color:#00e5ff; }}
    .kg-link {{ color:#00bcd4; text-decoration:none; font-size:16px; }}
    .kg-link:hover {{ color:#00e5ff; }}
    .mono {{ font-family:'JetBrains Mono',monospace; }}
    .muted {{ color:#50566e; }}
    .center {{ text-align:center; }}
    .count  {{ background:#0c0e14; color:#00e5ff; border:1px solid #1b1e2a;
               border-radius:12px; padding:2px 10px; font-size:12px; }}
  </style>
</head>
<body>
  <div class="bar">
    <a href="/">← Dashboard</a>
    <a href="/test-cases">Test Cases</a>
    <a href="/kg/viz">KG Viz ↗</a>
    <a href="/chat">Chat ↗</a>
  </div>
  <h1>Matter Clusters</h1>
  <div class="sub">{len(clusters)} clusters from DM XML &nbsp;·&nbsp;
    click a cluster to view its schema, requirements, and test cases</div>

  <div style="margin-bottom:16px">
    <input id="filter" placeholder="Filter clusters…" oninput="filterTable(this.value)" autofocus/>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th>Cluster Name</th>
        <th>Cluster ID</th>
        <th>PICS Code</th>
        <th style="text-align:center">Revision</th>
        <th style="text-align:center">KG Graph</th>
      </tr>
    </thead>
    <tbody id="tbody">
      {rows_html}
    </tbody>
  </table>

  <script>
    function filterTable(q) {{
      q = q.toLowerCase();
      document.querySelectorAll('#tbody tr').forEach(function(row) {{
        row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/cluster/{cluster_name:path}", response_class=HTMLResponse,
         summary="Cluster summary: DM schema + requirements + test cases")
def get_cluster(
    cluster_name: str,
    format: Optional[str] = Query(None, description="'json' for raw JSON"),
):
    """One-page summary for a Matter cluster.

    Sections
    --------
    * **Schema** — ATTRIBUTE / COMMAND / EVENT / FEATURE nodes from DM XML
    * **Requirements** — REQUIREMENT / BEHAVIOR_RULE nodes linked to this cluster
    * **Test Cases** — TEST_CASE nodes that target this cluster

    Add ``?format=json`` to get raw JSON instead of HTML.
    """
    if _state.kg is None:
        raise HTTPException(status_code=503, detail="Knowledge graph not loaded — check /health")

    if not cluster_name.strip():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/cluster/", status_code=302)

    kg = _state.kg
    graph = kg._graph
    name_q = cluster_name.lower()

    # ── Find the CLUSTER node ────────────────────────────────────────────────
    # Prefer exact (case-insensitive) match to avoid "Basic Information Cluster"
    # accidentally matching "Bridged Device Basic Information Cluster".
    cluster_node = None
    cluster_node_substr = None  # fallback if no exact match
    for nid, ndata in graph.nodes(data=True):
        obj = ndata.get("obj")
        if obj is None:
            continue
        nt = obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type)
        if nt != "CLUSTER":
            continue
        label_lower = obj.label.lower()
        if label_lower == name_q:
            cluster_node = obj
            break
        if cluster_node_substr is None and name_q in label_lower:
            cluster_node_substr = obj
    if cluster_node is None:
        cluster_node = cluster_node_substr

    if cluster_node is None:
        raise HTTPException(
            status_code=404,
            detail=f"No CLUSTER node found matching '{cluster_name}'. "
                   "Check /kg/nodes?node_type=CLUSTER for available names.",
        )

    cname = cluster_node.label
    cluster_id = cluster_node.node_id

    # ── Collect neighbours by type ───────────────────────────────────────────
    attrs, cmds, events, features = [], [], [], []
    requirements, test_cases, cross_cluster_test_cases = [], [], []

    # Walk ALL nodes: match by cluster property or by BELONGS_TO edge from cluster
    # 1. DM entity nodes linked via BELONGS_TO edges from the cluster node
    for _, tgt, edata in graph.out_edges(cluster_id, data=True):
        tgt_data = graph.nodes.get(tgt, {})
        obj = tgt_data.get("obj")
        if obj is None:
            continue
        nt = obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type)
        if nt == "ATTRIBUTE":
            attrs.append(obj)
        elif nt == "COMMAND":
            cmds.append(obj)
        elif nt == "EVENT":
            events.append(obj)
        elif nt == "FEATURE":
            features.append(obj)

    # 2. REQUIREMENT / BEHAVIOR_RULE and TEST_CASE by cluster property
    cname_lower = cname.lower()
    for nid, ndata in graph.nodes(data=True):
        obj = ndata.get("obj")
        if obj is None:
            continue
        nt = obj.node_type.name if hasattr(obj.node_type, "name") else str(obj.node_type)
        node_cluster = str(obj.properties.get("cluster", "")).lower()
        if nt in ("REQUIREMENT", "BEHAVIOR_RULE"):
            if node_cluster and cname_lower in node_cluster:
                requirements.append(obj)
        elif nt == "TEST_CASE":
            # Primary cluster TCs — shown in main table.
            # Cross-cluster TCs (related_clusters match) — shown in a separate section
            # so the main table stays focused on this cluster's own test cases.
            node_related = [c.lower() for c in obj.properties.get("related_clusters", [])]
            if node_cluster and cname_lower in node_cluster:
                test_cases.append(obj)
            elif any(cname_lower in rc for rc in node_related):
                cross_cluster_test_cases.append(obj)

    # Sort for stable display
    attrs.sort(key=lambda o: o.properties.get("code", o.label))
    cmds.sort(key=lambda o: o.properties.get("code", o.label))
    events.sort(key=lambda o: o.label)
    features.sort(key=lambda o: o.properties.get("feature_bit", o.label))
    test_cases.sort(key=lambda o: o.label)
    cross_cluster_test_cases.sort(key=lambda o: o.label)

    # ── JSON response ────────────────────────────────────────────────────────
    if format == "json":
        def _obj(o):
            return {"node_id": o.node_id, "label": o.label, "properties": o.properties}
        return JSONResponse({
            "cluster": {"node_id": cluster_id, "label": cname,
                        "properties": cluster_node.properties},
            "schema": {
                "attributes":  [_obj(o) for o in attrs],
                "commands":    [_obj(o) for o in cmds],
                "events":      [_obj(o) for o in events],
                "features":    [_obj(o) for o in features],
            },
            "requirements": [_obj(o) for o in requirements],
            "test_cases":   [_obj(o) for o in test_cases],
            "cross_cluster_test_cases": [_obj(o) for o in cross_cluster_test_cases],
        })

    # ── HTML helpers ─────────────────────────────────────────────────────────
    def _esc(s) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _prop(o, *keys, default="—"):
        for k in keys:
            v = o.properties.get(k)
            if v is not None and str(v).strip():
                return _esc(str(v))
        return default

    def _schema_rows(nodes, id_key, extra_key=None, extra_label=None):
        if not nodes:
            return "<tr><td colspan='4' class='muted' style='padding:.8rem 1rem'>None in DM XML</td></tr>"
        rows = ""
        for o in nodes:
            eid = _esc(str(o.properties.get(id_key, "—")))
            extra = _esc(str(o.properties.get(extra_key, "—"))) if extra_key else ""
            conformance = _prop(o, "conformance", "mandatory", default="—")
            access = _prop(o, "access", "rw", default="—")
            rows += f"<tr><td class='mono id-cell'>{eid}</td><td class='ent-name'>{_esc(o.label)}</td>"
            if extra_key:
                rows += f"<td class='muted'>{extra}</td>"
            rows += f"<td class='pill-conf'>{conformance}</td><td class='muted'>{access}</td></tr>"
        return rows

    def _req_rows(nodes):
        if not nodes:
            return "<tr><td colspan='4' class='muted' style='padding:.8rem 1rem'>No requirements found</td></tr>"
        rows = ""
        for o in nodes:
            nt = o.node_type.name if hasattr(o.node_type, "name") else str(o.node_type)
            rtype = _esc(str(o.properties.get("requirement_type", nt)))
            norm = str(o.properties.get("normative_text", o.label))
            ctx  = str(o.properties.get("context_text", ""))
            # prefer context_text when it's longer (normative_text may be a sentence fragment)
            full = ctx if len(ctx) > len(norm) else norm
            text  = _esc(full[:300])
            conf  = o.properties.get("confidence", "")
            conf_s = f"{float(conf):.2f}" if conf != "" else "—"
            section = _esc((o.properties.get("section_path") or "").strip()[:120])
            rows += (f"<tr>"
                     f"<td><span class='pill-req'>{rtype}</span></td>"
                     f"<td style='font-size:11px;color:var(--teal);max-width:220px;word-break:break-word;line-height:1.4'>{section or '—'}</td>"
                     f"<td class='req-text'>{text}</td>"
                     f"<td class='muted center'>{conf_s}</td></tr>")
        return rows

    def _tc_rows(nodes):
        if not nodes:
            return "<tr><td colspan='4' class='muted' style='padding:.8rem 1rem'>No test cases found</td></tr>"
        rows = ""
        for o in nodes:
            tid   = _esc(o.label)
            url   = f"/test-cases/{o.node_id}"
            intents = o.properties.get("intents", [])
            intents_s = _esc(", ".join(intents) if isinstance(intents, list) else str(intents))
            purpose = _esc(str(o.properties.get("purpose", o.properties.get("content", "")))[:150])
            pics = o.properties.get("pics_codes", [])
            pics_s = _esc(", ".join(pics[:5]) + ("…" if len(pics) > 5 else "")) if pics else "—"
            rows += (f"<tr><td><a href='{_esc(url)}' class='tc-link'>{tid}</a></td>"
                     f"<td class='muted'>{intents_s}</td>"
                     f"<td class='purpose'>{purpose}</td>"
                     f"<td class='muted pics'>{pics_s}</td></tr>")
        return rows

    # ── Feature rows (bit / code / name) ─────────────────────────────────────
    def _feat_rows(nodes):
        if not nodes:
            return "<tr><td colspan='5' class='muted' style='padding:.8rem 1rem'>None in DM XML</td></tr>"
        rows = ""
        for o in nodes:
            bit  = _esc(str(o.properties.get("bit", "—")))
            code = _esc(str(o.properties.get("code_short", "—")))
            conf = _esc(str(o.properties.get("conformance", "—")))
            summ = _esc(str(o.properties.get("summary", "")))
            rows += (f"<tr><td class='mono id-cell'>{bit}</td>"
                     f"<td class='mono id-cell'>{code}</td>"
                     f"<td class='ent-name'>{_esc(o.label)}</td>"
                     f"<td class='pill-conf'>{conf}</td>"
                     f"<td class='muted'>{summ}</td></tr>")
        return rows

    attr_rows    = _schema_rows(attrs,    "code",  "datatype",   "Type")
    cmd_rows     = _schema_rows(cmds,     "code",  "direction",  "Direction")
    event_rows   = _schema_rows(events,   "code",  "priority",   "Priority")
    feat_rows    = _feat_rows(features)
    req_rows     = _req_rows(requirements)
    tc_rows      = _tc_rows(test_cases)
    cross_tc_rows = _tc_rows(cross_cluster_test_cases)

    json_url = f"/cluster/{_esc(cluster_name)}?format=json"
    viz_url  = f"/kg/viz?center={_esc(cluster_id)}&hops=2"
    tc_url   = f"/test-cases?cluster={_esc(cname)}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{_esc(cname)} // Matter RAG</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg:#05060a; --surf:#0c0e14; --card:#0f1119; --border:#1b1e2a;
      --text:#cdd2e8; --muted:#50566e; --cyan:#00e5ff; --green:#2de08a;
      --amber:#f5a623; --violet:#a78bfa; --orange:#ff6830; --red:#ef9a9a;
      --teal:#80cbc4; --yellow:#fff176;
    }}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    html{{font-size:14px}}
    body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;min-height:100vh}}
    body::before{{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
      background-image:radial-gradient(circle,#1a1d28 1px,transparent 1px);
      background-size:26px 26px;opacity:.35}}
    body::after{{content:'';position:fixed;top:0;left:0;right:0;height:1px;z-index:200;
      background:linear-gradient(90deg,transparent 0%,var(--cyan) 50%,transparent 100%)}}
    .z1{{position:relative;z-index:1}}
    nav{{position:sticky;top:0;z-index:50;border-bottom:1px solid var(--border);
      background:rgba(5,6,10,.92);backdrop-filter:blur(14px);
      padding:.7rem 2rem;display:flex;align-items:center;gap:10px}}
    .logo-hex{{width:28px;height:28px;background:var(--cyan);clip-path:polygon(50% 0%,93% 25%,93% 75%,50% 100%,7% 75%,7% 25%);
      display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px;color:#05060a}}
    .nav-name{{font-weight:600;color:#fff;font-size:13px}}
    .nav-slash{{color:var(--muted)}}
    .nav-sub{{color:var(--cyan);font-size:12px}}
    .nav-r{{margin-left:auto;display:flex;gap:1.2rem;align-items:center}}
    .nav-a{{font-size:11px;color:var(--muted);text-decoration:none;letter-spacing:.05em}}
    .nav-a:hover{{color:var(--cyan)}}
    .wrap{{max-width:1400px;margin:0 auto;padding:2rem 2rem 5rem}}
    .page-hdr{{margin-bottom:2rem}}
    .eyebrow{{font-size:10px;color:var(--cyan);letter-spacing:.15em;text-transform:uppercase;margin-bottom:.4rem}}
    h1{{font-family:'Syne',sans-serif;font-size:2rem;color:#fff}}
    .sub{{font-size:12px;color:var(--muted);margin-top:.4rem}}
    .stats-bar{{display:flex;gap:2rem;margin:1.2rem 0;flex-wrap:wrap}}
    .stat{{display:flex;flex-direction:column;gap:.2rem}}
    .stat-val{{font-size:1.6rem;font-weight:600;color:var(--cyan);font-family:'Syne',sans-serif}}
    .stat-lbl{{font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}}
    .section{{margin-bottom:2.5rem}}
    .sec-hdr{{display:flex;align-items:center;gap:.8rem;margin-bottom:.8rem;border-bottom:1px solid var(--border);padding-bottom:.5rem}}
    .sec-title{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:600}}
    .sec-count{{font-size:11px;color:var(--muted)}}
    .dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
    .dot-attr{{background:#ce93d8}}.dot-cmd{{background:#ffb74d}}.dot-evt{{background:#ef9a9a}}
    .dot-feat{{background:#80cbc4}}.dot-req{{background:#fff176}}.dot-tc{{background:#2de08a}}
    table{{width:100%;border-collapse:collapse;font-size:12px}}
    th{{text-align:left;padding:.5rem 1rem;border-bottom:1px solid var(--border);
      font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;
      background:var(--surf)}}
    td{{padding:.6rem 1rem;border-bottom:1px solid var(--border);vertical-align:top}}
    tr:hover td{{background:rgba(255,255,255,.025)}}
    .mono{{font-family:'JetBrains Mono',monospace}}
    .id-cell{{color:var(--violet);font-size:11px;white-space:nowrap}}
    .ent-name{{color:var(--text);font-weight:500}}
    .tc-link{{color:var(--cyan);text-decoration:none;font-weight:500}}
    .tc-link:hover{{text-decoration:underline}}
    .pill-conf{{font-size:10px;color:var(--amber);background:rgba(245,166,35,.1);
      padding:.1rem .5rem;border:1px solid rgba(245,166,35,.25)}}
    .pill-req{{font-size:10px;color:var(--yellow);background:rgba(255,241,118,.08);
      padding:.1rem .5rem;border:1px solid rgba(255,241,118,.2);white-space:nowrap}}
    .req-text{{color:#9cdcfe;max-width:600px;word-break:break-word;line-height:1.5}}
    .purpose{{color:#9ea5bf;max-width:420px;word-break:break-word;line-height:1.5}}
    .pics{{font-size:11px;color:var(--muted);max-width:200px;word-break:break-all}}
    .center{{text-align:center}}
    .quick-links{{display:flex;gap:.75rem;flex-wrap:wrap;margin-bottom:2rem}}
    .ql{{padding:.4rem 1rem;font-size:11px;text-decoration:none;border:1px solid var(--border);
      color:var(--muted);letter-spacing:.04em}}
    .ql:hover{{border-color:var(--cyan);color:var(--cyan)}}
    .ql-primary{{border-color:rgba(0,229,255,.35);color:var(--cyan)}}
  </style>
</head>
<body><div class="z1">
<nav>
  <div class="logo-hex">M</div>
  <span class="nav-name">Matter RAG</span>
  <span class="nav-slash">/</span>
  <span class="nav-sub">{_esc(cname)}</span>
  <div class="nav-r">
    <a href="/" class="nav-a">← Dashboard</a>
    <a href="/kg/nodes?node_type=CLUSTER" class="nav-a">All Clusters</a>
    <a href="{json_url}" class="nav-a">JSON&nbsp;↗</a>
    <a href="{viz_url}" class="nav-a">KG Viz&nbsp;↗</a>
    <a href="/chat" class="nav-a">→ CHAT</a>
  </div>
</nav>

<div class="wrap">
  <div class="page-hdr">
    <div class="eyebrow">cluster</div>
    <h1>{_esc(cname)}</h1>
    <p class="sub">{_esc(cluster_node.properties.get("description", cluster_id))}</p>
    <div class="stats-bar">
      <div class="stat"><span class="stat-val">{len(attrs)}</span><span class="stat-lbl">Attributes</span></div>
      <div class="stat"><span class="stat-val">{len(cmds)}</span><span class="stat-lbl">Commands</span></div>
      <div class="stat"><span class="stat-val">{len(events)}</span><span class="stat-lbl">Events</span></div>
      <div class="stat"><span class="stat-val">{len(features)}</span><span class="stat-lbl">Features</span></div>
      <div class="stat"><span class="stat-val">{len(requirements)}</span><span class="stat-lbl">Requirements</span></div>
      <div class="stat"><span class="stat-val" style="color:var(--green)">{len(test_cases)}</span><span class="stat-lbl">Test Cases</span></div>
    </div>
  </div>

  <div class="quick-links">
    <a href="{tc_url}" class="ql ql-primary">View all test cases →</a>
    <a href="{viz_url}" class="ql">KG neighborhood (2-hop) →</a>
    <a href="{json_url}" class="ql">Raw JSON →</a>
  </div>

  <!-- ATTRIBUTES -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-attr"></span>
      <span class="sec-title" style="color:#ce93d8">Attributes</span>
      <span class="sec-count">({len(attrs)})</span>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Conformance</th><th>Access</th></tr></thead>
      <tbody>{attr_rows}</tbody>
    </table>
  </div>

  <!-- COMMANDS -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-cmd"></span>
      <span class="sec-title" style="color:#ffb74d">Commands</span>
      <span class="sec-count">({len(cmds)})</span>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Name</th><th>Direction</th><th>Conformance</th><th>Access</th></tr></thead>
      <tbody>{cmd_rows}</tbody>
    </table>
  </div>

  <!-- EVENTS -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-evt"></span>
      <span class="sec-title" style="color:#ef9a9a">Events</span>
      <span class="sec-count">({len(events)})</span>
    </div>
    <table>
      <thead><tr><th>ID</th><th>Name</th><th>Priority</th><th>Conformance</th><th>Access</th></tr></thead>
      <tbody>{event_rows}</tbody>
    </table>
  </div>

  <!-- FEATURES -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-feat"></span>
      <span class="sec-title" style="color:#80cbc4">Features</span>
      <span class="sec-count">({len(features)})</span>
    </div>
    <table>
      <thead><tr><th>Bit</th><th>Code</th><th>Name</th><th>Conformance</th><th>Summary</th></tr></thead>
      <tbody>{feat_rows}</tbody>
    </table>
  </div>

  <!-- REQUIREMENTS -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-req"></span>
      <span class="sec-title" style="color:#fff176">Spec Requirements</span>
      <span class="sec-count">({len(requirements)})</span>
    </div>
    <table>
      <thead><tr><th>Type</th><th>Spec Section</th><th>Normative Text</th><th style="text-align:center">Confidence</th></tr></thead>
      <tbody>{req_rows}</tbody>
    </table>
  </div>

  <!-- TEST CASES -->
  <div class="section">
    <div class="sec-hdr">
      <span class="dot dot-tc"></span>
      <span class="sec-title" style="color:#2de08a">Test Cases</span>
      <span class="sec-count">({len(test_cases)})</span>
    </div>
    <table>
      <thead><tr><th>TC ID</th><th>Intents</th><th>Purpose</th><th>PICS Codes</th></tr></thead>
      <tbody>{tc_rows}</tbody>
    </table>
  </div>

  <!-- CROSS-CLUSTER TEST CASES -->
  {"" if not cross_cluster_test_cases else f'''
  <div class="section">
    <div class="sec-hdr">
      <span class="dot" style="background:#80cbc4"></span>
      <span class="sec-title" style="color:#80cbc4">Cross-Cluster Test Cases</span>
      <span class="sec-count">({len(cross_cluster_test_cases)})</span>
    </div>
    <p style="font-size:11px;color:var(--muted);margin-bottom:.8rem">
      These test cases belong to another cluster but reference {_esc(cname)} in their steps or prerequisites.
    </p>
    <table>
      <thead><tr><th>TC ID</th><th>Intents</th><th>Purpose</th><th>PICS Codes</th></tr></thead>
      <tbody>{cross_tc_rows}</tbody>
    </table>
  </div>'''}

</div>
</div>
</body>
</html>"""

    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# POST /reload
# ---------------------------------------------------------------------------

@app.post("/reload", summary="Reload config, vector store, and KG from disk")
def reload():
    """Force a reload of all storage components without restarting the server.

    Useful after running the pipeline to see updated indexes immediately.
    """
    _load_stores()
    return ReloadResponse(
        status="ok" if not _state.load_errors else "partial",
        errors=_state.load_errors,
    )
