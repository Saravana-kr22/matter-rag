# Processor Module — Skills

## Apply global rules to a document
```python
from src.fetcher.base_fetcher import FetchedDocument
from src.processor.document_processor import DocumentProcessor

processor = DocumentProcessor(".ignore_rules.json")
doc = FetchedDocument("spec.adoc", "// comment\nCopyright 2024\nReal content\n\n\n\nMore content")
cleaned = processor.process(doc)
print(cleaned.content)
# → "Real content\n\nMore content"
```

## Apply global + per-source rules
```python
per_source_rules = [
    {"type": "strip_first_lines", "count": 3},
]
cleaned = processor.process(doc, source_rules=per_source_rules)
```

## Process all fetched docs in batch (as process_node does)
```python
pr_docs = [processor.process(d, d.metadata.get("_process_rules", [])) for d in pr_docs]
```

## Strip a block between markers
```json
{
  "type": "strip_block_between",
  "start_pattern": "^== Legal Notice$",
  "end_pattern": "^== ",
  "apply_to": [".adoc"]
}
```
Add this rule to `.ignore_rules.json` to strip everything from `== Legal Notice` up to the next section.

## Replace abbreviations before chunking
```json
{
  "type": "replace_regex",
  "pattern": "\\bTC-OO\\b",
  "replacement": "TC-OnOff"
}
```

## Run without a global rules file (in-memory rules only)
```python
processor = DocumentProcessor(global_rules_path=None)
cleaned = processor.process(doc, source_rules=[{"type": "normalize_whitespace"}])
```

## Check how many rules were applied
```python
cleaned = processor.process(doc)
print(cleaned.metadata["_rules_applied"])   # e.g. 3
```
