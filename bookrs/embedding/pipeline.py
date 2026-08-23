"""Generate and store embeddings for works that need them.

A work needs (re-)embedding when it has no vector, when its bibliography
has changed since the vector was made, or when the model or composition
has changed. All three are one SQL predicate, which is why the vector
row carries the source hash, the model name and the embedder version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

from bookrs.embedding.encoder import EMBEDDER_VERSION, Encoder
from bookrs.embedding.text import build_text

log = logging.getLogger(__name__)


@dataclass
class EmbedStats:
    considered: int = 0
    embedded: int = 0
    title_only: int = 0
    skipped_empty: int = 0


# A work is stale when it has no embedding, or its embedding was made
# from different bibliographic content, or by a different model or
# composition. LEFT JOIN rather than NOT EXISTS so all three cases fall
# out of one scan.
_STALE = """
    SELECT w.id, w.title, w.subjects, w.summary, w.contents, w.content_hash
    FROM works w
    LEFT JOIN embeddings e ON e.work_id = w.id
    WHERE w.deleted_at IS NULL
      AND (
            e.work_id IS NULL
         OR e.source_hash      IS DISTINCT FROM w.content_hash
         OR e.model            <> %(model)s
         OR e.embedder_version <> %(version)s
      )
      -- Cast is required: PostgreSQL cannot infer a type for a
      -- parameter that only ever appears in IS NULL and an equality,
      -- and raises AmbiguousParameter when None is passed.
      AND (%(source_id)s::int IS NULL OR w.source_id = %(source_id)s::int)
    ORDER BY w.id
"""


def embed_stale(conn: psycopg.Connection, encoder: Encoder, *,
                source_id: int | None = None, batch: int = 512) -> EmbedStats:
    """Embed every work whose vector is missing or out of date.

    Works in batches so a large catalogue is not held in memory: 300,000
    works at 384 float32 is roughly 460 MB of vectors alone, before the
    text they were built from.
    """
    stats = EmbedStats()
    params = {"model": encoder.model_name, "version": EMBEDDER_VERSION,
              "source_id": source_id}

    with conn.cursor() as cur:
        cur.execute(_STALE, params)
        while rows := cur.fetchmany(batch):
            stats.considered += len(rows)

            prepared = []
            for work_id, title, subjects, summary, contents, content_hash in rows:
                text = build_text(title or "", subjects or [],
                                  summary or "", contents or "")
                if not text.core and not text.description:
                    # No title and no subjects. Encoding an empty string
                    # yields a vector that is identical for every such
                    # work, which would make them mutually "similar".
                    stats.skipped_empty += 1
                    continue
                prepared.append((work_id, text, content_hash))

            if not prepared:
                continue

            vectors = encoder.encode([t for _, t, _ in prepared])

            # executemany, not a loop of execute: one round trip per
            # row made the database the bottleneck rather than the
            # model -- 88 works/sec against an encoder measured at 208.
            payload = [
                (work_id, vector.tolist(), encoder.dimensions,
                 encoder.model_name, EMBEDDER_VERSION, content_hash,
                 text.is_title_only)
                for (work_id, text, content_hash), vector in zip(prepared, vectors)
            ]
            with conn.cursor() as write:
                write.executemany(
                    """
                    INSERT INTO embeddings (work_id, vector, dimensions, model,
                                            embedder_version, source_hash,
                                            is_title_only, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (work_id) DO UPDATE SET
                        vector           = EXCLUDED.vector,
                        dimensions       = EXCLUDED.dimensions,
                        model            = EXCLUDED.model,
                        embedder_version = EXCLUDED.embedder_version,
                        source_hash      = EXCLUDED.source_hash,
                        is_title_only    = EXCLUDED.is_title_only,
                        created_at       = now()
                    """,
                    payload,
                )
            stats.embedded += len(payload)
            stats.title_only += sum(1 for _, t, _ in prepared if t.is_title_only)
            conn.commit()
            log.info("embedded %d/%d", stats.embedded, stats.considered)

    return stats
