"""Read, read-only, the ratings and review flags a Koha library holds.

Koha collects patron star ratings and written reviews, and offers no
REST route that returns either (docs/marc-field-analysis.md). They are
read here from the two tables that hold them. The catalogue itself is
read by ``koha_db.py`` in the same way; between them the two modules are
everything this service needs from a library to run the thesis's model:
what the books are, who rated which, and whether they also reviewed it.

What this does NOT do:

* **Write.** Every statement sent to the library is a SELECT. The
  account a library grants should be read-only and scoped to the tables
  named below; the code is not the thing standing between this service
  and a library's data, the grant is.
* **Read what a patron wrote.** The review's text is never selected.
  The thesis's confidence formula needs to know that a review exists,
  not what it says, so that is all that crosses.
* **Change the privacy position.** A borrower number goes through the
  same keyed HMAC as it does for loans, so a patron who borrows a book
  and rates it produces one ``patron_ref``, not two, and no borrower
  number is stored.

**Scale.** The first version of this module fetched every rating into a
list and inserted them one statement at a time. That was written against
a library with a handful of ratings; against sixteen million it needs
several gigabytes and most of a day. Rows are now streamed from the
library in batches, copied into a staging table, and merged in one
statement.

**A full read** makes what is stored equal to what the library holds: a
rating a patron has since removed is removed here too. The one exception
is a read that returns nothing while ratings are already stored. That is
far more often a wrong connection than a library whose patrons all
withdrew their ratings, so it stops instead of emptying the table. On the
UCSD fixture a full read takes twelve minutes, nine of them comparing
sixteen million ratings that did not change.

**A read of changes** (``--changes``) asks only for what changed since the
last read, and takes seconds, so it can run every few minutes and a
patron's new rating counts for their recommendations soon after they
give it. Since when is measured by Koha's own clock: each read records
Koha's ``NOW()`` at its start (``sources.ratings_watermark``, migration
7), and the next asks for ratings stamped from ten minutes before it --
a margin for writes that commit late, harmless because unchanged
ratings are not rewritten. A changed rating carries its review flag as
Koha holds it, asked of Koha for that patron and record; a review
written since the last read sets the flag on its rating. What a read of
changes cannot see is what was removed, a rating or a review, so
``--full-every 1d`` still reads everything once a day; and with no
earlier read recorded, the first read is a full one.

Koha has no index on ``ratings.timestamp``, and this service creates
none in a library's database: the library's server scans the ratings to
find the changed ones, a few seconds for sixteen million.

    docker compose run --rm ingestion python -m bookrs.ingestion.direct \\
        --source-id 1 --url 'mysql://ro_user:pw@host:3306/koha_kohadev'
    docker compose run --rm ingestion python -m bookrs.ingestion.direct \\
        --source-id 1 --changes --full-every 1d
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone

from bookrs.ingestion.circulation import patron_reference

log = logging.getLogger("bookrs.ingestion.direct")

# Named columns, no joins. A reader that selects * breaks when a column
# is added; a reader that joins depends on more of the schema than it
# needs. No ORDER BY: at this size a sort on the library's server is
# work done for nothing, since the merge does not care about order.
_KOHA_RATINGS = """
    SELECT borrowernumber, biblionumber, rating_value, timestamp
    FROM ratings
"""

# `review` -- the text -- is deliberately absent from this list.
#
# Approved or not: `approved` records a librarian's decision about what
# the public catalogue displays, not whether the patron wrote something.
# Both foreign keys are nullable in Koha (a deleted patron's reviews are
# kept with the borrower set to NULL), and a review with either missing
# cannot be attributed.
_KOHA_REVIEWS = """
    SELECT borrowernumber, biblionumber
    FROM reviews
    WHERE borrowernumber IS NOT NULL AND biblionumber IS NOT NULL
"""

# Koha's clock, asked before anything is read: the next read of changes
# starts from this moment as the same clock that stamps ratings saw it.
_KOHA_NOW = "SELECT NOW()"

_KOHA_RATINGS_SINCE = """
    SELECT borrowernumber, biblionumber, rating_value, timestamp
    FROM ratings
    WHERE timestamp >= %s
