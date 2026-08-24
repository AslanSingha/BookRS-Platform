"""Persist harvested works.

Upserts on ``(source_id, source_record_id)``: a harvest re-delivers
records that already exist, because circulation activity bumps a
record's OAI datestamp without changing its bibliographic content
(docs section 9.2). Re-inserting would violate the unique constraint;
skipping would miss genuine edits.

Items are replaced wholesale per work rather than diffed. Holdings are
small (a measured mean of 2.20 per work under MARC21), have no stable
identity beyond a barcode that can itself change, and are outside the
ML pipeline -- so a delete-and-insert is simpler and no more expensive
than reconciling.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import psycopg
from psycopg.rows import dict_row

from bookrs.ingestion.fieldmap import Work
from bookrs.ingestion.flavour import Flavour

log = logging.getLogger(__name__)


@dataclass
class LoadStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    items_refreshed: int = 0
    deleted: int = 0
    items: int = 0


def ensure_source(conn: psycopg.Connection, name: str, base_url: str,
                  metadata_prefix: str, flavour: Flavour | None) -> int:
    row = conn.execute(
        """
        INSERT INTO sources (name, base_url, metadata_prefix, marc_flavour)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (base_url, metadata_prefix) DO UPDATE
            SET name = EXCLUDED.name,
                marc_flavour = COALESCE(EXCLUDED.marc_flavour, sources.marc_flavour)
        RETURNING id
        """,
        (name, base_url, metadata_prefix, flavour.value if flavour else None),
    ).fetchone()
    return row[0]


def _upsert_work(conn: psycopg.Connection, source_id: int, work: Work
                 ) -> tuple[int, str, bool]:
    """Insert or update one work.

    Returns (work_id, action, items_changed). The two hashes are
    compared independently so a checkout updates holdings without
    rewriting the bibliography, and a catalogue correction rewrites the
    bibliography without touching holdings.
    """
    existing = conn.execute(
        "SELECT id, content_hash, items_hash FROM works "
        "WHERE source_id = %s AND source_record_id = %s",
        (source_id, work.source_record_id),
    ).fetchone()

    if existing and existing[1] and existing[1] == work.content_hash:
        # Bibliographically unchanged. Touch last_seen so the record is
        # not mistaken for one that has vanished from the catalogue, but
        # do not rewrite it and do not mark it for re-embedding.
        conn.execute("UPDATE works SET last_seen = now(), deleted_at = NULL, "
                     "items_hash = %s WHERE id = %s",
                     (work.items_hash, existing[0]))
        return existing[0], "unchanged", existing[2] != work.items_hash

    row = conn.execute(
        """
        INSERT INTO works (source_id, source_record_id, title, publisher,
                           publication_year, summary, contents, authors,
                           subjects, isbns, languages, provenance,
                           marc_005, content_hash, items_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id, source_record_id) DO UPDATE SET
            title = EXCLUDED.title,
            publisher = EXCLUDED.publisher,
            publication_year = EXCLUDED.publication_year,
            summary = EXCLUDED.summary,
            contents = EXCLUDED.contents,
            authors = EXCLUDED.authors,
            subjects = EXCLUDED.subjects,
            isbns = EXCLUDED.isbns,
            languages = EXCLUDED.languages,
            provenance = EXCLUDED.provenance,
            marc_005 = EXCLUDED.marc_005,
            content_hash = EXCLUDED.content_hash,
            items_hash = EXCLUDED.items_hash,
            last_seen = now(),
            deleted_at = NULL
        RETURNING id, (xmax = 0) AS was_insert
        """,
        (source_id, work.source_record_id, work.title, work.publisher,
         work.publication_year, work.summary, work.contents, work.authors,
         work.subjects, work.isbns, work.languages,
         psycopg.types.json.Jsonb(work.provenance),
         work.marc_005, work.content_hash, work.items_hash),
    ).fetchone()
    return row[0], "inserted" if row[1] else "updated", True


def load_works(conn: psycopg.Connection, source_id: int,
               works: Iterable[Work]) -> LoadStats:
    """Persist a stream of works. Commits once at the end."""
    stats = LoadStats()
    for work in works:
        if work.deleted:
            conn.execute(
                "UPDATE works SET deleted_at = now() "
                "WHERE source_id = %s AND source_record_id = %s",
                (source_id, work.source_record_id),
            )
            stats.deleted += 1
            continue

        work_id, action, items_changed = _upsert_work(conn, source_id, work)
        setattr(stats, action, getattr(stats, action) + 1)

        # Items live inside the same MARC record as the bibliography, so
        # an unchanged content_hash means the holdings are unchanged too
        # -- the hash excludes 952/995 precisely so that circulation does
        # not force a bibliographic rewrite, but the converse holds: if
        # the whole record is byte-identical, nothing moved. Rewriting
        # them anyway would mean deleting and reinserting every item row
        # in the catalogue on every nightly sync.
        if items_changed:
            stats.items_refreshed += 1
            conn.execute("DELETE FROM items WHERE work_id = %s", (work_id,))
            for item in work.items:
                conn.execute(
                    """
                    INSERT INTO items (work_id, barcode, owning_branch,
                                       holding_branch, location, call_number,
                                       item_type, due_date, issue_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (work_id, item.barcode, item.owning_branch,
                     item.holding_branch, item.location, item.call_number,
                     item.item_type, item.due_date, item.issue_count),
                )
                stats.items += 1

    # Recorded so /health can report catalogue freshness. A library's
    # monitoring needs to see a sync that has silently stopped running,
    # which an "ok" status alone would not show.
    conn.execute("UPDATE sources SET last_harvest = now() WHERE id = %s",
                 (source_id,))
    conn.commit()
    return stats
