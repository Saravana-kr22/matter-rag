# Document Updater Module

## Purpose
Write LLM-generated test case suggestions back to source `.adoc` files.
Invoked by `write_adoc_updates_node` after `analyze_chunks_with_llm_node` produces `analysis_results`.

## Files

| File | Class(es) | Role |
|---|---|---|
| `base_updater.py` | `BaseDocumentUpdater` (ABC) | Abstract interface |
| `adoc_updater.py` | `AdocUpdater` | Write TC updates to AsciiDoc files |
| `updater_registry.py` | `REGISTRY`, `create_updater()` | Extension → updater factory |
| `tc_index_builder.py` | `TcIndexBuilder` | Scan adoc files → build `data/tc_index.json` routing index |

## TcIndexBuilder

Scans all `.adoc` files under a given directory and builds a three-map routing index at `data/tc_index.json`:

| Map | Key | Value |
|---|---|---|
| `tc_map` | Exact TC-ID (e.g. `"TC-OO-2.1"`) | Absolute path to the source `.adoc` file |
| `prefix_map` | TC prefix (e.g. `"TC-OO"`) | Most common `.adoc` path for that prefix |
| `stem_map` | File stem (e.g. `"Test_Plan_OnOff"`) | Absolute path |

The index is used by `write_updated_testplan_node` in `src/engine/nodes.py` to route
each LLM-suggested TC update to the correct source `.adoc` file without a full directory scan.

**Auto-build**: `fetch_documents_node` rebuilds `tc_index.json` automatically (mtime-based cache)
when adoc sources are loaded. Manual rebuild: `python scripts/helper_scripts/build_tc_index.py --adoc-dir <dir>`.

```python
from src.document_updater.tc_index_builder import TcIndexBuilder
builder = TcIndexBuilder()
builder.build(adoc_dir="data/test_plan_adocs/src", output_path="data/tc_index.json")
```

## BaseDocumentUpdater ABC
```python
class BaseDocumentUpdater(ABC):
    @abstractmethod
    def supported_extension(self) -> str: ...

    @abstractmethod
    def write_updates(
        self,
        analysis_results: List[dict],
        search_results: Dict[str, List[SearchResult]],
        output_dir: str,
    ) -> List[str]: ...   # returns list of written file paths
```

## AdocUpdater

Processes `analysis_results` from the LLM and applies them to `.adoc` test plan files.

### Output files
Files are written to `output_dir` with the suffix `_matter_ai_rag_update.adoc`.
Original source files are never modified in-place.

### Update modes
- **Replace** — existing TC section found by `_TC_HEADING_RE` is replaced with LLM-suggested content.
- **Append** — new TCs not present in the original file are appended at the end.

### TC heading regex
```python
_TC_HEADING_RE = re.compile(r'^(={1,6})\s+(TC-[A-Z0-9]+-\d+\.\d+)', re.MULTILINE)
```

## Registry
```python
REGISTRY = {
    ".adoc": AdocUpdater,
}
```

```python
from src.document_updater.updater_registry import create_updater
updater = create_updater(".adoc")
paths = updater.write_updates(analysis_results, search_results, output_dir="reports/")
```

## Adding a new format
1. Create `src/document_updater/<fmt>_updater.py` subclassing `BaseDocumentUpdater`.
2. Implement `supported_extension()` and `write_updates()`.
3. Register in `REGISTRY` in `updater_registry.py`.
4. Add test in `tests/test_document_updater.py`.
