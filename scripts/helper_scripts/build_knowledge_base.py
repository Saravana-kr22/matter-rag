# -*- coding: utf-8 -*-
"""Standalone script to build (or rebuild) the KnowledgeBase using the new KB pipeline.

Reads sources from sources.json (role="data_model", "spec", "test_plan").
Outputs:
    data/knowledge_graph/knowledge_base.json   — full KB (schema + spec + TCs + graph + chunks)

Usage:
    python scripts/build_knowledge_base.py
    python scripts/build_knowledge_base.py --output data/knowledge_graph/my_kb.json
    python scripts/build_knowledge_base.py --no-spec      # skip spec extraction
    python scripts/build_knowledge_base.py --no-test-plan # skip test plan extraction
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# ── project root on sys.path ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.config_loader import load_config
from src.fetcher.fetcher_registry import load_sources, create_fetcher
from src.knowledge_graph.knowledge_base import KnowledgeBaseBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build_kb")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Matter KnowledgeBase (standalone)")
    parser.add_argument("--output", default="data/knowledge_graph/knowledge_base.json",
                        help="Output path for kb.json (default: data/knowledge_graph/knowledge_base.json)")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Config file (default: config/config.yaml)")
    parser.add_argument("--no-spec", action="store_true",
                        help="Skip spec documents (no REQUIREMENT nodes)")
    parser.add_argument("--no-test-plan", action="store_true",
                        help="Skip test plan documents (no TEST_CASE nodes)")
    parser.add_argument("--no-data-model", action="store_true",
                        help="Skip DM XML documents (no CLUSTER/ATTRIBUTE nodes)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    sources = load_sources("sources.json")

    data_model_docs = []
    spec_docs       = []
    test_plan_docs  = []

    logger.info("════ Fetching sources ════")
    for src in sources:
        role = src.get("role", "")
        src_id = src.get("id", role)
        try:
            fetcher = create_fetcher(src, cfg)
            docs = fetcher.fetch()
            if role == "data_model" and not args.no_data_model:
                data_model_docs.extend(docs)
                logger.info("  [data_model] %s → %d docs", src_id, len(docs))
            elif role == "spec" and not args.no_spec:
                spec_docs.extend(docs)
                logger.info("  [spec]       %s → %d docs", src_id, len(docs))
            elif role == "test_plan" and not args.no_test_plan:
                test_plan_docs.extend(docs)
                logger.info("  [test_plan]  %s → %d docs", src_id, len(docs))
            else:
                logger.debug("  [skip]       %s (role=%s)", src_id, role)
        except Exception as exc:
            logger.warning("  [skip]       %s failed: %s", src_id, exc)

    logger.info(
        "Fetched: %d data_model | %d spec | %d test_plan docs",
        len(data_model_docs), len(spec_docs), len(test_plan_docs),
    )

    t0 = time.time()
    builder = KnowledgeBaseBuilder()
    kb = builder.build(
        data_model_docs=data_model_docs or None,
        spec_docs=spec_docs or None,
        test_plan_docs=test_plan_docs or None,
    )
    elapsed = time.time() - t0

    logger.info("════ Build complete in %.1fs ════", elapsed)
    logger.info("  Clusters:      %d", len(kb.canonical_schema.clusters))
    logger.info("  Entities:      %d", len(kb.canonical_schema.entity_lookup))
    logger.info("  Spec records:  %d", len(kb.spec_records))
    logger.info("  Test cases:    %d", len(kb.test_case_records))
    logger.info("  Graph nodes:   %d", len(kb.graph.nodes))
    logger.info("  Graph edges:   %d", len(kb.graph.edges))
    logger.info("  Vector chunks: %d", len(kb.vector_chunks))

    logger.info("════ Saving to %s ════", args.output)
    output = args.output
    builder.export_json(kb, output)
    logger.info("Saved → %s", output)


if __name__ == "__main__":
    main()
