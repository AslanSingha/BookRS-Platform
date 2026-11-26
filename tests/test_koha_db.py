"""The catalogue read from Koha's database: pages, changes, removals.

A fake Koha answers the reader's own queries from its own tables --
biblio, biblioitems and items with the time stamps Koha keeps on each --
so the tests can say what a library looked like and what the platform
must have stored after reading it.
"""

import os
from datetime import datetime, timedelta

import psycopg
import pytest

from bookrs.ingestion import koha_db

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
K0 = datetime(2026, 11, 27, 1, 0)      # Koha's clock: no zone, as Koha gives it
MIN = timedelta(minutes=1)


def _norm(sql):
    return " ".join(sql.split())


class _Koha:
    """biblio, biblioitems and items, stamped as Koha stamps them."""

    def __init__(self, now=K0):
        self.now = now
        self.biblio, self.items, self.queries = {}, [], []

    def add(self, bib, title, *, at=None, genre="fiction", copies=1, hidden=False,
            isbn="9780000000000"):
        at = at or self.now - 600 * MIN
        self.biblio[bib] = dict(title=title, author=f"Author {bib}", abstract=f"About {title}",
                                hidden=hidden, at=at, isbn=isbn, bi_at=at)
        for c in range(copies):
            self.items.append(dict(bib=bib, barcode=f"{bib}-{c}", onloan=None, ccode=genre, at=at))

    def touch_item(self, bib, at, **change):
        for it in self.items:
            if it["bib"] == bib:
                it.update(change, at=at)

    # -- pymysql's interface, as the reader uses it -------------------------
    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass

    # -- the reader's queries ----------------------------------------------
    def _work(self, bib):
        b = self.biblio[bib]
        return (bib, b["title"], "", b["author"], b["abstract"], "", "2001", b["at"],
                b["isbn"], 2001, "Publisher")

    def _item(self, it):
        return (it["bib"], it["barcode"], "CPL", "CPL", "GEN", "823 X", "BK", it["onloan"],
                0, 0, 0, 0, it["ccode"])

    def visible(self):
        return sorted(b for b, v in self.biblio.items() if not v["hidden"])

    def answer(self, q, params):
        page_since = _norm(koha_db._WORKS_PAGE.format(since="AND b.timestamp > %s"))
        if q == _norm(koha_db._KOHA_NOW):
            return [(self.now,)]
        if q == _norm(koha_db._WORKS_PAGE.format(since="")):
            last, limit = params
            return [self._work(b) for b in self.visible() if b > last][:limit]
        if q == page_since:
            last, since, limit = params
            return [self._work(b) for b in self.visible()
                    if b > last and self.biblio[b]["at"] > since][:limit]
        if q == _norm(koha_db._ITEMS_RANGE):
            lo, hi = params
            vis = set(self.visible())
            return [self._item(i) for i in self.items if lo <= i["bib"] <= hi and i["bib"] in vis]
        if q == _norm(koha_db._CHANGED[0]):
            return [(b,) for b, v in self.biblio.items() if v["at"] >= params[0]]
        if q == _norm(koha_db._CHANGED[1]):
            return [(b,) for b, v in self.biblio.items() if v["bi_at"] >= params[0]]
        if q == _norm(koha_db._CHANGED[2]):
            return sorted({(i["bib"],) for i in self.items if i["at"] >= params[0]})
        head = _norm(koha_db._WORKS_IN.split("{ids}")[0])
        if q.startswith(head):
            vis = set(self.visible())
            return [self._work(b) for b in sorted(params) if b in vis]
        head = _norm(koha_db._ITEMS_IN.split("{ids}")[0])
        if q.startswith(head):
            vis, want = set(self.visible()), set(params)
            return [self._item(i) for i in self.items if i["bib"] in want and i["bib"] in vis]
        raise AssertionError(f"a query Koha was not expected to get: {q}")


