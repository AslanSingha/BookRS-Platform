"""Read a Koha catalogue from its own database, read-only.

The harvest path (``bookrs/ingestion/``) reads cataloguing over OAI-PMH
and parses MARC. This reads the same catalogue from Koha's tables as
columns. Both produce rows in ``works`` and ``items``; everything
downstream — embedding, factorisation, ranking, the widget — does not
know or care which produced them.

**Every statement here is a SELECT.** Nothing in this module writes to
the library's database, and the account it connects with should be
read-only. The code is not what stands between this service and a
library's data; the grant is. A deployment should create a user with
SELECT on the five tables named below and nothing else.

What this path gains:

* **No MARC parsing.** ``biblio`` and ``biblioitems`` carry title,
  subtitle, author, abstract, ISBN, publication year and publisher as
  columns. Flavour detection, field maps and alternate-script
  resolution are not needed, because there is no record to interpret.
* **Holdings are current, not harvested.** ``items.onloan`` is the live
  circulation state rather than a MARC 952 subfield written at export
  time.
* **Incremental sync filters in SQL.** ``biblio.timestamp`` updates on
  modification, so a sync can ask for what changed rather than fetching
  everything and comparing hashes. The hash is still computed and
  stored, because a timestamp that moves without the bibliography
  changing is a known Koha behaviour (a checkout touches it).

What this path loses, and it is not small:

* **Subject headings.** Koha stores them in the MARC record and indexes
  them for search; there is no subjects column and no subjects table.
  They exist only inside ``biblio_metadata.metadata`` as MARCXML.
  Reading them would mean parsing MARC from a different source, which
  is what this path exists to avoid, so ``works.subjects`` is left
  empty here. The content layer runs on title, author and abstract.
* **Portability.** A database schema carries no compatibility promise
  across versions, where OAI-PMH does. This module is written against
  Koha 24.11 and should be expected to need attention on an upgrade.
* **Deployment adjacency.** OAI-PMH is HTTP and reachable over any route
  a library permits. This needs network access to the database itself,
  which a library does not expose — in development the harvester has to
  join Koha's own container network to reach it.

    docker compose run --rm ingestion python -m bookrs.ingestion.koha_db \
        --source-id 1 --url 'mysql://ro:pw@kohadev-db-1:3306/koha_kohadev'
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("bookrs.ingestion.koha_db")


@dataclass
class DbWork:
    biblionumber: str
    title: str
    authors: list[str]
    summary: str
    contents: str
    isbns: list[str]
    publisher: str
    publication_year: int | None
    modified: object
    items: list[dict] = field(default_factory=list)

    def content_hash(self) -> str:
        """Over the bibliographic fields only, excluding holdings.

        Same split as the harvest path: a change to a record's
        description should re-embed it, a change to where a copy sits
        on a shelf should not.
        """
        parts = [self.title, "|".join(self.authors), self.summary,
                 self.contents, "|".join(self.isbns), self.publisher,
                 str(self.publication_year or "")]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def items_hash(self) -> str:
        parts = [f"{i['barcode']}:{i['holding_branch']}:{i['call_number']}:"
                 f"{i['item_type']}:{i['due_date']}:{i.get('collection') or ''}"
                 for i in self.items]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# Five tables, named columns, no SELECT *. A reader that selects * is a
# reader that breaks when a column is added.
#
# LEFT JOIN on biblioitems because a biblio without one is malformed but
# does exist in the wild, and dropping it silently would be the wrong
# failure. opac_suppressed is respected: a record the library has chosen
# to hide from its own catalogue should not be recommended in it.
#
# Records are read a page at a time, by record number. A catalogue of
# 883,000 records read in one statement is a gigabyte in this process
# before the first one is stored; a page of BATCH is a few megabytes,
# and each page is stored and committed before the next is asked for.
_WORK_COLUMNS = """
    SELECT b.biblionumber,
           b.title, b.subtitle, b.author,
           b.abstract, b.notes,
           b.copyrightdate, b.timestamp,
           bi.isbn, bi.publicationyear, bi.publishercode
    FROM biblio b
    LEFT JOIN biblioitems bi ON bi.biblionumber = b.biblionumber
    WHERE b.opac_suppressed = 0
