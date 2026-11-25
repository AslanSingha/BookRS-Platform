"""What the similar-books answer says its reader evidence is.

The OPAC heads a record page's similar books "Readers also borrowed"
only when this answer says the factors were learned from loans, and
"Readers who rated this also rated" when it says ratings. Through the
real application and a real database, because the answer comes from
what the last refit stored.
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
def client(work, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    from bookrs.api import main
    with TestClient(main.app) as c:
        yield c


class TestSimilarNamesItsEvidence:
    def test_factors_learned_from_ratings_are_reported_as_ratings(self, client, work):
        _refit_from("ratings")
        body = client.get(f"/works/{work}/similar").json()
        assert body["collaborative_signal"] == "ratings"

    def test_factors_learned_from_loans_are_reported_as_loans(self, client, work):
        _refit_from("loans")
        body = client.get(f"/works/{work}/similar").json()
        assert body["collaborative_signal"] == "loans"

    def test_factors_from_before_the_record_report_null(self, client, work):
        """Sent, and null: the page then claims neither borrowing nor
        rating, rather than guessing which."""
        body = client.get(f"/works/{work}/similar").json()
        assert "collaborative_signal" in body
        assert body["collaborative_signal"] is None

    def test_a_refit_is_noticed_without_a_restart(self, client, work):
        """The factors are cached in the API process. A refit from the
        other signal must change the answer at once, or a page goes on
        claiming borrowing after the factors were relearned from
        ratings."""
        _refit_from("loans")
        assert client.get(f"/works/{work}/similar").json()["collaborative_signal"] == "loans"
        _refit_from("ratings")
        assert client.get(f"/works/{work}/similar").json()["collaborative_signal"] == "ratings"
