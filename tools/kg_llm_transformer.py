#!/usr/bin/env python3
"""
tools/kg_llm_transformer.py
============================
Build a knowledge graph from Matter spec / test-plan documents using
LangChain's LLMGraphTransformer + a **local Ollama LLM**, then optionally
diff the result against the pipeline's rule-based knowledge graph.

This is a standalone utility — it does NOT modify the pipeline's KG store.
Use it to spot-check whether the rule-based KG missed entities or relationships.

Requirements (install once, separate from main pipeline):
    pip install langchain-experimental langchain-ollama

    # If langchain-ollama is unavailable on your Python version:
    pip install langchain-community

Usage examples:
    # Quick test — 20 chunks from a single HTML file
    python tools/kg_llm_transformer.py \\
        --source data/input_doc/appclusters_diff.html \\
        --limit 20

    # Full spec folder with default llama3.2
    python tools/kg_llm_transformer.py \\
        --source data/matter_spec/

    # Use a different model (mistral, llama3.1, gemma2, etc.)
    python tools/kg_llm_transformer.py \\
        --source data/matter_spec/ \\
        --model mistral

    # Extract then diff against the pipeline's KG
    python tools/kg_llm_transformer.py \\
        --source data/matter_spec/ \\
        --compare data/knowledge_graph/matter_kg.json \\
        --output tools/llm_kg_output.json

    # Custom Ollama server URL
    python tools/kg_llm_transformer.py \\
        --source data/matter_spec/ \\
        --ollama-url http://localhost:11434
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def _check_deps() -> None:
    missing = []
    try:
        import langchain_experimental  # noqa: F401
    except ImportError:
        missing.append("langchain-experimental")
    try:
        from langchain_ollama import ChatOllama  # noqa: F401
    except ImportError:
        try:
            from langchain_community.chat_models import ChatOllama  # noqa: F401
        except ImportError:
            missing.append("langchain-ollama  (or langchain-community)")
    if missing:
        print("Missing dependencies. Install with:")
        print(f"    pip install {' '.join(missing)}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Document loading + text parsing
# ---------------------------------------------------------------------------

def _load_documents(source: Path, limit: Optional[int]) -> List[Any]:
    """Load HTML/adoc/txt files and return LangChain Document chunks."""
    from langchain_core.documents import Document as LCDocument

    extensions = {".html", ".htm", ".adoc", ".md", ".txt"}
    files: List[Path] = (
        sorted(p for p in source.rglob("*") if p.suffix.lower() in extensions)
        if source.is_dir()
        else [source]
    )
    if not files:
        logger.error("No supported files found in %s", source)
        sys.exit(1)
    logger.info("Found %d file(s) under %s", len(files), source)

    docs: List[LCDocument] = []
    for fp in files:
        try:
            text = _parse_file(fp)
            if not text.strip():
                continue
            for i, chunk in enumerate(_split_text(text)):
                docs.append(LCDocument(
                    page_content=chunk,
                    metadata={"source": str(fp), "chunk_index": i, "filename": fp.name},
                ))
        except Exception as exc:
            logger.warning("Skipping %s: %s", fp, exc)

    logger.info("Produced %d text chunks from %d file(s)", len(docs), len(files))
    if limit and len(docs) > limit:
        logger.info("Limiting to first %d chunks (--limit %d)", limit, limit)
        docs = docs[:limit]
    return docs


def _parse_file(fp: Path) -> str:
    """Parse a single file to plain text, preferring the pipeline's HTML parser."""
    if fp.suffix.lower() in (".html", ".htm"):
        # Try the pipeline's semantic HTML parser first — it strips navigation,
        # CSS, JS and produces clean structured text.
        try:
            _add_project_root()
            from src.processor.html_semantic_parser import parse_file as _parse_html
            result = _parse_html(fp.read_text(encoding="utf-8", errors="replace"),
                                 doc_id=fp.stem)
            lines: List[str] = []
            # result is a dict (TypedDict) with either "sections" or "test_cases"
            if isinstance(result, dict):
                for sec in result.get("sections", []):
                    if sec.get("heading"):
                        lines.append(f"\n== {sec['heading']} ==")
                    for chunk in sec.get("chunks", []):
                        lines.append(chunk.get("text", ""))
                for tc in result.get("test_cases", []):
                    lines.append(f"\n[{tc.get('tc_id','')}] {tc.get('title','')}")
                    for chunk in tc.get("chunks", []):
                        lines.append(chunk.get("text", ""))
            text = "\n".join(lines)
            if text.strip():
                return text
        except Exception:
            pass  # fall through to BeautifulSoup

        # BeautifulSoup fallback
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(fp.read_bytes(), "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "template"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            pass

    return fp.read_text(encoding="utf-8", errors="replace")


def _add_project_root() -> None:
    """Ensure the project root is on sys.path so pipeline imports work."""
    root = str(Path(__file__).parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def _split_text(text: str, chunk_size: int = 2000, overlap: int = 150) -> List[str]:
    """Simple character splitter with overlap — keeps context across boundaries."""
    if len(text) <= chunk_size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start: start + chunk_size])
        start += chunk_size - overlap
    return chunks


