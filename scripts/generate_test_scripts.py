#!/usr/bin/env python3
"""Generate Python test scripts from pipeline report_data using the Claude CLI.

Reads TC specifications from a pipeline report, assembles a focused prompt per TC,
calls ``claude --print`` to generate each script, and writes the output to files.

No API key required — uses the local ``claude`` CLI auth.

Usage:
    python scripts/generate_test_scripts.py \\
        --reports reports/matter_rag_reports_20260505_*/report_data*.json \\
        --sdk-path /path/to/connectedhomeip \\
        --output-dir /path/to/output

    # With vendor-specific context
    python scripts/generate_test_scripts.py \\
        --reports /path/to/report_data.json \\
        --sdk-path /path/to/connectedhomeip \\
        --context /path/to/context.md \\
        --output-dir /path/to/output \\
        --workers 4

    # Single TC
    python scripts/generate_test_scripts.py \\
        --reports /path/to/report_data.json \\
        --sdk-path /path/to/connectedhomeip \\
        --tc TC-XYZ-2.1 \\
        --output-dir /path/to/output
"""

import argparse
import glob
import json
import logging
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _find_claude_bin() -> str:
    result = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR: claude CLI not found on PATH.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def _call_claude(prompt: str, claude_bin: str, timeout: int = 600) -> str:
    proc = subprocess.Popen(
        [claude_bin, "--print", "--output-format", "text"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (code {proc.returncode}): {stderr[:300]}")
    return stdout


def _load_report(report_path: str) -> list:
    data = json.loads(Path(report_path).read_text())
    tcs = list(data.get("missing_tests", []))
    tcs.extend(data.get("coverage_gap_tests", []))
    return tcs


# Reference script selection by TC characteristics
_REFERENCE_PROFILES = {
    "attribute": [
        "TC_DGSW_2_1.py",      # diagnostics attribute read
        "TC_BINFO_3_1.py",     # basic info attributes
    ],
    "command": [
        "TC_DRLK_2_2.py",     # door lock commands
        "TC_LVL_2_2.py",      # level control commands
    ],
    "event": [
        "TC_SMOKECO_2_1.py",   # smoke/CO alarm events
        "TC_BOOLCFG_5_1.py",   # boolean state events
    ],
    "subscription": [
        "TC_DGSW_2_2.py",     # diagnostics subscription
    ],
    "lifecycle": [
        "TC_CADMIN_1_3.py",    # commissioning lifecycle
    ],
}


def _detect_tc_profile(tc: dict) -> str:
    """Classify a TC into a reference profile based on its spec content."""
    adoc = (tc.get("adoc_section", "") + " " + tc.get("title", "")).lower()

    if any(kw in adoc for kw in ["subscri", "report", "notification"]):
        return "subscription"
    if any(kw in adoc for kw in ["event", "observe", "trigger"]):
        return "event"
    if any(kw in adoc for kw in ["send command", "invoke", "sends", "command"]):
        # Check if it's primarily a command test vs just having a commissioning step
        cmd_keywords = sum(1 for kw in ["send command", "invoke", "sends command", "th sends"]
                           if kw in adoc)
        attr_keywords = sum(1 for kw in ["reads", "read attribute", "th reads", "write attribute"]
                            if kw in adoc)
        if cmd_keywords > attr_keywords:
            return "command"
    if any(kw in adoc for kw in ["lifecycle", "commission", "fabric", "power cycle"]):
        return "lifecycle"
    return "attribute"


def _find_reference_script(sdk_path: str, profile: str = "attribute") -> str:
    """Find the best reference script for the given TC profile."""
    testing_dir = Path(sdk_path) / "src" / "python_testing"
    candidates = _REFERENCE_PROFILES.get(profile, _REFERENCE_PROFILES["attribute"])

    for name in candidates:
        p = testing_dir / name
        if p.exists():
            return p.read_text(encoding="utf-8")[:5000]

    # Fallback: try any profile's candidates
    for profile_candidates in _REFERENCE_PROFILES.values():
        for name in profile_candidates:
            p = testing_dir / name
            if p.exists():
                return p.read_text(encoding="utf-8")[:5000]
    return ""


def _find_sdk_cluster_class(sdk_path: str, cluster_name: str) -> str:
    """Search SDK for the cluster's Python class definition and return it if found."""
    objects_file = Path(sdk_path) / "src" / "controller" / "python" / "matter" / "clusters" / "Objects.py"
    if not objects_file.exists():
        objects_file = Path(sdk_path) / "src" / "controller" / "python" / "chip" / "clusters" / "Objects.py"
    if not objects_file.exists():
        return ""

    # Normalize cluster name for class lookup: "On/Off Cluster" → "OnOff"
    class_name = cluster_name.replace(" Cluster", "").replace(" ", "").replace("/", "")
    try:
        content = objects_file.read_text(encoding="utf-8")
        # Find the class definition
        pattern = f"class {class_name}(Cluster):"
        idx = content.find(pattern)
        if idx < 0:
            # Try variations
            for variant in [class_name, class_name.replace("-", "")]:
                idx = content.find(f"class {variant}(")
                if idx >= 0:
                    break
        if idx < 0:
            return ""
        # Extract ~3000 chars from the class definition (attributes, commands, enums)
        chunk = content[idx:idx + 4000]
        # Trim at the next top-level class to avoid bleeding
        next_class = chunk.find("\nclass ", 100)
        if next_class > 0:
            chunk = chunk[:next_class]
        return f"# SDK cluster class found:\n{chunk}"
    except Exception:
        return ""


def _build_prompt(tc: dict, context: str, reference: str, sdk_path: str,
                  sdk_class: str = "") -> str:
    adoc = tc.get("adoc_section", "")
    title = tc.get("title", "")
    cluster = tc.get("cluster", "")

    # Extract TC-ID from adoc or title
    m = re.search(r"TC-[A-Z]+-\d+\.\d+", adoc or title)
    tc_id = m.group(0) if m else "TC-UNKNOWN-0.0"

    prompt = (
        "You are a Matter SDK test automation engineer. Generate a COMPLETE Python test "
        "script for the following test case specification.\n\n"
    )

    if context:
        prompt += f"=== CONTEXT (follow these conventions) ===\n{context}\n\n"

    if sdk_class:
        prompt += f"=== SDK CLUSTER CLASS (use these typed APIs) ===\n{sdk_class}\n\n"

    if reference:
        prompt += (
            f"=== REFERENCE SCRIPT (match this style) ===\n"
            f"{reference}\n\n"
        )

    prompt += (
        f"=== TEST CASE SPECIFICATION ===\n"
        f"TC-ID: {tc_id}\n"
        f"Cluster: {cluster}\n\n"
        f"{adoc}\n\n"
        f"=== INSTRUCTIONS ===\n"
        f"Generate a COMPLETE Python test script implementing ALL procedure steps above.\n"
    )
    if sdk_class:
        prompt += (
            f"Use the typed SDK APIs from the cluster class above "
            f"(Clusters.{cluster.replace(' Cluster','').replace(' ','').replace('/','')}.Attributes.*, "
            f".Commands.*, etc.).\n"
        )
    else:
        prompt += (
            "This cluster is NOT in the SDK. Use raw cluster/attribute IDs as shown in the context.\n"
        )
    prompt += "Output ONLY the Python code — no markdown fences, no explanation.\n"
    prompt += "The output will be saved directly as a .py file.\n"

    return prompt


def _tc_to_filename(tc: dict) -> str:
    adoc = tc.get("adoc_section", "") or tc.get("title", "")
    m = re.search(r"TC-([A-Z]+)-(\d+)\.(\d+)", adoc)
    if m:
        prefix, major, minor = m.group(1), m.group(2), m.group(3)
        return f"TC_{prefix}_{major}_{minor}.py"
    return None


def generate_one(tc: dict, context: str, sdk_path: str,
                 output_dir: Path, claude_bin: str, timeout: int) -> dict:
    filename = _tc_to_filename(tc)
    if not filename:
        return {"file": None, "error": "Could not extract TC-ID", "title": tc.get("title", "")}

    output_path = output_dir / filename
    if output_path.exists():
        return {"file": filename, "error": None, "skipped": True, "title": tc.get("title", "")}

    # Smart reference selection based on TC profile
    profile = _detect_tc_profile(tc)
    reference = _find_reference_script(sdk_path, profile=profile)

    # Try to find SDK cluster class for typed API usage
    cluster = tc.get("cluster", "")
    sdk_class = _find_sdk_cluster_class(sdk_path, cluster) if cluster else ""

    prompt = _build_prompt(tc, context, reference, sdk_path, sdk_class=sdk_class)

    try:
        t0 = time.time()
        response = _call_claude(prompt, claude_bin, timeout=timeout)
        elapsed = time.time() - t0

        # Strip any markdown fences if present
        code = response.strip()
        if code.startswith("```"):
            code = re.sub(r"^```[a-z]*\n?", "", code)
            code = re.sub(r"\n?```$", "", code)

        output_path.write_text(code, encoding="utf-8")
        logger.info("Generated %s (%.1fs)", filename, elapsed)
        return {"file": filename, "error": None, "skipped": False, "title": tc.get("title", "")}

    except Exception as exc:
        logger.error("Failed %s: %s", filename, exc)
        return {"file": filename, "error": str(exc), "title": tc.get("title", "")}


def main():
    parser = argparse.ArgumentParser(
        description="Generate Python test scripts from pipeline reports using Claude CLI"
    )
    parser.add_argument("--reports", required=True,
                        help="Path to report_data*.json or directory containing it")
    parser.add_argument("--sdk-path", required=True,
                        help="Path to connectedhomeip SDK root")
    parser.add_argument("--context", default="",
                        help="Path to context.md file or directory containing context.md")
    parser.add_argument("--output-dir", default="reports/generated_python_tests",
                        help="Output directory for generated scripts")
    parser.add_argument("--tc", default="",
                        help="Generate only this TC-ID (e.g., TC-XYZ-2.1)")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel workers (default 2; each spawns a claude process)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout per LLM call in seconds (default 600)")
    args = parser.parse_args()

    # Resolve report path
    report_path = Path(args.reports)
    if report_path.is_dir():
        jsons = sorted(glob.glob(str(report_path / "report_data*.json")))
        if not jsons:
            print(f"ERROR: No report_data*.json found in {report_path}", file=sys.stderr)
            sys.exit(1)
        report_path = Path(jsons[-1])
    elif not report_path.exists():
        print(f"ERROR: {report_path} does not exist", file=sys.stderr)
        sys.exit(1)

    # Verify SDK
    sdk_path = Path(args.sdk_path)
    if not (sdk_path / "src" / "python_testing").exists():
        print(f"ERROR: {sdk_path}/src/python_testing/ not found", file=sys.stderr)
        sys.exit(1)

    # Load context
    context = ""
    if args.context:
        ctx_path = Path(args.context)
        if ctx_path.is_dir():
            parts = []
            for md_file in sorted(ctx_path.glob("*.md")):
                parts.append(md_file.read_text(encoding="utf-8").strip())
            context = "\n\n".join(parts)
        elif ctx_path.is_file():
            context = ctx_path.read_text(encoding="utf-8")

    # Load TCs
    tcs = _load_report(str(report_path))
    if not tcs:
        print("ERROR: No test cases found in report", file=sys.stderr)
        sys.exit(1)

    # Filter single TC if specified
    if args.tc:
        tc_filter = args.tc.upper()
        tcs = [t for t in tcs if tc_filter in (t.get("adoc_section", "") + t.get("title", "")).upper()]
        if not tcs:
            print(f"ERROR: TC {args.tc} not found in report", file=sys.stderr)
            sys.exit(1)

    # Setup
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    claude_bin = _find_claude_bin()

    print(f"Generating {len(tcs)} test script(s) → {output_dir}/")
    print(f"  SDK: {sdk_path}")
    print(f"  Context: {args.context or '(none)'}")
    print(f"  Workers: {args.workers}")
    print()

    # Generate
    results = []
    if args.workers <= 1:
        for i, tc in enumerate(tcs):
            profile = _detect_tc_profile(tc)
            print(f"  [{i+1}/{len(tcs)}] {_tc_to_filename(tc) or '?'} (profile={profile})...", flush=True)
            r = generate_one(tc, context, str(sdk_path),
                             output_dir, claude_bin, args.timeout)
            results.append(r)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(generate_one, tc, context, str(sdk_path),
                                output_dir, claude_bin, args.timeout): tc
                for tc in tcs
            }
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                done = len(results)
                if done % 5 == 0 or done == len(tcs):
                    print(f"  {done}/{len(tcs)} done", flush=True)

    # Summary
    generated = [r for r in results if r.get("file") and not r.get("error") and not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    failed = [r for r in results if r.get("error")]

    print(f"\nDone: {len(generated)} generated, {len(skipped)} skipped (exist), {len(failed)} failed")
    if generated:
        print("\nGenerated:")
        for r in sorted(generated, key=lambda x: x["file"]):
            print(f"  {r['file']}")
    if failed:
        print("\nFailed:")
        for r in failed:
            print(f"  {r.get('file', '?')}: {r['error'][:100]}")
    print(f"\nOutput: {output_dir}/")


if __name__ == "__main__":
    main()
