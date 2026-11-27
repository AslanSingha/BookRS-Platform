"""Tests for the ratings and review-flag reader.

The library side is a fake connection: what matters there is which
columns are asked for and that rows are consumed in batches, and a fake
shows both. The storage side runs against a real PostgreSQL, because the
logic under test is a merge -- insert, update, remove -- and a mock
would only confirm that the SQL was sent.
"""

import os
from datetime import datetime, timedelta, timezone

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


# --- reading what changed --------------------------------------------------

def _norm(sql):
    return " ".join(sql.split())


class _Library:
    """A fake Koha answering the reader's own queries from its own tables,
    as Koha would: ratings keyed by (borrower, record) with a timestamp,
    reviews with the date they were written, and a clock."""

    def __init__(self, now, ratings=(), reviews=()):
        self.now = now
        self.ratings = {(b, r): (v, t) for b, r, v, t in ratings}
        self.reviews = list(reviews)        # (borrower, record, datereviewed)
        self.queries = []
        self.open_cursors = 0

    def rate(self, borrower, record, value, at):
        self.ratings[(borrower, record)] = (value, at)

    def cursor(self):
        return _LibraryCursor(self)

    def close(self):
        pass


class _LibraryCursor:
    def __init__(self, library):
        self.lib, self.rows = library, []

    def __enter__(self):
        self.lib.open_cursors += 1
        return self

    def __exit__(self, *exc):
        self.lib.open_cursors -= 1
        return False

    def execute(self, sql, params=None):
        q, lib = _norm(sql), self.lib
        lib.queries.append((q, params))
        if q == _norm(direct._KOHA_NOW):
            self.rows = [(lib.now,)]
        elif q == _norm(direct._KOHA_RATINGS):
            self.rows = [(b, r, v, t) for (b, r), (v, t) in lib.ratings.items()]
        elif q == _norm(direct._KOHA_REVIEWS):
            self.rows = [(b, r) for b, r, _ in lib.reviews]
        elif q == _norm(direct._KOHA_RATINGS_SINCE):
            self.rows = [(b, r, v, t) for (b, r), (v, t) in lib.ratings.items()
                         if t >= params[0]]
        elif q == _norm(direct._KOHA_REVIEWS_SINCE):
            self.rows = [(b, r) for b, r, d in lib.reviews if d >= params[0]]
        elif q.startswith(_norm(direct._KOHA_REVIEWS_OF.split("{pairs}")[0])):
            asked = set(zip(params[::2], params[1::2]))
            self.rows = sorted({(b, r) for b, r, _ in lib.reviews if (b, r) in asked})
        else:
            raise AssertionError(f"a query Koha was not expected to get: {q}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


K0 = datetime(2026, 11, 26, 7, 0)            # Koha's clock: no zone, as Koha gives it
MIN = timedelta(minutes=1)


class TestReadingChanges:
    def test_only_ratings_stamped_since_are_read(self):
        lib = _Library(K0, ratings=[(7, 1, 4, K0 - 60 * MIN), (8, 2, 5, K0 - MIN)])
        ratings, flagged = direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - 5 * MIN)
        assert [(r[1], r[2]) for r in ratings] == [("2", 5)] and flagged == []

    def test_a_changed_rating_carries_its_review_flag_whenever_it_was_written(self):
        """A patron who reviewed a book last year and re-rates it today:
        the flag is Koha's, not only what changed since."""
        lib = _Library(K0, ratings=[(7, 1, 5, K0 - MIN), (8, 1, 2, K0 - MIN)],
                       reviews=[(7, 1, K0 - 365 * 24 * 60 * MIN)])
        ratings, _ = direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - 5 * MIN)
        flags = {r[2]: r[4] for r in ratings}
        assert flags == {5: True, 2: False}

    def test_a_review_written_since_is_reported_for_flagging(self):
        lib = _Library(K0, ratings=[(7, 1, 5, K0 - 60 * MIN)], reviews=[(7, 1, K0 - MIN)])
        ratings, flagged = direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - 5 * MIN)
        p = _Pseudonymiser("s")
        assert ratings == [] and flagged == [(p(7), "1")]

    def test_the_review_question_names_the_changed_pairs_only(self):
        lib = _Library(K0, ratings=[(7, 1, 5, K0 - MIN), (9, 3, 4, K0 - 99 * MIN)])
        direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - 5 * MIN)
        (asked,) = [p for q, p in lib.queries if "IN (" in q]
        assert asked == (7, 1)

    def test_many_changes_are_asked_about_in_chunks(self, monkeypatch):
        monkeypatch.setattr(direct, "_PAIRS_PER_QUERY", 2)
        lib = _Library(K0, ratings=[(b, 1, 4, K0) for b in range(5)])
        direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - MIN)
        assert sum("IN (" in q for q, _ in lib.queries) == 3

    def test_more_changes_than_a_read_of_changes_holds_are_refused(self):
        """After a bulk load (26 million new ratings with the full UCSD
        catalogue) the read must not try to hold them all. It stops past
        the limit, and closes the library's cursor before the full read
        asks anything else."""
        lib = _Library(K0, ratings=[(b, 1, 4, K0) for b in range(5)])
        with pytest.raises(direct.TooManyChanges):
            direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - MIN, limit=3)
        assert lib.open_cursors == 0
        assert not any("IN (" in q for q, _ in lib.queries)

    def test_up_to_the_limit_is_an_ordinary_read_of_changes(self):
        lib = _Library(K0, ratings=[(b, 1, 4, K0) for b in range(3)])
        ratings, _ = direct.read_koha_changes(lib, _Pseudonymiser("s"), K0 - MIN, limit=3)
        assert len(ratings) == 3

    def test_no_changes_ask_no_review_question(self):
        lib = _Library(K0)
        assert direct.read_koha_changes(lib, _Pseudonymiser("s"), K0) == ([], [])
        assert not any("IN (" in q for q, _ in lib.queries)

    def test_nothing_sent_to_the_library_can_write_or_read_review_text(self):
        pairs = direct._KOHA_REVIEWS_OF.format(pairs="(%s, %s)")
        for sql in (direct._KOHA_NOW, direct._KOHA_RATINGS_SINCE,
                    direct._KOHA_REVIEWS_SINCE, pairs):
            words = sql.upper().replace(",", " ").split()
            assert words[0] == "SELECT"
            assert not {"INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER",
                        "DROP", "CREATE", "TRUNCATE", "INTO"} & set(words)
            selected = sql.lower().split(" from ")[0]
            assert "review" not in selected.replace("reviews", "") and "*" not in selected


