"""The API's in-process vectors (bookrs/api/vectors.py) and how they keep
up with the database without reloading everything.

Against a real database: what is under test is what the queries see
while rows are added, rewritten, withdrawn and removed.
"""

import os
import struct
import threading
import time

import pytest

psycopg = pytest.importorskip("psycopg")
np = pytest.importorskip("numpy")

from bookrs.api import vectors  # noqa: E402
from bookrs.db.arrays import decode_float4_array, vector_cursor  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


# -- the binary REAL[] reader ---------------------------------------------

def _wire(values, ndim=1, has_nulls=0):
    head = struct.pack("!iiI", ndim, has_nulls, 700)
    if ndim == 0:
        return head
    body = b"".join(struct.pack("!if", 4, v) for v in values)
    return head + struct.pack("!ii", len(values), 1) + body


class TestBinaryArrays:
    def test_reads_the_numbers_as_float32(self):
        got = decode_float4_array(_wire([1.5, -2.25, 0.0]))
        assert got.dtype == np.float32
        assert got.tolist() == [1.5, -2.25, 0.0]

    def test_an_empty_array_is_an_empty_vector(self):
        assert decode_float4_array(_wire([], ndim=0)).shape == (0,)

    def test_a_null_is_refused(self):
        with pytest.raises(ValueError):
            decode_float4_array(_wire([1.0], has_nulls=1))

    def test_two_dimensions_are_refused(self):
        with pytest.raises(ValueError):
            decode_float4_array(struct.pack("!iiI", 2, 0, 700) + b"\0" * 16)

    def test_round_trip_through_postgres(self, conn):
        with vector_cursor(conn) as cur:
            vec, n, text, names = cur.execute(
                "SELECT ARRAY[0.25, -1, 3e-8]::real[], 7::bigint, 'x'::text, "
                "ARRAY['a','b']::text[]").fetchone()
        assert vec.dtype == np.float32
        assert vec.tolist() == pytest.approx([0.25, -1.0, 3e-8])
        # every other column as psycopg gives it
        assert (n, text, names) == (7, "x", ["a", "b"])

    def test_other_cursors_are_unchanged(self, conn):
        with vector_cursor(conn):
            pass
        assert conn.execute("SELECT ARRAY[1.0]::real[]").fetchone()[0] == [1.0]


# -- the catalogue's vectors ----------------------------------------------

@pytest.fixture
def conn():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        c = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with c:
        c.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k')")
        c.commit()
        yield c


def _work(conn, n, source_id=1):
    return conn.execute(
        "INSERT INTO works (source_id, source_record_id, title, content_hash, items_hash) "
        "VALUES (%s, %s, %s, 'c', 'i') RETURNING id", (source_id, f"r{n}", f"Book {n}")
    ).fetchone()[0]


def _unit(seed, d=4):
    v = np.random.default_rng(seed).normal(size=d)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _embed(conn, work_id, seed, title_only=False, d=4):
    """One embedding run's batch: its own transaction, as the pipeline
    commits each batch."""
    conn.execute(
        "INSERT INTO embeddings (work_id, vector, dimensions, model, embedder_version, "
        "source_hash, is_title_only) VALUES (%s, %s, %s, 'm', 1, 'h', %s) "
        "ON CONFLICT (work_id) DO UPDATE SET vector = EXCLUDED.vector, "
        "dimensions = EXCLUDED.dimensions, created_at = now()",
        (work_id, _unit(seed, d).tolist(), d, title_only))
    conn.commit()


def _library(conn, n=5):
    ids = [_work(conn, j) for j in range(n)]
    conn.commit()
    for j, w in enumerate(ids):
        _embed(conn, w, seed=j)
    return ids


def _row(snap, work_id):
    return int(np.flatnonzero(snap.ids == work_id)[0])


def _cache(**kw):
    kw.setdefault("probe_every", 0)
    kw.setdefault("full_every", 0)
    return vectors.CatalogueVectors(**kw)


