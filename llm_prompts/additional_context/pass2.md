# Pass 2: Cluster Review — Behavioral Guidelines
#
# This file is injected into the Pass 2 (Cluster Review) LLM prompt.
# Edit to customize what the review checks for. No code changes needed.

## Review Checklist — Missing Test Types

For each changed entity in the cluster, check that the generated TCs collectively
cover these test categories. Flag any category that is missing:

### Boundary Values
For numeric attributes (uint8/16/32, int8/16, temperature, etc.), is there a TC that
writes min value, max value, and an out-of-range value? For nullable attributes, is
null tested?

### Error Paths / Constraint Violations
For writable attributes with constraints (min/max, enum range, string length), is there
a TC that writes an invalid value and verifies CONSTRAINT_ERROR? For read-only attributes,
is there a TC that attempts a write and verifies UNSUPPORTED_WRITE or WRITE_INTERACTION_DISABLED?

### Subscription / Reporting
For attributes that changed quality to Q (Quieter Reporting), is there both a sub-threshold
write (no report) AND super-threshold write (report emitted) TC? For any observable attribute,
is there at least one subscribe-and-verify TC?

### Persistence / Non-Volatile
For attributes with N (non-volatile) quality, is there a power-cycle persistence TC?

### Conformance Transitions
For O->M (optional to mandatory), is there a mandatory-device TC (no feature flag gating)?
For access R->RW, is there a write-path + authorization TC?

### Command Error Handling
For commands, is there a TC that sends the command with invalid/missing mandatory fields
and verifies INVALID_COMMAND or appropriate error status?

### Access Control
For safety-critical or write-access attributes, is there a TC that verifies an
unprivileged fabric/session cannot modify the attribute (UNSUPPORTED_ACCESS)?

## DUT Type Classification

When suggesting new TCs, classify DUT type correctly:
- **Server**: DUT implements the cluster server (attribute reads/writes, command handling, event emission)
- **Client**: DUT implements the cluster client (sends requests, discovers services, handles responses)
- **Commissioner**: DUT performs commissioning flows (discovers commissionees, PASE/CASE establishment, fabric join)