class _Cursor:
    def __init__(self, koha):
        self.koha, self.rows = koha, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        q = _norm(sql)
        self.koha.queries.append((q, params))
        self.rows = self.koha.answer(q, tuple(params or ()))

    def fetchall(self):
        return list(self.rows)


def _titles(pages):
    return [[w.title for w in page] for page in pages]


# -- reading Koha: no database needed ---------------------------------------

class TestPages:
    def test_every_record_a_page_at_a_time_in_record_order(self):
        k = _Koha()
        for b in (3, 1, 2, 5, 4):
            k.add(b, f"T{b}")
        assert _titles(koha_db.read_pages(k, batch=2)) == [["T1", "T2"], ["T3", "T4"], ["T5"]]

    def test_each_record_carries_its_own_copies_and_their_collection(self):
        k = _Koha()
        k.add(1, "T1", copies=2, genre="romance")
        k.add(2, "T2", genre="fantasy, paranormal")
        (page,) = koha_db.read_pages(k)
        assert [(w.biblionumber, [i["collection"] for i in w.items]) for w in page] == \
            [("1", ["romance", "romance"]), ("2", ["fantasy, paranormal"])]

    def test_a_record_hidden_from_the_opac_is_not_read(self):
        k = _Koha()
        k.add(1, "T1")
        k.add(2, "T2", hidden=True)
        assert _titles(koha_db.read_pages(k)) == [["T1"]]

    def test_an_exactly_full_last_page_ends_the_read(self):
        k = _Koha()
        for b in (1, 2):
            k.add(b, f"T{b}")
        assert _titles(koha_db.read_pages(k, batch=2)) == [["T1", "T2"]]

    def test_an_empty_catalogue_is_no_pages(self):
        assert list(koha_db.read_pages(_Koha())) == []


class TestChanges:
    def test_a_record_changed_in_any_of_its_three_tables_is_found(self):
        k = _Koha()
        for b in (1, 2, 3, 4):
            k.add(b, f"T{b}")
        k.biblio[1]["at"] = K0 - MIN                 # the record edited
        k.biblio[2]["bi_at"] = K0 - MIN              # its ISBN corrected
        k.touch_item(3, K0 - MIN, onloan="2026-12-01")   # a copy checked out
        assert koha_db.changed_records(k, K0 - 5 * MIN) == [1, 2, 3]

    def test_changed_records_are_read_whole_with_all_their_copies(self):
        k = _Koha()
        k.add(1, "T1", copies=3)
        k.add(2, "T2")
        k.touch_item(1, K0 - MIN)
        (page,) = koha_db.read_records(k, [1])
        assert [(w.title, len(w.items)) for w in page] == [("T1", 3)]

    def test_a_record_deleted_since_is_simply_not_returned(self):
        k = _Koha()
        k.add(1, "T1")
        assert _titles(koha_db.read_records(k, [1, 99])) == [["T1"]]

    def test_many_changes_are_asked_for_in_chunks(self):
        k = _Koha()
        for b in range(1, 6):
            k.add(b, f"T{b}")
        pages = list(koha_db.read_records(k, [1, 2, 3, 4, 5], batch=2))
        assert [len(p) for p in pages] == [2, 2, 1]

    def test_nothing_sent_to_the_library_can_write(self):
        sqls = [koha_db._KOHA_NOW, koha_db._WORKS_PAGE.format(since="AND b.timestamp > %s"),
                koha_db._WORKS_IN.format(ids="%s"), koha_db._ITEMS_RANGE,
                koha_db._ITEMS_IN.format(ids="%s"), *koha_db._CHANGED]
        for sql in sqls:
            words = set(sql.upper().replace(",", " ").replace("(", " ").split())
            assert sql.split()[0].upper() == "SELECT"
            assert not {"INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "DROP",
                        "CREATE", "TRUNCATE", "INTO"} & words, sql


# -- storing: the platform's database ---------------------------------------

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


def _source(conn) -> int:
    sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('koha', 'mysql://k') "
                       "RETURNING id").fetchone()[0]
    conn.commit()
    return sid


