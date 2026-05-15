# All Passes — Behavioral Guidelines
#
# This file is injected into ALL LLM passes (Pass 1, 2, 3).
# Add general rules that apply universally. No code changes needed.

## PICS Code Rules — Minimal Set

When listing PICS prerequisites for a test case, use the MINIMAL set needed to
correctly gate the test. Aggressive PICS lists cause engineers to incorrectly
skip tests they should run.

### Core rules:
- Only include PICS for entities DIRECTLY exercised in the test procedure
- Mandatory attributes/commands that exist on ALL devices implementing the cluster
  do not need individual PICS entries — only CLUSTER.S (or CLUSTER.C) is needed
- Optional attributes/commands DO need their individual PICS entry

### Feature-gated mandatory entities (CRITICAL):
When an entity is MANDATORY on a feature, gate the test on the FEATURE PICS only.
Do NOT redundantly list the entity PICS — it misleads engineers into thinking
the entity is optional when it's actually required by the feature.

Example — On/Off cluster:
- OnWithTimedOff command (0x42) is MANDATORY when the Lighting (LT) feature is present
- CORRECT PICS for a TC testing OnWithTimedOff: `OO.S.F00` (LT feature)
- WRONG PICS: `OO.S.F00` + `OO.S.C42` — the C42 is redundant and misleading;
  an engineer might see C42 and think "we don't support that optional command"
  when it's actually mandatory on their LT-enabled device

General pattern:
- If entity X is mandatory on feature F → gate on F only (e.g., `CLUSTER.S.F00`)
- If entity X is independently optional → gate on entity PICS (e.g., `CLUSTER.S.Axxxx`)
- If entity X is universally mandatory (no feature gate) → no entity PICS needed,
  just `CLUSTER.S`

### Cross-cluster dependencies:
- Include cross-cluster PICS when the test depends on another cluster
  (e.g., `TSTAT.S` for Thermostat, `TIMESYNC.S` for Time Synchronization)

## TC Heading Format

Always use `[DUT as Server]`, `[DUT as Client]`, or `[DUT as Commissioner]` in TC headings.
Never use `[Server as Server]` or other variations.

## TC Consolidation Rules — Avoid Redundant Test Cases

### One prefix per cluster
Use ONE consistent TC prefix per cluster. The prefix MUST match the cluster's PICS
code from the DM XML `<classification picsCode="XX"/>` element. If the DM XML has no
PICS code, derive a short prefix from the cluster name (e.g., "Proximity Ranging" → "PR",
"AV Analysis" → "AVA"). Never generate parallel TC families with different prefixes
for the same cluster.

### Attribute-read TCs: ONE per cluster
All attribute reads for a cluster belong in a SINGLE TC (typically TC-XX-2.1).
This TC reads EVERY server attribute, validates type, constraint, access, and quality.
Do NOT create separate TCs for individual attributes or attribute subsets.

Pattern:
- TC-XX-2.1 [DUT as Server]: Read and validate ALL server attributes
  (types, constraints, nullable, persistence, access)

### Command TCs: at most 2 per command
Each command gets at most:
- 1 positive-path TC (success flow, state transitions, response validation)
- 1 negative/boundary TC (error codes, constraint violations, invalid states)
Do NOT split a single command into 3+ TCs covering the same functionality.

### Event TCs: consolidate by lifecycle
Group event verification into lifecycle-oriented TCs rather than one-event-per-TC:
- 1 TC for the full event lifecycle (start → perceived/data → end)
- 1 TC for event field validation (types, ranges, cross-references)

### General rule
If two proposed TCs exercise the same entities with overlapping procedure steps,
merge them into ONE TC. A 25-step TC is better than three 10-step TCs that repeat
setup/teardown and attribute reads.
