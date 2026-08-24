"""Tests for the query layer behind the API.

Against a real database: trigram matching, accent folding and array
handling are the behaviour under test, and none of them survive being
mocked.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")
np = pytest.importorskip("numpy")

from bookrs.api.queries import get_work, search_exact, similar_works

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


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
        c.execute("INSERT INTO sources (name, base_url, metadata_prefix) "
                  "VALUES ('t','http://x/oai.pl','marc21')")
        c.execute(
            """
            INSERT INTO works (source_id, source_record_id, title, authors,
                               subjects, isbns, languages, content_hash)
            VALUES
              (1,'R:1','Perl best practices', ARRAY['Conway, Damian'],
               ARRAY['Perl (Computer program language)'],
               ARRAY['0596001738']::varchar(13)[], ARRAY['eng']::varchar(3)[], 'h1'),
              (1,'R:2','La mythologie Grecque', ARRAY['Mira Pons, Michèle'],
               ARRAY['Mythologie'], '{}'::varchar(13)[],
               ARRAY['fre']::varchar(3)[], 'h2'),
              (1,'R:3','The C programming language',
               ARRAY['Kernighan, Brian W.','Ritchie, Dennis M.'],
               ARRAY['C (Computer program language)'],
               ARRAY['0131103628']::varchar(13)[], ARRAY['eng']::varchar(3)[], 'h3')
            """
        )
        c.execute(
            "INSERT INTO items (work_id, barcode, due_date) VALUES "
            "(1,'b1',NULL), (1,'b2','2026-09-06'), (2,'b3','2026-09-06')"
        )
        c.commit()
        yield c


def _vec(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=384).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


class TestExactSearch:
    def test_isbn_matches_exactly(self, conn):
        results = search_exact(conn, "0596001738")
        assert [w.title for w in results] == ["Perl best practices"]
        assert results[0].score == 1.0

    def test_isbn_with_punctuation(self, conn):
        assert search_exact(conn, "059-600-1738")[0].title == "Perl best practices"

    def test_accent_folded_title(self, conn):
        """A patron typing without accents must still find the record --
        in a predominantly French catalogue this is most of them."""
        assert search_exact(conn, "mythologie grecque")[0].title == "La mythologie Grecque"

    def test_typo_tolerated(self, conn):
        assert search_exact(conn, "perl best practises")[0].title == "Perl best practices"

    def test_surname_matches_a_multi_author_record(self, conn):
        """Plain trigram similarity scores 'kernighan' against
        'kernighan, brian w. ritchie, dennis m.' at 0.294, below
        pg_trgm's default. word_similarity scores it 1.0."""
        assert search_exact(conn, "Kernighan")[0].title == "The C programming language"

    def test_nonsense_returns_nothing(self, conn):
        assert search_exact(conn, "zzzzqqqxyz") == []

    def test_limit_is_respected(self, conn):
        assert len(search_exact(conn, "programming", limit=1)) <= 1


class TestAvailability:
    def test_counts_copies_in_and_out(self, conn):
        work = search_exact(conn, "0596001738")[0]
        assert work.copies_total == 2 and work.copies_available == 1
        assert work.is_available

    def test_all_copies_on_loan(self, conn):
        work = search_exact(conn, "mythologie grecque")[0]
        assert work.copies_total == 1 and work.copies_available == 0
        assert not work.is_available

    def test_work_with_no_copies(self, conn):
        """Normal in real catalogues: on-order titles, electronic
        resources, catalogue-only records."""
        work = search_exact(conn, "Kernighan")[0]
        assert work.copies_total == 0 and not work.is_available


class TestSimilarWorks:
    def _embed(self, conn, work_id, seed, title_only=False):
        conn.execute(
            "INSERT INTO embeddings (work_id, vector, dimensions, model, "
            "embedder_version, source_hash, is_title_only) "
            "VALUES (%s,%s,384,'m',1,'h',%s)",
            (work_id, _vec(seed), title_only),
        )
        conn.commit()

    def test_returns_nothing_without_an_embedding(self, conn):
        assert similar_works(conn, 1) == []

    def test_excludes_the_probe_itself(self, conn):
        for i in (1, 2, 3):
            self._embed(conn, i, seed=i)
        assert 1 not in [w.id for w in similar_works(conn, 1)]

    def test_identical_vectors_score_one(self, conn):
        self._embed(conn, 1, seed=7)
        self._embed(conn, 2, seed=7)
        results = similar_works(conn, 1)
        assert results[0].id == 2
        assert results[0].score == pytest.approx(1.0, abs=1e-5)

    def test_title_only_can_be_excluded(self, conn):
        self._embed(conn, 1, seed=1)
        self._embed(conn, 2, seed=2, title_only=True)
        self._embed(conn, 3, seed=3)
        ids = [w.id for w in similar_works(conn, 1, exclude_title_only=True)]
        assert 2 not in ids and 3 in ids

    def test_results_are_ordered_by_score(self, conn):
        for i in (1, 2, 3):
            self._embed(conn, i, seed=i)
        scores = [w.score for w in similar_works(conn, 1)]
        assert scores == sorted(scores, reverse=True)


class TestGetWork:
    def test_returns_the_work(self, conn):
        assert get_work(conn, 1).title == "Perl best practices"

    def test_missing_id(self, conn):
        assert get_work(conn, 999999) is None

    def test_deleted_work_is_not_returned(self, conn):
        conn.execute("UPDATE works SET deleted_at = now() WHERE id = 1")
        assert get_work(conn, 1) is None
