# Chunker Module — Skills

## Use GenericChunker
```python
from src.chunker.base_chunker import GenericChunker
chunker = GenericChunker(chunk_size=800, chunk_overlap=100)
docs = chunker.chunk("Long text...", metadata={"source": "readme.txt"})
# docs[0].metadata["chunk_index"] == 0
```

## Use MatterTCChunker (basic)
```python
from src.chunker.matter_tc_chunker import MatterTCChunker
chunker = MatterTCChunker(chunk_size=1000, chunk_overlap=200)
docs = chunker.chunk(adoc_text, metadata={"path": "TC-OO.adoc"})
# Each doc: tc_id, cluster_name, pics_codes, section_type
```

## Use MatterTCChunker with ignore rules
```python
from src.chunker.matter_tc_chunker import MatterTCChunker, IgnoreRule
rules = [
    IgnoreRule(pattern="Copyright", scope="paragraph", match="contains"),
    IgnoreRule(pattern="NOTE:", scope="line", match="startswith"),
    IgnoreRule(pattern=r"^\s*//", scope="line", match="regex"),
]
chunker = MatterTCChunker(ignore_rules=rules)
docs = chunker.chunk(adoc_text, metadata={})
```

## Pass ignore rules from YAML config (dict list)
```python
rules = [{"pattern": "Copyright", "scope": "paragraph", "match": "contains"}]
chunker = MatterTCChunker(ignore_rules=rules)   # dicts auto-converted
```

## Inject custom chunker into loader factory
```python
from src.loader.loader_factory import DocumentLoaderFactory
from src.chunker.base_chunker import GenericChunker
factory = DocumentLoaderFactory(loader_cfg, chunker_map={".adoc": GenericChunker()})
```

## Apply ignore rules standalone
```python
from src.chunker.matter_tc_chunker import apply_ignore_rules, IgnoreRule
clean = apply_ignore_rules(raw_text, [IgnoreRule("NOTE:", scope="line", match="startswith")])
```

## Add a new chunker
1. Subclass `BaseChunker` in a new file under `src/chunker/`
2. Implement `chunk(text, metadata) -> List[Document]`
3. Export from `__init__.py`
4. Register in `DocumentLoaderFactory` via `chunker_map`