"""

# Reviews written or edited since the last read. Each sets the flag on
# its patron's stored rating of that record.
_KOHA_REVIEWS_SINCE = """
    SELECT borrowernumber, biblionumber
    FROM reviews
    WHERE datereviewed >= %s
      AND borrowernumber IS NOT NULL AND biblionumber IS NOT NULL
"""

# Whether each changed rating has a review, written whenever: asked for
# those patron and record pairs only, through the indexes Koha keeps on
# both columns. {pairs} is "(%s, %s), (%s, %s), ..." -- placeholders only.
_KOHA_REVIEWS_OF = """
    SELECT DISTINCT borrowernumber, biblionumber
    FROM reviews
    WHERE (borrowernumber, biblionumber) IN ({pairs})
"""

# How far before the recorded watermark a read of changes starts.
CHANGES_MARGIN = timedelta(minutes=10)
#: More ratings changed than this and the read is a full one instead. A
#: read of changes holds them in memory, which is right for the handful
#: between reads every few minutes and wrong after a bulk load: the full
#: UCSD catalogue added 26 million ratings at once. The full read streams.
MAX_CHANGES = 200_000
_PAIRS_PER_QUERY = 1_000

_BATCH = 50_000


class _Pseudonymiser:
    """HMAC each borrower once.

    A library has far fewer patrons than ratings -- 447 thousand against
    16 million in the UCSD fixture -- so caching by borrower saves all
    but a few percent of the hashing.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._seen: dict[int, str] = {}

    def __call__(self, borrowernumber: int) -> str:
        ref = self._seen.get(borrowernumber)
        if ref is None:
            ref = self._seen[borrowernumber] = patron_reference(
                borrowernumber, self._secret)
        return ref

    def __len__(self) -> int:
        return len(self._seen)


def _stream(conn, sql: str, params: tuple = ()) -> Iterator[tuple]:
    """Rows of a query, a batch at a time.

    Whether this actually streams depends on the cursor the connection
    hands out: main() asks pymysql for an unbuffered one. With a
    buffered cursor it is still correct, only no longer frugal.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params or None)
        while True:
            rows = cur.fetchmany(_BATCH)
            if not rows:
                return
            yield from rows


def read_koha_ratings(conn, pseudonym) -> Iterator[tuple]:
    """(patron_ref, biblionumber, rating, rated_at) for every rating.

    Koha's ``ratings`` table is keyed on (borrowernumber, biblionumber),
    so a patron has at most one rating per record and there is nothing
    to deduplicate. A re-rating overwrites in place and arrives here as
    the current value with a new timestamp.
    """
    for borrower, biblio, value, stamp in _stream(conn, _KOHA_RATINGS):
        yield pseudonym(borrower), str(biblio), int(value), stamp


def read_koha_reviews(conn, pseudonym) -> Iterator[tuple]:
    """(patron_ref, biblionumber) for every attributable review.

    Unlike ratings, nothing stops a patron reviewing a record twice;
    the merge treats any number of reviews as one flag.
    """
    for borrower, biblio in _stream(conn, _KOHA_REVIEWS):
        yield pseudonym(borrower), str(biblio)


def koha_clock(conn) -> datetime:
    """Koha's NOW(), as Koha's connection reports it: the moment the next
    read of changes starts from."""
    with conn.cursor() as cur:
        cur.execute(_KOHA_NOW)
        return cur.fetchone()[0]


class TooManyChanges(Exception):
    """More ratings changed than a read of changes should hold."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"more than {limit:,} ratings changed")
        self.limit = limit


