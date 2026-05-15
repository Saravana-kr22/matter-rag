"""Generate LLM-based summaries for protocol-area spec sections.

Reads prompt_sections entries from config.yaml that have a ``summary_file`` field,
extracts the relevant sections from the spec HTML files, calls the configured LLM to
produce a structured Markdown summary, and saves it to the summary_file path.

Run this script once before the pipeline when adding new protocol area summaries, or
whenever the spec HTML files are updated and the summaries need refreshing.

Usage::

    # Use only index.html (avoids appclusters.html cluster DM tables)
    python scripts/helper_scripts/generate_spec_summaries.py --section "DD Protocol" --input-doc data/matter_spec/index.html

    # Same for DD Commissioning Flows
    python scripts/helper_scripts/generate_spec_summaries.py --section "DD Commissioning Flows" --input-doc data/matter_spec/index.html

    # Multiple explicit files
    python scripts/helper_scripts/generate_spec_summaries.py --input-doc data/matter_spec/index.html --input-doc data/matter_spec/other.html

    # Generate all missing summaries (scans spec-dir)
    python scripts/helper_scripts/generate_spec_summaries.py

    # Regenerate a specific section (by label)
    python scripts/helper_scripts/generate_spec_summaries.py --section "SC Protocol"

    # Force-regenerate all (even existing files)
    python scripts/helper_scripts/generate_spec_summaries.py --force

    # Use a custom spec HTML directory
    python scripts/helper_scripts/generate_spec_summaries.py --spec-dir data/other_spec

    # Point at a non-default config
    python scripts/helper_scripts/generate_spec_summaries.py --config config/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path so src.* imports work when run from any directory
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*\.\s+")


def _strip_nums(path: str) -> str:
    return " > ".join(_SECTION_NUM_RE.sub("", seg.strip()) for seg in path.split(" > "))


def _parse_html_spec(html_path: Path) -> list[dict]:
    """Parse a spec HTML file via html_semantic_parser → list of section dicts."""
    from src.processor.html_semantic_parser import parse_spec  # type: ignore
    import re as _re

    def _strip_html(text: str) -> str:
        return _re.sub(r"<[^>]+>", " ", text or "").strip()

    html = html_path.read_text(encoding="utf-8", errors="replace")
    result = parse_spec(html, doc_id=html_path.name)
    out = []
    for sec in result.get("sections", []):
        heading = _strip_html(sec.get("title") or sec.get("heading") or "")
        full_text = _strip_html(sec.get("full_text") or "")
        if not full_text:
            full_text = "\n\n".join(
                _strip_html(c.get("text", ""))
                for c in sec.get("chunks", [])
                if c.get("text")
            )
        path_parts = sec.get("section_path") or sec.get("path") or [heading]
        section_path = " > ".join(str(p) for p in path_parts)
        out.append({"heading": heading, "full_text": full_text, "section_path": section_path})
    return out


def _collect_section_text(
    spec_sections: list[dict],
    path_prefix: str,
    source_char_limit: int = 60_000,
) -> tuple[str, list[str]]:
    """Return (concatenated_text, matched_paths) for sections matching path_prefix."""
    prefix_norm = _strip_nums(path_prefix).lower()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sec in spec_sections:
        sp = sec["section_path"]
        sp_norm = _strip_nums(sp).lower()
        if prefix_norm not in sp_norm:
            continue
        txt = sec["full_text"].strip()
        if not txt or sp in seen:
            continue
        seen.add(sp)
        hits.append((sp, txt))

    hits.sort(key=lambda t: t[0])
    parts: list[str] = []
    total = 0
    for sp, txt in hits:
        if total + len(txt) > source_char_limit:
            remaining = source_char_limit - total
            if remaining > 200:
                parts.append(f"--- {sp} ---\n{txt[:remaining]}")
            break
        parts.append(f"--- {sp} ---\n{txt}")
        total += len(txt)

    return "\n\n".join(parts), [sp for sp, _ in hits[: len(parts)]]


_SUMMARIZATION_PROMPT = """\
You are a technical writer creating a reference summary of a section of the Matter protocol \
specification for use by a test-case generation system.

CRITICAL — ANTI-HALLUCINATION RULES (follow strictly):
1. BASE EVERY STATEMENT SOLELY on the spec text provided below. Do NOT use your training
   knowledge to add, infer, or fill in details not present in that text.
