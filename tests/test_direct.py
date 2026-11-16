"""Tests for the ratings and review-flag reader.

The library side is a fake connection: what matters there is which
columns are asked for and that rows are consumed in batches, and a fake
shows both. The storage side runs against a real PostgreSQL, because the
logic under test is a merge -- insert, update, remove -- and a mock
would only confirm that the SQL was sent.
"""

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from bookrs.ingestion import direct
from bookrs.ingestion.direct import (
    EmptyRead, _Pseudonymiser, read_koha_ratings, read_koha_reviews, store,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
T = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        connection = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with connection:
        connection.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        connection.commit()
        yield connection


def _library(conn, records=("1", "2", "3"), name="koha") -> int:
    sid = conn.execute(
        "INSERT INTO sources (name, base_url) VALUES (%s, %s) RETURNING id",
        (name, f"mysql://{name}")).fetchone()[0]
    for rid in records:
        conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, "
            "content_hash, items_hash) VALUES (%s, %s, %s, 'c', 'i')",
            (sid, rid, f"work {rid}"))
    conn.commit()
    return sid


def _stored(conn, sid):
    return conn.execute(
        "SELECT r.patron_ref, w.source_record_id, r.rating, r.reviewed "
        "FROM ratings r JOIN works w ON w.id = r.work_id "
        "WHERE r.source_id = %s ORDER BY 1, 2", (sid,)).fetchall()


class _Cursor:
    def __init__(self, rows, log):
        self._rows, self._log, self._at = rows, log, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))

    def fetchmany(self, size):
        self._log.append(size)
        batch = self._rows[self._at:self._at + size]
        self._at += size
        return batch


class _Koha:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return _Cursor(self.rows, self.log)


class TestReading:
    def test_ratings_are_pseudonymised_and_typed(self):
        koha = _Koha([(7, 41, 5, T), (7, 42, 3, T), (9, 41, 1, T)])
        rows = list(read_koha_ratings(koha, _Pseudonymiser("s3cret")))
        assert [r[1:] for r in rows] == [("41", 5, T), ("42", 3, T), ("41", 1, T)]
        assert rows[0][0] == rows[1][0] != rows[2][0]
        assert len(rows[0][0]) == 32 and rows[0][0] != "7"

    def test_the_same_borrower_is_the_same_patron_in_both_tables(self):
        """A review must land on its own patron's rating."""
        p = _Pseudonymiser("s3cret")
        rating = next(read_koha_ratings(_Koha([(7, 41, 5, T)]), p))
        review = next(read_koha_reviews(_Koha([(7, 41)]), p))
        assert rating[:2] == review

    def test_a_different_secret_gives_different_patrons(self):
        a = next(read_koha_ratings(_Koha([(7, 41, 5, T)]), _Pseudonymiser("a")))
        b = next(read_koha_ratings(_Koha([(7, 41, 5, T)]), _Pseudonymiser("b")))
        assert a[0] != b[0]

    def test_the_review_text_is_never_selected(self):
        koha = _Koha([])
        list(read_koha_reviews(koha, _Pseudonymiser("s")))
        sql = koha.log[0].lower()
        selected = sql.split(" from ")[0]
        assert "review" not in selected.replace("reviews", "")
        assert "*" not in selected

    def test_nothing_sent_to_the_library_can_write(self):
        for sql in (direct._KOHA_RATINGS, direct._KOHA_REVIEWS):
            words = sql.upper().split()
            assert words[0] == "SELECT"
            assert not {"INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER",
                        "DROP", "CREATE", "TRUNCATE", "INTO"} & set(words)

    def test_rows_are_fetched_in_batches_not_all_at_once(self, monkeypatch):
        monkeypatch.setattr(direct, "_BATCH", 2)
        koha = _Koha([(b, 1, 4, T) for b in range(5)])
        assert len(list(read_koha_ratings(koha, _Pseudonymiser("s")))) == 5
        assert koha.log[1:] == [2, 2, 2, 2]      # 2 + 2 + 1, then the empty one

    def test_borrowers_are_hashed_once(self):
        p = _Pseudonymiser("s")
        list(read_koha_ratings(_Koha([(7, b, 4, T) for b in range(50)]), p))
        assert len(p) == 1


