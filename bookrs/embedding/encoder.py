"""Encode works into vectors.

Model choice is a deployment configuration value. The default is
multilingual, measured rather than assumed: against a same-subject
similarity gap on real catalogue data, paraphrase-multilingual-MiniLM
scored +0.604 on English where all-MiniLM-L6-v2 scored +0.597, and
+0.301 on French where the English-only model scored +0.169. There is
no trade-off to weigh -- only a larger download and roughly a third of
the throughput. Both are 384-dimensional, so the schema is unaffected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

from bookrs.embedding.text import WorkText

log = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Bump when the embedding changes in a way that makes stored vectors
# incomparable to newly-generated ones: a different model, a different
# text composition, a different pooling rule. Vectors carry it so a
# change re-embeds rather than silently mixing two vector spaces --
# the same failure the ingestion mapper version prevents, one layer up.
EMBEDDER_VERSION = 1


@dataclass
class EncodedWork:
    work_id: int
    vector: np.ndarray
    is_title_only: bool


class Encoder:
    """Wraps a sentence-transformer with the project's pooling rule."""

    def __init__(self, model_name: str | None = None, batch_size: int = 128):
        # Imported lazily: sentence-transformers pulls in PyTorch, and
        # the text-assembly module above must stay importable without it.
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name or os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)
        self.batch_size = batch_size
        self.model = SentenceTransformer(self.model_name)
        self.dimensions = self.model.get_sentence_embedding_dimension()
        self.max_tokens = self.model.max_seq_length
        log.info("encoder: %s dim=%d max_tokens=%d",
                 self.model_name, self.dimensions, self.max_tokens)

    def encode(self, texts: list[WorkText]) -> np.ndarray:
        """Encode works, averaging core and description where both exist.

        Concatenating the two would exceed the model's input window for
        half the works that carry a description -- 19 of 39 in the MARC21
        reference corpus -- silently discarding the tail of the richest
        field available. Encoding separately and averaging keeps all of
        it.

        The result is L2-normalised, so a dot product is cosine
        similarity and the search layer needs no further scaling.
        """
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)

        core = self._encode_batch([t.core for t in texts])

        described = [i for i, t in enumerate(texts) if t.description]
        if described:
            extra = self._encode_batch([texts[i].description for i in described])
            # Mean of the two unit vectors, then renormalise. Averaging
            # first and normalising after keeps the result on the unit
            # sphere; averaging alone would not.
            core[described] = (core[described] + extra) / 2.0
            core = self._normalise(core)

        return core.astype(np.float32)

    def _encode_batch(self, strings: list[str]) -> np.ndarray:
        return self.model.encode(
            strings,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )

    @staticmethod
    def _normalise(vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        # A zero vector cannot be normalised; leave it rather than
        # dividing by zero. It can only arise from empty input.
        norms[norms == 0] = 1.0
        return vectors / norms
