"""Pipeline execution layer.

This module provides ``PipelineRunner`` — the single execution entry point for
any client-defined LangGraph graph.

Design
------
``PipelineRunner`` knows nothing about which nodes are in the graph or how they
are connected.  It only:

1. Injects ``run_ctx`` and ``run_dir`` into the initial state.
2. Calls ``graph.invoke(state)``.
3. Extracts a ``PipelineResult`` from the final state.
4. Logs a one-line summary.

The graph topology lives in ``src/engine/graphs/``.  Each client builds its own
graph and hands it to ``PipelineRunner``:

    from src.engine.graphs.cli_graph import build_cli_graph
    from src.engine.pipeline import PipelineRunner
    from src.engine.run_context import create_run_context, set_run_context

    run_ctx = create_run_context("matter_rag_pipeline", run_dir=run_dir)
    token   = set_run_context(run_ctx)
    try:
        runner = PipelineRunner(build_cli_graph(), run_ctx)
        result = runner.run(initial_state)
    finally:
        run_ctx.close()
        _current_run_ctx.reset(token)

Backward compatibility
----------------------
``MatterRAGPipeline`` is kept as a thin wrapper around ``PipelineRunner`` +
``build_cli_graph()`` so that existing code that instantiates
``MatterRAGPipeline(config)`` continues to work without changes.

See ``src/engine/ARCHITECTURE.md`` for the full design rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from src.config.config_loader import AppConfig
from src.engine.nodes import PipelineState
from src.engine.run_context import RunContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Structured result returned by ``PipelineRunner.run()``.

    Fields are populated from the final ``PipelineState``.  Fields not written
    by the graph (e.g. ``report_path`` for the chat graph) are left at their
    default empty values.
    """

    report_path: str = ""
    missing_tests: List[dict] = None        # type: ignore[assignment]
    update_candidates: List[dict] = None    # type: ignore[assignment]
    negative_tests: List[dict] = None       # type: ignore[assignment]
    adoc_output_paths: List[str] = None     # type: ignore[assignment]
    errors: List[str] = None                # type: ignore[assignment]
    num_pr_chunks: int = 0
    num_test_plan_chunks: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    # Chat-path output: plain reply string (empty for CLI runs)
    llm_reply: str = ""

    def __post_init__(self) -> None:
        # Replace None sentinels with empty lists so callers can iterate safely.
        if self.missing_tests is None:
            self.missing_tests = []
        if self.update_candidates is None:
            self.update_candidates = []
        if self.negative_tests is None:
            self.negative_tests = []
        if self.adoc_output_paths is None:
            self.adoc_output_paths = []
        if self.errors is None:
            self.errors = []

    @classmethod
    def from_state(cls, state: PipelineState) -> "PipelineResult":
        """Extract a ``PipelineResult`` from a completed ``PipelineState``."""
        kg = state.get("knowledge_graph")
        return cls(
            report_path=state.get("report_path", ""),
            missing_tests=state.get("missing_tests", []),
            update_candidates=state.get("update_candidates", []),
            negative_tests=state.get("negative_tests", []),
            adoc_output_paths=state.get("adoc_output_paths", []),
            errors=state.get("errors", []),
            num_pr_chunks=len(state.get("pr_chunks", [])),
            num_test_plan_chunks=len(state.get("test_plan_chunks", [])),
            graph_nodes=kg.num_nodes if kg else 0,
            graph_edges=kg.num_edges if kg else 0,
            llm_reply=state.get("llm_reply", ""),
        )


# ---------------------------------------------------------------------------
# PipelineRunner — client-agnostic executor
# ---------------------------------------------------------------------------

