"""FAISSStore — FAISS-backed vector store with metadata JSON sidecar."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from src.database.base_store import BaseVectorStore, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class StoredEntry:
    """One persisted entry in the FAISS store."""
    doc_id: str
    page_content: str
    metadata: dict


class FAISSStore(BaseVectorStore):
    """FAISS-backed vector store with metadata persistence.

    Usage::

        store = FAISSStore(config.database)
        store.add_documents(docs, embeddings)
        store.save()
        # later …
        store.load()
        results = store.search_by_vector(query_vec, k=10, threshold=0.65)
    """

    def __init__(self, config) -> None:
        self.config = config
        self._index = None          # faiss.Index
        self._entries: List[StoredEntry] = []
        self._id_counter = 0
        self._id_index: Dict[str, int] = {}  # doc_id → index in _entries (O(1) lookup)

    # ------------------------------------------------------------------
    # BaseVectorStore implementation
    # ------------------------------------------------------------------

    def add_documents(self, documents, embeddings: np.ndarray) -> List[str]:
        """Add documents and their embeddings to the FAISS index."""
        if len(documents) != len(embeddings):
            raise ValueError(
                f"Mismatch: {len(documents)} documents but {len(embeddings)} embeddings."
            )

        import faiss  # type: ignore

        dim = embeddings.shape[1]
        num_docs = len(documents)
        if self._index is None:
            self._index = self._create_index(dim, num_docs=num_docs)
            logger.info("Created FAISS index (%s, dim=%d)", self.config.index_type, dim)

        vecs = embeddings.astype(np.float32)
        # Normalize for any inner-product based index (IndexFlatIP, IndexIVFFlat)
        faiss.normalize_L2(vecs)

        self._index.add(vecs)

        doc_ids: List[str] = []
        for doc in documents:
            doc_id = f"doc_{self._id_counter:06d}"
            self._id_counter += 1
            entry = StoredEntry(
                doc_id=doc_id,
                page_content=doc.page_content,
                metadata=doc.metadata,
            )
            self._id_index[doc_id] = len(self._entries)
            self._entries.append(entry)
            doc_ids.append(doc_id)

        logger.info("Added %d documents. Total stored: %d", len(documents), len(self._entries))
        return doc_ids

    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        """Search FAISS index with a precomputed query vector."""
        import faiss  # type: ignore

        if self._index is None or self.is_empty:
            return []

        vec = query_vector.astype(np.float32).reshape(1, -1)
        faiss.normalize_L2(vec)

        actual_k = min(k, self.size)
        scores, indices = self._index.search(vec, actual_k)

        results: List[SearchResult] = []
        for rank, (score, idx) in enumerate(zip(scores[0], indices[0])):
            if idx == -1 or float(score) < threshold:
                continue
            entry = self._entries[int(idx)]
            results.append(SearchResult(
                score=float(score),
                doc_id=entry.doc_id,
                page_content=entry.page_content,
                metadata=entry.metadata,
                rank=rank,
            ))

        logger.debug("FAISS search: %d results (k=%d, threshold=%.2f)", len(results), k, threshold)
        for r in results:
            tc_id = r.metadata.get("tc_id", "?")
            cluster = r.metadata.get("cluster_name", r.metadata.get("cluster", "?"))
            chunk_type = r.metadata.get("chunk_type", "?")
            logger.debug(
                "  [rank %d] score=%.3f  tc_id=%-20s  cluster=%-30s  chunk_type=%s  doc_id=%s",
                r.rank, r.score, tc_id, cluster, chunk_type, r.doc_id,
            )
        return results

    def save(self, index_path: Optional[str] = None, meta_path: Optional[str] = None) -> None:
        """Persist the FAISS index and metadata to disk."""
        import faiss  # type: ignore

        idx_path = Path(index_path or self.config.faiss_index_path)
        m_path = Path(meta_path or self.config.metadata_path)

        idx_path.parent.mkdir(parents=True, exist_ok=True)
        m_path.parent.mkdir(parents=True, exist_ok=True)

        if self._index is not None:
            faiss.write_index(self._index, str(idx_path))
            logger.info("FAISS index saved to %s", idx_path)

        meta = {
            "id_counter": self._id_counter,
            "entries": [asdict(e) for e in self._entries],
        }
        m_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        logger.info("Metadata saved to %s (%d entries)", m_path, len(self._entries))

    def load(self, index_path: Optional[str] = None, meta_path: Optional[str] = None) -> None:
        """Load a previously saved FAISS index and metadata from disk."""
        import faiss  # type: ignore

        idx_path = Path(index_path or self.config.faiss_index_path)
        m_path = Path(meta_path or self.config.metadata_path)

        if not idx_path.exists():
            raise FileNotFoundError(f"FAISS index not found: {idx_path}")
        if not m_path.exists():
            raise FileNotFoundError(f"Metadata file not found: {m_path}")

        self._index = faiss.read_index(str(idx_path))
        raw = json.loads(m_path.read_text())
        self._id_counter = raw.get("id_counter", 0)
        self._entries = [StoredEntry(**e) for e in raw.get("entries", [])]
        self._id_index = {entry.doc_id: i for i, entry in enumerate(self._entries)}
        logger.info(
            "Loaded FAISS index (%d vectors) and %d metadata entries",
            self._index.ntotal, len(self._entries),
        )

    @property
    def size(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Helpers for backward-compat with old VectorStore users
    # ------------------------------------------------------------------

    @property
    def index(self):
        """Raw faiss.Index (kept for backward compatibility)."""
        return self._index

    def get_by_id(self, doc_id: str) -> Optional[StoredEntry]:
        idx = self._id_index.get(doc_id)
        if idx is not None:
            return self._entries[idx]
        return None

    def get_by_indices(self, indices: List[int]) -> List[StoredEntry]:
        results = []
        for idx in indices:
            if 0 <= idx < len(self._entries):
                results.append(self._entries[idx])
        return results

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _create_index(self, dim: int, num_docs: int = 0):
        import faiss  # type: ignore

        if self.config.index_type == "IndexFlatIP":
            return faiss.IndexFlatIP(dim)
        elif self.config.index_type == "IndexIVFFlat":
            quantizer = faiss.IndexFlatIP(dim)
            # Use num_docs (count of documents being added) instead of self.size
            # which is 0 at creation time before any documents have been added.
            total = self.size + num_docs
            nlist = max(4, min(256, total // 40))
            return faiss.IndexIVFFlat(quantizer, dim, nlist, faiss.METRIC_INNER_PRODUCT)
        else:
            raise ValueError(f"Unsupported index type: {self.config.index_type}")
