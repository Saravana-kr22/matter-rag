#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export the Matter KG to CSV files for inspection and health-check analysis.

Outputs (written to --output-dir, default: reports/kg_export/):
  nodes.csv        — every node with key properties + computed degree/connectivity fields
  edges.csv        — every edge (source, target, edge_type)
  health_check.csv — nodes with potential structural problems (orphans, missing edges, etc.)

Usage:
  python scripts/export_kg_csv.py
  python scripts/export_kg_csv.py --kg-path data/knowledge_graph/matter_kg.json
  python scripts/export_kg_csv.py --output-dir reports/kg_debug
  python scripts/export_kg_csv.py --node-types CLUSTER TEST_CASE REQUIREMENT  # filter nodes.csv
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return "|".join(str(v) for v in val)
    return str(val)


def _truncate(text: str, n: int = 200) -> str:
    return text[:n] + "…" if len(text) > n else text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_adjacency(edges):
    """Return (out_edges, in_edges) dicts: node_id → list of edge dicts."""
    out_edges: dict[str, list] = defaultdict(list)
    in_edges: dict[str, list] = defaultdict(list)
    for e in edges:
        out_edges[e["source"]].append(e)
        in_edges[e["target"]].append(e)
    return out_edges, in_edges


def export_nodes(nodes, out_edges, in_edges, filter_types: set | None, out_path: Path):
    fieldnames = [
        "node_id", "node_type", "cluster",
        "out_degree", "in_degree",
        "out_edge_types", "in_edge_types",
        # type-specific properties
        "tc_id", "title", "name", "requirement_type", "normative_text",
        "confidence", "doc_type", "source_doc", "datatype", "access",
        "conformance", "default", "code",
        # pre-joined relationship columns
        "covering_tcs",        # REQUIREMENT/BEHAVIOR_RULE: pipe-separated TC IDs that verify this req
        "covered_req_count",   # TEST_CASE: number of requirements this TC verifies
        "covered_reqs",        # TEST_CASE: pipe-separated REQ/BEHAVIOR_RULE IDs verified
        "cluster_attributes",  # CLUSTER: pipe-separated attribute names
        "cluster_commands",    # CLUSTER: pipe-separated command names
        "cluster_tcs",         # CLUSTER: pipe-separated TC IDs for this cluster
        # health flags (computed)
        "flag_no_cluster", "flag_isolated", "flag_no_tc_coverage",
        "flag_no_parent_cluster", "flag_no_verifies_edges",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for n in nodes:
            ntype = n["node_type"]
            if filter_types and ntype not in filter_types:
                continue
            nid = n["id"]
            props = n.get("properties", {})
            outs = out_edges.get(nid, [])
            ins  = in_edges.get(nid, [])

            out_types = sorted({e["edge_type"] for e in outs})
            in_types  = sorted({e["edge_type"] for e in ins})

            # Health flags
            cluster = _str(props.get("cluster", ""))
            flag_no_cluster = "1" if not cluster and ntype not in ("SECTION", "PROTOCOL_AREA") else ""
            flag_isolated   = "1" if not outs and not ins else ""

            # REQUIREMENT/BEHAVIOR_RULE: no TEST_CASE verifying it
            flag_no_tc_coverage = ""
            covering_tcs = ""
            if ntype in ("REQUIREMENT", "BEHAVIOR_RULE"):
                tc_ids = sorted({e["source"] for e in ins if e["edge_type"] == "verifies_requirement"})
                covering_tcs = "|".join(tc_ids)
                flag_no_tc_coverage = "" if tc_ids else "1"

            # ATTRIBUTE / COMMAND / EVENT / FEATURE: no parent CLUSTER edge
            flag_no_parent_cluster = ""
            if ntype in ("ATTRIBUTE", "COMMAND", "EVENT", "FEATURE"):
                parent_edge = "HAS_" + ntype
                has_parent = any(e["edge_type"] == parent_edge for e in ins)
                flag_no_parent_cluster = "" if has_parent else "1"

            # TEST_CASE: no verifies_* outgoing edges; list what it covers
            flag_no_verifies_edges = ""
            covered_reqs = ""
            covered_req_count = ""
            if ntype == "TEST_CASE":
                req_ids = sorted({e["target"] for e in outs if e["edge_type"] == "verifies_requirement"})
                covered_reqs = "|".join(req_ids)
                covered_req_count = str(len(req_ids))
                has_verifies = any(e["edge_type"].startswith("verif") for e in outs)
                flag_no_verifies_edges = "" if has_verifies else "1"

            # CLUSTER: list child attributes, commands, and test cases
            cluster_attributes = ""
            cluster_commands = ""
            cluster_tcs = ""
            if ntype == "CLUSTER":
                cluster_attributes = "|".join(sorted(
                    e["target"].split("::")[-1] for e in outs if e["edge_type"] == "HAS_ATTRIBUTE"
                ))
                cluster_commands = "|".join(sorted(
                    e["target"].split("::")[-1] for e in outs if e["edge_type"] == "HAS_COMMAND"
                ))
                # TCs for this cluster: TEST_CASE nodes whose cluster property matches
                cluster_tcs = "|".join(sorted(
                    e["source"] for e in ins
                    if e["source_type"] == "TEST_CASE" and e["edge_type"] == "belongs_to"
                ))

            w.writerow({
                "node_id":            nid,
                "node_type":          ntype,
                "cluster":            cluster,
                "out_degree":         len(outs),
                "in_degree":          len(ins),
                "out_edge_types":     "|".join(out_types),
                "in_edge_types":      "|".join(in_types),
                "tc_id":              _str(props.get("tc_id")),
                "title":              _truncate(_str(props.get("title", ""))),
                "name":               _str(props.get("name", "")),
                "requirement_type":   _str(props.get("requirement_type", "")),
                "normative_text":     _truncate(_str(props.get("normative_text", ""))),
                "confidence":         _str(props.get("confidence", "")),
                "doc_type":           _str(props.get("doc_type", "")),
                "source_doc":         _str(props.get("source_doc", "")),
                "datatype":           _str(props.get("datatype", "")),
                "access":             _str(props.get("access", "")),
                "conformance":        _str(props.get("conformance", "")),
                "default":            _str(props.get("default", "")),
                "code":               _str(props.get("code", "")),
                "covering_tcs":           covering_tcs,
                "covered_req_count":      covered_req_count,
                "covered_reqs":           covered_reqs,
                "cluster_attributes":     cluster_attributes,
                "cluster_commands":       cluster_commands,
                "cluster_tcs":            cluster_tcs,
                "flag_no_cluster":        flag_no_cluster,
                "flag_isolated":          flag_isolated,
                "flag_no_tc_coverage":    flag_no_tc_coverage,
                "flag_no_parent_cluster": flag_no_parent_cluster,
                "flag_no_verifies_edges": flag_no_verifies_edges,
            })


def export_edges(edges, out_path: Path):
    fieldnames = ["edge_type", "source", "target",
                  "source_type", "target_type"]

    # Build id → type map for source/target enrichment
    # (not available here — enriched in main)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for e in edges:
            w.writerow({
                "edge_type":   e["edge_type"],
                "source":      e["source"],
                "target":      e["target"],
                "source_type": e.get("source_type", ""),
                "target_type": e.get("target_type", ""),
            })


def export_health_check(nodes, out_edges, in_edges, out_path: Path):
    """Write a focused CSV listing only nodes with structural problems."""
    CHECKS = [
        # (flag_field, description)
        ("flag_no_cluster",        "Missing cluster assignment"),
        ("flag_isolated",          "Isolated node — no edges at all"),
        ("flag_no_tc_coverage",    "REQUIREMENT/BEHAVIOR_RULE with no TEST_CASE coverage"),
        ("flag_no_parent_cluster", "ATTRIBUTE/COMMAND/EVENT/FEATURE with no parent CLUSTER edge"),
        ("flag_no_verifies_edges", "TEST_CASE with no verifies_* outgoing edges"),
    ]

    fieldnames = [
        "issue", "node_id", "node_type", "cluster",
        "out_degree", "in_degree",
        "tc_id", "name", "requirement_type", "normative_text",
        "covering_tcs",   # for REQUIREMENT rows: which TCs cover it (empty = gap)
        "covered_reqs",   # for TEST_CASE rows: which REQs it verifies
    ]

    rows = []
    for n in nodes:
        ntype = n["node_type"]
        nid   = n["id"]
        props = n.get("properties", {})
        cluster = _str(props.get("cluster", ""))
        outs = out_edges.get(nid, [])
        ins  = in_edges.get(nid, [])

        # Recompute flags (same logic as export_nodes)
        flags = {}
        flags["flag_no_cluster"] = (
            not cluster and ntype not in ("SECTION", "PROTOCOL_AREA")
        )
        flags["flag_isolated"] = not outs and not ins

        if ntype in ("REQUIREMENT", "BEHAVIOR_RULE"):
            flags["flag_no_tc_coverage"] = not any(
                e["edge_type"] == "verifies_requirement" for e in ins
            )
        else:
            flags["flag_no_tc_coverage"] = False

        if ntype in ("ATTRIBUTE", "COMMAND", "EVENT", "FEATURE"):
            parent_edge = "HAS_" + ntype
            flags["flag_no_parent_cluster"] = not any(
                e["edge_type"] == parent_edge for e in ins
            )
        else:
            flags["flag_no_parent_cluster"] = False

        if ntype == "TEST_CASE":
            flags["flag_no_verifies_edges"] = not any(
                e["edge_type"].startswith("verif") for e in outs
            )
        else:
            flags["flag_no_verifies_edges"] = False

        for flag_key, description in CHECKS:
            if flags.get(flag_key):
                rows.append({
                    "issue":            description,
                    "node_id":          nid,
                    "node_type":        ntype,
                    "cluster":          cluster,
                    "out_degree":       len(outs),
                    "in_degree":        len(ins),
                    "tc_id":            _str(props.get("tc_id", "")),
                    "name":             _str(props.get("name", "")),
                    "requirement_type": _str(props.get("requirement_type", "")),
                    "normative_text":   _truncate(_str(props.get("normative_text", "")), 150),
                    "covering_tcs":     "|".join(sorted(
                        e["source"] for e in ins if e["edge_type"] == "verifies_requirement"
                    )) if ntype in ("REQUIREMENT", "BEHAVIOR_RULE") else "",
                    "covered_reqs":     "|".join(sorted(
                        e["target"] for e in outs if e["edge_type"] == "verifies_requirement"
                    )) if ntype == "TEST_CASE" else "",
                })

    # Sort: issue → cluster → node_type → node_id
    rows.sort(key=lambda r: (r["issue"], r["cluster"], r["node_type"], r["node_id"]))

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    return rows


def print_summary(nodes, edges, health_rows):
    from collections import Counter
    type_counts = Counter(n["node_type"] for n in nodes)
    edge_counts = Counter(e["edge_type"] for e in edges)
    issue_counts = Counter(r["issue"] for r in health_rows)

    print("\n=== KG Export Summary ===")
    print(f"Nodes: {len(nodes):,}  Edges: {len(edges):,}")
    print("\nNode types:")
    for t, c in type_counts.most_common():
        print(f"  {t:<30} {c:>6,}")
    print("\nEdge types (top 15):")
    for t, c in edge_counts.most_common(15):
        print(f"  {t:<40} {c:>7,}")
    print(f"\nHealth issues ({len(health_rows):,} flagged nodes):")
    for issue, count in issue_counts.most_common():
        print(f"  {issue:<55} {count:>5,}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kg-path", default="data/knowledge_graph/matter_kg.json",
                        help="Path to matter_kg.json (default: data/knowledge_graph/matter_kg.json)")
    parser.add_argument("--output-dir", default="reports/kg_export",
                        help="Directory to write CSV files (default: reports/kg_export)")
    parser.add_argument("--node-types", nargs="+",
                        help="Only include these node types in nodes.csv (default: all). "
                             "E.g. --node-types CLUSTER TEST_CASE REQUIREMENT")
    args = parser.parse_args()

    kg_path = Path(args.kg_path)
    if not kg_path.exists():
        print(f"ERROR: KG file not found: {kg_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"Loading KG from {kg_path} …", end=" ", flush=True)
    kg = json.loads(kg_path.read_text())
    nodes = kg["nodes"]
    edges = kg["edges"]
    print(f"{len(nodes):,} nodes, {len(edges):,} edges")

    # Enrich edges with source/target node types for easier filtering
    id_to_type = {n["id"]: n["node_type"] for n in nodes}
    for e in edges:
        e["source_type"] = id_to_type.get(e["source"], "")
        e["target_type"] = id_to_type.get(e["target"], "")

    print("Building adjacency …", end=" ", flush=True)
    out_edges, in_edges = build_adjacency(edges)
    print("done")

    filter_types = set(args.node_types) if args.node_types else None

    nodes_path  = output_dir / f"nodes_{ts}.csv"
    edges_path  = output_dir / f"edges_{ts}.csv"
    health_path = output_dir / f"health_check_{ts}.csv"

    print(f"Writing {nodes_path.name} …", end=" ", flush=True)
    export_nodes(nodes, out_edges, in_edges, filter_types, nodes_path)
    print("done")

    print(f"Writing {edges_path.name} …", end=" ", flush=True)
    export_edges(edges, edges_path)
    print("done")

    print(f"Writing {health_path.name} …", end=" ", flush=True)
    health_rows = export_health_check(nodes, out_edges, in_edges, health_path)
    print("done")

    print_summary(nodes, edges, health_rows)

    print(f"\nFiles written to: {output_dir.resolve()}/")
    print(f"  {nodes_path.name}")
    print(f"  {edges_path.name}")
    print(f"  {health_path.name}")


if __name__ == "__main__":
    main()
