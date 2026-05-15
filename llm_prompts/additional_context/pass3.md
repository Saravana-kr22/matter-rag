# Pass 3: Consolidation + Coverage Gaps — Behavioral Guidelines
#
# This file is injected into the Pass 3 LLM prompts (consolidation and coverage gap expand).
# Edit to customize TC generation behavior. No code changes needed.

## DUT Type Classification

Classify each TC's DUT type based on the requirement being tested:
- **Server**: DUT implements the cluster server (attribute reads/writes, command handling, event emission). Most cluster TCs are server-side.
- **Client**: DUT implements the cluster client (client SHALL send requests, discover services, handle responses). TC numbering: typically TC-XX-3.x for client tests.
- **Commissioner**: DUT performs commissioning flows (discovers commissionees, establishes PASE/CASE sessions, joins fabric). Protocol tests like DD, DA, SC often use this.

## Specification Mapping

In the === Specification Mapping section, reference the actual spec section paths
(e.g., "5.1. MyCustomAttribute Attribute") not internal requirement IDs.
These come from the (spec: ...) annotations in the RELEVANT REQUIREMENTS block.

## PICS Guidelines — Minimal Set

Use the MINIMAL set of PICS codes needed to gate the test. Rules:
- Only include PICS for entities that are DIRECTLY tested in the procedure steps
- Do NOT include PICS for entities that are merely preconditions (e.g., if a feature
  makes an attribute mandatory, only include the feature PICS if the test explicitly
  verifies feature-gated behavior)
- If an entity is mandatory on a feature (e.g., OnWithTimedOff is mandatory when LT
  feature is present), the feature PICS (e.g., OO.S.F00) gates the test — do NOT
  additionally list the command PICS since it's implied by the feature
- Prefer feature-level PICS over individual entity PICS when the entity is mandatory
  on that feature

## Coverage Gap TC Quality

When generating coverage gap TCs:
- Do NOT generate a TC if the requirement is already covered by a PLANNED TC shown in
  the existing TC block
- Each TC should have minimum 8 procedure steps for functional conformance tests
- Include concrete test vector values (specific hex IDs, byte lengths, enum values)
- Every step must state its expected outcome inline