"""
_WORKS_PAGE = _WORK_COLUMNS + """
      AND b.biblionumber > %s {since}
    ORDER BY b.biblionumber
    LIMIT %s
"""
_WORKS_IN = _WORK_COLUMNS + """
      AND b.biblionumber IN ({ids})
    ORDER BY b.biblionumber
"""

# ccode is the collection code: where a library records genre on the
# shelf ("Mystery", "Young adult"). The UCSD fixture writes the thesis's
# genres there (tools/ucsd_to_marc21.py, 952$8).
_ITEM_COLUMNS = """
    SELECT i.biblionumber, i.barcode, i.homebranch, i.holdingbranch,
           i.location, i.itemcallnumber, i.itype, i.onloan,
           i.issues, i.notforloan, i.itemlost, i.withdrawn, i.ccode
    FROM items i
    JOIN biblio b ON b.biblionumber = i.biblionumber
    WHERE b.opac_suppressed = 0
"""
_ITEMS_RANGE = _ITEM_COLUMNS + """
      AND i.biblionumber BETWEEN %s AND %s
    ORDER BY i.biblionumber
"""
_ITEMS_IN = _ITEM_COLUMNS + """
      AND i.biblionumber IN ({ids})
    ORDER BY i.biblionumber
"""

# What changed since: Koha stamps all three on every change (ON UPDATE
# current_timestamp). A copy checked out or moved changes items only, a
# corrected ISBN biblioitems only -- so all three are asked.
_CHANGED = (
    "SELECT biblionumber FROM biblio WHERE timestamp >= %s",
    "SELECT biblionumber FROM biblioitems WHERE timestamp >= %s",
    "SELECT DISTINCT biblionumber FROM items WHERE timestamp >= %s",
)
_KOHA_NOW = "SELECT NOW()"

BATCH = 5_000


def _split_authors(author: str | None) -> list[str]:
    """Koha's biblio.author is one free-text string.

    MARC separates 100 from 700, and the harvest path keeps that
    distinction. Here there is one column, so the best available
    reading is to treat it as a single name rather than guess at
    delimiters -- a comma in "Heylin, Clinton" is part of the name, not
    a separator, and splitting on it would produce two authors from one
    person.
    """
    a = (author or "").strip()
    return [a] if a else []


def _isbns(isbn: str | None) -> list[str]:
    """biblioitems.isbn can hold several, separated by | or spaces."""
    raw = (isbn or "").replace("|", " ").split()
    out = []
    for v in raw:
        v = "".join(c for c in v if c.isalnum())
        if 10 <= len(v) <= 13:
            out.append(v[:13])
    return out


def _to_works(rows) -> dict[str, DbWork]:
    works: dict[str, DbWork] = {}
    for r in rows:
        bib = str(r[0])
        title = (r[1] or "").strip()
        subtitle = (r[2] or "").strip()
        if subtitle and subtitle.lower() not in title.lower():
            title = f"{title} : {subtitle}" if title else subtitle
        year = r[9] or r[6]
        try:
            year_i = int(str(year)[:4]) if year else None
        except (TypeError, ValueError):
            year_i = None
        works[bib] = DbWork(
            biblionumber=bib,
            title=title,
            authors=_split_authors(r[3]),
            summary=(r[4] or "").strip(),
            contents=(r[5] or "").strip(),
            isbns=_isbns(r[8]),
            publisher=(r[10] or "").strip()[:255],
            publication_year=year_i,
            modified=r[7],
        )
    return works


def _attach_items(works: dict[str, DbWork], rows) -> None:
    for r in rows:
        w = works.get(str(r[0]))
        if w is None:
            continue
        # notforloan, itemlost and withdrawn are carried so the
        # availability count can exclude copies that exist but
        # cannot be borrowed -- a distinction MARC 952 export does
        # not always preserve.
        w.items.append({
            "barcode": r[1], "home_branch": r[2], "holding_branch": r[3],
            "location": r[4], "call_number": r[5], "item_type": r[6],
            "due_date": r[7], "issues": r[8] or 0,
            "lendable": not (r[9] or r[10] or r[11]),
            "collection": (r[12] or "").strip()[:80] or None,
        })


def _fetch(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params or None)
        return cur.fetchall()


def read_pages(conn, since=None, batch: int = BATCH):
    """Every record (or those stamped after ``since``), a page at a time."""
    last = -1
    clause = "AND b.timestamp > %s" if since else ""
    while True:
        params = (last,) + ((since,) if since else ()) + (batch,)
        rows = _fetch(conn, _WORKS_PAGE.format(since=clause), params)
        if not rows:
            return
        works = _to_works(rows)
        first, last = int(rows[0][0]), int(rows[-1][0])
        _attach_items(works, _fetch(conn, _ITEMS_RANGE, (first, last)))
        yield list(works.values())
        if len(rows) < batch:
            return


def changed_records(conn, since) -> list[int]:
    """Record numbers whose record, publication details or copies were
    stamped at or after ``since``, by Koha's clock."""
    found: set[int] = set()
    for sql in _CHANGED:
        found.update(int(r[0]) for r in _fetch(conn, sql, (since,)) if r[0] is not None)
    return sorted(found)