class TestFirstLoad:
    def test_every_embedding_is_held_as_float32(self, conn):
        ids = _library(conn)
        snap = _cache().get(conn)
        assert sorted(snap.ids.tolist()) == ids
        assert snap.matrix.dtype == np.float32 and snap.dimensions == 4
        assert np.allclose(snap.matrix[_row(snap, ids[2])], _unit(2))
        assert snap.live.all()

    def test_it_is_read_a_page_at_a_time(self, conn, monkeypatch):
        """At 883,000 books the rows are handed over in pages; the
        result must not depend on where a page ends."""
        monkeypatch.setattr(vectors, "PAGE", 2)
        ids = _library(conn, n=7)
        snap = _cache().get(conn)
        assert sorted(snap.ids.tolist()) == ids
        for j, w in enumerate(ids):
            assert np.allclose(snap.matrix[_row(snap, w)], _unit(j))

    def test_a_withdrawn_book_is_held_but_masked(self, conn):
        ids = _library(conn)
        conn.execute("UPDATE works SET deleted_at = now() WHERE id = %s", (ids[1],))
        conn.commit()
        snap = _cache().get(conn)
        assert not snap.live[_row(snap, ids[1])]
        assert snap.live.sum() == len(ids) - 1

    def test_an_empty_catalogue(self, conn):
        snap = _cache().get(conn)
        assert len(snap) == 0


class TestKeepingUp:
    def test_a_new_embedding_is_added_without_a_reload(self, conn):
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        loaded = cache._loaded_at
        new = _work(conn, 99)
        conn.commit()
        _embed(conn, new, seed=99)
        snap = cache.get(conn)
        assert cache._loaded_at == loaded, "reloaded everything for one book"
        assert len(snap) == len(ids) + 1
        assert np.allclose(snap.matrix[_row(snap, new)], _unit(99))

    def test_a_book_embedded_again_is_overwritten_not_doubled(self, conn):
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        loaded = cache._loaded_at
        _embed(conn, ids[3], seed=42)
        snap = cache.get(conn)
        assert cache._loaded_at == loaded
        assert len(snap) == len(ids)
        assert np.allclose(snap.matrix[_row(snap, ids[3])], _unit(42))

    def test_an_embedding_run_in_batches(self, conn):
        """The pipeline commits every 512 books; the API looks between
        batches and must end with every book once."""
        cache = _cache()
        cache.get(conn)
        ids = [_work(conn, j) for j in range(12)]
        conn.commit()
        for batch in range(0, 12, 4):
            for j in range(batch, batch + 4):
                _embed(conn, ids[j], seed=j)
            cache.get(conn)
        snap = cache.get(conn)
        assert sorted(snap.ids.tolist()) == ids
        assert snap.dimensions == 4      # the empty start took the first vector's size

    def test_withdrawn_and_restored_without_a_reload(self, conn):
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        loaded = cache._loaded_at
        conn.execute("UPDATE works SET deleted_at = now() WHERE id = %s", (ids[0],))
        conn.commit()
        snap = cache.get(conn)
        assert not snap.live[_row(snap, ids[0])]
        conn.execute("UPDATE works SET deleted_at = NULL WHERE id = %s", (ids[0],))
        conn.commit()
        snap = cache.get(conn)
        assert snap.live.all()
        assert cache._loaded_at == loaded

    def test_removed_embeddings_start_a_full_reload(self, conn):
        """Only a reload can see a row that left: a count that no longer
        adds up is how it is noticed, even when a new row arrived at the
        same time."""
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        conn.execute("DELETE FROM embeddings WHERE work_id = %s", (ids[0],))
        conn.commit()
        new = _work(conn, 77)
        conn.commit()
        _embed(conn, new, seed=77)
        snap = cache.get(conn)
        assert ids[0] not in snap.ids.tolist()
        assert new in snap.ids.tolist()
        assert len(snap) == len(ids)

    def test_many_changes_are_reloaded_rather_than_applied_in_a_request(self, conn, monkeypatch):
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        loaded = cache._loaded_at
        monkeypatch.setattr(vectors, "INLINE_LIMIT", 1)
        _embed(conn, ids[0], seed=50)
        _embed(conn, ids[1], seed=51)
        snap = cache.get(conn)
        assert cache._loaded_at != loaded
        assert np.allclose(snap.matrix[_row(snap, ids[1])], _unit(51))

    def test_a_new_model_s_vectors_replace_the_old_size(self, conn):
        """Embedded again with a model of another size, books not yet
        redone are left out rather than mixed with the new ones, and the
        count still adds up afterwards (no reload at every look)."""
        ids = _library(conn)
        cache = _cache()
        cache.get(conn)
        _embed(conn, ids[0], seed=60, d=6)
        snap = cache.get(conn)
        assert snap.dimensions == 6
        assert snap.ids.tolist() == [ids[0]]
        loaded = cache._loaded_at
        cache.get(conn)
        assert cache._loaded_at == loaded

    def test_the_periodic_full_reload(self, conn):
        _library(conn)
        cache = _cache(full_every=1e-9)
        cache.get(conn)
        loaded = cache._loaded_at
        cache.get(conn)
        assert cache._loaded_at != loaded


