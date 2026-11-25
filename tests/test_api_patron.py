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


class TestPopular:
    """What a visitor sees: public, the same for everyone."""

    def test_most_rated_first_one_edition_each_no_token_needed(self, client, library, monkeypatch):
        sid, ids = library
        monkeypatch.delenv("BOOKRS_SERVICE_TOKEN")
        r = client.get("/works/popular", params={"source_id": sid, "limit": 3})
        assert r.status_code == 200
        assert [w["id"] for w in r.json()["results"]] == [ids[10], ids[0], ids[1]]

    def test_it_is_not_mistaken_for_a_work_id(self, client, library):
        assert client.get("/works/popular", params={"source_id": library[0]}).status_code == 200

    def test_a_library_without_factors_says_so(self, client, library):
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("DELETE FROM work_factors")
        assert client.get("/works/popular", params={"source_id": library[0]}).status_code == 503


class _FakeEncoder:
    """Every query means [1, 0, 0]: what is under test is the order."""

    def encode(self, texts):
        return np.array([[1.0, 0.0, 0.0]] * len(texts), dtype=np.float32)


@pytest.fixture
def searchable(library, client, monkeypatch):
    """The library above, with meaning vectors.

    By meaning alone the B books win (cosine .85, .84) over A6 and A7
    (.80, .78); every other book is far. The patron rated A1-A5, so
    taste favours group A.
    """
    sid, ids = library
    near = {ids[7]: [0.85, 0.53, 0.0], ids[8]: [0.84, 0.54, 0.0],
            ids[5]: [0.80, 0.60, 0.0], ids[6]: [0.78, 0.62, 0.0]}
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        for w in ids:
            conn.execute(
                "INSERT INTO embeddings (work_id, vector, dimensions, model, "
                "embedder_version, source_hash) VALUES (%s, %s, 3, 'test', 1, 'c')",
                (w, near.get(w, [0.0, 0.0, 1.0])))
    from bookrs.api import main
    monkeypatch.setattr(main, "_get_encoder", lambda: _FakeEncoder())
    main._query_vectors = type(main._query_vectors)()
    return sid, ids


def _search(client, sid, ref=REF, token=TOKEN, **params):
    headers = {"X-BookRS-Token": token} if token else {}
    if ref:
        headers["X-BookRS-Patron"] = ref
    return client.get("/patrons/me/search", params={"q": "anything", "source_id": sid, **params},
                      headers=headers)


class TestPersonalSearch:
    def test_plain_search_puts_the_closest_meaning_first(self, client, searchable):
        sid, ids = searchable
        r = client.get("/search/semantic", params={"q": "anything", "source_id": sid, "limit": 4})
        assert [w["id"] for w in r.json()["results"]] == [ids[7], ids[8], ids[5], ids[6]]

    def test_the_patrons_taste_reorders_what_the_words_found(self, client, searchable):
        sid, ids = searchable
        body = _search(client, sid, limit=4).json()
        assert body["method"] == "full" and body["ratings_used"] == 5
        got = [w["id"] for w in body["results"]]
        # A6 and A7 rise above both B books; between the two B books,
        # almost tied on meaning, taste decides too.
        assert got[:2] == [ids[5], ids[6]] and set(got[2:]) == {ids[7], ids[8]}

    def test_each_result_keeps_its_closeness_in_meaning(self, client, searchable):
        sid, ids = searchable
        first = _search(client, sid, limit=1).json()["results"][0]
        assert first["id"] == ids[5] and first["score"] == pytest.approx(0.80, abs=1e-4)

    def test_a_patron_with_no_ratings_gets_the_plain_order_said_as_such(self, client, searchable):
        sid, ids = searchable
        body = _search(client, sid, ref="e" * 32, limit=4).json()
        assert body["method"] == "none" and body["ratings_used"] == 0
        assert [w["id"] for w in body["results"]] == [ids[7], ids[8], ids[5], ids[6]]

    def test_taste_never_brings_in_a_book_the_words_did_not_find(self, client, searchable, monkeypatch):
        from bookrs.api import main
        sid, ids = searchable
        monkeypatch.setattr(main, "PERSONAL_SEARCH_POOL", 2)
        got = [w["id"] for w in _search(client, sid, limit=10).json()["results"]]
        assert sorted(got) == sorted([ids[7], ids[8]])

    def test_a_close_match_is_reordered_and_says_so(self, client, searchable):
        sid, _ = searchable
        assert _search(client, sid, limit=4).json()["reordered"] is True

    def test_words_that_found_no_close_match_keep_the_meaning_order(
            self, client, searchable, monkeypatch):
        """Below the bar every candidate is about as far from the words
        as the next, and taste alone would choose the order: a failed
        search turned into a recommendation list ("the art of coding"
        once put a patron's favourite romance first). The plain order is
        kept, and said to be kept; the method still says who asked."""
        from bookrs.api import main
        sid, ids = searchable
        monkeypatch.setattr(main, "PERSONAL_SEARCH_MIN_MATCH", 0.9)  # best here: 0.85
        body = _search(client, sid, limit=4).json()
        assert body["reordered"] is False and body["method"] == "full"
        assert [w["id"] for w in body["results"]] == [ids[7], ids[8], ids[5], ids[6]]

    def test_a_patron_with_no_ratings_is_not_reordered(self, client, searchable):
        sid, _ = searchable
        assert _search(client, sid, ref="e" * 32, limit=4).json()["reordered"] is False

    def test_taste_stops_at_the_opacs_own_bar_for_a_close_match(self):
        """The page calls a match close at data-min-score. Were the two
        bars apart, a page could show a list ordered by the patron's
        ratings under "no book is a close match"."""
        from pathlib import Path

        from bookrs.api import main
        widget = (Path(main.__file__).parent / "static" / "widget.js").read_text(encoding="utf-8")
        assert 'getAttribute("data-min-score") || "0.55"' in widget
        assert main.PERSONAL_SEARCH_MIN_MATCH == 0.55

    def test_it_is_refused_like_every_patron_route(self, client, searchable):
        sid, _ = searchable
        assert _search(client, sid, token="guess").status_code == 401
        assert _search(client, sid, ref="12345").status_code == 400
        assert _search(client, sid).headers["cache-control"] == "no-store, private"

    def test_an_empty_index_answers_without_loading_the_encoder(self, client, library, monkeypatch):
        from bookrs.api import main
        sid, _ = library
        def boom():
            raise AssertionError("encoder loaded for an empty index")
        monkeypatch.setattr(main, "_get_encoder", boom)
        main._query_vectors = type(main._query_vectors)()
        body = _search(client, sid).json()
        assert body["results"] == [] and body["method"] == "full"