2. For Section 3 (Normative Requirements), quote each SHALL / MUST / SHOULD / MAY requirement
   VERBATIM from the spec text. Do not paraphrase normative language.
3. For Section 4 (Data Structures), copy field names, opcodes, and numeric values exactly as
   they appear in the spec text. Do not invent or guess values not shown.
4. If a section heading (e.g. "Security Considerations") has no corresponding content in the
   provided text, write exactly: "(Not covered in provided spec sections.)" — do not invent content.
5. If the spec text is truncated mid-sentence, stop at that point rather than completing the
   thought from training knowledge.

FOCUS RULE — PROTOCOL FLOWS ONLY:
This summary will be used alongside a separate data-model knowledge graph that already contains
every cluster's attributes, commands, events, and feature-map entries. Therefore:
- DO NOT summarize cluster attribute tables, command parameter lists, or event field definitions.
  Skip any subsection that is primarily a cluster DM reference (e.g. "X Cluster Attributes",
  "X Cluster Commands", "X Cluster Events"). These are already in the knowledge graph.
- DO focus on: protocol message flows, state machines, session lifecycle, handshake sequences,
  sequencing constraints, timing requirements, role assignments, transport-level behavior,
  cross-cluster interactions, and security/error-handling rules that govern HOW the protocol
  operates — not WHAT data each cluster stores.

You are summarizing "{section_title}" for the Matter specification.

Your summary MUST include all of the following sections in this order:

## 1. Overview
What this protocol/section is about, key concepts, roles, and scope — based only on the
provided text. Skip cluster DM details.

## 2. Protocol Flow & State Machine
Message exchange sequences, state transitions, session lifecycle phases, role assignments,
handshake steps, modes of operation — based only on the provided text.

## 3. Normative Requirements
ALL SHALL / MUST / SHOULD / MAY requirements found in the provided text that govern protocol
behavior (not cluster attribute conformance). Grouped by subsection. Quote VERBATIM.
This section is the most critical — be exhaustive and faithful to the source text.

## 4. Message Formats & Data Structures
Wire-level message formats, TLV encodings, frame fields, opcodes, status/error codes with
numeric values — copied exactly from the provided text. Skip cluster attribute/command tables.

## 5. Security Considerations
Session establishment security requirements, transport constraints, authentication, key
derivation, threat model — based only on the provided text.

## 6. Error Handling & Timing
Error codes, failure modes, timeout values, retry behavior, recovery procedures —
based only on the provided text.

=== SPEC TEXT ({source_chars} chars from {section_count} matched sections) ===

{spec_text}

=== END SPEC TEXT ===

