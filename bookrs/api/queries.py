"""Database queries behind the API.

Kept apart from the HTTP layer so they can be tested directly, and so
the SQL is readable in one place rather than scattered through route
handlers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np
import psycopg

# word_similarity, not similarity. Plain trigram similarity divides by
# the union of trigrams, so a short query against a long field scores
# low however exact the match: "kernighan" against
# "kernighan, brian w. ritchie, dennis m." scores 0.294, below pg_trgm's
# 0.3 default, and a third author would push it lower still. Patrons
# type a surname; records hold "Surname, Forename Initial." for every
# author.
#
# word_similarity measures the query against the best-matching portion
# of the target instead. Measured on the reference catalogue: an exact
# author or title match scores 1.000, a related title ("la mythologie
# celte" for "mythologie grecque") 0.579, and an unrelated one 0.000.
WORD_SIMILARITY_THRESHOLD = 0.45


@dataclass
class WorkSummary:
    id: int
    # The library's own identifier for this record. Our id is
    # meaningless to the OPAC, so every result carries the identifier
    # the catalogue uses -- without it a widget cannot link back to the
    # page a patron came from.
    source_record_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    publication_year: int | None = None
    languages: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    isbns: list[str] = field(default_factory=list)
    copies_total: int = 0
    copies_available: int = 0
    score: float | None = None

    @property
    def is_available(self) -> bool:
        """Availability is the absence of a due date on some copy.

        Derived from the last harvest, so it is as fresh as the sync
        interval. A live check against the ILS would be more accurate
        between syncs, and is deliberately not done here -- this service
        never talks to the library's production system.
        """
        return self.copies_available > 0


_SELECT = """
    SELECT w.id, w.source_record_id,
           w.title, w.authors, w.publisher, w.publication_year,
           w.languages, w.subjects, w.isbns,
           count(i.id)                                        AS copies_total,
           count(i.id) FILTER (WHERE i.due_date IS NULL)       AS copies_available
    FROM works w
    LEFT JOIN items i ON i.work_id = w.id
