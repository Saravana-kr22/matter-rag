#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify KG test case cluster mappings.

Loads matter_kg.json and runs structural checks on every TEST_CASE node.

Checks (per TC)
---------------
  1  cluster_node_exists      Primary cluster property names a real CLUSTER node in the KG.
  2  has_tests_edge           TC has ≥1 outgoing TESTS edge.
  3  tests_targets_consistent CLUSTER::* targets of TESTS edges = {primary} ∪ related_clusters.
                              Flags clusters in related_clusters with no TESTS edge (edge missing)
                              and TESTS edge targets not in either list (stale/phantom edge).
  4  pics_cluster_covered     For each PICS code, the cluster resolved from the PICS prefix
                              has a TESTS edge.  Flags uncovered clusters.
  5  has_req_coverage         TC has ≥1 verifies_requirement edge.
                              Protocol-family TCs (IDM, SC, BDX, DD, DA, ACE, RR …) are WARN,
                              not FAIL, since they may legitimately have no cluster REQ nodes.

Results
-------
  PASS  — check passed
  WARN  — potential issue, not necessarily wrong
  FAIL  — structural inconsistency in the KG

Usage
-----
  python scripts/verify_kg_tc_mapping.py
  python scripts/verify_kg_tc_mapping.py --kg-path data/knowledge_graph/matter_kg.json
  python scripts/verify_kg_tc_mapping.py --tc TC-RR-1.1          # single TC deep-dive
  python scripts/verify_kg_tc_mapping.py --cluster "On/Off"       # filter by cluster
  python scripts/verify_kg_tc_mapping.py --output reports/verify_kg_tc_mapping  # custom output dir
  python scripts/verify_kg_tc_mapping.py --only-issues            # suppress PASS rows
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# TC-ID prefixes that are protocol-family (no cluster-level REQ nodes expected).
# This set is populated dynamically from DM XML at startup via _build_protocol_prefix_set().
# Fallback hard-coded set used when --dm-dir is not found.
_PROTOCOL_PREFIXES: frozenset = frozenset({
    "IDM", "SC", "BDX", "DD", "DA", "MCORE",
    "RR", "BI", "SIGMA", "PAKE", "CASE", "CERT", "MSG",
})


def _build_protocol_prefix_set(dm_dir: Path) -> frozenset:
    """Return the set of TC prefixes that are NOT in DM XML (i.e. protocol/transport tests).

    Loads all DM XML files, collects picsCode values (= known cluster prefixes), then
    returns a frozenset of ALL prefixes from the KG that are not among those.
    The frozenset returned here is only used by _is_protocol_tc(); it is derived
    dynamically so new clusters added to DM XML are automatically recognised.
    """
    import xml.etree.ElementTree as ET
    known: set = set()
    if dm_dir.exists():
        for xml_file in sorted(dm_dir.glob("*.xml")):
            try:
                root = ET.parse(xml_file).getroot()
                cls_el = root.find(".//{*}classification") or root.find(".//classification")
                if cls_el is not None:
                    code = cls_el.get("picsCode", "").strip().upper()
                    if code:
                        known.add(code)
            except Exception:
                pass
    return known  # return KNOWN cluster prefixes (caller inverts to get protocol set)


# Known cluster prefixes populated at startup (empty = use hard-coded fallback above)
_known_cluster_prefixes: frozenset = frozenset()


def _tc_prefix(tc_id: str) -> str:
    """Return the prefix part: 'TC-RR-1.1' → 'RR'."""
    parts = tc_id.split("-")
    return parts[1].upper() if len(parts) >= 2 and parts[0] == "TC" else ""


def _is_protocol_tc(tc_id: str) -> bool:
    prefix = _tc_prefix(tc_id)
    if not prefix:
        return False
    if _known_cluster_prefixes:
        return prefix not in _known_cluster_prefixes
    return prefix in _PROTOCOL_PREFIXES  # fallback when DM XML not loaded


def _strip(s) -> str:
    return str(s).strip() if s else ""


def _cluster_node_id(name: str) -> str:
    return f"CLUSTER::{name}"


# ---------------------------------------------------------------------------
# Check functions — each returns (status, detail_str)
#   status: "PASS" | "WARN" | "FAIL"
# ---------------------------------------------------------------------------

def check_cluster_node_exists(tc_id: str, tc_props: dict, cluster_node_ids: set) -> tuple:
    cluster = _strip(tc_props.get("cluster"))
    if not cluster:
        if _is_protocol_tc(tc_id):
            return "PASS", ""  # protocol TCs intentionally have no primary cluster
        return "WARN", "primary cluster property is empty"
    cid = _cluster_node_id(cluster)
    if cid not in cluster_node_ids:
        return "FAIL", f"CLUSTER::{cluster!r} not found in KG nodes"
    return "PASS", ""


