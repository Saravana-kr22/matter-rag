This specification defines a proprietary cluster extension that is not part of the standard Matter specification.
It will not be found in the existing knowledge graph or test plan vector database.
There are no existing test cases for this cluster — generate all TCs from scratch.

Key considerations:
- These clusters follow Matter data model conventions (attributes, commands, events, features)
- PICS codes follow standard Matter format: PREFIX.S.ANNNN (attributes), PREFIX.S.CNN (commands), PREFIX.S.ENN (events), PREFIX.S.FNN (features)
- Test procedure steps should use standard Matter test harness commands (TH reads, TH writes, TH sends, TH subscribes)
- DUT is always a Matter device implementing this cluster as Server unless stated otherwise
