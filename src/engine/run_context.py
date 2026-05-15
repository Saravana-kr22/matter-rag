"""Per-pipeline-run context: log directory, run identity, client name.

Every pipeline invocation — whether from the CLI or a FastAPI chat request — creates
a ``RunContext`` that owns:

* A run directory under ``logs/`` named ``<client>_<timestamp>/``
* Structured per-query JSONL log (``rag_queries.jsonl``)
* Ordered list of nodes executed (``nodes_executed``)

The context is stored in a ``contextvars.ContextVar`` so that concurrent asyncio
tasks (e.g. two simultaneous chat requests) each see their own context without
any global state conflict.

Usage — CLI
-----------
::

    run_ctx = create_run_context("matter_rag_pipeline", run_dir=existing_run_dir)
    token = set_run_context(run_ctx)
    try:
        pipeline.run(...)
    finally:
        run_ctx.close()
        _current_run_ctx.reset(token)

Usage — FastAPI (per request)
------------------------------
::

    run_ctx = create_run_context("app_chat")
    token = set_run_context(run_ctx)
    try:
        reply = await run_pipeline(payload, _state)
    finally:
        run_ctx.close()
        _current_run_ctx.reset(token)

RunAwareFileHandler
-------------------
A single ``RunAwareFileHandler`` instance is added to the *root* logger once at
application startup (FastAPI only — CLI uses ``configure_pipeline_logging``).

For every log record it:

1. Reads the current ``ContextVar`` to find the active ``RunContext``.
2. Matches ``record.name`` against a prefix map to pick the per-module file.
3. Opens (or reuses) a ``FileHandler`` for ``run_dir/<module>.log``.
4. Also always writes to ``run_dir/master.log``.

Because the handler opens files lazily on first use, no I/O happens unless a run
is actually in progress.
"""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    """Encapsulates per-run identity and output paths."""

    run_id: str
    run_dir: Path
    client: str
    nodes_executed: List[str] = field(default_factory=list)

    # ---- derived paths ----

    @property
    def rag_query_log(self) -> Path:
        return self.run_dir / "rag_queries.jsonl"

    # ---- helpers ----

    def record_node(self, node_name: str) -> None:
        """Append *node_name* to the execution sequence (called by @log_node)."""
        self.nodes_executed.append(node_name)

    def log_rag_query(
        self,
        query: str,
        threshold: float,
        vector_results: List[dict],
        kg_results: List[dict],
    ) -> None:
        """Write a structured RAG-query record to ``rag_queries.jsonl``."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "threshold": threshold,
            "vector_hits": len(vector_results),
            "kg_hits": len(kg_results),
            "vector_results": vector_results,
            "kg_results": kg_results,
        }
        with open(self.rag_query_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def close(self) -> None:
        """Flush and close all file handles opened for this run."""
        RunAwareFileHandler.close_run(self.run_id)


# ---------------------------------------------------------------------------
# ContextVar
# ---------------------------------------------------------------------------

_current_run_ctx: ContextVar[Optional[RunContext]] = ContextVar(
    "run_ctx", default=None
)


def create_run_context(
    client: str,
    log_root: Optional[Path] = None,
    run_dir: Optional[Path] = None,
) -> RunContext:
    """Create and return a new ``RunContext``.

    Args:
        client:   Short identifier for the caller, e.g. ``"app_chat"`` or
                  ``"matter_rag_pipeline"``.  Used as the directory name prefix.
        log_root: Root directory for log output.  Defaults to ``logs/`` under
                  the project root (two levels above this file), so the folder
                  always lands in the right place regardless of the working
                  directory the process was started from.
        run_dir:  If provided, use this existing directory instead of creating
                  a new timestamped one.  Used by the CLI so that the run
                  context points at the directory already created by
                  ``configure_pipeline_logging()``.
    """
    if log_root is None:
        # Anchor to project root: src/engine/run_context.py → src/engine → src → project root
        log_root = Path(__file__).resolve().parent.parent.parent / "logs"
    if run_dir is None:
        ts = datetime.now().strftime("%m%d%Y_%H%M%S")
        run_dir = log_root / f"{client}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name  # e.g. "app_chat_04132026_143022"
    return RunContext(run_id=run_id, run_dir=run_dir, client=client)


def set_run_context(ctx: RunContext) -> Token:
    """Store *ctx* in the current async/thread context.

    Returns a ``Token`` that can be passed to ``_current_run_ctx.reset(token)``
    to restore the previous value when the run completes.
    """
    return _current_run_ctx.set(ctx)


def get_run_context() -> Optional[RunContext]:
    """Return the ``RunContext`` for the current async task / thread, or None."""
    return _current_run_ctx.get()


# ---------------------------------------------------------------------------
# RunAwareFileHandler
# ---------------------------------------------------------------------------

# Maps logger name prefixes → per-module log file names.
# Order matters: more-specific prefixes must come first.
_MODULE_FILES: List[tuple[str, str]] = [
    ("src.engine",             "engine.log"),
    ("src.fetcher",            "fetcher.log"),
    ("src.processor",          "processor.log"),
    ("src.loader",             "loader.log"),
    ("src.embeddings",         "embeddings.log"),
    ("src.database",           "database.log"),
    ("src.search.vector",      "vector_db_search.log"),  # more specific first
    ("src.search.kg",          "kg_search.log"),
    ("src.search",             "vector_db_search.log"),  # FAISSSearch itself → vector
    ("src.knowledge_graph",    "knowledge_graph.log"),
    ("src.llm",                "llm.log"),
    ("src.config",             "config.log"),
    ("src.document_updater",   "document_updater.log"),
    ("tests.app",              "app.log"),
]


class RunAwareFileHandler(logging.Handler):
    """Routes log records to the current run's per-module files.

    Add a single instance of this handler to the root logger once at startup::

        handler = RunAwareFileHandler()
        handler.setFormatter(fmt)
        logging.getLogger().addHandler(handler)

    For every record:
    - Looks up the current ``RunContext`` via ContextVar.
    - If no context is set, the record is silently dropped (not a run in progress).
    - Routes the record to a module-specific file *and* to ``master.log``.
    - Opens ``FileHandler`` instances lazily and caches them per run.
    """

    # {run_id: {filename: FileHandler}}
    _open_handles: Dict[str, Dict[str, logging.FileHandler]] = {}
    _lock: threading.Lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        ctx = _current_run_ctx.get()
        if ctx is None:
            return

        target_file = "master.log"
        for prefix, fname in _MODULE_FILES:
            if record.name.startswith(prefix):
                target_file = fname
                break

        try:
            if target_file != "master.log":
                self._get_handle(ctx, target_file).emit(record)
            self._get_handle(ctx, "master.log").emit(record)
        except Exception:
            self.handleError(record)

    def _get_handle(self, ctx: RunContext, filename: str) -> logging.FileHandler:
        with self._lock:
            if ctx.run_id not in self._open_handles:
                self._open_handles[ctx.run_id] = {}
            handles = self._open_handles[ctx.run_id]
            if filename not in handles:
                fh = logging.FileHandler(
                    ctx.run_dir / filename, encoding="utf-8"
                )
                fh.setFormatter(self.formatter)
                handles[filename] = fh
            return handles[filename]

    @classmethod
    def close_run(cls, run_id: str) -> None:
        """Flush and close all file handles for *run_id*."""
        with cls._lock:
            if run_id in cls._open_handles:
                for fh in cls._open_handles.pop(run_id).values():
                    try:
                        fh.flush()
                        fh.close()
                    except Exception:
                        pass
