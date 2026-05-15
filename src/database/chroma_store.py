"""ChromaStore — ChromaDB-backed vector store with rich JSON metadata."""

from __future__ import annotations

import json
import logging
from typing import List

import numpy as np

from src.database.base_store import BaseVectorStore, SearchResult

logger = logging.getLogger(__name__)

# ChromaDB metadata values must be str | int | float | bool.
# Lists (e.g. pics_codes) are serialised as JSON strings and decoded on retrieval.
_LIST_FIELDS = ("pics_codes",)


def _encode_metadata(meta: dict) -> dict:
    """Encode list fields to JSON strings for ChromaDB storage."""
    out = {}
    for k, v in meta.items():
        if isinstance(v, list):
            out[k] = json.dumps(v)
        elif v is None:
            out[k] = ""
        else:
            out[k] = v
    return out


def _decode_metadata(meta: dict) -> dict:
    """Decode JSON-string list fields back to Python lists."""
    out = dict(meta)
    for field in _LIST_FIELDS:
        raw = out.get(field)
        if isinstance(raw, str) and raw:
            try:
                out[field] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return out


class ChromaStore(BaseVectorStore):
    """ChromaDB-backed vector store.

    Embeddings and full JSON metadata (tc_id, cluster_name, pics_codes,
    section_type, path, …) are stored in a persistent ChromaDB collection.

    Usage::

        store = ChromaStore(config.database)
        store.add_documents(docs, embeddings)
        results = store.search_by_vector(query_vec, k=10, threshold=0.65)
    """

    def __init__(self, config) -> None:
        import chromadb  # type: ignore

        self.config = config
        self._client = chromadb.PersistentClient(path=config.chroma_persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=config.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
        self._id_counter = self._collection.count()
        logger.info(
            "ChromaDB collection '%s' ready (%d existing vectors)",
            config.chroma_collection, self._id_counter,
        )

    def add_documents(self, documents, embeddings: np.ndarray) -> List[str]:
        if len(documents) != len(embeddings):
            raise ValueError(
                f"Mismatch: {len(documents)} documents but {len(embeddings)} embeddings."
            )

        ids, texts, metas, vecs = [], [], [], []
        for doc, emb in zip(documents, embeddings):
            doc_id = f"doc_{self._id_counter:06d}"
            self._id_counter += 1
            ids.append(doc_id)
            texts.append(doc.page_content)
            metas.append(_encode_metadata(doc.metadata))
            vecs.append(emb.tolist())

        self._collection.add(
            ids=ids,
            documents=texts,
            metadatas=metas,
            embeddings=vecs,
        )
        logger.info("ChromaDB: added %d documents. Collection total: %d", len(ids), self._id_counter)
        return ids

    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        if self.is_empty:
            return []

        actual_k = min(k, self.size)
        qvec = query_vector.astype(np.float32).tolist()

        response = self._collection.query(
            query_embeddings=[qvec],
            n_results=actual_k,
            include=["documents", "metadatas", "distances"],
        )

        results: List[SearchResult] = []
        ids = response.get("ids", [[]])[0]
        docs = response.get("documents", [[]])[0]
        metas = response.get("metadatas", [[]])[0]
        # ChromaDB returns cosine *distance* (0=identical, 2=opposite); convert to similarity
        distances = response.get("distances", [[]])[0]

        for rank, (doc_id, page_content, meta, dist) in enumerate(
            zip(ids, docs, metas, distances)
        ):
            score = 1.0 - float(dist) / 2.0   # cosine distance → cosine similarity
            if score < threshold:
                continue
            results.append(SearchResult(
                score=score,
                doc_id=doc_id,
                page_content=page_content,
                metadata=_decode_metadata(meta),
                rank=rank,
            ))

        logger.debug("ChromaDB search: %d results (k=%d, threshold=%.2f)", len(results), k, threshold)
        return results

    @property
    def size(self) -> int:
        return self._collection.count()
