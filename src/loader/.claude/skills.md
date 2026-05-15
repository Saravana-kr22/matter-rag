# Loader Module — Skills

## Load a directory of files via factory
```python
from src.loader.loader_factory import DocumentLoaderFactory
from src.config.config_loader import load_config
cfg = load_config()
factory = DocumentLoaderFactory(cfg.loader, cfg.chunker)
docs = factory.load_all(fetched_documents)
print(f"{len(docs)} chunks from {len(fetched_documents)} files")
```

## Load a single file
```python
doc_list = factory.load_one(fetched_document)
```

## Use a loader directly
```python
from src.loader.adoc_loader import AdocLoader
from src.chunker.matter_tc_chunker import MatterTCChunker
loader = AdocLoader(chunker=MatterTCChunker())
docs = loader.load(fetched_doc)
```

## Use a custom chunker for one extension
```python
from src.chunker.base_chunker import GenericChunker
factory = DocumentLoaderFactory(
    cfg.loader,
    chunker_map={".adoc": GenericChunker(chunk_size=500, chunk_overlap=50)},
)
```

## Use DocumentLoader (backward compat)
```python
from src.loader.document_loader import DocumentLoader
loader = DocumentLoader(cfg.loader, cfg.chunker)
docs = loader.load_all(fetched_documents)
```

## Import Document (from either location)
```python
from src.loader.base_loader import Document        # canonical
from src.loader.document_loader import Document    # backward-compat re-export
```

## Add a new file format
1. Create `src/loader/<fmt>_loader.py` subclassing `BaseDocumentLoader`
2. Implement `load(fetched)` and `supported_extensions()`
3. Register in `DocumentLoaderFactory.__init__()`: `".<ext>": FmtLoader(chunker)`
4. Add extension to `FetcherConfig.local_extensions` default in `models.py`
5. Add test in `tests/test_loader.py`
