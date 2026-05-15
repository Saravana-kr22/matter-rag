#!/usr/bin/env python3
"""Convert AsciiDoc files to diff-annotated HTML for the Matter RAG pipeline.

Wraps each paragraph, table cell, list item, and heading in ``<ins class="diff-new">``
tags so the pipeline's HTML processor treats all content as newly added.

Usage:
    # Single file
    python scripts/helper_scripts/adoc_to_diff_html.py /path/to/cluster.adoc

    # Directory (converts all .adoc files recursively)
    python scripts/helper_scripts/adoc_to_diff_html.py /path/to/specs/

    # Custom output directory
    python scripts/helper_scripts/adoc_to_diff_html.py /path/to/specs/ --output data/input_doc/my_diffs/

Requires: asciidoctor (brew install asciidoctor)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: beautifulsoup4 required. Install: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)


DIFF_CSS = "<style>ins.diff-new { background-color: #ffffcc; text-decoration: none; }</style>"


def convert_adoc_to_diff_html(adoc_path: Path, output_dir: Path) -> Path:
    """Convert a single .adoc file to diff-annotated HTML."""
    stem = adoc_path.stem
    tmp_html = output_dir / f"{stem}_tmp.html"
    diff_html = output_dir / f"{stem}_diff.html"

    result = subprocess.run(
        ["asciidoctor", "-o", str(tmp_html), str(adoc_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  FAIL: {adoc_path.name} — {result.stderr[:200]}", file=sys.stderr)
        return None

    soup = BeautifulSoup(tmp_html.read_text(encoding="utf-8"), "html.parser")

    for tag in soup.find_all(["p", "td", "th", "li", "dt", "dd"]):
        if tag.find("ins") or not tag.get_text(strip=True):
            continue
        original = tag.decode_contents()
        tag.clear()
        ins = soup.new_tag("ins")
        ins["class"] = ["diff-new"]
        ins.append(BeautifulSoup(original, "html.parser"))
        tag.append(ins)

    for hdr in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        original = hdr.decode_contents()
        hdr.clear()
        ins = soup.new_tag("ins")
        ins["class"] = ["diff-new"]
        ins.append(BeautifulSoup(original, "html.parser"))
        hdr.append(ins)

    head = soup.find("head")
    if head:
        style = soup.new_tag("style")
        style.string = "ins.diff-new { background-color: #ffffcc; text-decoration: none; }"
        head.append(style)

    diff_html.write_text(str(soup), encoding="utf-8")
    tmp_html.unlink(missing_ok=True)

    ins_count = len(soup.find_all("ins", class_="diff-new"))
    return diff_html, ins_count


def main():
    parser = argparse.ArgumentParser(description="Convert AsciiDoc files to diff-annotated HTML")
    parser.add_argument("input", help="Path to .adoc file or directory containing .adoc files")
    parser.add_argument("--output", "-o", default="data/input_doc/adoc_diffs",
                        help="Output directory for diff HTML files (default: data/input_doc/adoc_diffs)")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not subprocess.run(["which", "asciidoctor"], capture_output=True).returncode == 0:
        print("ERROR: asciidoctor not found. Install: brew install asciidoctor", file=sys.stderr)
        sys.exit(1)

    if input_path.is_file():
        adoc_files = [input_path]
    elif input_path.is_dir():
        adoc_files = sorted(input_path.rglob("*.adoc"))
    else:
        print(f"ERROR: {input_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Converting {len(adoc_files)} .adoc file(s) → {output_dir}/")
    success = 0
    for adoc in adoc_files:
        # Preserve subfolder structure relative to input directory
        if input_path.is_dir():
            rel = adoc.relative_to(input_path)
            target_dir = output_dir / rel.parent
        else:
            target_dir = output_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        result = convert_adoc_to_diff_html(adoc, target_dir)
        if result:
            diff_html, ins_count = result
            rel_path = diff_html.relative_to(output_dir)
            print(f"  OK: {rel_path} ({ins_count} ins.diff-new tags)")
            success += 1

    print(f"\nDone: {success}/{len(adoc_files)} converted")
    print(f"Output: {output_dir}/")
    print(f"\nRun pipeline:")
    print(f"  python scripts/run_ghpr_analysis.py --compare-only --input-doc {output_dir}/<file>_diff.html")


if __name__ == "__main__":
    main()
