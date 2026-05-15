#!/bin/bash
# Matter RAG Pipeline — Docker Entrypoint
#
# Three-step orchestration, each gated so re-runs skip completed work:
#   Step 1: Convert spec adocs to diff HTML (if not already done)
#   Step 2: Build KG + FAISS (if not already built)
#   Step 3: Run analysis pipeline
#
# Environment variables:
#   ANTHROPIC_API_KEY — required for claude_cli provider
#   GITHUB_TOKEN     — required for --pr-url mode
#   CLUSTER          — filter to one cluster (optional)
#   FORCE_REBUILD    — set to 1 to rebuild KG + FAISS even if cached
#   PR_URL           — spec PR URL (triggers diff generation)
#   SPEC_ENTRY       — override adoc entry file for diff conversion
#   PIPELINE_ARGS    — extra flags forwarded to run_ghpr_analysis.py

set -e

cd /app

echo "============================================================"
echo "  Matter RAG Pipeline — Docker Runtime"
echo "============================================================"
echo ""

# ── Step 1: Generate diff HTML ──────────────────────────────────────────
if [ -n "$PR_URL" ]; then
    echo "[Step 1] Generating diff HTML from PR: $PR_URL"
    python scripts/run_ghpr_analysis.py \
        --pr-url "$PR_URL" \
        --spec-repo /app/spec-repo \
        --compare-only \
        --auto-detect-clusters \
        ${CLUSTER:+--cluster "$CLUSTER"} \
        $PIPELINE_ARGS
    exit $?
fi

# If no PR_URL, check for existing diff HTMLs
DIFF_COUNT=$(find /app/data/input_doc -name "*_diff.html" 2>/dev/null | wc -l)

if [ "$DIFF_COUNT" -eq 0 ] && [ -n "$SPEC_ENTRY" ]; then
    echo "[Step 1] Converting spec adocs to diff HTML..."
    python scripts/helper_scripts/adoc_to_diff_html.py \
        --input /app/spec-repo/src/ \
        --output /app/data/input_doc/
    DIFF_COUNT=$(find /app/data/input_doc -name "*_diff.html" 2>/dev/null | wc -l)
elif [ "$DIFF_COUNT" -gt 0 ]; then
    echo "[Step 1] Found $DIFF_COUNT existing diff HTML(s) — skipping conversion"
fi

if [ "$DIFF_COUNT" -eq 0 ]; then
    echo "[Step 1] No diff HTMLs found and no PR_URL set. Nothing to analyze."
    echo "  Set PR_URL=<url> or mount diff HTMLs to /app/data/input_doc/"
    exit 1
fi

# ── Step 2: Build KG + FAISS (if needed) ────────────────────────────────
KG_EXISTS=$(test -f /app/data/knowledge_graph/matter_kg.json && echo 1 || echo 0)
FAISS_EXISTS=$(test -f /app/data/faiss_index/matter.index && echo 1 || echo 0)

if [ "$FORCE_REBUILD" = "1" ] || [ "$KG_EXISTS" = "0" ] || [ "$FAISS_EXISTS" = "0" ]; then
    echo "[Step 2] Building KG + FAISS index..."
    python scripts/run_ghpr_analysis.py --index-only
else
    echo "[Step 2] KG + FAISS already built — skipping (set FORCE_REBUILD=1 to rebuild)"
fi

# ── Step 3: Run analysis pipeline ───────────────────────────────────────
echo "[Step 3] Running analysis pipeline..."
python scripts/run_ghpr_analysis.py \
    --compare-only \
    --input-doc-dir /app/data/input_doc/ \
    --auto-detect-clusters \
    ${CLUSTER:+--cluster "$CLUSTER"} \
    $PIPELINE_ARGS

echo ""
echo "============================================================"
echo "  Pipeline complete. Reports in /app/reports/"
echo "============================================================"
