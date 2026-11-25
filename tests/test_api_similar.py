"""The similar-books answer behind a record page.

What it says its reader evidence is: the OPAC heads the panel "Readers
also borrowed" only when this answer says the factors were learned from
loans, and "Readers who rated this also rated" when it says ratings.

And which books it lists: each book once, never the page's own book
under another edition.

Through the real application and a real database, because both come
from what is stored: the last refit, and the catalogue's records.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("numpy")
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def work():
    """Three works with factors, as a refit from before migration 5
    left them: no signal recorded."""
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
        ids = [conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, content_hash, items_hash) "
            "VALUES (%s, %s, %s, 'c', 'i') RETURNING id", (sid, str(j), f"work {j}")
        ).fetchone()[0] for j in range(3)]
        for w in ids:
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
                "factoriser_version, n_patrons) VALUES (%s, %s, %s, 2, 2, 10)",
                (w, [1.0, 0.0], [1.0, 0.0]))
        conn.commit()
    return ids[0]


def _refit_from(signal):
    """What a refit leaves: every row from one signal, a new timestamp."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute("UPDATE work_factors SET signal = %s, "
                     "trained_at = clock_timestamp()", (signal,))


@pytest.fixture
def editions():
    """A record page and its neighbours by meaning, closest first: the
    page's own book in a second edition, a book in two editions, then
    two other books. No factors, so the order is meaning alone."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    records = [
        ("The Hunger Games", "Suzanne Collins", [1.0, 0.0, 0.0]),     # the page
        ("The Hunger Games", "Suzanne Collins", [0.99, 0.14, 0.0]),   # the same book
        ("Catching Fire", "Suzanne Collins", [0.95, 0.31, 0.0]),
        ("Catching Fire", "Suzanne Collins", [0.94, 0.34, 0.0]),      # the same book
        ("Mockingjay", "Suzanne Collins", [0.90, 0.44, 0.0]),
        ("Far away", "Someone Else", [0.0, 0.0, 1.0]),
    ]
    with conn:
        conn.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k') "
                           "RETURNING id").fetchone()[0]
        ids = []
        for j, (title, author, vector) in enumerate(records):
            wid = conn.execute(
                "INSERT INTO works (source_id, source_record_id, title, authors, "
                "content_hash, items_hash) VALUES (%s, %s, %s, %s, 'c', 'i') RETURNING id",
                (sid, f"K:{j}", title, [author])).fetchone()[0]
            conn.execute(
                "INSERT INTO embeddings (work_id, vector, dimensions, model, "
                "embedder_version, source_hash) VALUES (%s, %s, 3, 'test', 1, 'c')",
                (wid, vector))
            ids.append(wid)
        conn.commit()
    return ids


@pytest.fixture
def client(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    from bookrs.api import main
    with TestClient(main.app) as c:
        yield c


class TestSimilarNamesItsEvidence:
    def test_factors_learned_from_ratings_are_reported_as_ratings(self, work, client):
        _refit_from("ratings")
        body = client.get(f"/works/{work}/similar").json()
        assert body["collaborative_signal"] == "ratings"

    def test_factors_learned_from_loans_are_reported_as_loans(self, work, client):
        _refit_from("loans")
        body = client.get(f"/works/{work}/similar").json()
        assert body["collaborative_signal"] == "loans"

    def test_factors_from_before_the_record_report_null(self, work, client):
        """Sent, and null: the page then claims neither borrowing nor
        rating, rather than guessing which."""
        body = client.get(f"/works/{work}/similar").json()
        assert "collaborative_signal" in body
        assert body["collaborative_signal"] is None

    def test_a_refit_is_noticed_without_a_restart(self, work, client):
        """The factors are cached in the API process. A refit from the
        other signal must change the answer at once, or a page goes on
        claiming borrowing after the factors were relearned from
        ratings."""
        _refit_from("loans")
        assert client.get(f"/works/{work}/similar").json()["collaborative_signal"] == "loans"
        _refit_from("ratings")
        assert client.get(f"/works/{work}/similar").json()["collaborative_signal"] == "ratings"


class TestOneBookOnePlace:
    """Editions are separate records in a real catalogue: the UCSD slice
    has 17 books in two. A record page must still name each book once,
    and never offer the page's own book again as similar to itself."""

    def test_each_book_appears_once(self, editions, client):
        body = client.get(f"/works/{editions[0]}/similar", params={"limit": 2}).json()
        assert [w["title"] for w in body["results"]] == ["Catching Fire", "Mockingjay"]
        assert body["count"] == 2

    def test_the_best_ranked_edition_is_the_one_kept(self, editions, client):
        body = client.get(f"/works/{editions[0]}/similar", params={"limit": 2}).json()
        assert body["results"][0]["id"] == editions[2]

    def test_the_pages_own_book_is_not_offered_as_similar(self, editions, client):
        ids = [w["id"] for w in client.get(f"/works/{editions[0]}/similar").json()["results"]]
        assert editions[1] not in ids and editions[0] not in ids

    def test_a_full_panel_survives_the_editions(self, editions, client):
        """Two records dropped, and the panel still holds the three
        other books it was asked for."""
        body = client.get(f"/works/{editions[0]}/similar", params={"limit": 3}).json()
        assert [w["title"] for w in body["results"]] == ["Catching Fire", "Mockingjay", "Far away"]