def check_has_tests_edge(tc_id: str, tc_tests_edges: set, has_protocol_area: bool = False) -> tuple:
    if not tc_tests_edges:
        if _is_protocol_tc(tc_id) and has_protocol_area:
            return "PASS", "no TESTS edges (protocol TC linked to PROTOCOL_AREA)"
        status = "WARN" if _is_protocol_tc(tc_id) else "FAIL"
        return status, "no TESTS edges"
    return "PASS", f"{len(tc_tests_edges)} TESTS edge(s)"


def check_tests_targets_consistent(tc_props: dict, tc_tests_edges: set) -> tuple:
    """
    TESTS edge targets (CLUSTER::X) should match {primary} ∪ related_clusters.
    Flags:
      - clusters in related_clusters/primary that have no TESTS edge  (edge missing)
      - TESTS edge targets not in either list                          (phantom edge)
    """
    primary = _strip(tc_props.get("cluster"))
    related = [_strip(r) for r in (tc_props.get("related_clusters") or []) if _strip(r)]

    expected_ids = set()
    if primary:
        expected_ids.add(_cluster_node_id(primary))
    for r in related:
        expected_ids.add(_cluster_node_id(r))

    actual_ids = set(tc_tests_edges)  # already CLUSTER::* strings

    missing_edges = expected_ids - actual_ids   # in property but no edge
    phantom_edges = actual_ids - expected_ids   # edge but not in property

    issues = []
    if missing_edges:
        names = sorted(i.split("::", 1)[-1] for i in missing_edges)
        issues.append(f"missing TESTS edges for: {names}")
    if phantom_edges:
        names = sorted(i.split("::", 1)[-1] for i in phantom_edges)
        issues.append(f"TESTS edges not in related_clusters: {names}")

    if issues:
        return "WARN", "; ".join(issues)
    return "PASS", f"consistent ({len(actual_ids)} cluster(s))"


def check_pics_cluster_covered(tc_props: dict, tc_tests_edges: set, pics_code_to_cluster: dict) -> tuple:
    """Every PICS prefix resolved to a cluster should have a TESTS edge."""
    pics_codes = tc_props.get("pics_codes") or []
    uncovered = []
    for code in pics_codes:
        prefix = code.split(".")[0].upper() if "." in code else code.upper()
        cluster_name = pics_code_to_cluster.get(prefix)
        if cluster_name and _cluster_node_id(cluster_name) not in tc_tests_edges:
            uncovered.append(f"{code} → {cluster_name}")
    if uncovered:
        return "WARN", f"PICS cluster(s) not in TESTS edges: {uncovered}"
    return "PASS", f"{len(pics_codes)} PICS code(s) checked"


def check_has_req_coverage(tc_id: str, verifies_req_count: int,
                           cluster: str = "", cluster_req_counts: dict = None,
                           alias_targets: dict = None) -> tuple:
    if verifies_req_count > 0:
        return "PASS", f"{verifies_req_count} requirement(s) covered"

    cluster_req_counts = cluster_req_counts or {}
    alias_targets = alias_targets or {}
    cluster_reqs = cluster_req_counts.get(cluster, 0)

    if cluster_reqs == 0:
        parent = alias_targets.get(cluster)
        if parent:
            parent_reqs = cluster_req_counts.get(parent, 0)
            if parent_reqs > 0:
                return "WARN", f"no REQs (parent {parent!r} has {parent_reqs}; ALIAS_OF not followed in linking)"
        return "WARN", f"no REQs exist for cluster {cluster!r}"
    else:
        return "WARN", f"cluster has {cluster_reqs} REQs but entity matching found none for this TC"


# ---------------------------------------------------------------------------
# Main verification
# ---------------------------------------------------------------------------

