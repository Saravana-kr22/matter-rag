"""Embeddings module — BGE sentence-transformer embeddings."""

from __future__ import annotations

import logging
from typing import List

import numpy as np
import os

from src.config.config_loader import EmbeddingsConfig
from src.loader.base_loader import Document

logger = logging.getLogger(__name__)

# BGE models use a query prefix for asymmetric search
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingsModule:
    """Create dense vector embeddings using a BGE sentence-transformer model."""

    def __init__(self, config: EmbeddingsConfig) -> None:
        self.config = config
        self._model = None  # lazy-loaded
        self._set_hf_offline = False  # track whether we set HF_HUB_OFFLINE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_documents(self, documents: List[Document]) -> np.ndarray:
        """Embed a list of Document objects.

        Args:
            documents: List of Document chunks to embed.

        Returns:
            np.ndarray of shape (N, embedding_dim).
        """
        texts = [doc.page_content for doc in documents]
        return self._encode(texts, is_query=False)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query string.

        Args:
            query: The search query.

        Returns:
            np.ndarray of shape (embedding_dim,).
        """
        result = self._encode([query], is_query=True)
        return result[0]

    def embed_texts(self, texts: List[str], is_query: bool = False) -> np.ndarray:
        """Embed arbitrary strings.

        Args:
            texts: List of strings to embed.
            is_query: If True, prepend BGE query prefix.

        Returns:
            np.ndarray of shape (N, embedding_dim).
        """
        return self._encode(texts, is_query=is_query)

    @property
    def embedding_dim(self) -> int:
        """Return the embedding dimension for the loaded model."""
        model = self._get_model()
        return model.get_sentence_embedding_dimension()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_model(self):
        """Lazy-load the sentence-transformer model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError:
                raise ImportError(
                    "Install sentence-transformers: pip install sentence-transformers"
                )

            from pathlib import Path
            cache_dir = str(Path(self.config.cache_dir).expanduser().resolve())

            if self.config.offline:
                # HF_HUB_OFFLINE=1 stops huggingface_hub from making ANY network
                # request (including the HEAD check that fires even with
                # local_files_only=True in newer huggingface-hub versions).
                # Only set if not already set by user to avoid mutating their env.
                if "HF_HUB_OFFLINE" not in os.environ:
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    self._set_hf_offline = True

            logger.info(
                "Loading embedding model '%s' on device '%s' (cache: %s, offline: %s)",
                self.config.model,
                self.config.device,
                cache_dir,
                self.config.offline,
            )
            self._model = SentenceTransformer(
                self.config.model,
                device=self.config.device,
                cache_folder=cache_dir,
                local_files_only=self.config.offline,
            )
        return self._model

    def _encode(self, texts: List[str], is_query: bool) -> np.ndarray:
        """Encode texts, optionally prepending BGE query prefix."""
        if not texts:
            return np.empty((0, 0), dtype=np.float32)

        model = self._get_model()

        # BGE models use a prefix for query encoding
        if is_query and "bge" in self.config.model.lower():
            texts = [_BGE_QUERY_PREFIX + t for t in texts]

        logger.debug("Encoding %d texts (is_query=%s)", len(texts), is_query)
        if is_query:
            for t in texts:
                logger.debug("  query: %r", t[:200])

        embeddings = model.encode(
            texts,
            batch_size=self.config.batch_size,
            normalize_embeddings=self.config.normalize,
            show_progress_bar=len(texts) > 50,
        )
        result = np.array(embeddings, dtype=np.float32)

        # Log output embedding stats for traceability.
        if is_query:
            for idx, vec in enumerate(result):
                logger.debug(
                    "  embedding[%d]: shape=%s  norm=%.4f  first_4=%s",
                    idx, vec.shape,
                    float(np.linalg.norm(vec)),
                    [round(float(v), 4) for v in vec[:4]],
                )
        else:
            logger.debug(
                "  encoded %d docs: shape=%s  norm_range=[%.4f, %.4f]",
                len(result), result.shape,
                float(np.linalg.norm(result, axis=1).min()),
                float(np.linalg.norm(result, axis=1).max()),
            )
        return result
