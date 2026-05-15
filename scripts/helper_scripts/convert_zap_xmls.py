#!/usr/bin/env python3
"""Convert ZAP-format cluster extension XMLs to standard DM XML format.

Recursively scans an input directory for XML files using the ZAP
``<configurator>`` / ``<clusterExtension>`` format and converts each to
the standard ``<cluster>`` DM XML format used by the pipeline.

Usage:
    python scripts/helper_scripts/convert_zap_xmls.py \\
        --input /path/to/extensions/code/ \\
        --output /path/to/workspace/data/dm_xmls/

    # With explicit cluster name mapping
    python scripts/helper_scripts/convert_zap_xmls.py \\
        --input /path/to/extensions/code/ \\
        --output /path/to/workspace/data/dm_xmls/ \\
        --base-dm-dir data/data_model
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.fetcher.sources.zap_xml_adapter import convert_zap_to_dm_xml, is_zap_format, is_device_type_xml


def _build_cluster_name_lookup(base_dm_dir: str) -> dict:
    """Build {cluster_id_int: cluster_name} from standard DM XML files."""
    import xml.etree.ElementTree as ET

    lookup = {}
    dm_path = Path(base_dm_dir)
    if not dm_path.is_dir():
        return lookup

    for xml_file in dm_path.glob("*.xml"):
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            # Standard DM XML: <cluster name="X" id="0xNNNN">
            cluster_name = root.get("name", "")
            cluster_id_str = root.get("id", "")
            if cluster_name and cluster_id_str:
                try:
                    cid = int(cluster_id_str, 16) if cluster_id_str.startswith("0x") else int(cluster_id_str)
                    lookup[cid] = cluster_name
                except ValueError:
                    pass
            # Also check <clusterIds> for multi-ID clusters
            for cid_el in root.iter("clusterId"):
                cid_str = cid_el.get("id", "")
                cname = cid_el.get("name", "")
                if cid_str and cname:
                    try:
                        cid = int(cid_str, 16) if cid_str.startswith("0x") else int(cid_str)
                        lookup[cid] = cname
                    except ValueError:
                        pass
        except Exception:
            continue

    return lookup


def main():
    parser = argparse.ArgumentParser(
        description="Convert ZAP-format cluster extension XMLs to standard DM XML format"
    )
    parser.add_argument("--input", "-i", required=True,
                        help="Input directory to scan recursively for ZAP XML files")
    parser.add_argument("--output", "-o", required=True,
                        help="Output directory for converted DM XML files")
    parser.add_argument("--base-dm-dir", default="data/data_model",
                        help="Base DM XML directory for cluster name resolution (default: data/data_model)")
    parser.add_argument("--skip-standard", action="store_true",
                        help="Skip standard (non-ZAP) XML files instead of copying them")
    parser.add_argument("--pics-map", default="",
                        help="Comma-separated PICS code overrides: 'ClusterName=CODE,Other=XYZ'. "
                             "Overrides auto-derived PICS codes for specific clusters.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)

    if not input_dir.is_dir():
        print(f"ERROR: Input directory does not exist: {input_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build cluster name lookup from base DM XMLs
    cluster_lookup = _build_cluster_name_lookup(args.base_dm_dir)
    if cluster_lookup:
        print(f"Loaded {len(cluster_lookup)} cluster names from {args.base_dm_dir}")
    else:
        print(f"WARNING: No cluster names loaded from {args.base_dm_dir} — "
              "extension clusters will use generic names", file=sys.stderr)

    # Parse PICS code overrides
    pics_overrides: dict = {}
    if args.pics_map:
        for pair in args.pics_map.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                pics_overrides[k.strip()] = v.strip()
        if pics_overrides:
            print(f"PICS code overrides: {pics_overrides}")

    # Find all XML files recursively
    xml_files = sorted(input_dir.rglob("*.xml"))
    print(f"Found {len(xml_files)} XML file(s) in {input_dir}")

    converted = 0
    copied = 0
    skipped = 0

    for xml_file in xml_files:
        if is_zap_format(xml_file):
            if is_device_type_xml(xml_file):
                print(f"  SKIPPED: {xml_file.name} (device type XML — not a cluster definition)")
                skipped += 1
                continue
            try:
                dm_xml_str = convert_zap_to_dm_xml(xml_file, cluster_name_lookup=cluster_lookup, pics_code_overrides=pics_overrides or None)
                if dm_xml_str:
                    out_name = xml_file.stem + "_dm.xml"
                    out_path = output_dir / out_name
                    out_path.write_text(dm_xml_str, encoding="utf-8")
                    print(f"  CONVERTED: {xml_file.name} → {out_name}")
                    converted += 1
                else:
                    print(f"  SKIPPED: {xml_file.name} (no cluster or clusterExtension found)")
                    skipped += 1
            except Exception as exc:
                print(f"  ERROR: {xml_file.name}: {exc}", file=sys.stderr)
                skipped += 1
        elif not args.skip_standard:
            import shutil
            out_path = output_dir / xml_file.name
            shutil.copy2(xml_file, out_path)
            print(f"  COPIED: {xml_file.name} (standard format)")
            copied += 1
        else:
            skipped += 1

    print(f"\nDone: {converted} converted, {copied} copied, {skipped} skipped")
    print(f"Output: {output_dir}/")


if __name__ == "__main__":
    main()