# ---------------------------------------------------------------------------
# LLM graph extraction — allowed Matter ontology
# ---------------------------------------------------------------------------

# Constrain LLMGraphTransformer to the same ontology as the pipeline KG so
# the comparison is apples-to-apples.
MATTER_ALLOWED_NODES = [
    "Cluster",
    "Attribute",
    "Command",
    "Event",
    "Feature",
    "Requirement",
    "BehaviorRule",
    "TestCase",
]

MATTER_ALLOWED_RELATIONSHIPS = [
    "BELONGS_TO",
    "COVERS",
    "TESTS",
    "IMPLEMENTS",
    "VALIDATES",
    "REFERENCES",
    "RELATED_TO",
    "IMPACTS",
]


def _build_llm_graph(docs: List[Any], model: str, ollama_url: str) -> List[Any]:
    """Run LLMGraphTransformer over docs and return a list of GraphDocument objects."""
    from langchain_experimental.graph_transformers import LLMGraphTransformer
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        from langchain_community.chat_models import ChatOllama

    logger.info("Connecting to Ollama model '%s' at %s …", model, ollama_url)
    llm = ChatOllama(model=model, temperature=0, base_url=ollama_url)

    transformer = LLMGraphTransformer(
        llm=llm,
        allowed_nodes=MATTER_ALLOWED_NODES,
        allowed_relationships=MATTER_ALLOWED_RELATIONSHIPS,
        # Ask the LLM to capture a short description for each extracted node.
        node_properties=["description"],
        relationship_properties=[],
    )

    logger.info("Extracting graph from %d chunk(s) — this may take a while …", len(docs))
    graph_docs: List[Any] = []
    for i, doc in enumerate(docs, 1):
        try:
            result = transformer.convert_to_graph_documents([doc])
            graph_docs.extend(result)
            if i % 10 == 0 or i == len(docs):
                n_nodes = sum(len(gd.nodes) for gd in graph_docs)
                n_rels  = sum(len(gd.relationships) for gd in graph_docs)
                logger.info("  [%d/%d] running totals: %d nodes, %d relationships",
                            i, len(docs), n_nodes, n_rels)
        except Exception as exc:
            logger.warning("  [%d/%d] chunk failed (%s): %s",
                           i, len(docs), doc.metadata.get("filename", "?"), exc)

    return graph_docs


# ---------------------------------------------------------------------------
# Serialise GraphDocument list to a JSON-friendly dict
# ---------------------------------------------------------------------------

def _graph_docs_to_dict(graph_docs: List[Any]) -> Dict[str, Any]:
    """Collapse LangChain GraphDocuments into {nodes, edges} ready for JSON export."""
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []

    for gd in graph_docs:
        for node in gd.nodes:
            nid = _slug(node.id)
            if nid not in nodes:
                nodes[nid] = {
                    "id":          nid,
                    "type":        node.type,
                    "label":       node.id,
                    "properties":  node.properties or {},
                    "source_files": [],
                }
            src = gd.source.metadata.get("filename", "")
            if src and src not in nodes[nid]["source_files"]:
                nodes[nid]["source_files"].append(src)

        for rel in gd.relationships:
            edges.append({
                "source": _slug(rel.source.id),
                "target": _slug(rel.target.id),
                "type":   rel.type,
            })

    return {
        "node_count": len(nodes),
        "edge_count":  len(edges),
        "nodes":       list(nodes.values()),
        "edges":       edges,
    }


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", raw.lower().strip()).strip("_")


# ---------------------------------------------------------------------------
# Comparison against the pipeline's rule-based KG
# ---------------------------------------------------------------------------

