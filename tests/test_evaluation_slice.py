"""Pinning an evaluation to the fixture's slice.

The report's numbers were measured while the library held only the
thesis's slice: 2,000 books and the readers with ten or more ratings
among them. Once the library holds the full catalogue, every evaluation
given ``--slice koha_biblios.tsv`` must see that population exactly --
the same readers, books and ratings, in the same order -- or the
numbers it prints are a different experiment's.
"""

import os

import numpy as np
import psycopg
import pytest

from bookrs.evaluation.thesis import load, restrict, slice_works

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
MIN = 3          # the thesis's ten, scaled down to a small library


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


def _library(conn, records, prefix="") -> tuple[int, dict[str, int]]:
    sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('koha', 'mysql://k') "
                       "RETURNING id").fetchone()[0]
    ids = _add_works(conn, sid, records, prefix)
    return sid, ids


def _add_works(conn, sid, records, prefix="") -> dict[str, int]:
    ids = {}
    for rid in records:
        ids[rid] = conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, content_hash, items_hash) "
            "VALUES (%s, %s, %s, 'c', 'i') RETURNING id",
            (sid, f"{prefix}{rid}", f"work {rid}")).fetchone()[0]
    conn.commit()
    return ids


def _rate(conn, sid, ids, rows):
    for ref, rid, stars, reviewed in rows:
        conn.execute("INSERT INTO ratings (source_id, patron_ref, work_id, rating, reviewed) "
                     "VALUES (%s, %s, %s, %s, %s)", (sid, ref, ids[rid], stars, reviewed))
    conn.commit()


def _biblios(tmp_path, records) -> str:
    p = tmp_path / "koha_biblios.tsv"
    p.write_text("ucsd_book_id\tbiblionumber\n"
                 + "".join(f"u{r}\t{r}\n" for r in records), encoding="utf-8")
    return str(p)


SLICE = ("1", "2", "3", "4")
# The slice as the fixture cut it: every reader has MIN or more ratings
# among its books.
SLICE_RATINGS = [
    ("pa", "1", 5, True), ("pa", "2", 4, False), ("pa", "3", 3, False),
    ("pb", "2", 2, False), ("pb", "3", 5, True), ("pb", "4", 4, False), ("pb", "1", 1, False),
    ("pc", "4", 5, False), ("pc", "1", 4, True), ("pc", "3", 4, False),
]


def _same(a, b):
    assert a.patrons == b.patrons
    for f in ("works", "u", "i", "r", "d"):
        np.testing.assert_array_equal(getattr(a, f), getattr(b, f), err_msg=f)


class TestTheSliceSurvivesAFullCatalogue:
    def test_the_pinned_load_is_the_slice_exactly(self, conn, tmp_path):
        sid, ids = _library(conn, SLICE)
        _rate(conn, sid, ids, SLICE_RATINGS)
        before = restrict(load(conn, sid), MIN)

        # The full catalogue arrives: new books, the slice's readers
        # rating them, and new readers -- some of whom rated a slice book,
        # but fewer than MIN of them, which is why the slice left them out.
        ids |= _add_works(conn, sid, ("5", "6", "7"))
        _rate(conn, sid, ids, [
            ("pa", "5", 5, True), ("pa", "6", 2, False), ("pc", "7", 3, False),
            ("pd", "1", 5, False), ("pd", "2", 4, False), ("pd", "5", 3, False),
            ("pd", "6", 2, False), ("pe", "7", 4, True), ("pe", "6", 1, False),
            ("demo", "3", 5, False),
        ])
        assert restrict(load(conn, sid), MIN).n != before.n     # the library did change

        after = restrict(load(conn, sid, slice_works(conn, sid, _biblios(tmp_path, SLICE))), MIN)
        _same(before, after)

    def test_only_the_given_works_are_loaded(self, conn):
        sid, ids = _library(conn, SLICE)
        _rate(conn, sid, ids, SLICE_RATINGS)
        data = load(conn, sid, np.array([ids["1"], ids["4"]]))
        assert set(data.works.tolist()) == {ids["1"], ids["4"]}
        assert data.n == sum(r[1] in ("1", "4") for r in SLICE_RATINGS)

    def test_without_a_slice_everything_is_loaded(self, conn):
        sid, ids = _library(conn, SLICE)
        _rate(conn, sid, ids, SLICE_RATINGS)
        assert load(conn, sid).n == len(SLICE_RATINGS)

    def test_two_pinned_loads_in_one_session_do_not_mix(self, conn):
        sid, ids = _library(conn, SLICE)
        _rate(conn, sid, ids, SLICE_RATINGS)
        load(conn, sid, np.array([ids["1"], ids["2"]]))
        second = load(conn, sid, np.array([ids["4"]]))
        assert set(second.works.tolist()) == {ids["4"]}


class TestSliceWorks:
    def test_records_are_found_by_biblionumber(self, conn, tmp_path):
        sid, ids = _library(conn, SLICE + ("9",))
        got = slice_works(conn, sid, _biblios(tmp_path, SLICE))
        np.testing.assert_array_equal(got, sorted(ids[r] for r in SLICE))

    def test_a_prefixed_record_id_is_found_too(self, conn, tmp_path):
        """Records read over OAI-PMH carry an archive prefix."""
        sid, ids = _library(conn, SLICE, prefix="oai:koha:")
        got = slice_works(conn, sid, _biblios(tmp_path, SLICE))
        assert len(got) == len(SLICE)

    def test_a_slice_record_missing_from_the_library_stops_the_run(self, conn, tmp_path):
        sid, _ = _library(conn, ("1", "2"))
        with pytest.raises(SystemExit, match="2 of the 4 records"):
            slice_works(conn, sid, _biblios(tmp_path, SLICE))

    def test_a_deleted_record_does_not_count_as_found(self, conn, tmp_path):
        sid, ids = _library(conn, SLICE)
        conn.execute("UPDATE works SET deleted_at = now() WHERE id = %s", (ids["4"],))
        conn.commit()
        with pytest.raises(SystemExit, match="1 of the 4 records"):
            slice_works(conn, sid, _biblios(tmp_path, SLICE))
