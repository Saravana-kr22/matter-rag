"""Audit: TC bleed in KG ego-graph visualization.

For every CLUSTER node, builds the 2-hop undirected ego-graph and checks what
fraction of the TEST_CASE nodes in that subgraph actually belong to a different
cluster family.  Reports clusters where foreign-TC % exceeds --threshold (default 5%).

Run from the project root:
    python scripts/audit_kg_viz_tc_bleed.py
    python scripts/audit_kg_viz_tc_bleed.py --threshold 10
    python scripts/audit_kg_viz_tc_bleed.py --kg-path data/knowledge_graph/matter_kg.json
    python scripts/audit_kg_viz_tc_bleed.py --mode both   # compare old vs fixed logic
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_graph(kg_path: str):
    """Load KG JSON → (DiGraph, node_id→node_dict, nt_cache, direct_tests)."""
    import networkx as nx

    raw = json.loads(Path(kg_path).read_text())
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in raw["nodes"]}
    nt_cache: dict[str, str] = {n["id"]: n.get("node_type", "") for n in raw["nodes"]}

    # direct_tests[cluster_id] = set of TC node IDs that have a direct `tests` edge to it
    direct_tests: dict[str, set] = {}
    G = nx.DiGraph()
    for n in raw["nodes"]:
        G.add_node(n["id"])
    for e in raw["edges"]:
        G.add_edge(e["source"], e["target"], edge_type=e.get("edge_type", ""))
        if e.get("edge_type", "").lower() == "tests":
            direct_tests.setdefault(e["target"], set()).add(e["source"])

    return G, nodes_by_id, nt_cache, direct_tests


def _build_filtered_graph_old(G, nt_cache: dict) -> object:
    """Old logic: only strips BELONGS_TO_PROTOCOL_AREA edges."""
    import networkx as nx

    return nx.Graph(
        (u, v, d)
        for u, v, d in G.to_undirected(as_view=True).edges(data=True)
        if d.get("edge_type", "") != "BELONGS_TO_PROTOCOL_AREA"
    )


def _build_filtered_graph_fixed(G, nt_cache: dict, center: str) -> object:
    """Fixed logic: also strips foreign-CLUSTER → non-CLUSTER edges."""
    import networkx as nx

    def _is_cluster(nid: str) -> bool:
        return nt_cache.get(nid, "").upper() == "CLUSTER"

    return nx.Graph(
        (u, v, d)
        for u, v, d in G.to_undirected(as_view=True).edges(data=True)
        if d.get("edge_type", "") != "BELONGS_TO_PROTOCOL_AREA"
        and not (
            (_is_cluster(u) and u != center and not _is_cluster(v))
            or (_is_cluster(v) and v != center and not _is_cluster(u))
        )
    )


def _tc_bleed(subg_nodes: set, center_id: str, center_label: str, nodes_by_id: dict, direct_tests: dict) -> tuple[int, int, list[str]]:
    """Return (total_tcs, foreign_tcs, [foreign_tc_ids]).

    A TC is NOT foreign if it has a direct `tests` edge to the center cluster —
    it's a legitimate multi-cluster TC, not visualization bleed.
    """
    center_lower = center_label.lower()
    center_direct = direct_tests.get(center_id, set())
    total = 0
    foreign = 0
    foreign_ids: list[str] = []

    for nid in subg_nodes:
        node = nodes_by_id.get(nid, {})
        if node.get("node_type") != "TEST_CASE":
            continue
        total += 1
        tc_cluster = node.get("properties", {}).get("cluster", "")
        if tc_cluster and center_lower not in tc_cluster.lower() and nid not in center_direct:
            foreign += 1
            foreign_ids.append(node.get("properties", {}).get("tc_id") or nid)

    return total, foreign, foreign_ids


def audit(kg_path: str, threshold: float, mode: str, hops: int) -> None:
    import networkx as nx

    print(f"Loading KG from {kg_path} …")
    G, nodes_by_id, nt_cache, direct_tests = _load_graph(kg_path)

    cluster_ids = [
        nid for nid, nt in nt_cache.items() if nt.upper() == "CLUSTER"
    ]
    print(f"Found {len(cluster_ids)} CLUSTER nodes  |  threshold={threshold}%  |  hops={hops}\n")

    # Pre-build fixed filtered graph for all clusters (center-independent filtering
    # is handled per-cluster below when mode needs it).

    flagged: list[dict] = []

    for center in sorted(cluster_ids):
        label = nodes_by_id.get(center, {}).get("label", center)

        # ── old logic ──────────────────────────────────────────────────────
        if mode in ("old", "both"):
            fg_old = _build_filtered_graph_old(G, nt_cache)
            if center not in fg_old:
                continue
            subg_old = nx.ego_graph(fg_old, center, radius=hops)
            total_old, foreign_old, ids_old = _tc_bleed(set(subg_old.nodes), center, label, nodes_by_id, direct_tests)
            pct_old = (foreign_old / total_old * 100) if total_old else 0.0
        else:
            total_old = foreign_old = pct_old = 0
            ids_old = []

        # ── fixed logic ────────────────────────────────────────────────────
        if mode in ("fixed", "both"):
            fg_fix = _build_filtered_graph_fixed(G, nt_cache, center)
            if center not in fg_fix:
                continue
            subg_fix = nx.ego_graph(fg_fix, center, radius=hops)
            total_fix, foreign_fix, ids_fix = _tc_bleed(set(subg_fix.nodes), center, label, nodes_by_id, direct_tests)
            pct_fix = (foreign_fix / total_fix * 100) if total_fix else 0.0
        else:
            total_fix = foreign_fix = pct_fix = 0
            ids_fix = []

        # decide whether to flag
        ref_pct = pct_old if mode in ("old", "both") else pct_fix
        ref_total = total_old if mode in ("old", "both") else total_fix
        if ref_pct > threshold and ref_total > 0:
            flagged.append({
                "cluster": label,
                "total_old": total_old,
                "foreign_old": foreign_old,
                "pct_old": pct_old,
                "total_fix": total_fix,
                "foreign_fix": foreign_fix,
                "pct_fix": pct_fix,
                "foreign_ids_old": ids_old,
            })

    # ── report ─────────────────────────────────────────────────────────────
    if not flagged:
        print(f"✓  No clusters exceed {threshold}% foreign TCs in {mode} mode.")
        return

    flagged.sort(key=lambda x: x["pct_old"] if mode != "fixed" else x["pct_fix"], reverse=True)

    if mode == "old":
        hdr = f"{'Cluster':<45}  {'TCs':>5}  {'Foreign':>7}  {'%':>6}"
        sep = "-" * len(hdr)
        print(hdr)
        print(sep)
        for f in flagged:
            print(
                f"  {f['cluster']:<43}  {f['total_old']:>5}  {f['foreign_old']:>7}  {f['pct_old']:>5.1f}%"
            )
            sample = f["foreign_ids_old"][:8]
            if sample:
                print(f"    sample foreign TCs: {', '.join(sample)}"
                      + (" …" if len(f["foreign_ids_old"]) > 8 else ""))

    elif mode == "fixed":
        hdr = f"{'Cluster':<45}  {'TCs':>5}  {'Foreign':>7}  {'%':>6}"
        sep = "-" * len(hdr)
        print(hdr)
        print(sep)
        for f in flagged:
            print(
                f"  {f['cluster']:<43}  {f['total_fix']:>5}  {f['foreign_fix']:>7}  {f['pct_fix']:>5.1f}%"
            )

    else:  # both
        hdr = (
            f"{'Cluster':<45}  "
            f"{'OLD TCs':>7}  {'OLD Fgn':>7}  {'OLD %':>6}  "
            f"{'FIX TCs':>7}  {'FIX Fgn':>7}  {'FIX %':>6}"
        )
        sep = "-" * len(hdr)
        print(hdr)
        print(sep)
        for f in flagged:
            fixed_ok = "✓" if f["pct_fix"] <= threshold else "✗"
            print(
                f"  {f['cluster']:<43}  "
                f"{f['total_old']:>7}  {f['foreign_old']:>7}  {f['pct_old']:>5.1f}%  "
                f"{f['total_fix']:>7}  {f['foreign_fix']:>7}  {f['pct_fix']:>5.1f}%  {fixed_ok}"
            )
            sample = f["foreign_ids_old"][:5]
            if sample:
                print(f"    foreign (old): {', '.join(sample)}"
                      + (" …" if len(f["foreign_ids_old"]) > 5 else ""))

    print(f"\n{len(flagged)} cluster(s) flagged (>{threshold}% foreign TCs in {mode} mode).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit TC bleed in KG ego-graph viz")
    parser.add_argument(
        "--kg-path",
        default="data/knowledge_graph/matter_kg.json",
        help="Path to matter_kg.json (default: data/knowledge_graph/matter_kg.json)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=25.0,
        help="Flag clusters where foreign-TC %% exceeds this value (default: 25.0)",
    )
    parser.add_argument(
        "--hops",
        type=int,
        default=2,
        help="Ego-graph radius (default: 2)",
    )
    parser.add_argument(
        "--mode",
        choices=["old", "fixed", "both"],
        default="both",
        help="old = broken logic, fixed = patched logic, both = side-by-side (default: both)",
    )
    args = parser.parse_args()

    audit(
        kg_path=args.kg_path,
        threshold=args.threshold,
        mode=args.mode,
        hops=args.hops,
    )


if __name__ == "__main__":
    main()
