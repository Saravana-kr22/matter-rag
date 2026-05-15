# Chunker Module

## Purpose
Text-splitting strategies that convert raw document text into `Document` chunks ready for embedding.
Two implementations live here; a third can be added by subclassing `BaseChunker`.

## Files

| File | Class(es) | Strategy |
|---|---|---|
| `base_chunker.py` | `BaseChunker` (ABC), `GenericChunker` | Sliding-window character split |
| `matter_tc_chunker.py` | `MatterTCChunker`, `IgnoreRule`, `apply_ignore_rules` | TC-boundary split with rich metadata |
| `__init__.py` | — | Re-exports all public symbols |

## BaseChunker ABC
```python
class BaseChunker(ABC):
    @abstractmethod
    def chunk(self, text: str, metadata: dict) -> List[Document]: ...
```

## GenericChunker
Overlapping character windows. Parameters: `chunk_size` (default 1000), `chunk_overlap` (default 200).
Empty / whitespace-only text returns `[]`.

## MatterTCChunker
Splits AsciiDoc on `^== TC-[A-Z0-9]+-\d+\.\d+` headings.
Each TC block becomes one or more `Document` chunks with metadata:

| Field | Type | Example |
|---|---|---|
| `tc_id` | str | `"TC-OO-2.1"` |
| `cluster_name` | str | `"OO"` |
| `pics_codes` | list[str] | `["OO.S", "OO.C.00.Rsp"]` |
| `section_type` | str | `"purpose"` \| `"pics"` \| `"steps"` \| `"env"` \| `"expected"` \| `"other"` \| `"preamble"` |

Falls back to `GenericChunker` for non-TC text.

## IgnoreRule
Strips content before chunking. Used for license headers, boilerplate, comments.

```python
@dataclass
class IgnoreRule:
    pattern: str
    match: str = "contains"    # "contains" | "startswith" | "exact" | "regex"
    scope: str = "line"        # "line" | "paragraph" | "block"
    case_sensitive: bool = False
```

Rules are applied via `apply_ignore_rules(text, rules)` before TC splitting.
`MatterTCChunker` accepts `ignore_rules: List[IgnoreRule | dict]` — raw dicts from YAML are auto-converted via `IgnoreRule.from_dict()`.

## Circular-import guard
`Document` is imported **lazily** inside `chunk()` methods (not at module level) to avoid a circular dependency with `src.loader.base_loader`.

## Dependency chain
```
src.loader.base_loader → Document
src.chunker.base_chunker → (lazy) Document
src.chunker.matter_tc_chunker → (lazy) Document
src.loader.loader_factory → MatterTCChunker, GenericChunker
```
