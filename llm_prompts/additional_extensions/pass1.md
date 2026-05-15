These are brand new proprietary clusters with no existing test cases in the Matter test plan knowledge base.
There are no existing TCs to update — all test cases must be newly created.
The KG and vector DB will have no matches for these clusters — this is expected, not an error.

When analyzing the diff content, treat ALL content as newly added specification text requiring test coverage.
Do not classify any requirement as "already covered" — nothing is covered yet.

For the analysis output:
- action should always be "add_new" (never "none" or "update_existing")
- Generate TC IDs using the cluster's natural prefix (derived from the cluster name abbreviation)
- Every attribute, command, event, and feature mentioned in the spec needs at least one TC