class TestChoosingTheRead:
    NOW = datetime(2026, 11, 26, 7, 0, tzinfo=timezone.utc)
    DAY = timedelta(days=1)

    def test_a_full_read_unless_changes_are_asked_for(self):
        assert direct.choose_read(False, K0, self.NOW, self.DAY, self.NOW).startswith("full")

    def test_the_first_read_of_changes_is_a_full_one(self):
        assert "no earlier read" in direct.choose_read(True, None, None, self.DAY, self.NOW)

    def test_a_full_read_once_full_every_has_passed(self):
        assert direct.choose_read(True, K0, self.NOW - self.DAY, self.DAY,
                                  self.NOW).startswith("full")

    def test_changes_in_between(self):
        assert direct.choose_read(True, K0, self.NOW - timedelta(hours=23),
                                  self.DAY, self.NOW) == "changes"

    def test_full_every_off_reads_changes_for_ever(self):
        assert direct.choose_read(True, K0, None, None, self.NOW) == "changes"


def _rated(conn, sid):
    return {(ref[:6], rec): (rating, reviewed) for ref, rec, rating, reviewed in _stored(conn, sid)}


class TestStoringChanges:
    def _full(self, conn, sid, rows, reviews=(), watermark=None):
        store(conn, sid, rows, reviews, watermark=watermark)

    def test_a_changed_rating_is_updated_and_the_rest_left_alone(self, conn):
        sid = _library(conn)
        self._full(conn, sid, [("p1", "1", 4, T), ("p2", "2", 3, T)])
        stats = direct.store_changes(conn, sid, [("p1", "1", 2, T, False)])
        assert _rated(conn, sid) == {("p1", "1"): (2, False), ("p2", "2"): (3, False)}
        assert (stats["stored"], stats["unchanged"]) == (1, 0)

    def test_a_new_rating_is_added_with_its_review_flag(self, conn):
        sid = _library(conn)
        direct.store_changes(conn, sid, [("p1", "3", 5, T, True)])
        assert _rated(conn, sid) == {("p1", "3"): (5, True)}

    def test_a_rating_read_twice_in_the_margin_is_stored_once(self, conn):
        sid = _library(conn)
        direct.store_changes(conn, sid, [("p1", "1", 4, T, False)])
        stats = direct.store_changes(conn, sid, [("p1", "1", 4, T, False)])
        assert (stats["stored"], stats["unchanged"]) == (0, 1)

    def test_a_review_written_since_flags_the_stored_rating(self, conn):
        sid = _library(conn)
        self._full(conn, sid, [("p1", "1", 4, T), ("p2", "1", 4, T)])
        stats = direct.store_changes(conn, sid, [], [("p1", "1"), ("p9", "2")])
        assert _rated(conn, sid) == {("p1", "1"): (4, True), ("p2", "1"): (4, False)}
        assert stats["flagged"] == 1

    def test_a_removed_rating_waits_for_the_full_read(self, conn):
        """A read of changes cannot see what was removed; it removes
        nothing, and --full-every is what catches it."""
        sid = _library(conn)
        self._full(conn, sid, [("p1", "1", 4, T)])
        direct.store_changes(conn, sid, [])
        assert _rated(conn, sid) == {("p1", "1"): (4, False)}

    def test_no_changes_is_the_usual_case_not_an_error(self, conn):
        sid = _library(conn)
        assert direct.store_changes(conn, sid, [])["read"] == 0

    def test_unknown_records_and_bad_values_are_counted_not_stored(self, conn):
        sid = _library(conn)
        stats = direct.store_changes(conn, sid, [("p1", "99", 4, T, False),
                                                 ("p1", "1", 9, T, False)])
        assert (stats["unresolved"], stats["invalid"]) == (1, 1) and _rated(conn, sid) == {}

    def test_another_librarys_ratings_are_untouched(self, conn):
        a, b = _library(conn, name="a"), _library(conn, name="b")
        self._full(conn, b, [("p1", "1", 4, T)])
        direct.store_changes(conn, a, [("p1", "1", 1, T, False)], [("p1", "1")])
        assert _rated(conn, b) == {("p1", "1"): (4, False)}

    def test_the_watermark_moves_with_the_ratings(self, conn):
        sid = _library(conn)
        direct.store_changes(conn, sid, [("p1", "1", 4, T, False)], watermark=K0)
        assert direct.read_state(conn, sid)[0] == K0

    def test_a_store_that_fails_leaves_the_watermark_where_it_was(self, conn):
        sid = _library(conn)
        direct.store_changes(conn, sid, [], watermark=K0)

        def broken():
            yield ("p1", "1", 4, T, False)
            raise RuntimeError("the connection to Koha dropped")
        with pytest.raises(RuntimeError):
            direct.store_changes(conn, sid, broken(), watermark=K0 + 60 * MIN)
        conn.rollback()
        assert direct.read_state(conn, sid)[0] == K0 and _rated(conn, sid) == {}

    def test_a_full_read_records_the_watermark_and_when_it_finished(self, conn):
        sid = _library(conn)
        self._full(conn, sid, [("p1", "1", 4, T)], watermark=K0)
        watermark, full_at = direct.read_state(conn, sid)
        assert watermark == K0 and full_at is not None


