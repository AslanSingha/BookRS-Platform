"""Fit the factorisation and store the result."""

from __future__ import annotations

import logging
import time

import numpy as np
import psycopg

from bookrs.recommend.als import (
    FACTORISER_VERSION,
    ALSParams,
    TrainingStats,
    build_matrix,
    load_interactions,
    train,
)

log = logging.getLogger(__name__)


def factorise(conn: psycopg.Connection, params: ALSParams | None = None,
              source_id: int | None = None) -> TrainingStats:
    """Refit from current circulation and replace the stored factors.

    Replaced wholesale rather than updated in place: a refit produces a
    new factor space, and old vectors are not comparable to new ones.
    Leaving stale rows behind would mean similarity computed across two
    unrelated spaces, which returns confident nonsense rather than an
    error.
    """
    params = params or ALSParams()

    interactions = load_interactions(conn, source_id)
    if not interactions:
        raise ValueError(
            "No circulation history. Collaborative filtering needs loans; "
            "harvest them before factorising."
        )

    matrix, patrons, works, stats = build_matrix(interactions, params)
    log.info("matrix %dx%d, %d observations, %.2f%% dense",
             matrix.shape[0], matrix.shape[1], matrix.nnz, stats.density)

    started = time.monotonic()
    factors = train(matrix, params)
    stats.seconds = time.monotonic() - started

    # Borrowers per work, so the ranking layer can tell a work backed by
    # two loans from one backed by two hundred.
    per_work = np.asarray((matrix > 0).sum(axis=0)).ravel()

    payload = [
        (work_id, factors[column].tolist(), factors.shape[1],
         FACTORISER_VERSION, int(per_work[column]))
        for column, work_id in enumerate(works)
    ]

    with conn.cursor() as cur:
        cur.execute("TRUNCATE work_factors")
        cur.executemany(
            """
            INSERT INTO work_factors (work_id, vector, dimensions,
                                      factoriser_version, n_patrons)
            VALUES (%s, %s, %s, %s, %s)
            """,
            payload,
        )
    conn.commit()

    log.info("stored %d work factors in %.1fs", len(payload), stats.seconds)
    return stats
