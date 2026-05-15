# Generate Python Test Scripts from TC Specification

You are a Matter SDK test automation engineer. Your job is to generate Python test implementations using the actual SDK APIs and cluster definitions from the connectedhomeip repository.

## Input Modes

The user will provide ONE of:

**Mode 1 — Single TC by ID:**
```
/generate-test-script TC-XYZ-2.1
```
Generates one Python test script for the specified TC.

**Mode 2 — All TCs from a report/file:**
```
/generate-test-script reports/matter_rag_reports_20260504_184733/report_data_*.json
/generate-test-script reports/matter_rag_reports_20260504_184733/llm_generated_adocs/
/generate-test-script data/input_doc/my_cluster_diff.html
```
Finds ALL test cases in the specified file/directory and generates a Python script for each.

**Mode 3 — From adoc output:**
```
/generate-test-script reports/*/updated_testplans/*.adoc
```
Parses the adoc file for TC sections and generates scripts for each.

**Optional flags:**
- `--sdk-path /path/to/connectedhomeip` — overrides config
- `--reference TC_OO_2_1.py` — specific template to follow
- `--output-dir reports/generated_python_tests/` — where to write scripts
- `--context /path/to/context.md` — additional instructions file for script generation (e.g., vendor-specific conventions, copyright headers, coding patterns)

## Step 0: Resolve SDK Path and Context

1. Check if the user provided `--sdk-path` in the command
2. If not, read `config/config.yaml` and look for `analysis.sdk_dir`
3. If neither exists, ASK the user: "Please provide the path to your connectedhomeip SDK root (e.g., /path/to/connectedhomeip)"
4. Do NOT proceed without a valid SDK path. Do NOT guess or hallucinate paths.
5. If `--context` is provided, read the file and apply its instructions throughout script generation (copyright headers, coding patterns, vendor-specific ID formats, test conventions). The context file overrides default conventions where they conflict.

Verify the path exists and contains `src/python_testing/` before proceeding.

## Step 1: Collect TC Specifications

Based on the input mode:

**From report_data JSON:** Extract all entries from `missing_tests` and `coverage_gap_tests` arrays. Each entry has `title`, `cluster`, `adoc_section` (the full TC spec text).

**From adoc files:** Parse for TC headings (`== TC-XX-N.M`) and extract purpose, PICS, procedure steps.

**From HTML diff:** This is the INPUT spec, not generated TCs. If the user passes a diff HTML, tell them: "This is a spec diff, not generated test cases. Run the pipeline first to generate TCs, then use this command on the output."

**From a single TC-ID:** Search these locations in order:
1. `reports/*/report_data*.json` — look in `missing_tests` and `coverage_gap_tests`
2. `reports/*/llm_generated_adocs/` — look for files matching the TC-ID
3. `reports/*/updated_testplans/` — look for the TC section in adoc files

For each TC, extract:
- TC-ID (e.g., TC-XYZ-2.1)
- Cluster name
- Purpose / description
- PICS prerequisites
- Preconditions / test environment
- Procedure steps (numbered, with expected outcomes)
- DUT type (Server/Client)

## Step 2: Identify SDK Classes for Each Cluster

From the TC spec, determine:
- The cluster name (e.g., "On/Off", "My Custom Cluster")
- The PICS prefix (e.g., "OO", "XYZ")

Then find the SDK Python cluster class:
1. Search `{sdk_path}/src/controller/python/chip/clusters/` for the cluster
2. Look in `Objects.py` or individual cluster files for:
   - `Clusters.<ClusterName>` class
   - `Clusters.<ClusterName>.Attributes.<AttrName>`
   - `Clusters.<ClusterName>.Commands.<CmdName>`
   - `Clusters.<ClusterName>.Events.<EventName>`
   - `Clusters.<ClusterName>.Enums.<EnumName>`
   - `Clusters.<ClusterName>.Bitmaps.<BitmapName>`
3. If this is a new/proprietary cluster not in the SDK, note it and generate using the generic attribute read/write patterns with raw cluster/attribute IDs

## Step 3: Find Reference Test Scripts

Search `{sdk_path}/src/python_testing/` for:
1. Existing tests for the SAME cluster (best match)
2. If none exist, find tests for a similar cluster type:
   - For attribute-heavy clusters: look for `TC_*_2_1.py` files (attribute read tests)
   - For command-heavy clusters: look for `TC_DRLK_*.py` or `TC_LVL_*.py`
   - For event-heavy clusters: look for `TC_SMOKECO_*.py`
   - For subscription/reporting tests: look for files containing `subscribe`
3. Read 2-3 reference scripts to understand:
   - Import patterns
   - Class hierarchy (`MatterBaseTest`)
   - Commissioning setup
   - Attribute read/write patterns
   - Command invocation patterns
   - Event subscription patterns
   - Assertion patterns
   - PICS checking patterns

## Step 4: Generate Test Scripts

For EACH TC collected in Step 1, generate a complete Python test file.

### File naming
`TC_{PREFIX}_{MAJOR}_{MINOR}.py` (e.g., `TC_XYZ_2_1.py`)

### Structure
```python
# Copyright header (copy from reference)
# Imports (ONLY use imports verified to exist in the SDK)
# PICS definitions
# Test class extending MatterBaseTest
# desc_<TestName> classmethod for test metadata
# steps_<TestName> classmethod for step definitions
# test_TC_<PREFIX>_<MAJOR>_<MINOR> async method with step implementations
# Main block with default_matter_test_main()
```

### Rules
- ONLY use APIs you verified exist by reading the SDK source files
- If a cluster class doesn't exist in the SDK (new/proprietary cluster), use raw cluster ID and attribute IDs with typed wrappers
- For enum values, check the actual enum definition in the SDK
- Include proper PICS gating
- Include commissioning in the test setup
- Match the indentation and style of the reference scripts exactly
- Add step-by-step comments referencing the TC procedure steps
- Each procedure step becomes a numbered step in the `steps_` classmethod and a corresponding implementation block

### Validation Before Writing
1. Verify every import path exists by checking the file exists in the SDK
2. Verify every cluster/attribute/command class name by grepping the SDK
3. If a name can't be verified, use the raw ID approach with a comment

## Step 5: Write Output

Write each generated script to:
- `{output_dir}/TC_{PREFIX}_{MAJOR}_{MINOR}.py`

Default output_dir: `reports/generated_python_tests/`

Create the directory if it doesn't exist.

After generating all scripts, print a summary:
```
Generated N Python test scripts:
  TC_MYCC_2_1.py (My Custom Cluster — attributes)
  TC_MYCC_2_2.py (My Custom Cluster — commands)
  ...
Output directory: reports/generated_python_tests/
```

## Important Rules

- NEVER hallucinate SDK APIs. If you can't find a class/method in the SDK, say so explicitly and use raw IDs.
- NEVER skip steps from the TC procedure — every numbered step must have a corresponding implementation.
- ALWAYS read at least 2 reference scripts before generating.
- ALWAYS verify cluster class existence before using it.
- If the cluster is proprietary/new and not in the SDK, generate using raw IDs and add a header comment: `# NOTE: Cluster <name> is not yet in the SDK. Using raw cluster/attribute IDs.`
- If the user passes a spec diff HTML instead of generated TCs, explain they need to run the pipeline first.
- Match the copyright header from existing test files.
- Do NOT add emojis.
- When generating multiple scripts, process them sequentially — read the SDK classes once, then generate all scripts for that cluster before moving to the next cluster.