Remember: use ONLY the spec text above. Focus on protocol mechanics, not cluster DM tables.
Do not supplement from training knowledge.
"""


def _call_llm(prompt: str, config) -> str:
    from src.llm.llm_provider import get_llm

    llm = get_llm(config.llm)
    response = llm.complete(prompt=prompt, system="")
    return response.strip() if response else ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate LLM summaries for protocol-area spec sections.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    p.add_argument(
        "--spec-dir",
        default="data/matter_spec",
        help="Directory containing spec HTML files (default: data/matter_spec)",
    )
    p.add_argument(
        "--section",
        default="",
        metavar="LABEL",
        help="Only generate summary for this section label (e.g. 'SC Protocol')",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if summary file already exists",
    )
    p.add_argument(
        "--input-doc",
        action="append",
        dest="input_docs",
        default=[],
        metavar="PATH",
        help=(
            "Specific HTML file(s) to use as spec source instead of scanning --spec-dir. "
            "Can be repeated: --input-doc data/matter_spec/index.html "
            "--input-doc data/matter_spec/appclusters.html. "
            "When omitted, all .html files in --spec-dir are used."
        ),
    )
    p.add_argument(
        "--source-char-limit",
        type=int,
        default=60_000,
        help="Max chars of spec text sent to LLM per section (default: 60000)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from src.config.config_loader import load_config

    config = load_config(args.config)

    ps_configs: list[dict] = getattr(config.knowledge_graph, "prompt_sections", None) or []
    targets = [e for e in ps_configs if e.get("summary_file")]
    if args.section:
        targets = [e for e in targets if e.get("label", "").lower() == args.section.lower()]
        if not targets:
            logger.error("No prompt_sections entry with label=%r found in config.", args.section)
            return 1

    if not targets:
        logger.info("No prompt_sections entries with summary_file found in config. Nothing to do.")
        return 0

    # Load spec HTML files — explicit --input-doc overrides spec-dir scan
    if args.input_docs:
        html_files = []
        for p_str in args.input_docs:
            p_path = Path(p_str)
            if not p_path.is_file():
                logger.error("--input-doc not found: %s", p_path)
                return 1
            html_files.append(p_path)
        logger.info(
            "Using %d explicitly specified HTML file(s): %s",
            len(html_files), ", ".join(f.name for f in html_files),
        )
    else:
        spec_dir = Path(args.spec_dir)
        html_files = sorted(spec_dir.glob("*.html"))
        if not html_files:
            logger.error("No .html files found in spec-dir: %s", spec_dir)
            return 1
        logger.info("Scanning spec-dir %s …", spec_dir)

    logger.info("Parsing %d spec HTML file(s) …", len(html_files))
    all_sections: list[dict] = []
    for html_path in html_files:
        logger.info("  Parsing %s …", html_path.name)
        try:
            sections = _parse_html_spec(html_path)
            all_sections.extend(sections)
            logger.info("    → %d sections", len(sections))
        except Exception as exc:
            logger.warning("  Failed to parse %s: %s", html_path.name, exc)

    if not all_sections:
        logger.error("No sections extracted from spec HTML files.")
        return 1

    logger.info("Total sections available: %d", len(all_sections))

    generated = skipped = failed = 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for entry in targets:
        label: str = entry.get("label", "")
        summary_file: str = entry.get("summary_file", "")
        # Support either path_prefix (single string) or path_prefixes (list of strings)
        path_prefix: str = entry.get("path_prefix", "")
        path_prefixes: list[str] = entry.get("path_prefixes") or ([path_prefix] if path_prefix else [])

        if not summary_file or not path_prefixes:
            logger.warning("Skipping entry %r — missing summary_file or path_prefix(es).", label)
            continue

        out_path = Path(summary_file)

        if out_path.is_file() and not args.force:
            logger.info("[%s] Already exists — skipping (use --force to regenerate).", label)
            skipped += 1
            continue

        # Collect spec text from all prefixes, splitting the char budget equally
        per_prefix_limit = args.source_char_limit // len(path_prefixes)
        all_text_parts: list[str] = []
        all_matched_paths: list[str] = []
        for pp in path_prefixes:
            logger.info("[%s] Collecting spec text for path_prefix=%r …", label, pp)
            txt, paths = _collect_section_text(
                all_sections, pp, source_char_limit=per_prefix_limit
            )
            if txt:
                all_text_parts.append(txt)
                all_matched_paths.extend(paths)

        spec_text = "\n\n".join(all_text_parts)
        matched_paths = all_matched_paths

        if not spec_text:
            logger.warning(
                "[%s] No spec sections matched path_prefix(es) %r — skipping.", label, path_prefixes
            )
            skipped += 1
            continue

        logger.info(
            "[%s] Matched %d section(s), %d chars across %d prefix(es). Calling LLM …",
            label, len(matched_paths), len(spec_text), len(path_prefixes),
        )

        prompt = _SUMMARIZATION_PROMPT.format(
            section_title=label,
            source_chars=len(spec_text),
            section_count=len(matched_paths),
            spec_text=spec_text,
        )

        try:
            summary_body = _call_llm(prompt, config)
        except Exception as exc:
            logger.error("[%s] LLM call failed: %s", label, exc)
            failed += 1
            continue

        if not summary_body:
            logger.warning("[%s] LLM returned empty response — skipping.", label)
            failed += 1
            continue

        word_count = len(summary_body.split())
        header = (
            f"# Matter Spec Summary: {label}\n\n"
            f"**Source sections matched:** {len(matched_paths)}  \n"
            f"**Source chars sent to LLM:** {len(spec_text):,}  \n"
            f"**Generated:** {timestamp}  \n"
            f"**Summary words:** {word_count:,}  \n"
            f"\n---\n\n"
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(header + summary_body, encoding="utf-8")
        logger.info("[%s] Saved %d-word summary → %s", label, word_count, out_path)
        generated += 1

    logger.info(
        "Done. Generated: %d  Skipped: %d  Failed: %d", generated, skipped, failed
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
