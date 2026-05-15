"""LangGraph nodes for the SDK coverage analysis pipeline.

Loads the knowledge graph from disk, maps spec REQUIREMENT / BEHAVIOR_RULE nodes to
SDK cluster source files (connectedhomeip src/app/clusters/), sends each cluster to the
LLM to assess which requirements are implemented / partial / not implemented, and
generates an HTML + JSON report.

State flow:
    load_sdk_stores_node
        → resolve_sdk_files_node
            → build_requirements_map_node
                → run_llm_sdk_analysis_node
                    → aggregate_sdk_findings_node
                        → generate_sdk_report_node → END
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict

from src.config.models import AppConfig
from src.engine.run_context import RunContext
from src.knowledge_graph.base_graph import NodeType, GraphNode
from src.knowledge_graph.graph_factory import create_knowledge_graph
from src.llm.llm_provider import get_llm
from src.logging_config import log_node, PipelineFatalError

logger = logging.getLogger(__name__)

# SDK source file extensions to read
_SDK_EXTENSIONS = frozenset({".cpp", ".h", ".hpp", ".cc"})
# Max total chars of SDK code to include in the LLM prompt per cluster
_MAX_SDK_CHARS = 10_000
# Max requirement nodes to include in the LLM prompt per cluster
_MAX_REQS_PER_CLUSTER = 40


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class SdkCoverageAnalysisState(TypedDict, total=False):
    config: AppConfig
    run_ctx: RunContext
    run_dir: str
    output_dir: str
    cluster_filter: str           # "" = all clusters
    max_llm_calls: int

    knowledge_graph: Any                   # MatterKGBuilder loaded from disk
    sdk_cluster_files: Dict[str, str]      # cluster_name → concatenated SDK source text
    requirements_map: Dict[str, Any]       # cluster_name → {requirements, sdk_code, cluster_dir}

    cluster_findings: List[Dict]           # [{cluster, cluster_dir, findings, stats}]
    total_not_implemented: int
    total_partial: int

    report_path: str
    errors: List[str]
    fatal_error: bool


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@log_node
def load_sdk_stores_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Load the knowledge graph and validate the SDK directory."""
    config = state["config"]

    # Validate KG
    graph_path = config.knowledge_graph.graph_store_path
    if not Path(graph_path).is_file():
        raise PipelineFatalError(
            f"Knowledge graph not found: {graph_path}. "
            "Run 'python scripts/run_ghpr_analysis.py --build-knowledge-graph' first."
        )

    # Validate SDK dir
    sdk_dir = getattr(config.analysis, "sdk_dir", "") or ""
    if not sdk_dir:
        raise PipelineFatalError(
            "config.analysis.sdk_dir is not set. "
            "Set it to the root of the connectedhomeip repo in config/config.yaml "
            "or pass --sdk-dir on the command line."
        )
    sdk_path = Path(sdk_dir)
    if not sdk_path.is_dir():
        raise PipelineFatalError(
            f"SDK directory does not exist: {sdk_dir}"
        )
    clusters_path = sdk_path / "src" / "app" / "clusters"
    if not clusters_path.is_dir():
        raise PipelineFatalError(
            f"Expected src/app/clusters/ inside SDK dir but not found: {clusters_path}"
        )

    kg = create_knowledge_graph(config.knowledge_graph)
    kg.load_from_json(graph_path)
    logger.info(
        "[load_sdk_stores_node] KG loaded: %d nodes, %d edges  |  SDK: %s",
        kg.num_nodes, kg.num_edges, clusters_path,
    )
    return {"knowledge_graph": kg}


