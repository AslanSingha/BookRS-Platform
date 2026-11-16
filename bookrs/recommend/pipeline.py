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
    fit_factors,
    load_interactions,
    normalise,
)

log = logging.getLogger(__name__)


SIGNALS = ("loans", "ratings")


def factorise(conn: psycopg.Connection, params: ALSParams | None = None,
              source_id: int | None = None, signal: str = "loans") -> TrainingStats:
    """Refit from the chosen signal and replace the stored factors.

    ``signal`` is what the library has: ``loans`` weighs circulation by
    intensity (``confidence.py``), ``ratings`` weighs star ratings by
    the thesis's formula. One or the other, not a mixture: a loan and a
    rating are different statements about a book, and how to add them
    is a question nothing here has measured.

    Replaced wholesale rather than updated in place: a refit produces a
    new factor space, and old vectors are not comparable to new ones.
    Leaving stale rows behind would mean similarity computed across two
    unrelated spaces, which returns confident nonsense rather than an
    error.
    """
    params = params or ALSParams()
    if signal not in SIGNALS:
        raise ValueError(f"signal must be one of {SIGNALS}")

    if signal == "ratings":
        from bookrs.recommend.ratings import build_rating_matrix, load_ratings

        u, w, r, d = load_ratings(conn, source_id)
        if not len(u):
            raise ValueError(
                "No ratings stored. Read them from the library first "
                "(bookrs.ingestion.direct).")
        matrix, works, stats = build_rating_matrix(u, w, r, d, params)
    else:
        interactions = load_interactions(conn, source_id)
        if not interactions:
            raise ValueError(
                "No circulation history. Collaborative filtering needs loans; "
                "harvest them before factorising."
            )
        matrix, _patrons, works, stats = build_matrix(interactions, params)

    log.info("matrix %dx%d, %d observations, %.2f%% dense",
             matrix.shape[0], matrix.shape[1], matrix.nnz, stats.density)

    started = time.monotonic()
    raw = fit_factors(matrix, params)
    factors = normalise(raw)
    stats.seconds = time.monotonic() - started

    # Patrons per work, so the ranking layer can tell a work backed by
    # two of them from one backed by two hundred -- and so a reader with
    # no history at all can be shown what most people chose.
    per_work = np.asarray((matrix > 0).sum(axis=0)).ravel()

    payload = [
        (work_id, factors[column].tolist(), raw[column].tolist(),
         factors.shape[1], FACTORISER_VERSION, int(per_work[column]))
        for column, work_id in enumerate(works)
    ]

    with conn.cursor() as cur:
        cur.execute("TRUNCATE work_factors")
        cur.executemany(
            """
            INSERT INTO work_factors (work_id, vector, raw_vector, dimensions,
                                      factoriser_version, n_patrons)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            payload,
        )
    conn.commit()

    log.info("stored %d work factors in %.1fs", len(payload), stats.seconds)
    return stats