class PipelineRunner:
    """Executes any compiled LangGraph graph with a given ``RunContext``.

    Parameters
    ----------
    graph:
        A compiled ``StateGraph`` returned by one of the builder functions in
        ``src/engine/graphs/``.  The runner makes no assumptions about which
        nodes are in the graph.
    run_ctx:
        The ``RunContext`` for this invocation.  Injected into
        ``PipelineState["run_ctx"]`` before the graph is invoked so that every
        node can read the client identity and log directory.

    Usage
    -----
    ::

        runner = PipelineRunner(build_cli_graph(), run_ctx)
        result = runner.run({
            "config": config,
            "pr_url":  pr_url,
            "output_dir": "reports/",
            ...
        })
    """

    def __init__(self, graph: Any, run_ctx: RunContext) -> None:
        self._graph = graph
        self._run_ctx = run_ctx

    def run(self, initial_state: PipelineState) -> PipelineResult:
        """Invoke the graph and return a structured result.

        Injects ``run_ctx`` and ``run_dir`` into *initial_state* before
        invocation so clients do not need to set them manually.

        Returns
        -------
        PipelineResult
            Populated from the final state.  Fields not written by this graph's
            nodes are left at their defaults (empty string / empty list).
        """
        # Inject runner-managed fields into the initial state.
        # Callers must not set these themselves — PipelineRunner owns them.
        state: PipelineState = {
            **initial_state,
            "run_ctx": self._run_ctx,
            "run_dir": str(self._run_ctx.run_dir),
        }

        logger.info(
            "[PipelineRunner] starting  client=%s  run_id=%s",
            self._run_ctx.client, self._run_ctx.run_id,
        )

        final_state: PipelineState = self._graph.invoke(state)

        result = PipelineResult.from_state(final_state)

        # Log a one-line summary regardless of which nodes ran.
        errors = result.errors
        logger.info(
            "[PipelineRunner] finished  client=%s  run_id=%s  "
            "nodes_executed=%d  errors=%d  pr_chunks=%d  missing_tests=%d",
            self._run_ctx.client,
            self._run_ctx.run_id,
            len(self._run_ctx.nodes_executed),
            len(errors),
            result.num_pr_chunks,
            len(result.missing_tests),
        )

        return result


# ---------------------------------------------------------------------------
# MatterRAGPipeline — backward-compatible wrapper for the CLI graph
# ---------------------------------------------------------------------------