def verify_kg(kg_path: Path, tc_filter: str, cluster_filter: str, only_issues: bool):
    print(f"Loading KG from {kg_path} …", end=" ", flush=True)
    kg = json.loads(kg_path.read_text())
    nodes = kg["nodes"]
    edges = kg["edges"]
    print(f"{len(nodes):,} nodes, {len(edges):,} edges")

    # ── Build lookup structures ──────────────────────────────────────────────
    cluster_node_ids: set = {n["id"] for n in nodes if n["node_type"] == "CLUSTER"}

    # PICS prefix → cluster name (from CLUSTER node properties["code"])
    pics_code_to_cluster: dict = {}
    for n in nodes:
        if n["node_type"] == "CLUSTER":
            code = _strip(n.get("properties", {}).get("code"))
            if code:
                pics_code_to_cluster[code.upper()] = _strip(n.get("properties", {}).get("name", ""))

    # Per-TC edge maps
    tc_tests_targets: dict = defaultdict(set)    # tc_id → {CLUSTER::X, ...}
    tc_verifies_req: dict = defaultdict(int)     # tc_id → count
    tc_has_protocol_area: set = set()            # tc_ids with ≥1 BELONGS_TO_PROTOCOL_AREA edge

    for e in edges:
        src = e["source"]
        etype = e["edge_type"]
        tgt = e["target"]
        if etype == "tests" and tgt.startswith("CLUSTER::"):
            tc_tests_targets[src].add(tgt)
        elif etype == "verifies_requirement":
            tc_verifies_req[src] += 1
        elif etype == "belongs_to_protocol_area" and tgt.startswith("PROTOCOL_AREA::"):
            tc_has_protocol_area.add(src)

    # Per-cluster REQ count (for has_req_coverage diagnostics)
    cluster_req_counts: dict = defaultdict(int)
    for n in nodes:
        if n["node_type"] in ("REQUIREMENT", "BEHAVIOR_RULE"):
            c = _strip(n.get("properties", {}).get("cluster", ""))
            if c:
                cluster_req_counts[c] += 1

    # ALIAS_OF: child cluster → parent cluster (for sub-cluster diagnostics)
    alias_targets: dict = {}
    for e in edges:
        if e["edge_type"] in ("alias_of", "ALIAS_OF"):
            child = e["source"].replace("CLUSTER::", "")
            parent = e["target"].replace("CLUSTER::", "")
            alias_targets[child] = parent

    # ── Collect TEST_CASE nodes ──────────────────────────────────────────────
    tc_nodes = [n for n in nodes if n["node_type"] == "TEST_CASE"]
    if tc_filter:
        tc_nodes = [n for n in tc_nodes if n["id"] == tc_filter]
    if cluster_filter:
        cf = cluster_filter.lower()
        tc_nodes = [
            n for n in tc_nodes
            if cf in _strip(n.get("properties", {}).get("cluster", "")).lower()
            or any(cf in r.lower() for r in (n.get("properties", {}).get("related_clusters") or []))
        ]

    print(f"Verifying {len(tc_nodes):,} TEST_CASE node(s)…\n")

    # ── Run checks ───────────────────────────────────────────────────────────
    CHECKS = [
        "cluster_node_exists",
        "has_tests_edge",
        "tests_targets_consistent",
        "pics_cluster_covered",
        "has_req_coverage",
    ]

    rows = []
    status_counts = Counter()  # (check_name, status) → count

    for n in sorted(tc_nodes, key=lambda x: x["id"]):
        tc_id = n["id"]
        props = n.get("properties", {})
        cluster = _strip(props.get("cluster"))
        related = [_strip(r) for r in (props.get("related_clusters") or []) if _strip(r)]
        tests_targets = tc_tests_targets[tc_id]
        verifies_count = tc_verifies_req[tc_id]

        c1_status, c1_detail = check_cluster_node_exists(tc_id, props, cluster_node_ids)
        c2_status, c2_detail = check_has_tests_edge(tc_id, tests_targets, has_protocol_area=tc_id in tc_has_protocol_area)
        c3_status, c3_detail = check_tests_targets_consistent(props, tests_targets)
        c4_status, c4_detail = check_pics_cluster_covered(props, tests_targets, pics_code_to_cluster)
        c5_status, c5_detail = check_has_req_coverage(
            tc_id, verifies_count, cluster=cluster,
            cluster_req_counts=cluster_req_counts, alias_targets=alias_targets,
        )

        check_results = [
            (c1_status, c1_detail),
            (c2_status, c2_detail),
            (c3_status, c3_detail),
            (c4_status, c4_detail),
            (c5_status, c5_detail),
        ]
        for check_name, (status, _) in zip(CHECKS, check_results):
            status_counts[(check_name, status)] += 1

        overall = (
            "FAIL" if any(s == "FAIL" for s, _ in check_results)
            else "WARN" if any(s == "WARN" for s, _ in check_results)
            else "PASS"
        )

        rows.append({
            "tc_id": tc_id,
            "overall": overall,
            "primary_cluster": cluster,
            "related_clusters": "|".join(related),
            "tests_edge_targets": "|".join(sorted(t.split("::", 1)[-1] for t in tests_targets)),
            "verifies_req_count": verifies_count,
            "pics_codes": "|".join(props.get("pics_codes") or []),
            # per-check columns
            "c1_cluster_node_exists": c1_status,
            "c1_detail": c1_detail,
            "c2_has_tests_edge": c2_status,
            "c2_detail": c2_detail,
            "c3_tests_consistent": c3_status,
            "c3_detail": c3_detail,
            "c4_pics_covered": c4_status,
            "c4_detail": c4_detail,
            "c5_req_coverage": c5_status,
            "c5_detail": c5_detail,
        })

    return rows, status_counts, CHECKS


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_summary(rows, status_counts, checks, only_issues: bool = False):
    total = len(rows)
    fails = sum(1 for r in rows if r["overall"] == "FAIL")
    warns = sum(1 for r in rows if r["overall"] == "WARN")
    passes = sum(1 for r in rows if r["overall"] == "PASS")

    print("=== KG Test Case Verification Summary ===")
    print(f"Total TCs checked : {total:,}")
    print(f"  PASS            : {passes:,}  ({100*passes//total if total else 0}%)")
    print(f"  WARN            : {warns:,}  ({100*warns//total if total else 0}%)")
    print(f"  FAIL            : {fails:,}  ({100*fails//total if total else 0}%)")

    print("\nPer-check breakdown:")
    for check in checks:
        p = status_counts.get((check, "PASS"), 0)
        w = status_counts.get((check, "WARN"), 0)
        f = status_counts.get((check, "FAIL"), 0)
        print(f"  {check:<35} PASS={p:>5}  WARN={w:>5}  FAIL={f:>5}")

    # Top failing TCs
    fail_rows = [r for r in rows if r["overall"] == "FAIL"]
    if fail_rows:
        print(f"\nFAIL TCs ({len(fail_rows)}):")
        for r in fail_rows[:20]:
            issues = [
                f"c1:{r['c1_detail']}" if r["c1_cluster_node_exists"] == "FAIL" else None,
                f"c2:{r['c2_detail']}" if r["c2_has_tests_edge"] == "FAIL" else None,
            ]
            issues = [i for i in issues if i]
            print(f"  {r['tc_id']:<25} cluster={r['primary_cluster'] or '(none)'!r:<30} {'; '.join(issues)}")
        if len(fail_rows) > 20:
            print(f"  … and {len(fail_rows) - 20} more (see CSV)")

    # Top WARN TCs (no req coverage) — grouped by root cause
    no_req = [r for r in rows if r["c5_req_coverage"] == "WARN" and r["overall"] != "FAIL"]
    print(f"\nTCs with no verifies_requirement edges: {len(no_req):,}")

    if no_req:
        cause_groups = defaultdict(list)
        for r in no_req:
            detail = r["c5_detail"]
            if "no REQs exist" in detail:
                cause_groups["No REQs in cluster"].append(r)
            elif "parent" in detail and "ALIAS_OF" in detail:
                cause_groups["Sub-cluster (parent has REQs, ALIAS_OF not followed)"].append(r)
            elif "entity matching found none" in detail:
                cause_groups["Cluster has REQs but entity matching missed this TC"].append(r)
            else:
                cause_groups["Other"].append(r)

        for cause, group in sorted(cause_groups.items(), key=lambda x: -len(x[1])):
            print(f"\n  {cause} ({len(group)} TCs):")
            by_cluster = defaultdict(list)
            for r in group:
                by_cluster[r["primary_cluster"] or "(none)"].append(r["tc_id"])
            for cluster, tc_ids in sorted(by_cluster.items()):
                if len(tc_ids) <= 3:
                    print(f"    {cluster}: {', '.join(tc_ids)}")
                else:
                    print(f"    {cluster}: {', '.join(tc_ids[:3])}, ... +{len(tc_ids)-3} more")

    if only_issues:
        issue_rows = [r for r in rows if r["overall"] != "PASS"]
        if issue_rows:
            print(f"\n=== WARN/FAIL TCs ({len(issue_rows)}) ===")
            for r in issue_rows:
                marker = "✗" if r["overall"] == "FAIL" else "!"
                cluster_str = r["primary_cluster"] or "(no cluster)"
                details = []
                for col, label in [
                    ("c1_cluster_node_exists", "cluster_node"),
                    ("c2_has_tests_edge",      "tests_edge"),
                    ("c3_tests_consistent",    "tests_consistent"),
                    ("c4_pics_covered",        "pics_covered"),
                    ("c5_req_coverage",        "req_coverage"),
                ]:
                    status = r[col]
                    detail_key = col.replace("_node_exists", "").replace("has_", "").replace("tests_", "c3_")
                    if col == "c1_cluster_node_exists":
                        detail = r["c1_detail"]
                    elif col == "c2_has_tests_edge":
                        detail = r["c2_detail"]
                    elif col == "c3_tests_consistent":
                        detail = r["c3_detail"]
                    elif col == "c4_pics_covered":
                        detail = r["c4_detail"]
                    else:
                        detail = r["c5_detail"]
                    if status != "PASS":
                        details.append(f"{label}={status}" + (f"({detail})" if detail else ""))
                print(f"  [{marker}] {r['tc_id']:<25} {cluster_str:<35} {'; '.join(details)}")


