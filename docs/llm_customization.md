# Customizing LLM Behavior

This guide explains how to control what the LLM sees and how it generates test cases, without modifying pipeline code.

---

## How Prompts Are Assembled

Every LLM call in the pipeline receives a **system prompt** and a **user prompt**. The system prompt is built from three configurable sources:

```
System Prompt = PROMPT_SECTION nodes (from KG)
              + Skill file (standing instructions)
              + Additional context (per-run)
```

The user prompt varies by pass (diff content, search results, existing TCs, etc.) and is not directly customizable — it's assembled from KG/FAISS search results and the PR diff.

---

## Level 1: Skill File (Permanent Rules)

**File:** `llm_prompts/matter_test_coverage_and_structure.md`
**Config:** `pipeline.system_prompt_skills_file` in `config/config.yaml`
**Scope:** Appended to the system prompt on EVERY LLM call in EVERY run

This is the right place for rules that should always apply:

- TC naming conventions (bracket format, numbering rules)
- PICS code format and usage rules
- Coverage checklist (what every TC must verify)
- Boundary and suppression testing requirements
- Quieter Reporting verification patterns
- Test step gating rules (use PICS, not cluster revision numbers)

**How to edit:**

Open the file and add your rules. No rebuild needed — changes take effect on the next pipeline run.

**Example additions:**

```markdown
## My Custom Rules

- Always include a reboot-persistence step for non-volatile attributes
- For every command TC, include an error-path step with invalid parameters
- Use PIXIT variables for all timing values, not hardcoded seconds
```

---

## Level 2: Per-Run Context (`--llm-additional-context`)

**Scope:** Injected into the system prompt (Pass 1) and expand prompt (Pass 2/3) for THIS run only

Use this when you need context that varies between runs — domain hints, cluster-specific instructions, or temporary rules you're testing.

### Three input formats:

**Inline text:**
```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... \
  --llm-additional-context "This is a brand new cluster with no existing TCs. Generate comprehensive coverage."
```

**Single file:**
```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... \
  --llm-additional-context /path/to/my_context.md
```

**Directory with per-pass files:**
```bash
python scripts/run_ghpr_analysis.py --compare-only --input-doc ... \
  --llm-additional-context /path/to/context_dir/
```

### Per-pass directory structure:

```
context_dir/
  all.md       ← injected into ALL passes (Pass 1, 2, 3)
  pass1.md     ← injected into Pass 1 only (analysis/classification)
  pass2.md     ← injected into Pass 2 only (TC outline + expand)
  pass3.md     ← injected into Pass 3 only (human outline re-expand)
```

Only create the files you need — missing files are silently skipped.

### When to use each pass file:

| File | Best for |
|------|----------|
| `all.md` | General context: "this is a new cluster", "no existing TCs", "use vendor-specific IDs" |
| `pass1.md` | Analysis guidance: "always classify as add_new", "don't say action=none" |
| `pass2.md` | TC generation quality: "minimum 8 steps", "include concrete test vectors", "test all features" |
| `pass3.md` | AsciiDoc formatting: "use table format", "include Specification Mapping section" |

### Example: New cluster extension

```markdown
# all.md
This is a proprietary cluster extension not in the standard Matter spec.
There are no existing test cases — generate all TCs from scratch.

# pass1.md
action should always be "add_new" (never "none" or "update_existing").
Generate TC IDs using the cluster's natural PICS prefix.

# pass2.md
Generate comprehensive TCs covering:
- ALL mandatory and optional attributes (read, write, boundary, persistence)
- ALL commands (success + error paths)
- ALL events (subscription + trigger)
- ALL features (enabled vs disabled behavior)
```

---

## Level 3: Re-Expanding Human-Edited Outlines

After a pipeline run, you can review the generated TC outlines, edit them, and re-run the expansion with your changes.

### Workflow:

**Step 1:** Run the pipeline normally:
```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off"
```

**Step 2:** Find the outline JSON in the output directory:
```
reports/matter_rag_reports_<timestamp>/outline_*.json
```

**Step 3:** Edit the outline — add TCs, remove TCs, change titles, adjust test_type:
```json
[
  {
    "tc_id": "TC-OO-3.5",
    "title": "My Custom Persistence Test",
    "test_type": "unit",
    "cluster": "On/Off Cluster",
    "justification": "Verify OnTime persists across reboot"
  }
]
```

**Step 4:** Re-run with `--third-pass-expand`:
```bash
python scripts/run_ghpr_analysis.py --compare-only \
  --input-doc data/input_doc/appclusters_diff.html \
  --cluster "On/Off" \
  --third-pass-expand reports/matter_rag_reports_<ts>/outline_edited.json
```

Pass 4 expands only non-existing TCs and merges with Pass 1 results.

---

## Precedence

When multiple sources provide instructions, this is the order (highest priority first):

1. `--llm-additional-context` (per-run, per-pass)
2. Skill file (`llm_prompts/matter_test_coverage_and_structure.md`)
3. PROMPT_SECTION nodes (spec sections baked into KG)

If instructions conflict, the LLM tends to follow the most specific/recent instruction — which is typically the additional context.

---

## Tips

- **Test your changes on one cluster first** before running on all clusters
- **Check `llm_calls.html`** in the report output to see exactly what prompt the LLM received — verify your instructions appear in the system prompt
- **Don't over-constrain** — too many rules can cause the LLM to focus on formatting compliance instead of test coverage quality
- **Use `pass1.md` sparingly** — Pass 1 classifies changes, not generates TCs. Heavy instructions here can cause misclassification
- **The skill file is shared across ALL users** — put team-wide conventions there. Put personal/project-specific instructions in `--llm-additional-context`
