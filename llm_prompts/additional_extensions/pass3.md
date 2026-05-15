When expanding this TC into full adoc format:

- Use AsciiDoc table format for preconditions, required devices, and procedure steps
- Include a Specification Mapping section listing exact spec section numbers from the input
- Include concrete test vectors — do not use placeholder values like "any valid value"
- For each step, specify the exact command/attribute name and expected response
- Include timing verification steps where the spec mentions time-based behavior
- Include negative/boundary steps alongside positive verification steps
- If the cluster has features, generate separate TCs per feature combination (not one monolithic TC)
- Each TC should be independently executable — do not depend on state from other TCs except commissioning
