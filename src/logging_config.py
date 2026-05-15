"""Logging infrastructure for the Matter RAG pipeline.

Usage (in run_pipeline.py / create_pipeline()):
    from src.logging_config import configure_pipeline_logging
    run_dir = configure_pipeline_logging(config)

Per-run layout:
    logs/
        matter_rag_pipeline_<MMDDYYYY_HHMMSS>/
            master.log            # every log record from every module
            engine.log            # src.engine.*
            fetcher.log           # src.fetcher.*
            processor.log         # src.processor.*
            loader.log            # src.loader.*
            embeddings.log        # src.embeddings.*
            database.log          # src.database.*
            search.log            # src.search.*
            knowledge_graph.log   # src.knowledge_graph.*
            llm.log               # src.llm.*
            config.log            # src.config.*
            document_updater.log  # src.document_updater.*

Custom log level:
    VERBOSE = 5  (below DEBUG=10)
    Enabled when config.logging.level = "VERBOSE"

Decorators:
    @log_node  — wrap a LangGraph node function; logs state diff at VERBOSE
    @log_call  — wrap any callable; logs args + return value at VERBOSE
"""

from __future__ import annotations

import functools
import logging
import reprlib
import time
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.models import AppConfig

# ---------------------------------------------------------------------------
# Custom VERBOSE level
# ---------------------------------------------------------------------------

VERBOSE: int = 5
logging.addLevelName(VERBOSE, "VERBOSE")


def _verbose(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    if self.isEnabledFor(VERBOSE):
        self._log(VERBOSE, message, args, **kwargs)  # type: ignore[attr-defined]


logging.Logger.verbose = _verbose  # type: ignore[attr-defined]


def _resolve_level(level_name: str) -> int:
    """Return numeric log level, accepting 'VERBOSE' as well as stdlib names."""
    if level_name.upper() == "VERBOSE":
        return VERBOSE
    return getattr(logging, level_name.upper(), logging.INFO)


# ---------------------------------------------------------------------------
# Per-module routing map
# ---------------------------------------------------------------------------

_MODULE_FILES: dict[str, str] = {
    "src.engine":           "engine.log",
    "src.fetcher":          "fetcher.log",
    "src.processor":        "processor.log",
    "src.loader":           "loader.log",
    "src.embeddings":       "embeddings.log",
    "src.database":         "database.log",
    "src.search.vector":    "vector_db_search.log",
    "src.search.kg":        "kg_search.log",
    "src.search":           "vector_db_search.log",  # FAISSSearch base class → vector
    "src.knowledge_graph":  "knowledge_graph.log",
    "src.llm":              "llm.log",
    "src.config":           "config.log",
    "src.document_updater": "document_updater.log",
}


# ---------------------------------------------------------------------------
# Main setup function
# ---------------------------------------------------------------------------

def configure_pipeline_logging(
    config: "AppConfig",
    pipeline_name: str = "matter_rag_pipeline",
    run_dir: Optional[Path] = None,
) -> Path:
    """Create per-run log directory and attach file handlers to all module loggers.

    Handlers are only added once per logger — safe to call multiple times.

    Returns:
        Path to the run-specific log directory.
    """
    level = _resolve_level(config.logging.level)
    fmt = logging.Formatter(config.logging.format)

    if run_dir is None:
        run_ts = time.strftime("%m%d%Y_%H%M%S")
        logs_base = Path(getattr(config.pipeline, "logs_dir", "logs") or "logs")
        run_dir = logs_base / f"{pipeline_name}_{run_ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any legacy handlers that were added before configure_pipeline_logging
    # is called (e.g. basicConfig handlers), so we don't get duplicate console output.
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.FileHandler)
    ]

    # ---- master.log — receives every record (via root) ----
    _add_handler(root, run_dir / "master.log", level, fmt)

    # ---- Per-module file handlers ----
    # Modules that need DEBUG level for full result traceability:
    #   src.llm      → full prompt/response text in llm.log
    #   src.database → per-FAISS-result rows in database.log
    #   src.search   → per-vector/KG-hit rows in search.log
    #   src.embeddings → query text in embeddings.log
    _DEBUG_MODULES = {"src.llm", "src.database", "src.search", "src.search.vector", "src.search.kg", "src.embeddings"}
    for logger_name, filename in _MODULE_FILES.items():
        mod_logger = logging.getLogger(logger_name)
        mod_level = logging.DEBUG if logger_name in _DEBUG_MODULES else level
        mod_logger.setLevel(mod_level)
        _add_handler(mod_logger, run_dir / filename, mod_level, fmt)

    # ---- Console — INFO or higher (never spam terminal with VERBOSE/DEBUG) ----
    console_level = max(level, logging.INFO)
    _add_console_handler(root, console_level, fmt)

    return run_dir


def _add_handler(logger: logging.Logger, path: Path, level: int, fmt: logging.Formatter) -> None:
    """Attach a FileHandler to *logger* only if no handler for the same path exists."""
    path_str = str(path)
    for existing in logger.handlers:
        if isinstance(existing, logging.FileHandler) and existing.baseFilename == path_str:
            return  # already attached
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(fmt)
    logger.addHandler(handler)


