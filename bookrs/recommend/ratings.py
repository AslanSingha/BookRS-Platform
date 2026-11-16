"""Build the training matrix from star ratings instead of loans.

``als.py`` builds its matrix from ``loans``, one Python object per
(patron, work) pair. That is the right shape for a library's
circulation and the wrong one for sixteen million ratings: the objects
alone would need several gigabytes. Ratings are loaded here straight
into arrays.

The result is the same kind of matrix -- patrons by works, confidence
in the cells -- so everything after it (``fit_factors``, storage,
ranking) is shared with the loan path and does not know which signal it
was given.
"""

from __future__ import annotations

import logging

import numpy as np
import psycopg
from scipy.sparse import csr_matrix

from bookrs.recommend.als import ALSParams, TrainingStats
from bookrs.recommend.confidence import rating_confidence

log = logging.getLogger(__name__)


def load_ratings(conn: psycopg.Connection, source_id: int | None = None
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(patron index, work id, rating, reviewed) for every stored rating.

    Patrons are returned as indices, never as references: training needs
    to know that two ratings belong to the same person and nothing else
    about them.
    """
    where = "" if source_id is None else f"WHERE source_id = {int(source_id)}"
    n = conn.execute(f"SELECT count(*) FROM ratings {where}").fetchone()[0]
    u = np.empty(n, dtype=np.int32)
    w = np.empty(n, dtype=np.int64)
    r = np.empty(n, dtype=np.float32)
    d = np.empty(n, dtype=np.float32)
    index: dict[str, int] = {}
    k = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY (SELECT patron_ref, work_id, rating, reviewed "
                      f"FROM ratings {where}) TO STDOUT") as copy:
            for ref, work, rating, reviewed in copy.rows():
                if k == n:              # rows added since the count
                    break
                u[k] = index.setdefault(ref, len(index))
                w[k], r[k], d[k] = int(work), int(rating), reviewed == "t"
                k += 1
    return u[:k], w[:k], r[:k], d[:k]


def build_rating_matrix(u: np.ndarray, w: np.ndarray, r: np.ndarray,
                        d: np.ndarray, params: ALSParams
                        ) -> tuple[csr_matrix, list[int], TrainingStats]:
    """Confidence-weighted patron-by-work matrix, thin rows and columns removed.

    The same two floors as the loan path, applied until nothing more
    falls: a work rated by one patron has no co-occurrence to learn
    from, and a patron with one rating has no pattern.
    """
    keep = np.ones(len(u), dtype=bool)
    for _ in range(5):
        per_work = np.unique(w[keep], return_counts=True)
        per_patron = np.bincount(u[keep], minlength=int(u.max()) + 1 if len(u) else 0)
        thin_works = per_work[0][per_work[1] < params.min_patrons_per_work]
        ok = keep & ~np.isin(w, thin_works) & (per_patron[u] >= params.min_works_per_patron)
        if ok.sum() == keep.sum():
            break
        keep = ok
    if not keep.any():
        raise ValueError(
            "No ratings survive filtering. A catalogue needs works rated by at "
            "least two patrons before collaborative filtering has anything to "
            "learn from.")

    works, cols = np.unique(w[keep], return_inverse=True)
    patrons, rows = np.unique(u[keep], return_inverse=True)
    conf = rating_confidence(r[keep], d[keep]).astype(np.float32)
    matrix = csr_matrix((conf, (rows, cols)), shape=(len(patrons), len(works)))
    stats = TrainingStats(
        interactions=int(keep.sum()), patrons=len(patrons), works=len(works),
        excluded_works=len(np.unique(w)) - len(works),
        excluded_patrons=len(np.unique(u)) - len(patrons),
        density=100.0 * int(keep.sum()) / (len(patrons) * len(works)),
    )
    return matrix, [int(x) for x in works], stats
