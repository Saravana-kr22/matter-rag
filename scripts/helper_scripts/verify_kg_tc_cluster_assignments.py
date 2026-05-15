#!/usr/bin/env python3
"""TC Inventory & Cluster Assignment Dashboard.

Loads matter_kg.json and produces:
  - A full TC inventory table (cluster, DUT role, mode, entity/req counts, intents)
  - Cluster assignment verification (wrong cluster, missing cluster, protocol TC issues)
  - Per-cluster summary with TC counts by DUT role

Usage:
    python scripts/helper_scripts/verify_kg_tc_cluster_assignments.py
    python scripts/helper_scripts/verify_kg_tc_cluster_assignments.py --cluster "On/Off"
    python scripts/helper_scripts/verify_kg_tc_cluster_assignments.py --output reports/tc_inventory.html
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT))

_TC_PREFIX_RE = re.compile(r"^TC-([A-Z0-9]+)-", re.I)

_DUT_SERVER_RE = re.compile(
    r"\bDUT\s+as\s+(?:a\s+)?Server\b"
    r"|\bserver\s+(?:side|DUT)\b"
    r"|\b(?:cluster|diagnostics)\s+server\b"
    r"|\bDUT\s+(?:is|as)\s+(?:the\s+)?server\b",
    re.I,
)
_DUT_CLIENT_RE = re.compile(
    r"\bDUT\s+as\s+(?:a\s+)?Client\b"
    r"|\bclient\s+(?:side|DUT)\b"
    r"|\bDUT\s+(?:is|as)\s+(?:the\s+)?client\b",
    re.I,
)
_DUT_COMMISSIONER_RE = re.compile(
    r"\bDUT\s+as\s+(?:a\s+)?Commissioner\b"
    r"|\bcommissioner\s+DUT\b"
    r"|\bDUT\s+(?:is|as)\s+(?:the\s+)?commissioner\b"
    r"|\bcommissioning\s+(?:device|node)\b",
    re.I,
)
_DUT_INITIATOR_RE = re.compile(
    r"\bDUT\s+as\s+(?:a\s+)?(?:Initiator|Responder)\b"
    r"|\binitiator\s+DUT\b"
    r"|\bresponder\s+DUT\b",
    re.I,
)


def _detect_dut_role(tc_id: str, purpose: str, pics_codes: List[str]) -> str:
    """Detect DUT role from PICS codes, purpose text, and TC-ID pattern."""
    server_pics = sum(1 for p in pics_codes if ".S." in p)
    client_pics = sum(1 for p in pics_codes if ".C." in p)
    if server_pics > 0 and client_pics == 0:
        return "Server"
    if client_pics > 0 and server_pics == 0:
        return "Client"
    if server_pics > 0 and client_pics > 0:
        return "Server+Client"

    text = purpose or ""
    if _DUT_SERVER_RE.search(text):
        return "Server"
    if _DUT_CLIENT_RE.search(text):
        return "Client"
    if _DUT_COMMISSIONER_RE.search(text):
        return "Commissioner"
    if _DUT_INITIATOR_RE.search(text):
        return "Initiator/Responder"

    m = _TC_PREFIX_RE.match(tc_id)
    if m:
        parts = tc_id.split("-")
        if len(parts) >= 3:
            try:
                major = int(parts[2].split(".")[0])
                if major == 2:
                    return "Server (by convention)"
                elif major == 3:
                    return "Client (by convention)"
            except ValueError:
                pass

    m2 = _TC_PREFIX_RE.match(tc_id)
    if m2:
        prefix = m2.group(1).upper()
        if prefix in _PROTOCOL_PREFIXES:
            return f"Protocol-{prefix}"

    return "Unknown"


_PROTOCOL_PREFIXES = frozenset({
    "IDM", "SC", "BDX", "DD", "DA", "ACE", "MC", "JFADMIN", "JF",
    "MCORE", "DT", "SU", "BR", "SM", "RR", "ICDB", "WEBRTC", "PAVSTI",
})


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def load_kg_data(kg_path: Path):
    """Load KG and return structured data for all TCs."""
    data = json.loads(kg_path.read_text())
    nodes_list = data.get("nodes", [])
    edges_list = data.get("edges", [])

    prefix_map: Dict[str, str] = {}
    for n in nodes_list:
        if (n.get("node_type") or "").upper() == "CLUSTER":
            props = n.get("properties", {})
            code = (props.get("pics_code") or props.get("code") or "").strip().upper()
            name = (props.get("name") or n.get("label") or "").strip()
            if code and name:
                prefix_map[code] = name

    tc_verifies_req: Dict[str, int] = defaultdict(int)
    tc_entity_edges: Dict[str, int] = defaultdict(int)
    _entity_edge_types = {
        "reads", "writes", "verifies_attribute", "tests_command",
        "observes_event", "validates_range", "validates_type",
        "validates_default", "validates_access", "validates_conformance",
    }
    for e in edges_list:
        src = e.get("source", "")
        et = e.get("edge_type", "")
        if et == "verifies_requirement":
            tc_verifies_req[src] += 1
        if et in _entity_edge_types:
            tc_entity_edges[src] += 1

    tcs = []
    for n in nodes_list:
        if (n.get("node_type") or "").upper() not in ("TEST_CASE", "TESTCASE"):
            continue
        props = n.get("properties", {})
        tc_id = (props.get("tc_id") or n.get("id", "")).strip()
        cluster = (props.get("cluster") or "").strip()
        purpose = (props.get("purpose") or "")
        pics_codes = props.get("pics_codes") or []
        entity_refs = props.get("entity_refs") or []
        intents = props.get("intents") or []
        mode = props.get("mode", "")

        dut_role = _detect_dut_role(tc_id, purpose, pics_codes)

        m = _TC_PREFIX_RE.match(tc_id)
        prefix = m.group(1).upper() if m else ""

        issue = None
        if prefix:
            in_map = prefix in prefix_map
            is_family = any(c.startswith(prefix) for c in prefix_map if len(c) > len(prefix))
            if not in_map and not is_family and not pics_codes and cluster:
                issue = "PROTOCOL_TC_HAS_CLUSTER"
            elif in_map:
                expected = prefix_map[prefix]
                norm = lambda s: s.lower().removesuffix(" cluster").strip()
                if not cluster:
                    issue = "CLUSTER_MISSING"
                elif norm(cluster) != norm(expected):
                    issue = "WRONG_CLUSTER"

        tcs.append({
            "tc_id": tc_id,
            "cluster": cluster,
            "dut_role": dut_role,
            "mode": mode,
            "prefix": prefix,
            "entity_refs_count": len(entity_refs),
            "entity_edges_count": tc_entity_edges.get(tc_id, 0),
            "verifies_req_count": tc_verifies_req.get(tc_id, 0),
            "intents": intents,
            "pics_count": len(pics_codes),
            "purpose": purpose[:200],
            "issue": issue,
        })

    return tcs, prefix_map


def generate_html_report(tcs: List[dict], out_path: Path) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    issues = [t for t in tcs if t["issue"]]
    issue_counts = Counter(t["issue"] for t in issues)

    by_cluster = defaultdict(lambda: {"total": 0, "server": 0, "client": 0, "other": 0, "reqs": 0})
    for t in tcs:
        c = t["cluster"] or "(none)"
        by_cluster[c]["total"] += 1
        role_lower = t["dut_role"].lower()
        if "server" in role_lower:
            by_cluster[c]["server"] += 1
        elif "client" in role_lower:
            by_cluster[c]["client"] += 1
        else:
            by_cluster[c]["other"] += 1
        by_cluster[c]["reqs"] += t["verifies_req_count"]

    dut_counts = Counter(t["dut_role"] for t in tcs)
    mode_counts = Counter(t["mode"] for t in tcs)

    dut_pills = " ".join(
        f'<span class="pill" style="background:#37474f">{role}: <b>{cnt}</b></span>'
        for role, cnt in dut_counts.most_common()
    )
    mode_pills = " ".join(
        f'<span class="pill" style="background:#263238">{mode}: <b>{cnt}</b></span>'
        for mode, cnt in mode_counts.most_common()
    )

    _ROLE_COLOR = {
        "Server": "#4caf50", "Client": "#42a5f5", "Commissioner": "#ff9800",
        "Server+Client": "#ab47bc", "Initiator/Responder": "#26a69a",
        "Server (by convention)": "#81c784", "Client (by convention)": "#64b5f6",
        "Unknown": "#616161",
    }
    _ISSUE_COLOR = {
        "PROTOCOL_TC_HAS_CLUSTER": "#ff7043",
        "WRONG_CLUSTER": "#ef5350",
        "CLUSTER_MISSING": "#ffa726",
    }

    cluster_rows = ""
    for cname in sorted(by_cluster.keys()):
        d = by_cluster[cname]
        cluster_rows += (
            f'<tr><td>{_escape(cname)}</td><td>{d["total"]}</td>'
            f'<td style="color:#4caf50">{d["server"]}</td>'
            f'<td style="color:#42a5f5">{d["client"]}</td>'
            f'<td>{d["other"]}</td><td>{d["reqs"]}</td></tr>\n'
        )

    tc_rows = ""
    for t in sorted(tcs, key=lambda x: x["tc_id"]):
        role_color = _ROLE_COLOR.get(t["dut_role"], "#616161")
        issue_html = ""
        if t["issue"]:
            ic = _ISSUE_COLOR.get(t["issue"], "#888")
            issue_html = f'<span style="background:{ic};color:#111;padding:1px 6px;border-radius:8px;font-size:11px">{t["issue"]}</span>'

        intents_str = ", ".join(t["intents"][:5])
        if len(t["intents"]) > 5:
            intents_str += f" +{len(t['intents'])-5}"

        tc_rows += (
            f'<tr data-cluster="{_escape(t["cluster"])}" data-role="{_escape(t["dut_role"])}">'
            f'<td><code style="color:#80cbc4">{_escape(t["tc_id"])}</code></td>'
            f'<td>{_escape(t["cluster"])}</td>'
            f'<td><span style="color:{role_color};font-weight:bold">{_escape(t["dut_role"])}</span></td>'
            f'<td>{_escape(t["mode"])}</td>'
            f'<td>{t["entity_refs_count"]}</td>'
            f'<td>{t["entity_edges_count"]}</td>'
            f'<td>{t["verifies_req_count"]}</td>'
            f'<td>{t["pics_count"]}</td>'
            f'<td style="font-size:11px;color:#aaa">{_escape(intents_str)}</td>'
            f'<td>{issue_html}</td>'
            f'</tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>TC Inventory &amp; Cluster Assignment Dashboard</title>
<style>
body {{ background:#1a1a1a; color:#e0e0e0; font-family:monospace; margin:0; padding:20px; }}
h1 {{ color:#80cbc4; margin-bottom:4px; }}
h2 {{ color:#80cbc4; margin-top:30px; }}
.sub {{ color:#888; font-size:13px; margin-bottom:16px; }}
.summary {{ background:#252525; border:1px solid #333; padding:14px 20px; border-radius:6px;
           margin-bottom:20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
.pill {{ padding:3px 10px; border-radius:12px; font-size:12px; color:#e0e0e0; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th {{ background:#252525; color:#80cbc4; padding:8px 10px; text-align:left;
      border-bottom:2px solid #333; position:sticky; top:0; cursor:pointer; }}
th:hover {{ background:#333; }}
td {{ padding:6px 10px; border-bottom:1px solid #2a2a2a; vertical-align:top; }}
tr:hover td {{ background:#202020; }}
code {{ font-size:12px; }}
.filters {{ margin-bottom:16px; display:flex; gap:12px; align-items:center; }}
.filters select, .filters input {{ background:#252525; color:#e0e0e0; border:1px solid #444;
  padding:6px 10px; border-radius:4px; font-family:monospace; font-size:12px; }}
</style>
</head>
<body>
<h1>TC Inventory &amp; Cluster Assignment Dashboard</h1>
<div class="sub">Generated: {ts} &nbsp;|&nbsp; Total TCs: <b>{len(tcs)}</b>
  &nbsp;|&nbsp; Issues: <b>{len(issues)}</b></div>

<div class="summary">
  <span style="font-size:14px;font-weight:bold">DUT Roles:</span> {dut_pills}
</div>
<div class="summary">
  <span style="font-size:14px;font-weight:bold">Modes:</span> {mode_pills}
</div>

<h2>Cluster Summary</h2>
<table id="clusterTable">
<thead><tr><th>Cluster</th><th>TCs</th><th>Server</th><th>Client</th><th>Other</th><th>REQ Edges</th></tr></thead>
<tbody>{cluster_rows}</tbody>
</table>

<h2>Full TC Inventory</h2>
<div class="filters">
  <label>Cluster: <select id="fCluster" onchange="filterTCs()">
    <option value="">All</option>
    {''.join(f'<option value="{_escape(c)}">{_escape(c)}</option>' for c in sorted(by_cluster.keys()))}
  </select></label>
  <label>DUT Role: <select id="fRole" onchange="filterTCs()">
    <option value="">All</option>
    {''.join(f'<option value="{_escape(r)}">{_escape(r)}</option>' for r in sorted(dut_counts.keys()))}
  </select></label>
  <label>Search: <input id="fSearch" type="text" placeholder="TC-ID or keyword" oninput="filterTCs()"/></label>
</div>

<table id="tcTable">
<thead><tr>
  <th>TC ID</th><th>Cluster</th><th>DUT Role</th><th>Mode</th>
  <th>Entities</th><th>Entity Edges</th><th>REQ Edges</th><th>PICS</th>
  <th>Intents</th><th>Issue</th>
</tr></thead>
<tbody>{tc_rows}</tbody>
</table>

<script>
function filterTCs() {{
  const cluster = document.getElementById('fCluster').value.toLowerCase();
  const role = document.getElementById('fRole').value.toLowerCase();
  const search = document.getElementById('fSearch').value.toLowerCase();
  document.querySelectorAll('#tcTable tbody tr').forEach(tr => {{
    const c = (tr.dataset.cluster || '').toLowerCase();
    const r = (tr.dataset.role || '').toLowerCase();
    const text = tr.textContent.toLowerCase();
    tr.style.display = (
      (!cluster || c === cluster) &&
      (!role || r === role) &&
      (!search || text.includes(search))
    ) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML report → {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TC Inventory & Cluster Assignment Dashboard")
    ap.add_argument("--kg", default="data/knowledge_graph/matter_kg.json")
    ap.add_argument("--cluster", default="", help="Filter by cluster (case-insensitive partial match)")
    ap.add_argument("--output", default="", help="HTML output path")
    args = ap.parse_args()

    kg_path = Path(args.kg)
    if not kg_path.exists():
        print(f"[ERROR] KG file not found: {kg_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading KG from {kg_path}…")
    tcs, prefix_map = load_kg_data(kg_path)
    print(f"  {len(prefix_map)} CLUSTER nodes with PICS prefix")
    print(f"  {len(tcs)} TEST_CASE nodes")

    if args.cluster:
        cf = args.cluster.lower()
        tcs = [t for t in tcs if cf in t["cluster"].lower()]
        print(f"  {len(tcs)} after --cluster filter ({args.cluster!r})")

    issues = [t for t in tcs if t["issue"]]
    dut_counts = Counter(t["dut_role"] for t in tcs)
    mode_counts = Counter(t["mode"] for t in tcs)

    print(f"\n{'─'*70}")
    print(f"Total TCs:   {len(tcs)}")
    print(f"Issues:      {len(issues)}")
    for it, cnt in Counter(t["issue"] for t in issues).most_common():
        print(f"  {it}: {cnt}")
    print(f"\nDUT Roles:")
    for role, cnt in dut_counts.most_common():
        print(f"  {role:30s} {cnt:4d}")
    print(f"\nModes:")
    for mode, cnt in mode_counts.most_common():
        print(f"  {mode:30s} {cnt:4d}")

    no_req = [t for t in tcs if t["verifies_req_count"] == 0]
    no_entity = [t for t in tcs if t["entity_edges_count"] == 0]
    print(f"\nTCs without verifies_requirement edges: {len(no_req)}")
    print(f"TCs without entity edges (reads/verifies_attribute/...): {len(no_entity)}")
    print(f"{'─'*70}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        out_path = Path(args.output)
    else:
        out_dir = Path("reports/verify_kg_tc_cluster_assignments")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"tc_inventory_{ts}.html"
    generate_html_report(tcs, out_path)


if __name__ == "__main__":
    main()