def _add_console_handler(logger: logging.Logger, level: int, fmt: logging.Formatter) -> None:
    """Attach a StreamHandler to *logger* only if none exists yet."""
    for existing in logger.handlers:
        if isinstance(existing, logging.StreamHandler) and not isinstance(existing, logging.FileHandler):
            existing.setLevel(level)
            return
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(fmt)
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Value summarisation helpers
# ---------------------------------------------------------------------------

_repr = reprlib.Repr()
_repr.maxstring = 120
_repr.maxother = 120
_repr.maxlist = 3
_repr.maxdict = 4
_repr.maxarray = 3


def _summarize_value(value: Any) -> str:
    """Return a short human-readable summary of a value."""
    if value is None:
        return "None"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        keys = list(value.keys())[:4]
        return f"dict({len(value)} keys, first: {keys})"
    if isinstance(value, str):
        snippet = value[:100].replace("\n", "\\n")
        suffix = "…" if len(value) > 100 else ""
        return f"str[{len(value)}]: {snippet!r}{suffix}"
    if isinstance(value, bool):
        return str(value)
    try:
        return _repr.repr(value)
    except Exception:
        return type(value).__name__


def _summarize_state(state: dict) -> str:
    """Return a compact multi-key summary of a state dict for VERBOSE logging."""
    parts = []
    for k, v in state.items():
        if v is None:
            continue
        parts.append(f"{k}={_summarize_value(v)}")
    return "{" + ", ".join(parts) + "}"


def _summarize_state_diff(before: dict, after: dict) -> str:
    """Return a summary of keys present in *after* (the node's return dict)."""
    parts = []
    for k, v in after.items():
        parts.append(f"{k}={_summarize_value(v)}")
    return "{" + ", ".join(parts) + "}"


# ---------------------------------------------------------------------------
# @log_node decorator  (LangGraph node: takes state dict, returns partial update)
# ---------------------------------------------------------------------------

class PipelineFatalError(RuntimeError):
    """Raised by a node to signal an unrecoverable error and halt the pipeline.

    Caught by the ``@log_node`` decorator, which records the error in state and
    sets ``fatal_error=True``.  Every subsequent node is then skipped immediately.
    """


def log_node(func: Callable) -> Callable:
    """Decorator for LangGraph node functions.

    At VERBOSE level, logs:
      - Node entry: keys + value summaries from the incoming state
      - Node exit:  keys + value summaries from the returned update dict
    At INFO level: logs entry/exit with just the node name (timing).

    Fatal-error handling:
      - If ``state["fatal_error"]`` is True the node is skipped (pipeline halted).
      - If the node raises ``PipelineFatalError`` the error is recorded in state,
        ``fatal_error`` is set to True, and the pipeline stops after this node.
    """
    node_name = func.__name__

    @functools.wraps(func)
    def wrapper(state: dict) -> dict:
        mod_logger = logging.getLogger(func.__module__)

        # Record this node in the per-run execution sequence
        from src.engine.run_context import get_run_context as _get_run_ctx
        _run_ctx = _get_run_ctx()
        if _run_ctx is not None:
            _run_ctx.record_node(node_name)

        # Skip node if a previous node already raised a fatal error
        if state.get("fatal_error"):
            mod_logger.warning(
                "[%s] SKIPPED — pipeline halted by earlier fatal error", node_name
            )
            return state

        if mod_logger.isEnabledFor(VERBOSE):
            mod_logger.log(
                VERBOSE,
                "[%s] ENTER — state: %s",
                node_name,
                _summarize_state(state),
            )
        else:
            mod_logger.info("[%s] starting", node_name)

        try:
            result = func(state)
        except PipelineFatalError as exc:
            msg = f"[{node_name}] FATAL: {exc}"
            mod_logger.error(msg)
            errors = list(state.get("errors", []))
            errors.append(msg)
            return {**state, "errors": errors, "fatal_error": True}

        if mod_logger.isEnabledFor(VERBOSE) and result:
            mod_logger.log(
                VERBOSE,
                "[%s] EXIT — updates: %s",
                node_name,
                _summarize_state_diff(state, result),
            )
        else:
            mod_logger.info("[%s] done", node_name)

        return result

    return wrapper


# ---------------------------------------------------------------------------
# @log_call decorator  (general-purpose function tracing)
# ---------------------------------------------------------------------------

def log_call(func: Callable) -> Callable:
    """Decorator for tracing key function calls at VERBOSE level.

    Logs the first args/kwargs and the return value summary.
    Does not log 'self' for methods.
    """
    qual = func.__qualname__

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        mod_logger = logging.getLogger(func.__module__)

        if mod_logger.isEnabledFor(VERBOSE):
            # Skip 'self' / 'cls' for readability
            display_args = args[1:] if args and hasattr(args[0], "__class__") else args
            arg_parts = [_summarize_value(a) for a in display_args]
            arg_parts += [f"{k}={_summarize_value(v)}" for k, v in kwargs.items()]
            mod_logger.log(
                VERBOSE,
                "[%s] CALL — args: (%s)",
                qual,
                ", ".join(arg_parts),
            )

        result = func(*args, **kwargs)

        if mod_logger.isEnabledFor(VERBOSE):
            mod_logger.log(
                VERBOSE,
                "[%s] RETURN — %s",
                qual,
                _summarize_value(result),
            )

        return result

    return wrapper
