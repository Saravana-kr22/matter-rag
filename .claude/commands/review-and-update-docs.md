# Review Code & Update Documentation

You are a documentation auditor for the Matter RAG pipeline. Your job is to ensure all `.md` files in the project accurately reflect the current state of the code.

## Step 1: Identify what changed

Run `git diff --name-only HEAD~5` (or use the user's specified range) to find recently modified Python files. Also check `git status` for uncommitted changes. These are the files whose documentation might be stale.

## Step 2: Audit each CLAUDE.md against its module

For each module directory under `src/`, there is a `.claude/CLAUDE.md` file. For each one:

1. **Read the CLAUDE.md** file
2. **Read the actual Python source files** in that module
3. **Check for staleness**:
   - Function signatures that changed (new parameters, removed parameters, renamed functions)
   - New files added to the module not mentioned in CLAUDE.md
   - Removed files still mentioned in CLAUDE.md
   - Changed class/dataclass fields (especially TypedDict state fields, config fields, enum values)
   - Changed edge types, node types, or other enum values
   - Pipeline DAG changes (new nodes, removed nodes, changed routing)
   - Changed CLI flags or config keys
   - Stale code examples or API usage patterns
4. **Update the CLAUDE.md** with accurate information. Preserve the existing structure and style — only change what's stale.

## Step 3: Audit the root CLAUDE.md

The root `.claude/CLAUDE.md` is the master project guide. Check:
- Pipeline stages diagram matches actual node names and order
- Config quick reference matches actual `config/config.yaml` fields and defaults
- CLI flags match actual argparse definitions in `scripts/run_ghpr_analysis.py`
- Module table matches actual files and their purposes
- Report output structure matches actual report generation
- Environment variables section is current
- LLM call counts are accurate

## Step 4: Audit docs/ files

- `docs/run_pipeline_options.md` — verify CLI options match argparse definitions
- `docs/projectflow.md` — verify pipeline flow matches actual graph

## Step 5: Audit module skills.md files

For each `src/*/.claude/skills.md`, verify the example code snippets still work with current function signatures.

## Step 6: Code review (lightweight)

While reading each module's code for documentation accuracy, flag any obvious issues:
- Functions referenced in docs that no longer exist
- State fields used in code but not documented in the engine CLAUDE.md PipelineState table
- New edge types or node types not mentioned in knowledge_graph CLAUDE.md

## Output

For each file you update, state:
- What was stale
- What you changed
- Why

Do NOT update files that are already accurate. Only touch what needs fixing.

## Important rules

- Do NOT add emojis
- Do NOT create new .md files — only update existing ones
- Preserve the existing formatting style of each file
- Keep descriptions concise — match the terse style of the existing docs
- When in doubt about a code change, read the actual source file rather than guessing
- Use parallel agents (Explore subagent) to read multiple modules simultaneously for efficiency