def read_koha_changes(conn, pseudonym, since: datetime,
                      limit: int | None = None) -> tuple[list, list]:
    """What changed in the library from ``since`` (Koha's clock) on.

    Returns ``(ratings, flagged)``. ``ratings`` are (patron_ref,
    biblionumber, rating, rated_at, reviewed) for each rating given or
    changed since, ``reviewed`` saying whether that patron has reviewed
    that record at any time. ``flagged`` are (patron_ref, biblionumber)
    for each review written or edited since.

    Held in memory rather than streamed: between reads every few minutes
    a library's changes are a handful, and the review question needs
    the whole list of pairs before it can be asked. More than ``limit``
    (MAX_CHANGES) raise TooManyChanges, having held no more than that.
    """
    limit = MAX_CHANGES if limit is None else limit
    changed = []
    rows = _stream(conn, _KOHA_RATINGS_SINCE, (since,))
    try:
        for row in rows:
            changed.append(row)
            if len(changed) > limit:
                raise TooManyChanges(limit)
    finally:
        rows.close()        # the library's cursor, before anything else asks
    pairs = [(borrower, biblio) for borrower, biblio, _, _ in changed]
    reviewed: set[tuple] = set()
    for i in range(0, len(pairs), _PAIRS_PER_QUERY):
        chunk = pairs[i:i + _PAIRS_PER_QUERY]
        sql = _KOHA_REVIEWS_OF.format(pairs=", ".join(["(%s, %s)"] * len(chunk)))
        reviewed.update(tuple(row) for row in
                        _stream(conn, sql, tuple(v for pair in chunk for v in pair)))
    ratings = [(pseudonym(b), str(r), int(v), stamp, (b, r) in reviewed)
               for b, r, v, stamp in changed]
    flagged = [(pseudonym(b), str(r))
               for b, r in _stream(conn, _KOHA_REVIEWS_SINCE, (since,))]
    return ratings, flagged


