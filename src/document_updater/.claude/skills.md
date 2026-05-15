# Document Updater Module — Skills

## Write TC updates to AsciiDoc files (via pipeline)
The `write_adoc_updates_node` in `nodes.py` calls this automatically.
Output files appear in `reports/` with the `_matter_ai_rag_update.adoc` suffix.

## Use directly in a script
```python
from src.document_updater.updater_registry import create_updater

updater = create_updater(".adoc")
if updater:
    paths = updater.write_updates(
        analysis_results=analysis_results,   # from analyze_chunks_with_llm_node
        search_results=search_results,       # from search_test_plan_vector_db_node
        output_dir="reports/",
    )
    for p in paths:
        print(f"Written: {p}")
```

## Check which extensions are supported
```python
from src.document_updater.updater_registry import REGISTRY
print(list(REGISTRY.keys()))   # e.g. [".adoc"]
```

## Add support for Markdown test plans
1. Create `src/document_updater/md_updater.py` subclassing `BaseDocumentUpdater`
2. Implement `supported_extension()` returning `".md"` and `write_updates()`
3. Register: `REGISTRY[".md"] = MdUpdater` in `updater_registry.py`

## Inspect generated update file
```bash
# View TC updates written by the pipeline
cat reports/*_matter_ai_rag_update.adoc | grep -A5 "^== TC-"
```
