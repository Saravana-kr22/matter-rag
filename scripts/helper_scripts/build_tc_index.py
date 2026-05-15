"""Build the TC routing index from adoc test plan files.

Scans all .adoc files under the adoc directory, extracts every TC-ID heading
(handles any heading level and optional bracket syntax), and writes a JSON
index to disk with three lookup maps:

    tc_map     — TC-OO-2.1  → /abs/path/to/onoff.adoc
    prefix_map — OO         → /abs/path/to/onoff.adoc
    stem_map   — onoff      → /abs/path/to/onoff.adoc

The index is read by write_updated_testplan_node to route LLM-suggested TC
updates and new TCs to the correct adoc file without guessing.

Usage
-----
    python scripts/build_tc_index.py
    python scripts/build_tc_index.py --adoc-dir data/test_plan_adocs/src
    python scripts/build_tc_index.py --output data/cache/tc_index.json
    python scripts/build_tc_index.py --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.document_updater.tc_index_builder import build_tc_index


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build tc_index.json from adoc test plan files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--adoc-dir",
        default="data/test_plan_adocs/src",
        help="Root directory containing .adoc test plan files (searched recursively). "
             "Default: data/test_plan_adocs/src",
    )
    p.add_argument(
        "--output",
        default="data/cache/tc_index.json",
        help="Path to write the index JSON. Default: data/cache/tc_index.json",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    index = build_tc_index(args.adoc_dir, args.output)

    stats = index.get("stats", {})
    print(
        f"\nTC index built successfully:\n"
        f"  adoc_root : {index.get('adoc_root')}\n"
        f"  output    : {args.output}\n"
        f"  files     : {stats.get('adoc_files_scanned', 0)}\n"
        f"  tc_ids    : {stats.get('tc_ids_indexed', 0)}\n"
        f"  prefixes  : {stats.get('prefixes_indexed', 0)}\n"
    )

    # Print first 5 entries from each map so the user can visually verify
    tc_map = index.get("tc_map", {})
    prefix_map = index.get("prefix_map", {})
    stem_map = index.get("stem_map", {})

    def _preview(label: str, d: dict, n: int = 5) -> None:
        items = list(d.items())[:n]
        if not items:
            print(f"  {label}: (empty)")
            return
        print(f"  {label} (first {min(n, len(d))} of {len(d)}):")
        for k, v in items:
            print(f"    {k!r:30s} → {Path(v).name}")

    _preview("tc_map", tc_map)
    _preview("prefix_map", prefix_map)
    _preview("stem_map", stem_map)


if __name__ == "__main__":
    main()