class TestStoring:
    def test_ratings_resolve_to_works(self, conn):
        sid = _library(conn)
        stats = store(conn, sid, [("p1", "1", 5, T), ("p1", "2", 3, T),
                                  ("p2", "1", 4, T)])
        assert _stored(conn, sid) == [("p1", "1", 5, False), ("p1", "2", 3, False),
                                      ("p2", "1", 4, False)]
        assert (stats["read"], stats["stored"], stats["unresolved"]) == (3, 3, 0)

    def test_a_review_sets_the_flag_on_that_patrons_rating_only(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T), ("p2", "1", 5, T), ("p1", "2", 5, T)],
              [("p1", "1")])
        assert _stored(conn, sid) == [("p1", "1", 5, True), ("p1", "2", 5, False),
                                      ("p2", "1", 5, False)]

    def test_two_reviews_of_one_record_are_one_flag(self, conn):
        sid = _library(conn)
        stats = store(conn, sid, [("p1", "1", 5, T)], [("p1", "1"), ("p1", "1")])
        assert _stored(conn, sid) == [("p1", "1", 5, True)]
        assert stats["reviews_read"] == 2

    def test_a_review_without_a_rating_is_counted_not_stored(self, conn):
        sid = _library(conn)
        stats = store(conn, sid, [("p1", "1", 5, T)], [("p2", "1"), ("p1", "3")])
        assert _stored(conn, sid) == [("p1", "1", 5, False)]
        assert stats["reviews_without_rating"] == 2

    def test_harvested_identifiers_resolve_by_their_last_segment(self, conn):
        sid = _library(conn, records=("oai:koha.test:41", "42"))
        store(conn, sid, [("p1", "41", 5, T), ("p1", "42", 2, T)])
        assert [r[1] for r in _stored(conn, sid)] == ["42", "oai:koha.test:41"]

    def test_a_rating_for_an_unknown_record_is_counted_and_dropped(self, conn):
        sid = _library(conn)
        stats = store(conn, sid, [("p1", "1", 5, T), ("p1", "999", 5, T)])
        assert len(_stored(conn, sid)) == 1 and stats["unresolved"] == 1

    def test_a_value_outside_one_to_five_is_counted_and_dropped(self, conn):
        """Koha's column is a tinyint; nothing in the database stops a 0."""
        sid = _library(conn)
        stats = store(conn, sid, [("p1", "1", 0, T), ("p1", "2", 6, T),
                                  ("p1", "3", 5, T)])
        assert _stored(conn, sid) == [("p1", "3", 5, False)]
        assert (stats["invalid"], stats["unresolved"]) == (2, 0)

    def test_a_second_identical_read_changes_nothing(self, conn):
        sid = _library(conn)
        rows, flags = [("p1", "1", 5, T), ("p2", "2", 3, T)], [("p1", "1")]
        store(conn, sid, rows, flags)
        stats = store(conn, sid, rows, flags)
        assert (stats["stored"], stats["unchanged"], stats["removed"]) == (0, 2, 0)

    def test_a_changed_rating_and_a_new_review_are_picked_up(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T), ("p2", "2", 3, T)])
        stats = store(conn, sid, [("p1", "1", 2, T), ("p2", "2", 3, T)], [("p2", "2")])
        assert _stored(conn, sid) == [("p1", "1", 2, False), ("p2", "2", 3, True)]
        assert (stats["stored"], stats["unchanged"]) == (2, 0)

    def test_a_withdrawn_review_clears_the_flag(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T)], [("p1", "1")])
        store(conn, sid, [("p1", "1", 5, T)])
        assert _stored(conn, sid) == [("p1", "1", 5, False)]

    def test_a_rating_removed_in_the_library_is_removed_here(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T), ("p2", "2", 3, T)])
        stats = store(conn, sid, [("p2", "2", 3, T)])
        assert _stored(conn, sid) == [("p2", "2", 3, False)] and stats["removed"] == 1

    def test_an_empty_read_does_not_empty_the_table(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T)])
        with pytest.raises(EmptyRead):
            store(conn, sid, [])
        conn.rollback()
        assert len(_stored(conn, sid)) == 1

    def test_an_empty_read_is_accepted_when_asked_for(self, conn):
        sid = _library(conn)
        store(conn, sid, [("p1", "1", 5, T)])
        assert store(conn, sid, [], allow_empty=True)["removed"] == 1
        assert _stored(conn, sid) == []

    def test_an_empty_library_with_nothing_stored_is_not_an_error(self, conn):
        assert store(conn, _library(conn), [])["read"] == 0

    def test_another_librarys_ratings_are_untouched(self, conn):
        a, b = _library(conn, name="a"), _library(conn, name="b")
        store(conn, a, [("p1", "1", 5, T)])
        store(conn, b, [("p1", "1", 2, T)])
        store(conn, b, [("p9", "2", 4, T)])
        assert _stored(conn, a) == [("p1", "1", 5, False)]
        assert _stored(conn, b) == [("p9", "2", 4, False)]

    def test_input_is_consumed_as_a_stream(self, conn):
        """A generator, never a list: sixteen million rows must not be held."""
        sid = _library(conn)
        store(conn, sid, (("p%d" % n, "1", 4, T) for n in range(1000)),
              (("p%d" % n, "1") for n in range(0, 1000, 10)))
        assert conn.execute("SELECT count(*), count(*) FILTER (WHERE reviewed) "
                            "FROM ratings").fetchone() == (1000, 100)