def read_state(conn, source_id: int) -> tuple:
    """(ratings_watermark, ratings_full_read_at) recorded for a source."""
    row = conn.execute(
        "SELECT ratings_watermark, ratings_full_read_at FROM sources WHERE id = %s",
        (source_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def choose_read(changes: bool, watermark, full_read_at, full_every, now) -> str:
    """'changes' or why the read must be a full one."""
    if not changes:
        return "full: asked for"
    if watermark is None:
        return "full: no earlier read is recorded to read changes from"
    if full_every is not None and (full_read_at is None
                                   or now - full_read_at >= full_every):
        return "full: the last full read is older than --full-every"
    return "changes"


def _copy(cur, table: str, rows: Iterable[tuple]) -> int:
    n = 0
    with cur.copy(f"COPY {table} FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
            n += 1
            if n % 2_000_000 == 0:
                log.info("  %s: %d rows", table.lstrip("_"), n)
    return n


class EmptyRead(RuntimeError):
    """The library returned no ratings, and some are already stored."""


# A source's works by biblionumber. source_record_id is a bare
# biblionumber when the catalogue was read from the database, and a full
# OAI identifier ending in the biblionumber when it was harvested; one
# expression covers both. Takes the source id as its one parameter.
_WORKS_BY_BIB = """(SELECT DISTINCT ON (bib) bib, id
                   FROM (SELECT regexp_replace(source_record_id, '^.*:', '') AS bib, id
                         FROM works WHERE source_id = %s) x
                   ORDER BY bib, id)"""

# The WHERE on the update keeps an unchanged rating from being rewritten,
# which is nearly all of them on every full read after the first.
#
# ORDER BY the primary key, because patron_ref is a hash: in arrival
# order every row lands on a random page of the index, and once the index
# outgrows memory each insert is a disk seek. The first sixteen-million-
# row load ran for over ten minutes on this statement alone. Sorted, the
# index is filled left to right.
_UPSERT = """
    INSERT INTO ratings (source_id, patron_ref, work_id, rating, rated_at, reviewed)
    SELECT %s, patron_ref, work_id, rating, rated_at, reviewed
    FROM {table}
    ORDER BY patron_ref, work_id
    ON CONFLICT (source_id, patron_ref, work_id) DO UPDATE
        SET rating = EXCLUDED.rating, rated_at = EXCLUDED.rated_at,
            reviewed = EXCLUDED.reviewed
        WHERE (ratings.rating, ratings.reviewed)
              IS DISTINCT FROM (EXCLUDED.rating, EXCLUDED.reviewed)
"""


def store(conn, source_id: int, ratings: Iterable[tuple],
          reviews: Iterable[tuple] = (), *, allow_empty: bool = False,
          watermark: datetime | None = None) -> dict:
    """Make this source's stored ratings equal to what was just read.

    Returns counts: ``read``, ``reviews_read``, ``stored`` (inserted or
    changed), ``unchanged``, ``removed``, ``unresolved`` (for records
    the catalogue read has not seen), ``invalid`` (outside 1-5) and
    ``reviews_without_rating``.

    A rating for a record this service does not hold is counted and
    dropped rather than inserted against a guess: the catalogue is read
    by koha_db.py, and a rating arriving first simply waits for it.
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE _ratings_in (
                patron_ref   varchar(64) NOT NULL,
                biblionumber text        NOT NULL,
                rating       integer     NOT NULL,
                rated_at     timestamptz
            ) ON COMMIT DROP""")
        cur.execute("""
            CREATE TEMP TABLE _reviews_in (
                patron_ref   varchar(64) NOT NULL,
                biblionumber text        NOT NULL
            ) ON COMMIT DROP""")

        n_ratings = _copy(cur, "_ratings_in", ratings)
        n_reviews = _copy(cur, "_reviews_in", reviews)
        # The merge below is several statements over everything just
        # read and reports nothing until it is done. Say so, or a long
        # silence after the last progress line looks like a hang.
        log.info("read %d ratings and %d reviews; merging (no output "
                 "until it finishes)", n_ratings, n_reviews)

        already = cur.execute(
            "SELECT count(*) FROM ratings WHERE source_id = %s",
            (source_id,)).fetchone()[0]
        if n_ratings == 0 and already and not allow_empty:
            raise EmptyRead(
                f"the library returned no ratings but {already} are stored "
                f"for source {source_id}. Nothing was changed. If the "
                "library really has none now, run again with --allow-empty.")

        # Statistics before the joins below: a temporary table has none
        # until asked, and without them the planner guesses.
        cur.execute("ANALYZE _reviews_in")
        cur.execute("ANALYZE _ratings_in")

        cur.execute(f"""
            CREATE TEMP TABLE _resolved ON COMMIT DROP AS
            SELECT r.patron_ref, w.id AS work_id, r.rating::smallint AS rating,
                   coalesce(r.rated_at, now()) AS rated_at,
                   v.patron_ref IS NOT NULL AS reviewed
            FROM _ratings_in r
            LEFT JOIN (SELECT DISTINCT patron_ref, biblionumber
                       FROM _reviews_in) v
                   ON v.patron_ref = r.patron_ref
                  AND v.biblionumber = r.biblionumber
            JOIN {_WORKS_BY_BIB} w ON w.bib = r.biblionumber
            WHERE r.rating BETWEEN 1 AND 5
            """, (source_id,))
        resolved = cur.rowcount
        cur.execute("CREATE UNIQUE INDEX ON _resolved (patron_ref, work_id)")
        cur.execute("ANALYZE _resolved")

        invalid = cur.execute(
            "SELECT count(*) FROM _ratings_in WHERE rating NOT BETWEEN 1 AND 5"
        ).fetchone()[0]

        # The WHERE on the update keeps an unchanged rating from being
        # rewritten, which is nearly all of them on every run after the
        # first.
        #
        # ORDER BY the primary key, because patron_ref is a hash: in
        # arrival order every row lands on a random page of the index,
        # and once the index outgrows memory each insert is a disk seek.
        # The first sixteen-million-row load ran for over ten minutes on
        # this statement alone. Sorted, the index is filled left to
        # right.
        cur.execute(_UPSERT.format(table="_resolved"), (source_id,))
        stored = cur.rowcount

        cur.execute("""
            DELETE FROM ratings r
            WHERE r.source_id = %s
              AND NOT EXISTS (SELECT 1 FROM _resolved s
                              WHERE s.patron_ref = r.patron_ref
                                AND s.work_id = r.work_id)
            """, (source_id,))
        removed = cur.rowcount

        orphans = cur.execute("""
            SELECT count(*) FROM (SELECT DISTINCT patron_ref, biblionumber
                                  FROM _reviews_in) v
            WHERE NOT EXISTS (SELECT 1 FROM _ratings_in r
                              WHERE r.patron_ref = v.patron_ref
                                AND r.biblionumber = v.biblionumber)
            """).fetchone()[0]

        # Recorded with the ratings, in the same transaction: a watermark
        # that moved without them would skip what they missed.
        cur.execute("UPDATE sources SET ratings_full_read_at = now(), "
                    "ratings_watermark = coalesce(%s, ratings_watermark) "
                    "WHERE id = %s", (watermark, source_id))
    conn.commit()

    return {
        "read": n_ratings, "reviews_read": n_reviews,
        "stored": stored, "unchanged": resolved - stored, "removed": removed,
        "unresolved": n_ratings - invalid - resolved, "invalid": invalid,
        "reviews_without_rating": orphans,
    }


def store_changes(conn, source_id: int, ratings: Iterable[tuple],
                  flagged: Iterable[tuple] = (), *,
                  watermark: datetime | None = None) -> dict:
    """Apply what changed since the last read; remove nothing.

    ``ratings`` as read_koha_changes returns them, each with its review
    flag; ``flagged`` the (patron_ref, biblionumber) of reviews written
    since, whose stored ratings gain the flag. Returns counts: ``read``,
    ``reviews_read``, ``stored``, ``unchanged``, ``flagged``,
    ``unresolved`` and ``invalid``. An empty set of changes is the usual
    case between reads, and is not an error.
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE _changed_in (
                patron_ref   varchar(64) NOT NULL,
                biblionumber text        NOT NULL,
                rating       integer     NOT NULL,
                rated_at     timestamptz,
                reviewed     boolean     NOT NULL
            ) ON COMMIT DROP""")
        cur.execute("""
            CREATE TEMP TABLE _flagged_in (
                patron_ref   varchar(64) NOT NULL,
                biblionumber text        NOT NULL
            ) ON COMMIT DROP""")
        n_ratings = _copy(cur, "_changed_in", ratings)
        n_flagged = _copy(cur, "_flagged_in", flagged)

        cur.execute(f"""
            CREATE TEMP TABLE _changes ON COMMIT DROP AS
            SELECT DISTINCT ON (c.patron_ref, w.id)
                   c.patron_ref, w.id AS work_id, c.rating::smallint AS rating,
                   coalesce(c.rated_at, now()) AS rated_at, c.reviewed
            FROM _changed_in c
            JOIN {_WORKS_BY_BIB} w ON w.bib = c.biblionumber
            WHERE c.rating BETWEEN 1 AND 5
            """, (source_id,))
        resolved = cur.rowcount
        invalid = cur.execute(
            "SELECT count(*) FROM _changed_in WHERE rating NOT BETWEEN 1 AND 5"
        ).fetchone()[0]

        cur.execute(_UPSERT.format(table="_changes"), (source_id,))
        stored = cur.rowcount

        cur.execute(f"""
            UPDATE ratings r SET reviewed = true
            FROM _flagged_in f
            JOIN {_WORKS_BY_BIB} w ON w.bib = f.biblionumber
            WHERE r.source_id = %s AND r.patron_ref = f.patron_ref
              AND r.work_id = w.id AND NOT r.reviewed
            """, (source_id, source_id))
        flagged_now = cur.rowcount

        if watermark is not None:
            cur.execute("UPDATE sources SET ratings_watermark = %s WHERE id = %s",
                        (watermark, source_id))
    conn.commit()

    return {
        "read": n_ratings, "reviews_read": n_flagged,
        "stored": stored, "unchanged": resolved - stored, "flagged": flagged_now,
        "unresolved": n_ratings - invalid - resolved, "invalid": invalid,
    }


def main() -> int:
    import argparse
    import sys

    import psycopg

    from bookrs.db.migrations import apply_migrations

    ap = argparse.ArgumentParser(
        prog="bookrs.ingestion.direct",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument(
        "--url", default=os.environ.get("KOHA_DB_URL", ""),
        help="Read-only connection to the library's database. "
             "Default: KOHA_DB_URL.",
    )
    ap.add_argument("--allow-empty", action="store_true",
                    help="Accept a read that returns no ratings and remove "
                         "the stored ones. Refused by default.")
    ap.add_argument("--changes", action="store_true",
                    help="Read only what changed since the last read. The "
                         "first read, with none recorded, is a full one.")
    ap.add_argument("--full-every", default="off",
                    help="With --changes: read everything anyway when the "
                         "last full read is older than this (30m, 6h, 1d, "
                         "2w), to catch removed ratings and reviews. "
                         "Default: off.")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    if not a.url:
        ap.error("KOHA_DB_URL must be set, or --url given")
    from bookrs.schedule import parse_every
    try:
        full_every = parse_every(a.full_every)
    except ValueError as exc:
        ap.error(f"--full-every: {exc}")
    secret = os.environ.get("BOOKRS_PATRON_SECRET", "")
    if not secret:
        ap.error("BOOKRS_PATRON_SECRET must be set: without the same secret "
                 "the circulation read uses, a patron's ratings and loans "
                 "would hash to two different references")

    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        print("error: pymysql is not installed in this image", file=sys.stderr)
        return 2

    from urllib.parse import unquote, urlparse

    u = urlparse(a.url)
    # SSCursor leaves the result on the library's server and reads it as
    # it is consumed. The default cursor would pull all of it into this
    # process before returning the first row.
    koha = pymysql.connect(
        host=u.hostname, port=u.port or 3306,
        user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=u.path.lstrip("/"),
        read_default_group=None,
        cursorclass=pymysql.cursors.SSCursor,
    )
    pseudonym = _Pseudonymiser(secret)
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            # This module needs ratings.reviewed (migration 3) and may be
            # the first thing run after an upgrade.
            apply_migrations(conn)
            conn.commit()
            watermark, full_read_at = read_state(conn, a.source_id)
            mode = choose_read(a.changes, watermark, full_read_at, full_every,
                               datetime.now(timezone.utc))
            # Before reading anything: what changes during this read is
            # what the next read of changes starts from.
            started = koha_clock(koha)
            if mode == "changes":
                since = watermark - CHANGES_MARGIN
                try:
                    ratings, flagged = read_koha_changes(koha, pseudonym, since)
                except TooManyChanges as exc:
                    mode = f"full: {exc} since the last read"
                else:
                    stats = store_changes(conn, a.source_id, ratings, flagged,
                                          watermark=started)
            if mode != "changes":
                if a.changes:
                    log.info("reading everything (%s)", mode.split(": ", 1)[1])
                try:
                    stats = store(conn, a.source_id,
                                  read_koha_ratings(koha, pseudonym),
                                  read_koha_reviews(koha, pseudonym),
                                  allow_empty=a.allow_empty, watermark=started)
                except EmptyRead as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 1
    finally:
        koha.close()

    if mode == "changes":
        log.info("changes since %s (Koha's clock): %d ratings given or "
                 "changed, %d reviews written", since.strftime("%Y-%m-%d %H:%M:%S"),
                 stats["read"], stats["reviews_read"])
        log.info("%d stored, %d unchanged, %d ratings newly flagged as reviewed",
                 stats["stored"], stats["unchanged"], stats["flagged"])
    else:
        log.info("read %d ratings and %d reviews from %d patrons",
                 stats["read"], stats["reviews_read"], len(pseudonym))
        log.info("%d stored, %d unchanged, %d removed",
                 stats["stored"], stats["unchanged"], stats["removed"])
    if stats["unresolved"]:
        log.warning("%d ratings are for records this service does not hold: "
                    "read the catalogue first (bookrs.ingestion.koha_db)",
                    stats["unresolved"])
    if stats["invalid"]:
        log.warning("%d ratings were outside 1-5 and were skipped",
                    stats["invalid"])
    if stats.get("reviews_without_rating"):
        log.info("%d reviews have no rating beside them and are not stored: "
                 "the flag qualifies a rating, it is not an interaction of "
                 "its own", stats["reviews_without_rating"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