def _works(conn, sid):
    return conn.execute(
        "SELECT w.source_record_id, w.title, w.deleted_at IS NOT NULL, "
        "array_agg(i.collection ORDER BY i.barcode) FILTER (WHERE i.id IS NOT NULL) "
        "FROM works w LEFT JOIN items i ON i.work_id = w.id WHERE w.source_id = %s "
        "GROUP BY w.id ORDER BY w.source_record_id", (sid,)).fetchall()


def _read_all(conn, sid, k, **kw):
    stats, seen = koha_db.new_stats(), koha_db.held(conn, sid)
    for page in koha_db.read_pages(k, batch=2):
        koha_db.store_page(conn, sid, page, seen, stats)
    koha_db.finish(conn, sid, stats, **kw)
    return stats


class TestStoring:
    def test_records_and_copies_are_stored_with_their_collection(self, conn):
        sid, k = _source(conn), _Koha()
        k.add(1, "T1", genre="romance", copies=2)
        k.add(2, "T2", genre="children")
        k.add(3, "T3")
        stats = _read_all(conn, sid, k)
        assert (stats["new"], stats["items"]) == (3, 4)
        assert _works(conn, sid) == [("1", "T1", False, ["romance", "romance"]),
                                     ("2", "T2", False, ["children"]),
                                     ("3", "T3", False, ["fiction"])]

    def test_a_second_read_finds_everything_unchanged(self, conn):
        sid, k = _source(conn), _Koha()
        for b in (1, 2, 3):
            k.add(b, f"T{b}")
        _read_all(conn, sid, k)
        before = conn.execute("SELECT max(last_seen) FROM works").fetchone()[0]
        stats = _read_all(conn, sid, k)
        after = conn.execute("SELECT min(last_seen) FROM works").fetchone()[0]
        assert (stats["new"], stats["updated"], stats["unchanged"]) == (0, 0, 3)
        assert after >= before        # seen again, in one statement a page

    def test_a_copy_checked_out_updates_the_record_without_new_text(self, conn):
        sid, k = _source(conn), _Koha()
        k.add(1, "T1")
        _read_all(conn, sid, k)
        hash_before = conn.execute("SELECT content_hash FROM works").fetchone()[0]
        k.touch_item(1, K0, onloan="2026-12-24")
        stats = _read_all(conn, sid, k)
        assert stats["updated"] == 1
        assert conn.execute("SELECT content_hash FROM works").fetchone()[0] == hash_before
        assert conn.execute("SELECT due_date::text FROM items").fetchone()[0] == "2026-12-24"

    def test_a_full_read_marks_what_koha_no_longer_holds(self, conn):
        sid, k = _source(conn), _Koha()
        for b in (1, 2, 3):
            k.add(b, f"T{b}")
        _read_all(conn, sid, k)
        del k.biblio[2]
        k.biblio[3]["hidden"] = True
        started = conn.execute("SELECT now()").fetchone()[0]
        conn.commit()
        stats = _read_all(conn, sid, k, full=True, full_started=started)
        assert stats["removed"] == 2
        assert [(r[0], r[2]) for r in _works(conn, sid)] == [("1", False), ("2", True), ("3", True)]

    def test_a_record_back_in_the_catalogue_is_no_longer_deleted(self, conn):
        sid, k = _source(conn), _Koha()
        k.add(1, "T1")
        _read_all(conn, sid, k)
        conn.execute("UPDATE works SET deleted_at = now()")
        conn.commit()
        k.biblio[1]["title"] = "T1, revised"
        _read_all(conn, sid, k)
        assert _works(conn, sid)[0][1:3] == ("T1, revised", False)

    def test_an_empty_full_read_deletes_nothing(self, conn):
        sid, k = _source(conn), _Koha()
        k.add(1, "T1")
        _read_all(conn, sid, k)
        started = conn.execute("SELECT now()").fetchone()[0]
        conn.commit()
        with pytest.raises(koha_db.EmptyCatalogue):
            _read_all(conn, sid, _Koha(), full=True, full_started=started)
        conn.rollback()
        assert _works(conn, sid)[0][2] is False

    def test_a_read_of_changes_marks_nothing_deleted(self, conn):
        sid, k = _source(conn), _Koha()
        for b in (1, 2):
            k.add(b, f"T{b}")
        _read_all(conn, sid, k)
        stats, seen = koha_db.new_stats(), koha_db.held(conn, sid)
        for page in koha_db.read_records(k, [1]):
            koha_db.store_page(conn, sid, page, seen, stats)
        koha_db.finish(conn, sid, stats, watermark=K0)
        assert [r[2] for r in _works(conn, sid)] == [False, False]
        assert koha_db.catalogue_state(conn, sid)[0] == K0