def print_tc_deep_dive(rows, tc_id: str):
    r = next((r for r in rows if r["tc_id"] == tc_id), None)
    if not r:
        print(f"TC {tc_id!r} not found in results.")
        return
    print(f"\n=== Deep dive: {tc_id} ===")
    print(f"Overall            : {r['overall']}")
    print(f"Primary cluster    : {r['primary_cluster'] or '(none)'}")
    print(f"Related clusters   : {r['related_clusters'] or '(none)'}")
    print(f"TESTS edge targets : {r['tests_edge_targets'] or '(none)'}")
    print(f"PICS codes         : {r['pics_codes'] or '(none)'}")
    print(f"Req coverage count : {r['verifies_req_count']}")
    print()
    checks = [
        ("cluster_node_exists",    r["c1_cluster_node_exists"], r["c1_detail"]),
        ("has_tests_edge",         r["c2_has_tests_edge"],      r["c2_detail"]),
        ("tests_targets_consistent", r["c3_tests_consistent"],  r["c3_detail"]),
        ("pics_cluster_covered",   r["c4_pics_covered"],        r["c4_detail"]),
        ("has_req_coverage",       r["c5_req_coverage"],        r["c5_detail"]),
    ]
    for name, status, detail in checks:
        marker = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}.get(status, "?")
        print(f"  [{marker}] {name:<35} {status}  {detail}")


