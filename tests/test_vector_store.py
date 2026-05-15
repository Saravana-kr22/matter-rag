"""Tests for FAISS vector store."""
import numpy as np
import pytest

from src.config.config_loader import DatabaseConfig
from src.database.vector_store import VectorStore
from src.loader.document_loader import Document

pytest.importorskip("faiss", reason="faiss-cpu not installed")


@pytest.fixture
def store(tmp_path):
    cfg = DatabaseConfig(
        faiss_index_path=str(tmp_path / "test.index"),
        metadata_path=str(tmp_path / "meta.json"),
    )
    return VectorStore(cfg)


def make_docs(n=5):
    return [Document(page_content=f"test doc {i}", metadata={"idx": i}) for i in range(n)]


def make_embeddings(n=5, dim=32):
    return np.random.rand(n, dim).astype(np.float32)


def test_add_and_size(store):
    docs = make_docs(3)
    embs = make_embeddings(3)
    ids = store.add_documents(docs, embs)
    assert len(ids) == 3
    assert store.size == 3


def test_save_and_load(store, tmp_path):
    docs = make_docs(4)
    embs = make_embeddings(4)
    store.add_documents(docs, embs)
    store.save()

    store2 = VectorStore(store.config)
    store2.load()
    assert store2.size == 4


def test_mismatch_raises(store):
    docs = make_docs(3)
    embs = make_embeddings(2)
    with pytest.raises(ValueError):
        store.add_documents(docs, embs)


def test_get_by_indices(store):
    docs = make_docs(3)
    embs = make_embeddings(3)
    store.add_documents(docs, embs)
    entries = store.get_by_indices([0, 2])
    assert len(entries) == 2