@log_node
def resolve_sdk_files_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Map KG cluster names → SDK cluster directories and read source files."""
    config = state["config"]
    kg = state["knowledge_graph"]
    sdk_dir = Path(getattr(config.analysis, "sdk_dir", ""))
    clusters_dir = sdk_dir / "src" / "app" / "clusters"

    # Collect all cluster names from the KG
    cluster_names: List[str] = []
    for _nid, data in kg._graph.nodes(data=True):
        obj: Optional[GraphNode] = data.get("obj")
        if obj is not None and obj.node_type == NodeType.CLUSTER:
            cluster_names.append(obj.label)

    if not cluster_names:
        logger.warning("[resolve_sdk_files_node] No CLUSTER nodes found in KG.")
        return {"sdk_cluster_files": {}}

    # Build available directory name list (lowercase) for fuzzy matching
    # Include the primary SDK clusters dir
    available_dirs: Dict[str, Path] = {}
    try:
        if clusters_dir.is_dir():
            available_dirs.update({d.name.lower(): d for d in clusters_dir.iterdir() if d.is_dir()})
    except OSError as exc:
        logger.warning("[resolve_sdk_files_node] Cannot list primary clusters dir: %s", exc)

    # Also include additional SDK directories — search recursively for dirs with source files
    _additional_sdk_dirs = getattr(config.analysis, "sdk_dirs_additional", []) or []
    for extra_dir_str in _additional_sdk_dirs:
        extra_dir = Path(extra_dir_str)
        if not extra_dir.is_dir():
            logger.warning("[resolve_sdk_files_node] Additional SDK dir not found: %s", extra_dir)
            continue
        try:
            # Recursively find all directories containing .cpp or .h files
            _visited: set = set()
            for src_file in extra_dir.rglob("*.[ch]*"):
                if src_file.suffix.lower() not in (".cpp", ".h", ".c"):
                    continue
                parent = src_file.parent
                if parent in _visited:
                    continue
                _visited.add(parent)
                available_dirs.setdefault(parent.name.lower(), parent)
            # Also add immediate subdirs (even if no source files yet)
            for d in extra_dir.iterdir():
                if d.is_dir():
                    available_dirs.setdefault(d.name.lower(), d)
        except OSError as exc:
            logger.warning("[resolve_sdk_files_node] Cannot list additional SDK dir %s: %s", extra_dir, exc)

    if not available_dirs:
        logger.error("[resolve_sdk_files_node] No SDK directories found to search.")
        return {"sdk_cluster_files": {}}

    sdk_cluster_files: Dict[str, str] = {}
    matched = 0
    unmatched: List[str] = []

    for cluster_name in cluster_names:
        slug = _cluster_name_to_slug(cluster_name)
        matched_dir = _resolve_sdk_dir(slug, available_dirs)
        if matched_dir is None:
            unmatched.append(cluster_name)
            continue

        code_text = _read_cluster_dir(matched_dir)
        if not code_text.strip():
            unmatched.append(cluster_name)
            continue

        sdk_cluster_files[cluster_name] = code_text
        matched += 1
        logger.debug(
            "[resolve_sdk_files_node] %r → %s (%d chars)",
            cluster_name, matched_dir.name, len(code_text),
        )

    logger.info(
        "[resolve_sdk_files_node] Matched %d/%d clusters to SDK dirs. "
        "Unmatched (%d): %s",
        matched, len(cluster_names), len(unmatched),
        ", ".join(unmatched[:10]) + ("…" if len(unmatched) > 10 else ""),
    )
    return {"sdk_cluster_files": sdk_cluster_files}


@log_node
def build_requirements_map_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Pull REQUIREMENT/BEHAVIOR_RULE nodes per cluster; join with SDK code."""
    kg = state["knowledge_graph"]
    sdk_cluster_files: Dict[str, str] = state.get("sdk_cluster_files", {})
    cluster_filter = (state.get("cluster_filter") or "").lower()

    req_by_cluster: Dict[str, List[GraphNode]] = {}
    for _nid, data in kg._graph.nodes(data=True):
        obj: Optional[GraphNode] = data.get("obj")
        if obj is None:
            continue
        if obj.node_type not in {NodeType.REQUIREMENT, NodeType.BEHAVIOR_RULE}:
            continue
        cluster = (obj.properties.get("cluster") or "").strip()
        if not cluster:
            continue
        if cluster_filter and cluster_filter not in cluster.lower():
            continue
        if cluster not in sdk_cluster_files:
            continue  # No SDK code for this cluster — skip
        req_by_cluster.setdefault(cluster, []).append(obj)

    # Apply cluster_filter to sdk_cluster_files too (for clusters with no reqs)
    requirements_map: Dict[str, Any] = {}
    for cluster_name, reqs in req_by_cluster.items():
        sdk_code = sdk_cluster_files[cluster_name]
        # Find the matching dir name for use in the LLM prompt
        cluster_dir = _cluster_name_to_slug(cluster_name)
        requirements_map[cluster_name] = {
            "requirements": reqs[:_MAX_REQS_PER_CLUSTER],
            "sdk_code": sdk_code,
            "cluster_dir": cluster_dir,
        }

    total_reqs = sum(len(v["requirements"]) for v in requirements_map.values())
    logger.info(
        "[build_requirements_map_node] %d clusters with SDK code, %d requirements to check",
        len(requirements_map), total_reqs,
    )
    return {"requirements_map": requirements_map}


