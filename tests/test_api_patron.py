"""Tests for the one API route that answers differently per patron.

Through the real application and a real database: what is under test
is who may ask and what comes back, and both live in the route.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")
np = pytest.importorskip("numpy")
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TOKEN = "shared-between-koha-and-bookrs"
REF = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def library():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with conn:
        conn.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k') "
                           "RETURNING id").fetchone()[0]
        titles = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "B1", "B2", "B3",
                  "Catching Fire", "Catching Fire"]
        ids = [conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, authors, content_hash, "
            "items_hash) VALUES (%s, %s, %s, %s, 'c', 'i') RETURNING id",
            (sid, str(100 + j), t, ["Someone"] if t == "Catching Fire" else [f"Author {t}"])
        ).fetchone()[0] for j, t in enumerate(titles)]
        # group A along one axis, group B along the other; the two
        # editions are the most-rated records
        for j, w in enumerate(ids):
            v = [1.0, 0.05 * j] if j < 7 else ([0.05 * j, 1.0] if j < 10 else [0.6, 0.6])
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
                "factoriser_version, n_patrons) VALUES (%s, %s, %s, 2, 2, %s)",
                (w, v, v, 1000 if j >= 10 else 100 - j))
        for w in ids[:5]:
            conn.execute("INSERT INTO ratings (source_id, patron_ref, work_id, rating) "
                         "VALUES (%s, %s, %s, 5)", (sid, REF, w))
        conn.commit()
    return sid, ids


@pytest.fixture
def client(library, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BOOKRS_SERVICE_TOKEN", TOKEN)
    from bookrs.api import main
    main._spaces = main._SpaceCache()
    with TestClient(main.app) as c:
        yield c


def _get(client, sid, token=TOKEN, ref=REF, **params):
    headers = {}
    if token is not None:
        headers["X-BookRS-Token"] = token
    if ref is not None:
        headers["X-BookRS-Patron"] = ref
    return client.get("/patrons/me/recommendations",
                      params={"source_id": sid, **params}, headers=headers)


class TestWhoMayAsk:
    def test_no_token_is_refused(self, client, library):
        assert _get(client, library[0], token=None).status_code == 401

    def test_a_wrong_token_is_refused(self, client, library):
        assert _get(client, library[0], token="guess").status_code == 401

    def test_the_route_is_off_until_a_token_is_configured(self, client, library, monkeypatch):
        monkeypatch.delenv("BOOKRS_SERVICE_TOKEN")
        assert _get(client, library[0]).status_code == 503

    def test_a_borrower_number_is_not_accepted_as_a_patron(self, client, library):
        for bad in ("12345", "", REF.upper(), REF + "0", "../" + REF[3:]):
            assert _get(client, library[0], ref=bad).status_code == 400, bad

    def test_the_route_is_not_in_the_public_schema(self, client):
        assert "/patrons/me/recommendations" not in client.get("/openapi.json").json()["paths"]


class TestWhatComesBack:
    def test_a_patron_with_five_ratings_gets_the_rest_of_their_group(self, client, library):
        sid, ids = library
        r = _get(client, sid, limit=2)
        assert r.status_code == 200
        body = r.json()
        assert body["method"] == "full" and body["ratings_used"] == 5
        assert [w["id"] for w in body["results"]] == ids[5:7]
        assert body["results"][0]["biblionumber"] == "105"
        assert "score" in body["results"][0]

    def test_rated_works_are_never_returned(self, client, library):
        sid, ids = library
        got = {w["id"] for w in _get(client, sid, limit=50).json()["results"]}
        assert not got & set(ids[:5])

    def test_an_unknown_patron_gets_the_most_rated_works_one_edition_each(self, client, library):
        sid, ids = library
        body = _get(client, sid, ref="f" * 32, limit=3).json()
        assert body["method"] == "none" and body["ratings_used"] == 0
        assert [w["id"] for w in body["results"]] == [ids[10], ids[0], ids[1]]

    def test_the_answer_is_marked_private(self, client, library):
        r = _get(client, library[0])
        assert r.headers["cache-control"] == "no-store, private"

    def test_a_library_without_factors_says_so(self, client, library):
        sid, _ = library
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("DELETE FROM work_factors")
        assert _get(client, sid).status_code == 503

    def test_a_refit_is_picked_up_without_a_restart(self, client, library):
        sid, ids = library
        assert _get(client, sid, limit=1).json()["results"][0]["id"] == ids[5]
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("UPDATE work_factors SET raw_vector = ARRAY[1.0, 0.0]::real[], "
                         "trained_at = now() + interval '1 minute' WHERE work_id = %s",
                         (ids[6],))
            conn.execute("UPDATE work_factors SET raw_vector = ARRAY[0.0, 1.0]::real[] "
                         "WHERE work_id = %s", (ids[5],))
        assert _get(client, sid, limit=1).json()["results"][0]["id"] == ids[6]
