#!/usr/bin/env python3
"""Launcher for the Matter RAG debug API.

Run from the project root::

    python tests/app/run.py [--host HOST] [--port PORT] [--reload]

Logs
----
All application logs are written to two files under ``logs/``:

* ``logs/app_server.log``   — uvicorn startup/error messages + root logger output
* ``logs/app_access.log``   — one line per HTTP request (method, path, status, ms)
* ``logs/llm_calls.jsonl``  — full prompt + response for every LLM call (existing)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _configure_logging(log_dir: Path) -> None:
    """Set up root logger to write to logs/app_server.log and stdout."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app_server.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)

    # Quieten noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("transformers").setLevel(logging.WARNING)
    logging.getLogger("faiss").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configured — server log: %s", log_path
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Matter RAG Debug API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000, help="Port (default: 9000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()

    log_dir = _PROJECT_ROOT / "logs"
    _configure_logging(log_dir)

    import uvicorn
    uvicorn.run(
        "tests.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        # Forward uvicorn access/error logs into our root logger
        log_config=None,  # disable uvicorn's own logging config so ours takes over
    )


if __name__ == "__main__":
    main()