@log_node
def run_llm_sdk_analysis_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Send each cluster's requirements + SDK code to the LLM for coverage assessment."""
    config = state["config"]
    requirements_map: Dict[str, Any] = state.get("requirements_map", {})
    _raw = state.get("max_llm_calls")
    if _raw is None:
        _raw = getattr(config.analysis, "max_llm_calls_per_run", 0)
    max_calls = _raw if _raw > 0 else 999_999
    errors: List[str] = list(state.get("errors") or [])

    if not requirements_map:
        logger.warning("[run_llm_sdk_analysis_node] No requirements/SDK data to analyze.")
        return {"cluster_findings": [], "errors": errors}

    llm = get_llm(config.llm, log_dir=state.get("run_dir"))
    findings: List[Dict] = []
    calls_made = 0

    _total_sdk_clusters = len(requirements_map)
    for cluster_name, cdata in sorted(requirements_map.items()):
        reqs: List[GraphNode] = cdata["requirements"]
        sdk_code: str = cdata["sdk_code"]
        cluster_dir: str = cdata["cluster_dir"]

        if not reqs:
            continue

        if calls_made >= max_calls:
            logger.info(
                "[run_llm_sdk_analysis_node] max_llm_calls=%d reached — stopping.", max_calls
            )
            break

        # Build requirement lines for prompt
        req_lines = []
        for req in reqs:
            text = (req.properties.get("normative_text") or req.label or "")[:300]
            req_type = req.properties.get("requirement_type", "")
            confidence = req.properties.get("confidence", 1.0)
            req_lines.append(
                f"REQ: {req.node_id}\n"
                f"  type: {req_type}  confidence: {confidence:.2f}\n"
                f"  text: {text!r}"
            )
        req_block = "\n\n".join(req_lines)

        prompt = (
            "You are a Matter SDK expert reviewing spec requirement implementation.\n\n"
            f"=== CLUSTER: {cluster_name} ===\n\n"
            f"SDK SOURCE CODE (src/app/clusters/{cluster_dir}/):\n"
            "```cpp\n"
            f"{sdk_code}\n"
            "```\n\n"
            f"SPEC REQUIREMENTS ({len(reqs)} total):\n"
            f"{req_block}\n\n"
            "TASK — SDK COVERAGE ANALYSIS:\n"
            "For each requirement determine:\n"
            "  a) implemented   — clearly addressed in the code above (cite function/variable)\n"
            "  b) partial       — some handling exists but requirement not fully addressed\n"
            "  c) not_implemented — no corresponding implementation found in the code\n"
            "  d) not_applicable  — about test tooling, spec formatting, or informative text only\n\n"
            "Return ONLY valid JSON (no markdown fences, no explanation outside the JSON):\n"
            '{"sdk_findings": ['
            '{"req_id": "<node_id>", '
            '"normative_text": "<truncated to 150 chars>", '
            '"status": "implemented|partial|not_implemented|not_applicable", '
            '"evidence": "<function name or code line if implemented/partial, else empty string>", '
            '"notes": "<1-sentence explanation for partial or not_implemented, else empty string>"}]}\n\n'
            "Include ALL requirements in the response (even implemented ones)."
        )

        try:
            _remaining = _total_sdk_clusters - (calls_made + 1)
            print(
                f"[sdk_coverage] LLM call {calls_made + 1}/{_total_sdk_clusters}"
                f" — {_remaining} remaining: cluster={cluster_name!r}",
                flush=True,
            )
            if hasattr(llm, "set_next_label"):
                llm.set_next_label(f"SDK coverage — {cluster_name}")
            response = llm.complete(prompt)
            calls_made += 1
            parsed = _parse_llm_json(response, context=cluster_name)
            sdk_findings = parsed.get("sdk_findings", [])

            # Tally stats
            stats: Dict[str, int] = {
                "implemented": 0, "partial": 0,
                "not_implemented": 0, "not_applicable": 0,
            }
            for item in sdk_findings:
                s = item.get("status", "not_implemented")
                if s in stats:
                    stats[s] += 1

            findings.append({
                "cluster": cluster_name,
                "cluster_dir": cluster_dir,
                "findings": sdk_findings,
                "stats": stats,
            })
            logger.info(
                "[run_llm_sdk_analysis_node] cluster=%r  implemented=%d  partial=%d  "
                "not_implemented=%d  not_applicable=%d",
                cluster_name,
                stats["implemented"], stats["partial"],
                stats["not_implemented"], stats["not_applicable"],
            )
        except Exception as exc:
            msg = f"LLM call failed for cluster {cluster_name!r}: {exc}"
            logger.error("[run_llm_sdk_analysis_node] %s", msg)
            errors.append(msg)

    logger.info(
        "[run_llm_sdk_analysis_node] done: %d clusters, %d LLM calls made",
        len(findings), calls_made,
    )
    return {"cluster_findings": findings, "errors": errors}