class TestHowOftenItLooks:
    def test_not_more_than_once_per_interval(self, conn):
        _library(conn)
        cache = _cache(probe_every=3600)
        before = cache.get(conn)
        new = _work(conn, 99)
        conn.commit()
        _embed(conn, new, seed=99)
        assert cache.get(conn) is before
        cache.probe_every = 0
        assert new in cache.get(conn).ids.tolist()

    def test_a_request_does_not_wait_for_another_s_look(self, conn):
        _library(conn)
        cache = _cache()
        held = cache.get(conn)
        with cache._lock:                # someone else is looking
            assert cache.get(conn) is held


class TestReloadInTheBackground:
    def test_the_held_vectors_answer_until_the_reload_is_done(self, conn):
        ids = _library(conn)
        go = threading.Event()

        class Slow:
            def __enter__(self):
                go.wait(timeout=10)
                self.c = psycopg.connect(TEST_DATABASE_URL)
                return self.c

            def __exit__(self, *exc):
                self.c.close()

        cache = _cache()
        cache.connect = Slow
        old = cache.get(conn)
        conn.execute("DELETE FROM embeddings WHERE work_id = %s", (ids[0],))
        conn.commit()

        assert cache.get(conn) is old          # starts the reload, answers at once
        assert cache.reloading
        assert cache.get(conn) is old          # and does not start another
        go.set()
        deadline = time.monotonic() + 10
        while cache.reloading and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not cache.reloading
        assert ids[0] not in cache.get(conn).ids.tolist()


class TestBuffers:
    def test_growing_leaves_an_earlier_snapshot_as_it_was(self):
        held = vectors._Buffers(2, capacity=2)
        held.put(10, np.array([1, 0], np.float32), False, 1, True)
        held.put(11, np.array([0, 1], np.float32), False, 1, True)
        before = held.snapshot()
        for w in range(12, 40):
            held.put(w, np.array([w, w], np.float32), True, 2, True)
        assert before.ids.tolist() == [10, 11]
        assert before.matrix.tolist() == [[1, 0], [0, 1]]
        after = held.snapshot()
        assert len(after) == 30 and after.matrix[-1].tolist() == [39, 39]

    def test_a_vector_of_another_size_is_refused(self):
        held = vectors._Buffers(2, capacity=4)
        held.put(1, np.array([1, 0], np.float32), False, 1, True)
        assert not held.put(2, np.array([1, 0, 0], np.float32), False, 1, True)
        assert held.n == 1


class TestTheFactorCachesLookLessOften:
    def _refit(self, conn, signal):
        w = _work(conn, 1)
        conn.execute("DELETE FROM work_factors")
        conn.execute(
            "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
            "factoriser_version, n_patrons, signal) VALUES (%s, %s, %s, 2, 2, 5, %s)",
            (w, [1.0, 0.0], [1.0, 0.0], signal))
        conn.commit()

    def test_similar_books_factors(self, conn):
        from bookrs.api.queries import _FactorCache
        self._refit(conn, "loans")
        cache = _FactorCache(probe_every=3600)
        assert cache.signal(conn) == "loans"
        conn.execute("UPDATE work_factors SET signal = 'ratings', trained_at = now() + interval '1 s'")
        conn.commit()
        assert cache.signal(conn) == "loans"
        cache.probe_every = 0
        assert cache.signal(conn) == "ratings"

    def test_factors_arrive_as_float32(self, conn):
        from bookrs.api.queries import _FactorCache
        self._refit(conn, "ratings")
        (vector, n_patrons), = _FactorCache(probe_every=0).get(conn).values()
        assert vector.dtype == np.float32 and vector.tolist() == [1.0, 0.0]
        assert n_patrons == 5


class TestHealthShowsWhatIsHeld:
    def test_the_vectors_held_catch_up_with_the_embeddings(self, conn, monkeypatch):
        pytest.importorskip("fastapi")
        pytest.importorskip("psycopg_pool")
        from fastapi.testclient import TestClient

        from bookrs.api import main
        _library(conn)
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setattr(main, "_get_encoder", lambda: None)
        with TestClient(main.app) as c:
            main.app.state.warm.join(timeout=10)
            body = c.get("/health").json()
        assert body["vectors_held"] == body["embeddings"] == 5
        assert body["vectors_reloading"] is False
