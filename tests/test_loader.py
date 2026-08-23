"""Tests for persistence.

Run against a real PostgreSQL rather than a mock: the logic under test
is upsert behaviour, array round-tripping and hash comparison, none of
which a fake connection would exercise meaningfully. Skipped when no
database is reachable.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from bookrs.db.loader import ensure_source, load_works
from bookrs.ingestion.fieldmap import Flavour, Item, Work

# Deliberately NOT DATABASE_URL. These tests truncate every table, so
# pointing them at the development database would silently destroy a
# loaded catalogue on any `pytest` run. TEST_DATABASE_URL must be set
# explicitly and must name a throwaway database.
DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def conn():
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with connection:
        # Each test gets a clean slate. TRUNCATE rather than DELETE so
        # the tests do not depend on whatever a previous run left.
        connection.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        connection.commit()
        yield connection


def _work(rid: str = "KOHA-OAI-TEST:1", *, content: str = "hash-a",
          items: str = "hash-i", **kw) -> Work:
    defaults = dict(
        title="E Street shuffle : the glory days",
        authors=["Heylin, Clinton"],
        subjects=["Rock musicians — United States — Biography"],
        isbns=["9780670026623"],
        languages=["eng"],
        publisher="Viking",
        publication_year=2013,
        provenance={"title": "245$ab"},
    )
    defaults.update(kw)
    work = Work(source_record_id=rid, flavour=Flavour.MARC21, **defaults)
    work.content_hash = content
    work.items_hash = items
    return work


def _item(barcode="bc1", **kw) -> Item:
    return Item(barcode=barcode, owning_branch="CPL", holding_branch="CPL",
                location="GEN", item_type="BK", **kw)


def _source(conn) -> int:
    sid = ensure_source(conn, "test", "http://koha.test/oai.pl", "marc21", Flavour.MARC21)
    conn.commit()
    return sid


class TestSource:
    def test_is_idempotent(self, conn):
        """A second harvest must not create a second source row."""
        first = _source(conn)
        second = ensure_source(conn, "test", "http://koha.test/oai.pl",
                               "marc21", Flavour.MARC21)
        conn.commit()
        assert first == second
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1

    def test_different_prefix_is_a_different_source(self, conn):
        _source(conn)
        other = ensure_source(conn, "test", "http://koha.test/oai.pl",
                              "marcxml", Flavour.MARC21)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 2


class TestInsertAndRoundTrip:
    def test_arrays_and_jsonb_survive(self, conn):
        sid = _source(conn)
        load_works(conn, sid, [_work()])
        row = conn.execute(
            "SELECT title, authors, subjects, isbns, languages, provenance->>'title' "
            "FROM works"
        ).fetchone()
        assert row[1] == ["Heylin, Clinton"]
        assert row[2] == ["Rock musicians — United States — Biography"]  # em dash
        assert row[3] == ["9780670026623"]
        assert [c.strip() for c in row[4]] == ["eng"]
        assert row[5] == "245$ab"

    def test_items_are_stored(self, conn):
        sid = _source(conn)
        work = _work()
        work.items = [_item("bc1"), _item("bc2", due_date="2026-09-06", issue_count=3)]
        stats = load_works(conn, sid, [work])
        assert stats.items == 2
        rows = conn.execute(
            "SELECT barcode, due_date, issue_count FROM items ORDER BY barcode"
        ).fetchall()
        assert rows[0][1] is None
        assert str(rows[1][1]) == "2026-09-06" and rows[1][2] == 3

    def test_work_without_items_is_valid(self, conn):
        """25 of 436 MARC21 records had no holdings at all."""
        sid = _source(conn)
        stats = load_works(conn, sid, [_work()])
        assert stats.inserted == 1 and stats.items == 0


class TestUpsert:
    def test_unchanged_hashes_do_not_rewrite(self, conn):
        sid = _source(conn)
        load_works(conn, sid, [_work()])
        stats = load_works(conn, sid, [_work()])
        assert stats.unchanged == 1 and stats.updated == 0 and stats.inserted == 0

    def test_changed_bibliography_updates(self, conn):
        sid = _source(conn)
        load_works(conn, sid, [_work()])
        stats = load_works(conn, sid, [_work(content="hash-b", title="New title")])
        assert stats.updated == 1
        assert conn.execute("SELECT title FROM works").fetchone()[0] == "New title"

    def test_no_duplicate_rows_on_reharvest(self, conn):
        sid = _source(conn)
        for _ in range(3):
            load_works(conn, sid, [_work()])
        assert conn.execute("SELECT count(*) FROM works").fetchone()[0] == 1


class TestTwoHashes:
    """Bibliography and holdings change independently: a checkout moves
    the items and not the bibliography, a correction the reverse."""

    def test_checkout_refreshes_items_without_touching_bibliography(self, conn):
        sid = _source(conn)
        first = _work()
        first.items = [_item("bc1")]
        load_works(conn, sid, [first])

        borrowed = _work(items="hash-i2")          # content_hash unchanged
        borrowed.items = [_item("bc1", due_date="2026-09-06", issue_count=1)]
        stats = load_works(conn, sid, [borrowed])

        assert stats.unchanged == 1        # no re-embedding triggered
        assert stats.items_refreshed == 1  # holdings rewritten
        row = conn.execute("SELECT due_date, issue_count FROM items").fetchone()
        assert str(row[0]) == "2026-09-06" and row[1] == 1

    def test_bibliographic_edit_leaves_holdings_alone(self, conn):
        sid = _source(conn)
        first = _work()
        first.items = [_item("bc1")]
        load_works(conn, sid, [first])

        corrected = _work(content="hash-b", title="Corrected")
        corrected.items = [_item("bc1")]           # items_hash unchanged
        stats = load_works(conn, sid, [corrected])
        assert stats.updated == 1
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1

    def test_items_are_replaced_not_accumulated(self, conn):
        sid = _source(conn)
        first = _work()
        first.items = [_item("bc1"), _item("bc2")]
        load_works(conn, sid, [first])

        withdrawn = _work(items="hash-i2")
        withdrawn.items = [_item("bc1")]
        load_works(conn, sid, [withdrawn])
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


class TestDeletion:
    def test_deleted_record_is_marked_not_removed(self, conn):
        """History is kept; the row is tombstoned."""
        sid = _source(conn)
        load_works(conn, sid, [_work()])
        stats = load_works(conn, sid, [Work(source_record_id="KOHA-OAI-TEST:1",
                                            flavour=Flavour.MARC21, deleted=True)])
        assert stats.deleted == 1
        assert conn.execute("SELECT deleted_at FROM works").fetchone()[0] is not None

    def test_reappearing_record_is_undeleted(self, conn):
        sid = _source(conn)
        load_works(conn, sid, [_work()])
        load_works(conn, sid, [Work(source_record_id="KOHA-OAI-TEST:1",
                                    flavour=Flavour.MARC21, deleted=True)])
        load_works(conn, sid, [_work(content="hash-b")])
        assert conn.execute("SELECT deleted_at FROM works").fetchone()[0] is None