"""

_GROUP = " GROUP BY w.id "


def _row_to_summary(row, score: float | None = None) -> WorkSummary:
    return WorkSummary(
        id=row[0], source_record_id=row[1], title=row[2],
        authors=row[3] or [], publisher=row[4] or "",
        publication_year=row[5], languages=[l.strip() for l in (row[6] or [])],
        subjects=row[7] or [], isbns=row[8] or [],
        copies_total=row[9], copies_available=row[10], score=score,
    )


def get_work(conn: psycopg.Connection, work_id: int) -> WorkSummary | None:
    row = conn.execute(
        _SELECT + " WHERE w.id = %s AND w.deleted_at IS NULL" + _GROUP,
        (work_id,),
    ).fetchone()
    return _row_to_summary(row) if row else None


def get_work_by_record_id(conn: psycopg.Connection, record_id: str,
                          source_id: int | None = None) -> WorkSummary | None:
    """Find a work by the library's own record identifier.

    Accepts either the full OAI identifier ("KOHA-OAI-TEST:42") or the
    bare biblionumber ("42"). The bare form is what an OPAC page has in
    its URL, and the archive prefix is operator-configurable, so
    requiring the full form would mean every widget had to know its
    library's OAI-PMH:archiveID setting.

    Matching a bare number against the identifier suffix is only
    unambiguous within one source. With several configured, source_id
    is required -- two Koha instances at their shipped defaults both
    emit KOHA-OAI-TEST:1, for different books.
    """
    exact = conn.execute(
        _SELECT + """
        WHERE w.source_record_id = %(rid)s
          AND w.deleted_at IS NULL
          AND (%(sid)s::int IS NULL OR w.source_id = %(sid)s::int)
        """ + _GROUP,
        {"rid": record_id, "sid": source_id},
    ).fetchone()
    if exact:
        return _row_to_summary(exact)

    if not record_id.isdigit():
        return None

    rows = conn.execute(
        _SELECT + """
        WHERE w.source_record_id LIKE %(suffix)s
          AND w.deleted_at IS NULL
          AND (%(sid)s::int IS NULL OR w.source_id = %(sid)s::int)
        """ + _GROUP + " LIMIT 2",
        {"suffix": f"%:{record_id}", "sid": source_id},
    ).fetchall()
    # Ambiguous across sources: refuse rather than return an arbitrary
    # one. Returning the wrong book here would be invisible -- the
    # widget would show plausible recommendations for a different work.
    return _row_to_summary(rows[0]) if len(rows) == 1 else None


def search_exact(conn: psycopg.Connection, query: str, limit: int = 20,
                 source_id: int | None = None) -> list[WorkSummary]:
    """Deterministic lookup by ISBN, title or author.

    ISBN is tried first and returns immediately on a hit: a patron who
    types an ISBN wants that book, not books whose titles resemble a
    string of digits.

    Title and author are matched by trigram, which is language-agnostic
    -- the reference catalogue spans French, English, Greek and Arabic,
    and a tsvector would need a language chosen per row for a field that
    is empty on 11% of records. Trigrams also tolerate the typos real
    catalogues contain: one reference record reads "Le beau chardond'Ali
    Boron", missing a space.
    """
    cleaned = "".join(ch for ch in query if ch.isalnum() or ch == "X" or ch == "x")
    if len(cleaned) in (10, 13) and cleaned[:-1].isdigit():
        rows = conn.execute(
            _SELECT + " WHERE %s = ANY(w.isbns) AND w.deleted_at IS NULL" + _GROUP,
            (cleaned.upper(),),
        ).fetchall()
        if rows:
            return [_row_to_summary(r, score=1.0) for r in rows]

    rows = conn.execute(
        f"""
        WITH q AS (SELECT public.immutable_unaccent(lower(%(q)s)) AS needle)
        SELECT * FROM (
            {_SELECT}
            CROSS JOIN q
            WHERE w.deleted_at IS NULL
              -- Two stages. The <%% operator probes the GIN trigram
              -- index and narrows the catalogue to plausible
              -- candidates; the explicit score below filters and ranks
              -- them. Scoring every row instead is correct but cannot
              -- use the index -- measured at 82-110 ms over 5,285
              -- works, which would be seconds over 300,000.
              --
              -- <%% uses its own threshold (0.6), stricter than the
              -- 0.45 applied below, so the candidate stage is the
              -- binding constraint: a work scoring between 0.45 and 0.6
              -- is never offered for scoring and will not appear.
              -- Measured on the reference catalogue, "mythologie
              -- grecque" returns three titles rather than the four that
              -- exhaustive scoring finds -- the fourth, "Les Plus
              -- Belles Legendes de la Mythologie", scores 0.579.
              --
              -- That is a deliberate trade: exhaustive scoring took
              -- 82-110 ms over 5,285 works and would take seconds over
              -- 300,000. Top-ranked results are unaffected; only the
              -- tail is. If recall there matters more than latency for
              -- a given deployment, lower pg_trgm's threshold with
              -- set_limit() rather than removing this stage.
              AND (q.needle <%% public.immutable_unaccent(lower(w.title))
                   OR q.needle <%% public.searchable_authors(w.authors)
                   OR public.immutable_unaccent(lower(w.title)) %% q.needle)
              AND greatest(
                    word_similarity(q.needle,
                        public.immutable_unaccent(lower(w.title))),
                    word_similarity(q.needle,
                        public.searchable_authors(w.authors))
                  ) >= %(threshold)s
              AND (%(source_id)s::int IS NULL OR w.source_id = %(source_id)s::int)
            {_GROUP}, q.needle
        ) matched
        ORDER BY greatest(
            word_similarity(public.immutable_unaccent(lower(%(q)s)),
                            public.immutable_unaccent(lower(matched.title))),
            word_similarity(public.immutable_unaccent(lower(%(q)s)),
                            public.searchable_authors(matched.authors))
        ) DESC
        LIMIT %(limit)s
        """,
        {"q": query, "limit": limit, "source_id": source_id,
         "threshold": WORD_SIMILARITY_THRESHOLD},
    ).fetchall()
    return [_row_to_summary(r) for r in rows]


class _VectorCache:
    """Holds the embedding matrix in process.

    Fetching 5,284 vectors from PostgreSQL costs 432 ms; the dot product
    against them costs 1 ms. Without a cache the database round trip is
    98% of every similarity request.

    Invalidated by comparing a cheap probe -- row count and the latest
    created_at -- rather than by a signal from the embedding service,
    which runs in a different container and may not be running at all.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probe: tuple | None = None
        self._ids: list[int] = []
        self._matrix: np.ndarray | None = None
        self._title_only: np.ndarray = np.zeros(0, dtype=bool)

    def get(self, conn: psycopg.Connection) -> tuple[list[int], np.ndarray]:
        probe = conn.execute(
            "SELECT count(*), max(created_at) FROM embeddings"
        ).fetchone()
        with self._lock:
            if probe != self._probe or self._matrix is None:
                rows = conn.execute(
                    """
                    SELECT e.work_id, e.vector, e.is_title_only
                    FROM embeddings e JOIN works w ON w.id = e.work_id
                    WHERE w.deleted_at IS NULL
                    ORDER BY e.work_id
                    """
                ).fetchall()
                self._ids = [r[0] for r in rows]
                self._title_only = np.array([r[2] for r in rows], dtype=bool)
                self._matrix = np.asarray([r[1] for r in rows], dtype=np.float32)
                self._probe = probe
            return self._ids, self._matrix

    @property
    def title_only_mask(self) -> np.ndarray:
        return self._title_only


