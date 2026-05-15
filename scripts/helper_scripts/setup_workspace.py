#!/usr/bin/env python3
"""Set up an external workspace for Matter RAG pipeline extensions.

Creates the directory structure, overlay config, and placeholder files needed
to run the pipeline with additional data sources (DM XMLs, test plans, spec HTML).

Usage:
    python scripts/helper_scripts/setup_workspace.py /path/to/my-workspace
    python scripts/helper_scripts/setup_workspace.py /path/to/my-workspace --name "My Extensions"
"""

import argparse
import sys
from pathlib import Path


_OVERLAY_CONFIG = """\
# Matter RAG — Overlay Config
# Pass this file to the pipeline via: --additional-config {config_path}
# All relative paths (./...) resolve relative to THIS file's directory.

pipeline:
  output_dir: ../reports
  logs_dir: ../logs
  llm_additional_context: ../llm_prompts

knowledge_graph:
  graph_store_path: ../data/knowledge_graph/matter_kg.json

analysis:
  dm_dirs_additional:
    - ../data/dm_xmls
  additional_test_plans_dir: ../data/test_plans
  additional_spec_dir: ../data/spec_html
"""

_ALL_MD = """\
# Additional LLM Context — All Passes
#
# This file is injected into ALL LLM passes (Pass 1, 2, 3).
# Add general context about your extensions here.
#
# Examples:
#   - "These are proprietary cluster extensions not in the standard Matter spec."
#   - "There are no existing test cases — generate all TCs from scratch."
"""

_PASS1_MD = """\
# Pass 1 — Analysis Context
#
# Injected into Pass 1 only (change classification / TC identification).
#
# Examples:
#   - "action should always be 'add_new' for new clusters"
#   - "Generate TC IDs using the cluster's natural PICS prefix"
"""

_PASS2_MD = """\
# Pass 2 — TC Generation Context
#
# Injected into Pass 2 only (TC outline + expand).
#
# Examples:
#   - "Minimum 8 procedure steps per TC"
#   - "Include boundary testing for all numeric attributes"
"""

_PASS3_MD = """\
# Pass 3 — Expansion Context
#
# Injected into Pass 3 only (human outline re-expand).
#
# Examples:
#   - "Use AsciiDoc table format for procedure steps"
#   - "Include Specification Mapping section"
"""

_GITIGNORE = """\
# Ignore generated/proprietary data
data/
reports/
logs/
"""


def main():
    parser = argparse.ArgumentParser(
        description="Set up an external workspace for Matter RAG pipeline extensions"
    )
    parser.add_argument("path", help="Path to create the workspace directory")
    parser.add_argument("--name", default="Extensions",
                        help="Workspace name (used in comments, default: Extensions)")
    args = parser.parse_args()

    root = Path(args.path)

    if root.exists() and any(root.iterdir()):
        print(f"WARNING: {root} already exists and is not empty.", file=sys.stderr)
        resp = input("Continue anyway? [y/N] ")
        if resp.lower() != "y":
            sys.exit(0)

    # Create directory structure
    dirs = [
        root / "config",
        root / "data" / "dm_xmls",
        root / "data" / "test_plans",
        root / "data" / "spec_html",
        root / "data" / "input_doc",
        root / "data" / "knowledge_graph",
        root / "data" / "faiss_index",
        root / "llm_prompts",
        root / "reports",
        root / "logs",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Write overlay config
    config_path = root / "config" / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            _OVERLAY_CONFIG.replace("{config_path}", str(config_path)),
            encoding="utf-8",
        )

    # Write LLM prompt placeholders
    prompts_dir = root / "llm_prompts"
    for name, content in [
        ("all.md", _ALL_MD),
        ("pass1.md", _PASS1_MD),
        ("pass2.md", _PASS2_MD),
        ("pass3.md", _PASS3_MD),
    ]:
        p = prompts_dir / name
        if not p.exists():
            p.write_text(content, encoding="utf-8")

    # Write .gitignore
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")

    # Print summary
    print(f"\nWorkspace created at: {root.resolve()}\n")
    print("Directory structure:")
    print(f"  {root}/")
    print(f"    config/                  ← configuration")
    print(f"      config.yaml            ← overlay config (use with --additional-config)")
    print(f"    llm_prompts/             ← per-pass LLM context")
    print(f"      all.md, pass1.md, pass2.md, pass3.md")
    print(f"    data/                    ← proprietary data (gitignored)")
    print(f"      dm_xmls/              ← put DM XML cluster files here")
    print(f"      test_plans/           ← put test plan HTMLs here")
    print(f"      spec_html/            ← put spec diff HTMLs here")
    print(f"      input_doc/            ← put PR diff HTMLs here")
    print(f"      knowledge_graph/      ← merged KG stored here")
    print(f"      faiss_index/          ← merged vector DB stored here")
    print(f"    reports/                 ← pipeline output")
    print(f"    logs/                    ← per-run logs")
    print(f"    .gitignore               ← ignores data/, reports/, logs/")
    print()
    print("Next steps:")
    print(f"  1. Place your DM XML files in:    {root}/data/dm_xmls/")
    print(f"  2. Place your test plan HTMLs in: {root}/data/test_plans/")
    print(f"  3. Place your spec HTMLs in:      {root}/data/spec_html/")
    print(f"  4. Edit llm_prompts/ files with your domain-specific instructions")
    print(f"  5. Build the merged KG:")
    print(f"       python scripts/run_ghpr_analysis.py --build-knowledge-graph \\")
    print(f"         --additional-config {config_path}")
    print(f"  6. Run analysis:")
    print(f"       python scripts/run_ghpr_analysis.py --compare-only \\")
    print(f"         --input-doc {root}/data/input_doc/my_diff.html \\")
    print(f"         --additional-config {config_path}")


if __name__ == "__main__":
    main()