def read_records(conn, ids: list[int], batch: int = BATCH):
    """Those records, a page at a time. A record no longer in Koha, or
    hidden from its OPAC, is simply not returned: the daily full read is
    what notices it is gone."""
    for at in range(0, len(ids), batch):
        chunk = [int(i) for i in ids[at:at + batch]]
        marks = ", ".join(["%s"] * len(chunk))
        works = _to_works(_fetch(conn, _WORKS_IN.format(ids=marks), tuple(chunk)))
        if works:
            _attach_items(works, _fetch(conn, _ITEMS_IN.format(ids=marks), tuple(chunk)))
        yield list(works.values())


def read_works(conn, since=None) -> list[DbWork]:
    """The whole catalogue (or what changed after ``since``) as one list."""
    return [w for page in read_pages(conn, since) for w in page]


def koha_clock(conn):
    """Koha's NOW(), in the clock that stamps its records."""
    return _fetch(conn, _KOHA_NOW)[0][0]


def held(conn, source_id: int) -> dict[str, tuple]:
    """What is already stored for this source: record -> (id, hashes)."""
    rows = conn.execute(
        "SELECT source_record_id, id, content_hash, items_hash "
        "FROM works WHERE source_id = %s", (source_id,)).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def new_stats() -> dict:
    return {"new": 0, "updated": 0, "unchanged": 0, "items": 0, "removed": 0,
            "had_items": False}


