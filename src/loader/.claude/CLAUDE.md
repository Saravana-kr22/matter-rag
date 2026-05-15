# Loader Module

## Purpose
Parse `FetchedDocument` objects into `Document` chunks ready for embedding.
Each loader handles one file format and delegates chunking to an injected `BaseChunker`.

## Files

| File | Class(es) | Role |
|---|---|---|
| `base_loader.py` | `Document`, `BaseDocumentLoader` | Dataclass + ABC |
| `pdf_loader.py` | `PDFLoader` | PDF → text via pypdf |
| `adoc_loader.py` | `AdocLoader` | AsciiDoc → sections → TC-aware chunks |
| `csv_loader.py` | `CSVLoader` | CSV rows → merged prose chunks |
| `text_loader.py` | `TextLoader` | Plain text and Markdown |
| `loader_factory.py` | `DocumentLoaderFactory` | Extension registry + dispatch |
| `document_loader.py` | `DocumentLoader` | Thin backward-compat wrapper |

## Document dataclass
```python
@dataclass
class Document:
    page_content: str
    metadata: dict   # path, source, chunk_index, tc_id, cluster_name, pics_codes, …
```

## BaseDocumentLoader ABC
```python
class BaseDocumentLoader(ABC):
    def __init__(self, chunker: BaseChunker): ...
    def load(self, fetched: FetchedDocument) -> List[Document]: ...
    def supported_extensions(self) -> List[str]: ...
```

## DocumentLoaderFactory
Builds an extension → loader registry at construction time:
```python
factory = DocumentLoaderFactory(loader_config, chunker_config, chunker_map)
docs = factory.load_all(fetched_documents)
```
Default chunker assignments:
- `.adoc` → `MatterTCChunker` (when `chunker_type == "matter_tc"`)
- all others → `GenericChunker`

Override with `chunker_map: Dict[str, BaseChunker]`.

## AdocLoader section splitting
When `loader_config.adoc_section_split=True`, AsciiDoc text is pre-split on `^(={1,6})\s+` headings before being passed to the chunker.

## Backward compatibility
- `DocumentLoader(loader_config)` still works — it wraps `DocumentLoaderFactory`.
- `from src.loader.document_loader import Document` still works — re-exported from `base_loader`.