@log_node
def aggregate_sdk_findings_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Count total not-implemented and partial requirements across all clusters."""
    findings: List[Dict] = state.get("cluster_findings", [])
    total_not_implemented = sum(
        f.get("stats", {}).get("not_implemented", 0) for f in findings
    )
    total_partial = sum(
        f.get("stats", {}).get("partial", 0) for f in findings
    )
    logger.info(
        "[aggregate_sdk_findings_node] not_implemented=%d  partial=%d",
        total_not_implemented, total_partial,
    )
    return {
        "total_not_implemented": total_not_implemented,
        "total_partial": total_partial,
    }


@log_node
def generate_sdk_report_node(state: SdkCoverageAnalysisState) -> SdkCoverageAnalysisState:
    """Write HTML and JSON reports for SDK coverage analysis results."""
    config = state["config"]
    findings: List[Dict] = state.get("cluster_findings", [])
    total_not_implemented = state.get("total_not_implemented", 0)
    total_partial = state.get("total_partial", 0)
    output_dir = (
        state.get("output_dir")
        or getattr(config.analysis, "output_dir", "reports")
    )

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"matter_rag_reports_{ts}_sdk_coverage"
    out_path.mkdir(parents=True, exist_ok=True)

    # JSON sidecar
    json_path = out_path / f"sdk_coverage_analysis_{ts}.json"
    json_data = {
        "timestamp": ts,
        "total_not_implemented": total_not_implemented,
        "total_partial": total_partial,
        "clusters_analyzed": len(findings),
        "findings": findings,
    }
    json_path.write_text(json.dumps(json_data, indent=2))

    # HTML report
    html_path = out_path / f"sdk_coverage_analysis_{ts}.html"
    html_path.write_text(_build_sdk_html(findings, total_not_implemented, total_partial, ts))

    logger.info(
        "[generate_sdk_report_node] Report written: %s  "
        "(not_implemented=%d  partial=%d)",
        html_path, total_not_implemented, total_partial,
    )

    import shutil as _shutil
    run_dir = state.get("run_dir", "")
    if run_dir:
        _src = Path(run_dir) / "llm_calls.html"
        if _src.is_file():
            _shutil.copy2(str(_src), str(out_path / "llm_calls.html"))
            logger.info("[generate_sdk_report_node] Copied llm_calls.html → %s", out_path)

    return {"report_path": str(html_path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cluster_name_to_slug(cluster_name: str) -> str:
    """Convert a cluster name like 'On/Off Cluster' to an SDK dir slug 'on-off'."""
    slug = cluster_name.lower()
    # Strip trailing "cluster" word
    slug = re.sub(r"\s+cluster\s*$", "", slug).strip()
    # Replace non-alphanum (spaces, slashes, parens, dots) with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Collapse and trim hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _resolve_sdk_dir(slug: str, available_dirs: Dict[str, Path]) -> Optional[Path]:
    """Fuzzy-resolve a cluster slug to an SDK clusters subdirectory."""
    # 1. Exact match
    if slug in available_dirs:
        return available_dirs[slug]

    # 2. Dash-stripped exact/substring match (handles CamelCase slugs vs dashed dir names)
    # Run this BEFORE loose substring match to prefer precise vendor-specific dirs
    slug_nodash = slug.replace("-", "")
    best_nodash: Optional[Path] = None
    best_nodash_len = 0
    for dname, dpath in available_dirs.items():
        dname_nodash = dname.replace("-", "")
        # Strip common suffixes from dir name
        for suffix in ("server", "client"):
            if dname_nodash.endswith(suffix):
                dname_stripped = dname_nodash[: -len(suffix)]
                break
        else:
            dname_stripped = dname_nodash
        if slug_nodash == dname_stripped:
            return dpath  # exact match after stripping — best possible
        if slug_nodash in dname_stripped or dname_stripped in slug_nodash:
            common = len(min(slug_nodash, dname_stripped, key=len))
            if common > best_nodash_len and common >= 6:
                best_nodash_len = common
                best_nodash = dpath
    if best_nodash and best_nodash_len > len(slug) * 0.7:
        return best_nodash

    # 3. Prefix match (either direction)
    for dname, dpath in available_dirs.items():
        if dname.startswith(slug) or slug.startswith(dname):
            if len(min(slug, dname, key=len)) >= 4:
                return dpath

    # 4. Substring match (slug inside dir name or vice versa)
    best: Optional[Path] = None
    best_len = 0
    for dname, dpath in available_dirs.items():
        if slug in dname or dname in slug:
            common = len(min(slug, dname, key=len))
            if common > best_len:
                best_len = common
                best = dpath
    if best and best_len >= 4:
        return best

    # 5. Return best dash-stripped match if any (fallback)
    return best_nodash


def _read_cluster_dir(directory: Path) -> str:
    """Read all SDK source files in a directory; concatenate and cap at _MAX_SDK_CHARS."""
    parts: List[str] = []
    total = 0
    for f in sorted(directory.iterdir()):
        if not f.is_file() or f.suffix.lower() not in _SDK_EXTENSIONS:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"// ===== {f.name} =====\n"
        parts.append(header + text)
        total += len(header) + len(text)
        if total >= _MAX_SDK_CHARS:
            break

    combined = "\n\n".join(parts)
    if len(combined) <= _MAX_SDK_CHARS:
        return combined
    # Keep first 8k + last 2k to preserve header and implementation tail
    return combined[:8000] + "\n\n// ... (truncated) ...\n\n" + combined[-2000:]


def _extract_json_balanced(text: str) -> str:
    """Extract the first balanced JSON object using brace depth tracking."""
    start = text.find('{')
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return ""


def _parse_llm_json(response: str, context: str = "") -> Dict:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        balanced = _extract_json_balanced(text)
        if balanced:
            try:
                return json.loads(balanced)
            except json.JSONDecodeError:
                pass
    logger.warning("[_parse_llm_json] Failed to parse LLM JSON response: %r", response[:300])
    try:
        from src.llm.call_logger import log_parse_error as _log_pe
        _log_pe(f"SDK coverage analysis{' — ' + context if context else ''}", response[:500])
    except Exception:
        pass
    return {}


def _build_sdk_html(
    findings: List[Dict],
    total_not_implemented: int,
    total_partial: int,
    ts: str,
) -> str:
    """Render the HTML report for SDK coverage analysis."""
    # Aggregate totals
    total_implemented = sum(f.get("stats", {}).get("implemented", 0) for f in findings)
    total_na = sum(f.get("stats", {}).get("not_applicable", 0) for f in findings)
    total_reqs = total_implemented + total_partial + total_not_implemented + total_na
    pct = lambda n: f"{100 * n / total_reqs:.0f}%" if total_reqs else "0%"

    # Cluster summary rows (sorted by not_implemented desc)
    cluster_rows = "".join(
        f"<tr>"
        f"<td>{f['cluster']}</td>"
        f"<td><code>{f['cluster_dir']}</code></td>"
        f"<td>{f['stats'].get('implemented', 0)}</td>"
        f"<td style='color:#ffb74d'>{f['stats'].get('partial', 0)}</td>"
        f"<td style='color:#ef9a9a'>{f['stats'].get('not_implemented', 0)}</td>"
        f"<td style='color:#90caf9'>{f['stats'].get('not_applicable', 0)}</td>"
        f"</tr>\n"
        for f in sorted(
            findings, key=lambda entry: -(entry.get("stats", {}).get("not_implemented", 0))
        )
        if any(v > 0 for v in f.get("stats", {}).values())
    ) or "<tr><td colspan='6' style='color:#6a9955'>No clusters analyzed.</td></tr>\n"

    # Flatten not_implemented rows
    def _rows_for_status(status: str, color: str) -> str:
        rows = []
        for finding in findings:
            cluster = finding["cluster"]
            for item in finding.get("findings", []):
                if item.get("status") != status:
                    continue
                req_id = item.get("req_id", "")
                text = (item.get("normative_text") or "")[:200]
                evidence = item.get("evidence", "")
                notes = item.get("notes", "")
                rows.append(
                    f"<tr>"
                    f"<td>{cluster}</td>"
                    f"<td><code>{req_id}</code></td>"
                    f"<td>{text}</td>"
                    f"<td style='color:{color}'><code>{evidence}</code></td>"
                    f"<td>{notes}</td>"
                    f"</tr>\n"
                )
        return "".join(rows) or f"<tr><td colspan='5' style='color:#6a9955'>None.</td></tr>\n"

    not_impl_rows = _rows_for_status("not_implemented", "#ef9a9a")
    partial_rows = _rows_for_status("partial", "#ffb74d")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>SDK Coverage Analysis — {ts}</title>
<style>
  body {{ font-family: monospace; background:#1e1e1e; color:#d4d4d4; padding:20px; }}
  h1, h2 {{ color:#4ec9b0; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:30px; }}
  th {{ background:#264f78; color:#d4d4d4; text-align:left; padding:8px; }}
  td {{ border-bottom:1px solid #3c3c3c; padding:8px; vertical-align:top; }}
  tr:hover {{ background:#2d2d2d; }}
  code {{ color:#ce9178; }}
  .summary {{ background:#264f78; padding:10px; margin-bottom:20px; border-radius:4px; }}
  .pill {{ display:inline-block; padding:2px 8px; border-radius:4px; margin-right:8px; }}
  .pill-red {{ background:#b71c1c; }}
  .pill-amber {{ background:#e65100; }}
  .pill-green {{ background:#1b5e20; }}
</style>
</head>
<body>
<h1>SDK Coverage Analysis Report</h1>
<p>Generated: {ts} &nbsp;|&nbsp; Pipeline: sdk_coverage_analysis</p>

<div class="summary">
  <strong>Requirements checked: {total_reqs}</strong>
  &nbsp;|&nbsp; Clusters analyzed: {len(findings)}
  &nbsp;&nbsp;
  <span class="pill pill-green">Implemented: {total_implemented} ({pct(total_implemented)})</span>
  <span class="pill pill-amber">Partial: {total_partial} ({pct(total_partial)})</span>
  <span class="pill pill-red">Not implemented: {total_not_implemented} ({pct(total_not_implemented)})</span>
  <span class="pill">N/A: {total_na} ({pct(total_na)})</span>
</div>

<h2>Cluster Summary</h2>
<table>
<tr>
  <th>Cluster</th>
  <th>SDK Directory</th>
  <th style='color:#a5d6a7'>Implemented</th>
  <th style='color:#ffb74d'>Partial</th>
  <th style='color:#ef9a9a'>Not Implemented</th>
  <th style='color:#90caf9'>N/A</th>
</tr>
{cluster_rows}
</table>

<h2>Not Implemented Requirements</h2>
<table>
<tr>
  <th>Cluster</th>
  <th>Requirement ID</th>
  <th>Normative Text</th>
  <th>Evidence</th>
  <th>Notes</th>
</tr>
{not_impl_rows}
</table>

<h2>Partial Implementation</h2>
<table>
<tr>
  <th>Cluster</th>
  <th>Requirement ID</th>
  <th>Normative Text</th>
  <th>Evidence</th>
  <th>Notes</th>
</tr>
{partial_rows}
</table>
</body>
</html>"""