def store_page(conn, source_id: int, works: list[DbWork], seen: dict,
               stats: dict) -> None:
    """Upsert one page into works and items, keyed on biblionumber, and
    commit it. ``seen`` (from ``held``) is kept current as records are
    added, so a later page never mistakes one for new.

    source_record_id is the bare biblionumber here, not an OAI
    identifier. A catalogue read this way has no archive prefix,
    because nothing issued one -- and /works/by-record-id already
    matches a bare biblionumber against an identifier's suffix, so the
    widget needs no change.
    """
    unchanged: list[int] = []
    stats["had_items"] = stats["had_items"] or any(w.items for w in works)
    with conn.cursor() as cur:
        for w in works:
            ch, ih = w.content_hash(), w.items_hash()
            existing = seen.get(w.biblionumber)
            if existing and existing[1] == ch and existing[2] == ih:
                stats["unchanged"] += 1
                unchanged.append(existing[0])
                continue

            cur.execute(
                """
                INSERT INTO works (source_id, source_record_id, title, authors,
                                   summary, contents, isbns, publisher,
                                   publication_year, content_hash, items_hash,
                                   provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s::varchar(13)[], %s, %s, %s, %s,
                        %s::jsonb)
                ON CONFLICT (source_id, source_record_id) DO UPDATE SET
                    title = EXCLUDED.title, authors = EXCLUDED.authors,
                    summary = EXCLUDED.summary, contents = EXCLUDED.contents,
                    isbns = EXCLUDED.isbns, publisher = EXCLUDED.publisher,
                    publication_year = EXCLUDED.publication_year,
                    content_hash = EXCLUDED.content_hash,
                    items_hash = EXCLUDED.items_hash,
                    provenance = EXCLUDED.provenance,
                    last_seen = now(), deleted_at = NULL
                RETURNING id
                """,
                (source_id, w.biblionumber, w.title, w.authors, w.summary,
                 w.contents, w.isbns, w.publisher, w.publication_year, ch, ih,
                 '{"channel": "koha-db", "subjects": "not available: Koha '
                 'stores them in MARC only"}'),
            )
            work_id = cur.fetchone()[0]
            stats["new" if existing is None else "updated"] += 1
            seen[w.biblionumber] = (work_id, ch, ih)

            cur.execute("DELETE FROM items WHERE work_id = %s", (work_id,))
            if w.items:
                cur.executemany(
                    """
                    INSERT INTO items (work_id, barcode, owning_branch,
                                       holding_branch, location, call_number,
                                       item_type, due_date, issue_count, collection)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [(work_id, it["barcode"], it["home_branch"], it["holding_branch"],
                      it["location"], it["call_number"], it["item_type"],
                      it["due_date"], it["issues"], it.get("collection"))
                     for it in w.items])
                stats["items"] += len(w.items)

        # One statement for the page, not one per record: on a full read
        # of a large catalogue nearly every record is unchanged.
        if unchanged:
            cur.execute("UPDATE works SET last_seen = now() WHERE id = ANY(%s)",
                        (unchanged,))
    conn.commit()


def finish(conn, source_id: int, stats: dict, *, watermark=None, full: bool = False,
           full_started=None) -> None:
    """Record the read on the source, and after a complete full read mark
    what Koha no longer holds as deleted.

    Only a full read can see a removal: a record deleted in Koha (or hidden
    from its OPAC) is simply not returned, so its row was not touched by
    this read. ``full_started`` is this service's clock when the read
    began; every record the read saw has ``last_seen`` after it.
    """
    with conn.cursor() as cur:
        if full and full_started is not None:
            saw = stats["new"] + stats["updated"] + stats["unchanged"]
            if saw == 0:
                raise EmptyCatalogue(
                    f"the library returned no records for source {source_id}; "
                    "nothing was marked deleted")
            cur.execute(
                "UPDATE works SET deleted_at = now() WHERE source_id = %s "
                "AND deleted_at IS NULL AND last_seen < %s",
                (source_id, full_started))
            stats["removed"] = cur.rowcount
        # Not a harvest, but the same question: when was this source
        # last read? A source that always reports "never" looks broken
        # to anyone checking freshness, and a stalled read is exactly
        # what freshness reporting exists to catch.
        cur.execute(
            "UPDATE sources SET last_harvest = now(), "
            # A full read says whether the catalogue has copies; a read of
            # changes that touched none says nothing about the rest.
            "last_had_items = CASE WHEN %s THEN %s "
            "ELSE coalesce(last_had_items, false) OR %s END, "
            "catalogue_watermark = coalesce(%s, catalogue_watermark), "
            "catalogue_full_read_at = CASE WHEN %s THEN now() "
            "ELSE catalogue_full_read_at END WHERE id = %s",
            (full, stats["had_items"], stats["had_items"], watermark, full, source_id))
    conn.commit()


class EmptyCatalogue(RuntimeError):
    """A full read returned nothing for a source that holds records."""


def store(conn, source_id: int, works: list[DbWork]) -> dict:
    """Upsert a list of records, and record the read. No removals: the
    list need not be the whole catalogue."""
    stats = new_stats()
    seen = held(conn, source_id)
    for at in range(0, len(works), BATCH):
        store_page(conn, source_id, works[at:at + BATCH], seen, stats)
    finish(conn, source_id, stats)
    return stats


def catalogue_state(conn, source_id: int) -> tuple:
    """(catalogue_watermark, catalogue_full_read_at) recorded for a source."""
    row = conn.execute(
        "SELECT catalogue_watermark, catalogue_full_read_at FROM sources WHERE id = %s",
        (source_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


# --------------------------------------------------------------------
# Circulation
#
# The REST path (bookrs/ingestion/circulation.py) reads the same data
# over /api/v1/checkouts. This reads it from the tables. The columns
# carry the same names, because Koha's API mirrors its schema, so the
# two produce identical rows in `loans`.
#
# Two tables, not one: Koha moves a loan from `issues` to `old_issues`
# on return rather than updating it in place. issue_id is not reused
# across the move, so source_loan_id stays unique without a prefix.
#
# The join runs issues -> items -> biblio because a loan is against a
# physical copy, not a bibliographic record. That is the same reason
# the thesis's user-item matrix had to be built over works rather than
# over items: two patrons borrowing two copies of one book have read
# the same book, and a matrix keyed on copies would not know it.
# --------------------------------------------------------------------

_LOANS = """
    SELECT {src}.issue_id, {src}.borrowernumber, i.biblionumber,
           {src}.issuedate, {src}.returndate, {src}.renewals_count
    FROM {src}
    JOIN items i ON i.itemnumber = {src}.itemnumber
    JOIN biblio b ON b.biblionumber = i.biblionumber
    WHERE b.opac_suppressed = 0
"""


@dataclass(frozen=True)
class DbLoan:
    source_loan_id: int
    patron_ref: str
    biblionumber: str
    checked_out_at: object
    checked_in_at: object
    renewals: int


def read_loans(conn, secret: str, include_history: bool = True) -> list[DbLoan]:
    """Current loans, and returned ones unless asked not to.

    `include_history=False` exists for the same reason the REST path
    has `--no-history`: a library may keep current circulation and
    purge the rest, and a service that demanded the history would be
    unusable there. Current loans alone are a thin signal, but they are
    a signal.
    """
    from bookrs.ingestion.circulation import patron_reference

    out: list[DbLoan] = []
    tables = ["issues"] + (["old_issues"] if include_history else [])
    for t in tables:
        with conn.cursor() as cur:
            cur.execute(_LOANS.format(src=t))
            for r in cur.fetchall():
                out.append(DbLoan(
                    source_loan_id=int(r[0]),
                    patron_ref=patron_reference(r[1], secret),
                    biblionumber=str(r[2]),
                    checked_out_at=r[3],
                    checked_in_at=r[4],
                    renewals=int(r[5] or 0),
                ))
    return out


def store_loans(conn, source_id: int, loans: list[DbLoan]) -> dict:
    """Upsert loans, resolving biblionumber to this service's work id.

    A loan against a record this source has not read is counted and
    dropped. On this path that should not happen -- both reads come
    from the same database in the same run -- and it is counted anyway,
    because an unresolved count that is always zero is cheap and a
    silent drop is not.
    """
    stats = {"inserted": 0, "updated": 0, "unresolved": 0,
             "current": 0, "historical": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT source_record_id, id FROM works "
                    "WHERE source_id = %s AND deleted_at IS NULL", (source_id,))
        by_bib = {r[0]: r[1] for r in cur.fetchall()}

        for l in loans:
            work_id = by_bib.get(l.biblionumber)
            if work_id is None:
                stats["unresolved"] += 1
                continue
            cur.execute(
                """
                INSERT INTO loans (source_id, work_id, patron_ref,
                                   source_loan_id, checked_out_at,
                                   checked_in_at, renewals)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, source_loan_id) DO UPDATE SET
                    checked_in_at = EXCLUDED.checked_in_at,
                    renewals = EXCLUDED.renewals,
                    last_seen = now()
                RETURNING (xmax = 0) AS inserted
                """,
                (source_id, work_id, l.patron_ref, l.source_loan_id,
                 l.checked_out_at, l.checked_in_at, l.renewals),
            )
            stats["inserted" if cur.fetchone()[0] else "updated"] += 1
            stats["historical" if l.checked_in_at else "current"] += 1
    conn.commit()
    return stats


def main() -> int:
    import argparse
    import sys
    from datetime import datetime, timezone
    from urllib.parse import unquote, urlparse

    import psycopg

    from bookrs.db.migrations import apply_migrations
    from bookrs.ingestion.direct import CHANGES_MARGIN, choose_read

    ap = argparse.ArgumentParser(
        prog="bookrs.ingestion.koha_db",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--url", default=os.environ.get("KOHA_DB_URL", ""),
                    help="Read-only connection. Default: KOHA_DB_URL.")
    ap.add_argument("--changes", action="store_true",
                    help="Read only the records changed since the last read "
                         "(a full read when none is recorded).")
    ap.add_argument("--full-every", default="off",
                    help="With --changes: read everything when the last full "
                         "read is older than this (30m, 6h, 1d, 2w, off). A full "
                         "read is the only one that sees a removed record.")
    ap.add_argument("--since", default=None,
                    help="Only records modified after this timestamp "
                         "(YYYY-MM-DD HH:MM:SS). Filters in SQL; records no "
                         "watermark.")
    ap.add_argument("--loans", action="store_true",
                    help="Read circulation as well as cataloguing.")
    ap.add_argument("--no-history", action="store_true",
                    help="With --loans: current checkouts only.")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if not a.url:
        ap.error("KOHA_DB_URL must be set, or --url given")
    from bookrs.schedule import parse_every
    try:
        full_every = parse_every(a.full_every)
    except ValueError as exc:
        ap.error(f"--full-every: {exc}")

    try:
        import pymysql
    except ImportError:
        print("error: pymysql is not installed in this image", file=sys.stderr)
        return 2

    u = urlparse(a.url)
    koha = pymysql.connect(host=u.hostname, port=u.port or 3306,
                           user=unquote(u.username or ""),
                           password=unquote(u.password or ""),
                           database=u.path.lstrip("/"))
    secret = os.environ.get("BOOKRS_PATRON_SECRET", "")
    if a.loans and not secret:
        ap.error("BOOKRS_PATRON_SECRET must be set to read circulation")

    stats, read, mode, since = new_stats(), 0, "full: asked for", None
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            # The watermark columns are migration 8; this may be the first
            # thing run after an upgrade.
            apply_migrations(conn)
            conn.commit()
            if a.since:
                mode = "since"
                pages = read_pages(koha, a.since)
            else:
                watermark, full_at = catalogue_state(conn, a.source_id)
                mode = choose_read(a.changes, watermark, full_at, full_every,
                                   datetime.now(timezone.utc))
                # Before reading anything: what changes during this read is
                # what the next read of changes starts from.
                started = koha_clock(koha)
                if mode == "changes":
                    since = watermark - CHANGES_MARGIN
                    pages = read_records(koha, changed_records(koha, since))
                else:
                    if a.changes:
                        log.info("reading the whole catalogue (%s)", mode.split(": ", 1)[1])
                    full_started = conn.execute("SELECT now()").fetchone()[0]
                    pages = read_pages(koha)

            seen = held(conn, a.source_id)
            for page in pages:
                store_page(conn, a.source_id, page, seen, stats)
                read += len(page)
                if read % (BATCH * 20) == 0:
                    log.info("  %d records so far", read)

            if mode == "since":
                finish(conn, a.source_id, stats)
            else:
                full = mode != "changes"
                try:
                    finish(conn, a.source_id, stats, watermark=started, full=full,
                           full_started=full_started if full else None)
                except EmptyCatalogue as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 1
            loans = read_loans(koha, secret, not a.no_history) if a.loans else []
            lstats = store_loans(conn, a.source_id, loans) if a.loans else None
    finally:
        koha.close()

    if mode == "changes":
        log.info("records changed since %s (Koha's clock): %d",
                 since.strftime("%Y-%m-%d %H:%M:%S"), read)
    else:
        log.info("read %d records from the catalogue database", read)
    log.info("%d new, %d updated, %d unchanged; %d item rows; %d no longer in "
             "the catalogue", stats["new"], stats["updated"], stats["unchanged"],
             stats["items"], stats["removed"])
    if lstats:
        log.info("%d loans (%d current, %d returned); %d inserted, %d updated, "
                 "%d unresolved", len(loans), lstats["current"],
                 lstats["historical"], lstats["inserted"], lstats["updated"],
                 lstats["unresolved"])
    if mode != "changes":
        log.info("subjects are empty on this path: Koha keeps them in the MARC "
                 "record only, and reading them would mean parsing MARC from the "
                 "database instead of from OAI-PMH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