VECTORS = _VectorCache()


def similar_works(conn: psycopg.Connection, work_id: int, limit: int = 10,
                  exclude_title_only: bool = False) -> list[WorkSummary]:
    """Works whose embeddings are nearest to this one.

    Similarity is computed in the process rather than the database:
    vectors are stored as REAL[], and one query vector against the whole
    catalogue is a single matrix multiply. At 300,000 works that is
    roughly 30 ms, which is why pgvector is not yet needed.

    Vectors are L2-normalised at generation, so the dot product is
    cosine similarity directly.
    """
    probe = conn.execute(
        "SELECT vector FROM embeddings WHERE work_id = %s", (work_id,)
    ).fetchone()
    if probe is None:
        return []

    ids, matrix = VECTORS.get(conn)
    if matrix is None or not len(ids):
        return []

    scores = matrix @ np.asarray(probe[0], dtype=np.float32)

    # Exclusions must remove candidates, not merely de-rank them.
    # Assigning a low score works only while there are more candidates
    # than the limit: with fewer, argpartition returns every index
    # including the ones scored -1, and the excluded works reappear.
    keep = np.array([wid != work_id for wid in ids])
    if exclude_title_only:
        keep &= ~VECTORS.title_only_mask
    if not keep.any():
        return []

    kept_ids = [wid for wid, k in zip(ids, keep) if k]
    kept_scores = scores[keep]

    n = min(limit, len(kept_scores))
    top = np.argpartition(-kept_scores, n - 1)[:n]
    top = top[np.argsort(-kept_scores[top])]
    chosen = {kept_ids[i]: float(kept_scores[i]) for i in top}

    detail = conn.execute(
        _SELECT + " WHERE w.id = ANY(%s)" + _GROUP, (list(chosen),)
    ).fetchall()
    results = [_row_to_summary(r, score=chosen[r[0]]) for r in detail]
    results.sort(key=lambda w: w.score, reverse=True)
    return results