def _compare_kgs(llm_data: Dict, pipeline_kg_path: Path) -> None:
    """Print a diff summary between the LLM-extracted graph and the pipeline KG."""
    logger.info("Loading pipeline KG from %s …", pipeline_kg_path)
    raw = json.loads(pipeline_kg_path.read_text())

    # Pipeline KG is stored as NetworkX node_link format.
    # Each node dict has keys: id, node_type, label, properties.
    pipeline_nodes: List[Dict] = []
    for n in raw.get("nodes", []):
        nid    = n.get("id", "")
        label  = n.get("label", nid)
        # node_type may be stored as enum value ("Cluster") or name ("CLUSTER")
        ntype  = n.get("node_type", "")
        pipeline_nodes.append({"id": nid, "label": label, "type": ntype})

    pipeline_edges = raw.get("links", raw.get("edges", []))

    llm_nodes = llm_data["nodes"]
    llm_edges = llm_data["edges"]

    pipeline_types = Counter(n["type"] for n in pipeline_nodes)
    llm_types      = Counter(n["type"] for n in llm_nodes)
    all_types      = sorted(set(pipeline_types) | set(llm_types))

    sep = "=" * 72

    print(f"\n{sep}")
    print("  KG CROSS-VERIFICATION REPORT")
    print(f"  Pipeline KG : {pipeline_kg_path}")
    print(f"  LLM model   : (see above)")
    print(sep)

    print(f"\nNode counts")
    print(f"  Pipeline (rule-based) : {len(pipeline_nodes):>6}")
    print(f"  LLM-extracted         : {len(llm_nodes):>6}")

    print(f"\nEdge counts")
    print(f"  Pipeline              : {len(pipeline_edges):>6}")
    print(f"  LLM-extracted         : {len(llm_edges):>6}")

    print(f"\nNode type distribution")
    print(f"  {'Type':<22} {'Pipeline':>10} {'LLM':>10}")
    print(f"  {'-'*22} {'-'*10} {'-'*10}")
    for t in all_types:
        print(f"  {t:<22} {pipeline_types.get(t, 0):>10} {llm_types.get(t, 0):>10}")

    # Normalise labels for comparison (slug both sides)
    pipeline_slugs = {_slug(n["label"]) for n in pipeline_nodes}
    llm_slugs      = {_slug(n["label"]) for n in llm_nodes}

    llm_only = [n for n in llm_nodes if _slug(n["label"]) not in pipeline_slugs]
    llm_only_by_type = Counter(n["type"] for n in llm_only)

    print(f"\nNodes found by LLM but NOT in pipeline KG  ({len(llm_only)} total)")
    for t, c in sorted(llm_only_by_type.items()):
        print(f"  {t}: {c}")
    if llm_only:
        print(f"  Examples (up to 15):")
        for n in llm_only[:15]:
            print(f"    [{n['type']:<14}] {n['label']}")
        if len(llm_only) > 15:
            print(f"    … and {len(llm_only) - 15} more")

    pipeline_only = [n for n in pipeline_nodes if _slug(n["label"]) not in llm_slugs]
    pipeline_only_by_type = Counter(n["type"] for n in pipeline_only)

    print(f"\nNodes in pipeline KG but NOT found by LLM  ({len(pipeline_only)} total)")
    for t, c in sorted(pipeline_only_by_type.items()):
        print(f"  {t}: {c}")
    if pipeline_only:
        print(f"  Examples (up to 15):")
        for n in pipeline_only[:15]:
            print(f"    [{n['type']:<14}] {n['label']}")
        if len(pipeline_only) > 15:
            print(f"    … and {len(pipeline_only) - 15} more")

    # Relationship type distribution
    pipeline_rel_types = Counter(
        e.get("edge_type", e.get("type", "")) for e in pipeline_edges
    )
    llm_rel_types = Counter(e["type"] for e in llm_edges)
    all_rel_types = sorted(set(pipeline_rel_types) | set(llm_rel_types))

    print(f"\nRelationship type distribution")
    print(f"  {'Type':<22} {'Pipeline':>10} {'LLM':>10}")
    print(f"  {'-'*22} {'-'*10} {'-'*10}")
    for t in all_rel_types:
        print(f"  {t:<22} {pipeline_rel_types.get(t, 0):>10} {llm_rel_types.get(t, 0):>10}")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a KG with LLMGraphTransformer + Ollama, "
            "optionally diff against the pipeline KG"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source", required=True,
        help="Directory or single file (HTML / adoc / txt) to extract entities from",
    )
    parser.add_argument(
        "--model", default="llama3.2",
        help="Ollama model name (default: llama3.2). "
             "Other options: mistral, llama3.1, gemma2, phi3, …",
    )
    parser.add_argument(
        "--ollama-url", default="http://localhost:11434",
        help="Ollama server URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--output", default="tools/llm_kg_output.json",
        help="Path to write the extracted KG JSON (default: tools/llm_kg_output.json)",
    )
    parser.add_argument(
        "--compare",
        help="Path to the pipeline KG JSON to diff against "
             "(e.g. data/knowledge_graph/matter_kg.json)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of text chunks to process — useful for quick tests",
    )
    args = parser.parse_args()

    _check_deps()

    source = Path(args.source)
    if not source.exists():
        logger.error("Source path does not exist: %s", source)
        sys.exit(1)

    # 1. Load + chunk documents
    docs = _load_documents(source, limit=args.limit)
    if not docs:
        logger.error("No document chunks produced — nothing to process")
        sys.exit(1)

    # 2. Extract graph via LLMGraphTransformer + Ollama
    graph_docs = _build_llm_graph(docs, model=args.model, ollama_url=args.ollama_url)

    # 3. Serialise to JSON
    llm_data = _graph_docs_to_dict(graph_docs)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(llm_data, indent=2, ensure_ascii=False))
    logger.info(
        "LLM KG written to %s  (%d nodes, %d edges)",
        out_path, llm_data["node_count"], llm_data["edge_count"],
    )

    # 4. Optional comparison
    if args.compare:
        compare_path = Path(args.compare)
        if not compare_path.exists():
            logger.warning("--compare path not found: %s", compare_path)
        else:
            _compare_kgs(llm_data, compare_path)


if __name__ == "__main__":
    main()
