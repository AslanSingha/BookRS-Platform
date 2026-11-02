"""Read, read-only, what an ILS stores but does not publish.

Everything else this service ingests arrives over a published interface:
cataloguing through OAI-PMH, circulation through Koha's REST API. Those
are stable across versions and a library enables them by setting a
preference. A database schema is neither, which is why this is a second
channel rather than a replacement.

It exists because some data has no interface at all. Koha collects
patron star ratings, enables the feature by default, and offers no REST
route that returns them (docs/marc-field-analysis.md). The ``ratings``
table in this service's own schema has sat unused since it was written,
for want of a way to fill it.

What this does NOT do:

* **Write.** Every statement here is a SELECT. The account a library
  grants should be read-only and scoped to the tables named below; the
  code is not the thing standing between this service and a library's
  data, the grant is.
* **Replace the harvest.** Bibliographic records still come over
  OAI-PMH. Reading ``biblio`` directly would skip flavour detection,
  field mapping and alternate-script resolution -- the parts of this
  project that discovered a UNIMARC Koha serving records labelled
  marc21 -- and would break on the next schema change.
* **Change the privacy position.** Ratings carry a borrower number
  exactly as loans do, and go through the same keyed HMAC. A patron who
  borrows a book and rates it produces one ``patron_ref``, not two.

Configured per source. A source with no direct connection behaves
exactly as before and simply has no ratings.

    docker compose run --rm ingestion python -m bookrs.ingestion.direct \
        --source-id 1 --url 'mysql://ro_user:pw@host:3306/koha_kohadev'
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from bookrs.ingestion.circulation import patron_reference

log = logging.getLogger("bookrs.ingestion.direct")


@dataclass(frozen=True)
class Rating:
    patron_ref: str
    biblionumber: str
    value: int
    rated_at: object


# One statement, named columns, no joins. A reader that selects * is a
# reader that breaks when a column is added; a reader that joins is one
# that depends on more of the schema than it needs.
_KOHA_RATINGS = """
    SELECT borrowernumber, biblionumber, rating_value, timestamp
    FROM ratings
    ORDER BY biblionumber
"""


def read_koha_ratings(conn, secret: str) -> list[Rating]:
    """Every rating in a Koha instance, with borrowers pseudonymised.

    Koha's ``ratings`` table is keyed on (borrowernumber, biblionumber),
    so a patron has at most one rating per record and there is nothing
    to deduplicate. A re-rating overwrites in place and arrives here as
    the current value with a new timestamp.
    """
    with conn.cursor() as cur:
        cur.execute(_KOHA_RATINGS)
        rows = cur.fetchall()
    return [
        Rating(
            patron_ref=patron_reference(r[0], secret),
            biblionumber=str(r[1]),
            value=int(r[2]),
            rated_at=r[3],
        )
        for r in rows
    ]


def store(conn, source_id: int, ratings: list[Rating]) -> tuple[int, int]:
    """Insert ratings, resolving biblionumber to this service's work id.

    Returns (stored, unresolved). A rating for a record the harvest has
    not seen is counted and dropped rather than inserted against a
    guess: the two systems are synchronised by harvest, not by this
    channel, and a rating arriving first simply waits for the record.
    """
    rows = conn.execute(
        "SELECT source_record_id, id FROM works WHERE source_id = %s",
        (source_id,),
    ).fetchall()
    # source_record_id is the full OAI identifier; the biblionumber is
    # its last colon-separated segment.
    by_bib = {rid.rpartition(":")[2]: wid for rid, wid in rows}

    stored = unresolved = 0
    for r in ratings:
        work_id = by_bib.get(r.biblionumber)
        if work_id is None:
            unresolved += 1
            continue
        conn.execute(
            """
            INSERT INTO ratings (source_id, patron_ref, work_id, rating, rated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_id, patron_ref, work_id) DO UPDATE
                SET rating = EXCLUDED.rating, rated_at = EXCLUDED.rated_at
            """,
            (source_id, r.patron_ref, work_id, r.value, r.rated_at),
        )
        stored += 1
    conn.commit()
    return stored, unresolved


def main() -> int:
    import argparse
    import sys

    import psycopg

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
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    if not a.url:
        ap.error("KOHA_DB_URL must be set, or --url given")
    secret = os.environ.get("BOOKRS_PATRON_SECRET", "")
    if not secret:
        ap.error("BOOKRS_PATRON_SECRET must be set: without the same secret "
                 "the circulation harvest uses, a patron's ratings and loans "
                 "would hash to two different references")

    try:
        import pymysql
    except ImportError:
        print("error: pymysql is not installed in this image", file=sys.stderr)
        return 2

    from urllib.parse import urlparse

    u = urlparse(a.url)
    koha = pymysql.connect(
        host=u.hostname, port=u.port or 3306,
        user=u.username, password=u.password,
        database=u.path.lstrip("/"),
        read_default_group=None,
    )
    try:
        ratings = read_koha_ratings(koha, secret)
    finally:
        koha.close()
    log.info("read %d ratings from the library's database", len(ratings))

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        stored, unresolved = store(conn, a.source_id, ratings)

    log.info("stored %d ratings; %d for records not yet harvested",
             stored, unresolved)
    if stored:
        log.info("these are not yet used in ranking: the confidence model "
                 "weights borrowing intensity, and changing it needs more "
                 "than this channel has so far produced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
