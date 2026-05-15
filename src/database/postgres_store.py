"""PostgresStore — PostgreSQL + pgvector-backed vector store."""

from __future__ import annotations

import json
import logging
import uuid
from typing import List

import numpy as np

from src.database.base_store import BaseVectorStore, SearchResult

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {table} (
    id          TEXT PRIMARY KEY,
    page_content TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{{}}',
    embedding   vector({dim})
);

CREATE INDEX IF NOT EXISTS {table}_embedding_idx
    ON {table} USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""

_INSERT_SQL = """
INSERT INTO {table} (id, page_content, metadata, embedding)
VALUES (%s, %s, %s::jsonb, %s::vector)
"""

_SEARCH_SQL = """
SELECT id, page_content, metadata,
       1 - (embedding <=> %s::vector) AS score
FROM {table}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


class PostgresStore(BaseVectorStore):
    """PostgreSQL + pgvector-backed vector store.

    The table schema stores all TC metadata as JSONB so the full context
    (tc_id, cluster_name, pics_codes, section_type, path, …) is available
    alongside each vector hit.

    Usage::

        store = PostgresStore(config.database)
        store.add_documents(docs, embeddings)
        results = store.search_by_vector(query_vec, k=10, threshold=0.65)

    Requires:
        POSTGRES_URL or config.database.postgres_url
        pip install psycopg2-binary pgvector
    """

    def __init__(self, config) -> None:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore

        self.config = config
        self._table = config.postgres_table
        self._conn = psycopg2.connect(config.postgres_url)
        self._conn.autocommit = False
        psycopg2.extras.register_default_jsonb(self._conn)
        self._dim: int | None = None
        logger.info("PostgresStore connected (table=%s)", self._table)

    # ------------------------------------------------------------------
    # BaseVectorStore
    # ------------------------------------------------------------------

    def add_documents(self, documents, embeddings: np.ndarray) -> List[str]:
        if len(documents) != len(embeddings):
            raise ValueError(
                f"Mismatch: {len(documents)} documents but {len(embeddings)} embeddings."
            )

        dim = int(embeddings.shape[1])
        self._ensure_table(dim)

        cur = self._conn.cursor()
        ids: List[str] = []
        for doc, emb in zip(documents, embeddings):
            doc_id = str(uuid.uuid4())
            ids.append(doc_id)
            cur.execute(
                _INSERT_SQL.format(table=self._table),
                (
                    doc_id,
                    doc.page_content,
                    json.dumps(doc.metadata),
                    _vec_to_pg(emb),
                ),
            )
        self._conn.commit()
        cur.close()
        logger.info("PostgresStore: inserted %d rows into '%s'", len(ids), self._table)
        return ids

    def search_by_vector(
        self,
        query_vector: np.ndarray,
        k: int = 10,
        threshold: float = 0.0,
    ) -> List[SearchResult]:
        if self.is_empty:
            return []

        qvec = _vec_to_pg(query_vector.astype(np.float32))
        cur = self._conn.cursor()
        cur.execute(
            _SEARCH_SQL.format(table=self._table),
            (qvec, qvec, k),
        )
        rows = cur.fetchall()
        cur.close()

        results: List[SearchResult] = []
        for rank, (doc_id, page_content, metadata, score) in enumerate(rows):
            if float(score) < threshold:
                continue
            results.append(SearchResult(
                score=float(score),
                doc_id=doc_id,
                page_content=page_content,
                metadata=metadata if isinstance(metadata, dict) else json.loads(metadata),
                rank=rank,
            ))

        logger.debug("PostgresStore search: %d results (k=%d, threshold=%.2f)", len(results), k, threshold)
        return results

    @property
    def size(self) -> int:
        cur = self._conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {self._table}")  # noqa: S608
        count = cur.fetchone()[0]
        cur.close()
        return int(count)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_table(self, dim: int) -> None:
        """Create table + index if they don't exist yet."""
        if self._dim == dim:
            return
        self._dim = dim
        cur = self._conn.cursor()
        # Execute each statement separately (psycopg2 doesn't support multi-statement)
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id           TEXT PRIMARY KEY,
                page_content TEXT NOT NULL,
                metadata     JSONB NOT NULL DEFAULT '{{}}',
                embedding    vector({dim})
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx
                ON {self._table} USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100);
        """)
        self._conn.commit()
        cur.close()
        logger.info("PostgresStore: table '%s' ready (dim=%d)", self._table, dim)

    def __del__(self):
        try:
            if hasattr(self, "_conn") and self._conn:
                self._conn.close()
        except Exception:
            pass


def _vec_to_pg(vec: np.ndarray) -> str:
    """Convert a numpy vector to PostgreSQL vector literal string."""
    return "[" + ",".join(f"{v:.8f}" for v in vec.flatten().tolist()) + "]"