class TestTheCommand:
    """main() end to end: the fake Koha behind pymysql's interface, the
    real database behind the service, the watermark between runs."""

    def _run(self, monkeypatch, koha, *args):
        import sys
        import types
        fake = types.ModuleType("pymysql")
        fake.connect = lambda **kw: koha
        monkeypatch.setitem(sys.modules, "pymysql", fake)
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        monkeypatch.setattr(sys, "argv", ["koha_db", "--source-id", str(self.sid),
                                          "--url", "mysql://u:p@koha:3306/koha", *args])
        return koha_db.main()

    def test_the_first_read_of_changes_is_full_and_the_next_reads_only_changes(
            self, conn, monkeypatch, caplog):
        import logging
        self.sid = _source(conn)
        k = _Koha()
        for b in (1, 2, 3):
            k.add(b, f"T{b}")
        assert self._run(monkeypatch, k, "--changes") == 0
        assert koha_db.catalogue_state(conn, self.sid)[0] == K0
        assert len(_works(conn, self.sid)) == 3

        k.now = K0 + 10 * MIN
        k.touch_item(2, K0 + 2 * MIN, onloan="2026-12-24")
        k.add(4, "T4", at=K0 + 3 * MIN)
        k.queries.clear()
        with caplog.at_level(logging.INFO, logger="bookrs.ingestion.koha_db"):
            assert self._run(monkeypatch, k, "--changes") == 0
        assert "records changed since 2026-11-27 00:50:00" in caplog.text
        assert [r[0] for r in _works(conn, self.sid)] == ["1", "2", "3", "4"]
        page = _norm(koha_db._WORKS_PAGE.format(since=""))
        assert not any(q == page for q, _ in k.queries)          # nothing read whole
        assert koha_db.catalogue_state(conn, self.sid)[0] == K0 + 10 * MIN

    def test_full_every_reads_everything_and_catches_a_removal(self, conn, monkeypatch):
        self.sid = _source(conn)
        k = _Koha()
        for b in (1, 2):
            k.add(b, f"T{b}")
        self._run(monkeypatch, k, "--changes", "--full-every", "1d")
        del k.biblio[2]
        k.now = K0 + 10 * MIN
        self._run(monkeypatch, k, "--changes", "--full-every", "1d")
        assert [r[2] for r in _works(conn, self.sid)] == [False, False]   # changes only
        conn.execute("UPDATE sources SET catalogue_full_read_at = now() - interval '25 hours'")
        conn.commit()
        self._run(monkeypatch, k, "--changes", "--full-every", "1d")
        assert [r[2] for r in _works(conn, self.sid)] == [False, True]

    def test_without_changes_every_read_is_full(self, conn, monkeypatch):
        self.sid = _source(conn)
        k = _Koha()
        k.add(1, "T1")
        self._run(monkeypatch, k)
        self._run(monkeypatch, k)
        page = _norm(koha_db._WORKS_PAGE.format(since=""))
        assert sum(q == page for q, _ in k.queries) == 2

    def test_a_bad_full_every_is_refused_before_koha_is_asked(self, conn, monkeypatch):
        self.sid = _source(conn)
        k = _Koha()
        with pytest.raises(SystemExit):
            self._run(monkeypatch, k, "--changes", "--full-every", "daily")
        assert k.queries == []