def write_csv(rows, output_dir: Path, only_issues: bool) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"kg_tc_verification_{ts}.csv"

    fieldnames = [
        "tc_id", "overall",
        "primary_cluster", "related_clusters", "tests_edge_targets",
        "verifies_req_count", "pics_codes",
        "c1_cluster_node_exists", "c1_detail",
        "c2_has_tests_edge",      "c2_detail",
        "c3_tests_consistent",    "c3_detail",
        "c4_pics_covered",        "c4_detail",
        "c5_req_coverage",        "c5_detail",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if only_issues and r["overall"] == "PASS":
                continue
            w.writerow(r)

    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dm-dir", default="data/data_model",
        help="Directory containing Matter DM XML files (used to derive protocol vs cluster TC prefixes; default: data/data_model)",
    )
    parser.add_argument(
        "--kg-path", default="data/knowledge_graph/matter_kg.json",
        help="Path to matter_kg.json (default: data/knowledge_graph/matter_kg.json)",
    )
    parser.add_argument(
        "--tc",
        help="Verify a single TC ID and print a detailed report (e.g. TC-RR-1.1)",
    )
    parser.add_argument(
        "--cluster",
        help="Filter to TCs whose primary or related cluster contains this string",
    )
    parser.add_argument(
        "--output", default="reports/verify_kg_tc_mapping",
        help="Directory to write the CSV (default: reports/verify_kg_tc_mapping)",
    )
    parser.add_argument(
        "--only-issues", action="store_true",
        help="Write only WARN/FAIL rows to the CSV (omit PASS)",
    )
    args = parser.parse_args()

    kg_path = Path(args.kg_path)
    if not kg_path.exists():
        print(f"ERROR: KG file not found: {kg_path}", file=sys.stderr)
        sys.exit(1)

    # Load DM XML to derive protocol vs cluster prefixes dynamically.
    global _known_cluster_prefixes
    dm_dir = Path(args.dm_dir)
    _known_cluster_prefixes = _build_protocol_prefix_set(dm_dir)
    if _known_cluster_prefixes:
        print(f"DM XML: {len(_known_cluster_prefixes)} known cluster prefixes from {dm_dir}")
    else:
        print(f"[WARN] No DM XML found at {dm_dir} — using hard-coded protocol prefix fallback", file=sys.stderr)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows, status_counts, checks = verify_kg(
        kg_path,
        tc_filter=args.tc or "",
        cluster_filter=args.cluster or "",
        only_issues=args.only_issues,
    )

    if args.tc:
        print_tc_deep_dive(rows, args.tc)
    else:
        print_summary(rows, status_counts, checks, only_issues=args.only_issues)

    out_path = write_csv(rows, output_dir, args.only_issues)
    print(f"\nCSV written to: {out_path}")


if __name__ == "__main__":
    main()
