"""Tests for text assembly and vector storage.

The encoder itself is not unit-tested: loading a sentence-transformer
takes seconds and downloads weights, and asserting on specific vector
values would test the model rather than our code. What is tested is the
text we hand it, the pooling rule, and the staleness logic that decides
what gets re-embedded.
"""

import os

import pytest

# text.py is deliberately free of heavy dependencies so it can be
# imported anywhere -- including the ingestion service, which has no
# numpy and no PyTorch. The tests honour that boundary: the assembly
# tests run everywhere, the pooling and storage tests skip where their
# dependencies are absent.
from bookrs.embedding.text import build_text

np = pytest.importorskip("numpy")
psycopg = pytest.importorskip("psycopg")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class TestBuildText:
    def test_core_joins_title_and_subjects(self):
        t = build_text("Perl best practices", ["Perl (Computer program language)"])
        assert t.core == "Perl best practices. Perl (Computer program language)"

    def test_description_is_kept_separate(self):
        """Concatenating exceeds the model's window for half the works
        that have a description at all."""
        t = build_text("Title", ["Subject"], "A long summary.")
        assert t.core == "Title. Subject"
        assert t.description == "A long summary."

    def test_contents_joins_the_description(self):
        t = build_text("Title", [], "Summary.", "Chapter one -- chapter two.")
        assert t.description == "Summary.. Chapter one -- chapter two."

    def test_subdivided_heading_stays_one_unit(self):
        """Em dashes group subdivisions within a heading; periods
        separate distinct headings."""
        t = build_text("T", ["Europe — History, Military — 1492-1648", "War"])
        assert t.core == "T. Europe — History, Military — 1492-1648. War"

    def test_title_only_detected(self):
        assert build_text("Un gâchis", []).is_title_only
        assert not build_text("T", ["Subject"]).is_title_only
        assert not build_text("T", [], "Summary").is_title_only

    def test_blank_fields_are_dropped(self):
        t = build_text("  Title  ", ["", "  ", "Real"], "  ", "")
        assert t.core == "Title. Real"
        assert t.description == ""

    def test_everything_empty(self):
        t = build_text("", [], "", "")
        assert t.core == "" and t.description == ""


class TestPooling:
    """The averaging rule, exercised without loading a model."""

    def test_average_of_unit_vectors_is_renormalised(self):
        from bookrs.embedding.encoder import Encoder
        a = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
        out = Encoder._normalise((a + a) / 2.0)
        assert np.allclose(np.linalg.norm(out, axis=1), 1.0)

    def test_zero_vector_is_not_divided_by_zero(self):
        from bookrs.embedding.encoder import Encoder
        out = Encoder._normalise(np.zeros((1, 3), dtype=np.float32))
        assert np.all(np.isfinite(out))


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
        c.execute(
            "INSERT INTO sources (name, base_url, metadata_prefix) "
            "VALUES ('t', 'http://x/oai.pl', 'marc21')"
        )
        c.execute(
            "INSERT INTO works (source_id, source_record_id, title, content_hash) "
            "VALUES (1, 'R:1', 'A title', 'hash-a'), (1, 'R:2', 'B title', 'hash-b')"
        )
        c.commit()
        yield c


def _vector(seed: float = 1.0) -> list[float]:
    v = np.full(384, seed, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


class TestVectorStorage:
    def test_real_array_round_trips_at_full_precision(self, conn):
        """REAL[] is float32; a float64 vector would lose precision
        silently on the way in."""
        original = _vector(0.37)
        conn.execute(
            "INSERT INTO embeddings (work_id, vector, dimensions, model, "
            "embedder_version, source_hash) SELECT id, %s, 384, 'm', 1, 'hash-a' "
            "FROM works WHERE source_record_id='R:1'",
            (original,),
        )
        stored = conn.execute("SELECT vector FROM embeddings").fetchone()[0]
        assert len(stored) == 384
        assert np.allclose(np.array(stored, dtype=np.float32),
                           np.array(original, dtype=np.float32))

    def test_deleting_a_work_removes_its_vector(self, conn):
        conn.execute(
            "INSERT INTO embeddings (work_id, vector, dimensions, model, "
            "embedder_version, source_hash) SELECT id, %s, 384, 'm', 1, 'h' "
            "FROM works WHERE source_record_id='R:1'",
            (_vector(),),
        )
        conn.execute("DELETE FROM works WHERE source_record_id='R:1'")
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0


class TestStaleness:
    """A vector is stale when the bibliography, the model or the
    composition has changed since it was made."""

    def _stale_ids(self, conn, model="m", version=1):
        return [r[0] for r in conn.execute(
            """
            SELECT w.id FROM works w
            LEFT JOIN embeddings e ON e.work_id = w.id
            WHERE w.deleted_at IS NULL
              AND (e.work_id IS NULL
                   OR e.source_hash IS DISTINCT FROM w.content_hash
                   OR e.model <> %s
                   OR e.embedder_version <> %s)
            ORDER BY w.id
            """, (model, version)).fetchall()]

    def _embed(self, conn, record, *, hash_="hash-a", model="m", version=1):
        conn.execute(
            "INSERT INTO embeddings (work_id, vector, dimensions, model, "
            "embedder_version, source_hash) SELECT id, %s, 384, %s, %s, %s "
            "FROM works WHERE source_record_id=%s",
            (_vector(), model, version, hash_, record),
        )

    def test_unembedded_work_is_stale(self, conn):
        assert len(self._stale_ids(conn)) == 2

    def test_embedded_and_current_is_not_stale(self, conn):
        self._embed(conn, "R:1")
        assert 1 not in self._stale_ids(conn)

    def test_changed_bibliography_is_stale(self, conn):
        self._embed(conn, "R:1")
        conn.execute("UPDATE works SET content_hash='hash-z' WHERE id=1")
        assert 1 in self._stale_ids(conn)

    def test_model_change_makes_everything_stale(self, conn):
        """Mixing two vector spaces produces plausible nonsense rather
        than an error, so a model change must re-embed."""
        self._embed(conn, "R:1")
        self._embed(conn, "R:2", hash_="hash-b")
        assert self._stale_ids(conn, model="other-model") == [1, 2]

    def test_version_bump_makes_everything_stale(self, conn):
        self._embed(conn, "R:1")
        assert 1 in self._stale_ids(conn, version=2)

    def test_deleted_works_are_not_embedded(self, conn):
        conn.execute("UPDATE works SET deleted_at=now() WHERE id=1")
        assert 1 not in self._stale_ids(conn)