class MatterRAGPipeline:
    """Backward-compatible wrapper that runs the full 14-node CLI pipeline.

    New code should prefer constructing a ``PipelineRunner`` directly with the
    appropriate graph from ``src/engine/graphs/``.  This class exists so that
    existing scripts and tests that call ``MatterRAGPipeline(config).run(...)``
    continue to work without modification.

    Internally it delegates to::

        PipelineRunner(build_cli_graph(), run_ctx).run(initial_state)
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        # Import here to avoid circular imports at module load time.
        from src.engine.graphs.cli_graph import build_cli_graph
        # Compile once per MatterRAGPipeline instance.
        self._compiled_graph = build_cli_graph()

    # ------------------------------------------------------------------
    # Public API (preserved for backward compatibility)
    # ------------------------------------------------------------------

    def run(
        self,
        pr_url: Optional[str] = None,
        input_doc: Optional[str] = None,
        cluster_filter: str = "",
        test_plan_dir: Optional[str] = None,
        build_test_plan_vectors: bool = False,
        build_knowledge_graph: bool = False,
        build_data_model: bool = False,
        build_knowledge_graph_with_llm: bool = False,
        # Backward-compat aliases kept for existing scripts / CI
        index_only: bool = False,
        compare_only: bool = False,
        output_dir: Optional[str] = None,
        run_label: str = "",
        run_ctx: Optional[RunContext] = None,
        max_pr_chunks: int = 0,
        third_pass_outline_path: str = "",
        generate_negative_tests: bool = False,
        include_coverage_gaps: bool = True,
        pr_snippet: str = "",
    ) -> PipelineResult:
        """Execute the full CLI pipeline and return a structured result.

        All parameters are the same as before.  ``run_ctx`` is optional: when
        omitted, a new ``RunContext`` is created using
        ``configure_pipeline_logging`` (backward-compat behaviour).

        Parameters
        ----------
        pr_url:
            GitHub PR URL to analyse.
        input_doc:
            Path to a local HTML/adoc file used as change input.
            Mutually exclusive with ``pr_url``.
        cluster_filter:
            Limit processing to one cluster (case-insensitive partial match).
            Leave empty (default) to process all clusters.
        build_test_plan_vectors:
            Rebuild and save the test plan vector DB.
        build_knowledge_graph:
            Rebuild and save the knowledge graph.
        build_data_model:
            Re-ingest Matter DM XML schema.
        build_knowledge_graph_with_llm:
            Run LLM-assisted spec refinement after building the KG to add
            cross-cluster DEPENDS_ON and REQ→entity REFERENCES edges.
            Implies ``build_knowledge_graph=True``.
        index_only:
            Backward-compat — implies all build flags = True, no PR required.
        compare_only:
            Backward-compat — implies all build flags = False.
        output_dir:
            Where to write the report (overrides config).
        run_ctx:
            Optional pre-created ``RunContext``.  When provided, ``run_dir``
            inside it is used as the log directory.  When omitted, a new
            RunContext is created via ``configure_pipeline_logging``.
        """
        # Resolve backward-compat aliases.
        if index_only:
            build_test_plan_vectors = True
            build_knowledge_graph = True
            build_data_model = True
        # LLM refinement implies KG build (nothing to refine if not building)
        if build_knowledge_graph_with_llm:
            build_knowledge_graph = True

        # Set up the run context and logging directory.
        if run_ctx is None:
            # Legacy path: create run_dir via configure_pipeline_logging and
            # wrap it in a RunContext so PipelineRunner can inject it.
            from src.logging_config import configure_pipeline_logging
            from src.engine.run_context import RunContext as _RC
            run_dir = configure_pipeline_logging(self.config)
            run_ctx = _RC(
                run_id=run_dir.name,
                run_dir=run_dir,
                client="matter_rag_pipeline",
            )

        # Create a per-run output folder so all reports for this run are grouped together.
        from datetime import datetime as _dt
        import re as _re
        _base_output = Path(output_dir or self.config.pipeline.output_dir)
        _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        if run_label:
            _slug = _re.sub(r"[^a-zA-Z0-9]+", "_", run_label).strip("_")
            _run_output_dir = str(_base_output / f"matter_rag_reports_{_ts}_{_slug}")
        else:
            _run_output_dir = str(_base_output / f"matter_rag_reports_{_ts}")
        Path(_run_output_dir).mkdir(parents=True, exist_ok=True)

        initial_state: PipelineState = {
            "config": self.config,
            "pr_url": pr_url,
            "input_doc": input_doc,
            "cluster_filter": cluster_filter,
            "test_plan_dir": test_plan_dir,
            "build_test_plan_vectors": build_test_plan_vectors,
            "build_knowledge_graph": build_knowledge_graph,
            "build_data_model": build_data_model,
            "build_knowledge_graph_with_llm": build_knowledge_graph_with_llm,
            "output_dir": _run_output_dir,
            "max_pr_chunks": max_pr_chunks,
            "third_pass_outline_path": third_pass_outline_path,
            "generate_negative_tests": generate_negative_tests,
            "include_coverage_gaps": include_coverage_gaps,
            "errors": [],
            "fatal_error": False,
        }

        # When a raw text snippet is supplied, build synthetic pr_chunks directly
        # so chunk_pr_node short-circuits without touching pr_documents.
        if pr_snippet:
            from src.loader.document_loader import Document as _Doc
            import re as _re
            paragraphs = [p.strip() for p in _re.split(r"\n{2,}", pr_snippet) if p.strip()]
            synthetic: list = []
            buf = ""
            for para in paragraphs:
                if len(buf) + len(para) + 2 > 2000 and buf:
                    chunk = _Doc(page_content=buf.strip(), metadata={"doc_type": "pr_change", "source": "pr_snippet"})
                    synthetic.append(chunk)
                    buf = para
                else:
                    buf = (buf + "\n\n" + para).strip() if buf else para
            if buf:
                synthetic.append(_Doc(page_content=buf.strip(), metadata={"doc_type": "pr_change", "source": "pr_snippet"}))
            initial_state["pr_chunks"] = synthetic  # type: ignore[assignment]

        runner = PipelineRunner(self._compiled_graph, run_ctx)
        result = runner.run(initial_state)

        # Remove the output dir if nothing was written (e.g. --index-only runs)
        _out = Path(_run_output_dir)
        try:
            if _out.exists() and not any(_out.iterdir()):
                _out.rmdir()
        except Exception:
            pass

        return result


# ---------------------------------------------------------------------------
# Convenience factory (backward compat)
# ---------------------------------------------------------------------------

def create_pipeline(config_path: str = "config/config.yaml") -> MatterRAGPipeline:
    """Create a ``MatterRAGPipeline`` from a config file path."""
    from src.config.config_loader import load_config
    config = load_config(config_path)
    return MatterRAGPipeline(config)