class TestTheCommand:
    """main() end to end: a fake Koha behind the pymysql interface, the
    real database behind the service, and the watermark between runs."""

    def _run(self, monkeypatch, library, *args):
        import sys
        import types
        fake = types.ModuleType("pymysql")
        cursors = types.ModuleType("pymysql.cursors")
        cursors.SSCursor = object
        fake.cursors = cursors
        fake.connect = lambda **kw: library
        monkeypatch.setitem(sys.modules, "pymysql", fake)
        monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors)
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        monkeypatch.setenv("BOOKRS_PATRON_SECRET", "s3cret")
        monkeypatch.setattr(sys, "argv", ["direct", "--source-id", str(self.sid),
                                          "--url", "mysql://u:p@koha:3306/koha", *args])
        return direct.main()

    def test_a_rating_given_after_a_read_arrives_with_the_next_read_of_changes(
            self, conn, monkeypatch, caplog):
        import logging
        self.sid = _library(conn)
        lib = _Library(K0, ratings=[(7, 1, 4, K0 - 600 * MIN), (8, 2, 3, K0 - 600 * MIN)])
        assert self._run(monkeypatch, lib, "--changes") == 0      # first: full
        assert len(_stored(conn, self.sid)) == 2
        assert direct.read_state(conn, self.sid)[0] == K0

        lib.now = K0 + 10 * MIN
        lib.rate(7, 3, 5, K0 + 2 * MIN)                            # in the OPAC
        lib.rate(8, 2, 1, K0 + 3 * MIN)                            # re-rated
        with caplog.at_level(logging.INFO, logger="bookrs.ingestion.direct"):
            assert self._run(monkeypatch, lib, "--changes") == 0
        assert "changes since 2026-11-26 06:50:00" in caplog.text
        ratings = {(rec, rating) for _, rec, rating, _ in _stored(conn, self.sid)}
        assert ratings == {("1", 4), ("2", 1), ("3", 5)}
        assert direct.read_state(conn, self.sid)[0] == K0 + 10 * MIN
        # The second read never asked for all the ratings.
        assert sum(q == _norm(direct._KOHA_RATINGS) for q, _ in lib.queries) == 1

    def test_full_every_reads_everything_again_and_catches_a_removal(
            self, conn, monkeypatch):
        self.sid = _library(conn)
        lib = _Library(K0, ratings=[(7, 1, 4, K0 - 600 * MIN), (8, 2, 3, K0 - 600 * MIN)])
        self._run(monkeypatch, lib, "--changes")
        del lib.ratings[(8, 2)]
        lib.now = K0 + 10 * MIN
        self._run(monkeypatch, lib, "--changes", "--full-every", "1d")
        assert len(_stored(conn, self.sid)) == 2                  # changes only
        conn.execute("UPDATE sources SET ratings_full_read_at = now() - interval '25 hours'")
        conn.commit()
        self._run(monkeypatch, lib, "--changes", "--full-every", "1d")
        assert len(_stored(conn, self.sid)) == 1                  # the full read

    def test_a_bulk_load_is_read_whole_instead(self, conn, monkeypatch, caplog):
        import logging
        self.sid = _library(conn)
        lib = _Library(K0, ratings=[(7, 1, 4, K0 - 600 * MIN)])
        self._run(monkeypatch, lib, "--changes")                  # first: full
        lib.now = K0 + 10 * MIN
        lib.rate(8, 2, 5, K0 + 2 * MIN)
        lib.rate(9, 3, 3, K0 + 3 * MIN)
        monkeypatch.setattr(direct, "MAX_CHANGES", 1)
        with caplog.at_level(logging.INFO, logger="bookrs.ingestion.direct"):
            assert self._run(monkeypatch, lib, "--changes") == 0
        assert "reading everything (more than 1 ratings changed since the last read)" \
            in caplog.text
        assert len(_stored(conn, self.sid)) == 3
        assert sum(q == _norm(direct._KOHA_RATINGS) for q, _ in lib.queries) == 2
        assert direct.read_state(conn, self.sid)[0] == K0 + 10 * MIN

    def test_a_bad_full_every_is_refused_before_koha_is_asked(self, conn, monkeypatch):
        self.sid = _library(conn)
        lib = _Library(K0)
        with pytest.raises(SystemExit):
            self._run(monkeypatch, lib, "--changes", "--full-every", "daily")
        assert lib.queries == []
