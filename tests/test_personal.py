"""Tests for training from ratings and for placing one reader.

The policy under test was chosen from a measurement
(bookrs/evaluation/coldstart.py). These tests pin the policy and check
that the function serving it is the function that was measured.
"""

import os

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
psycopg = pytest.importorskip("psycopg")
from scipy.sparse import csr_matrix

from bookrs.recommend.als import ALSParams, normalise
from bookrs.recommend.confidence import rating_confidence
from bookrs.recommend.folding import FULL_SOLVE_FROM, FactorSpace, place_reader
from bookrs.recommend.personal import recommend, recommend_for_patron
from bookrs.recommend.ratings import build_rating_matrix

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _space(n=40, k=6, seed=0):
    rng = np.random.default_rng(seed)
    Y = rng.normal(size=(n, k)).astype(np.float32)
    return FactorSpace.from_rows(
        [(100 + j, Y[j].tolist(), int(1000 - 10 * j)) for j in range(n)])


class TestRatingConfidence:
    def test_the_thesis_formula(self):
        assert rating_confidence(5, 1) == 14.0
        assert rating_confidence(1, 0) == 3.0

    def test_it_works_on_arrays(self):
        got = rating_confidence(np.array([1.0, 5.0]), np.array([0.0, 1.0]))
        assert got.tolist() == [3.0, 14.0]


class TestRatingMatrix:
    def _arrays(self):
        # patron 0: works 10, 20, 30   patron 1: 10, 20   patron 2: 99 only
        u = np.array([0, 0, 0, 1, 1, 2], dtype=np.int32)
        w = np.array([10, 20, 30, 10, 20, 99], dtype=np.int64)
        r = np.array([5, 4, 3, 2, 1, 5], dtype=np.float32)
        d = np.array([1, 0, 0, 0, 0, 0], dtype=np.float32)
        return u, w, r, d

    def test_thin_works_and_patrons_are_removed(self):
        m, works, stats = build_rating_matrix(*self._arrays(), ALSParams())
        # work 30 has one patron, work 99 one, patron 2 one work
        assert works == [10, 20] and m.shape == (2, 2)
        assert (stats.excluded_works, stats.excluded_patrons) == (2, 1)
        assert stats.interactions == 4

    def test_cells_hold_the_thesis_confidence(self):
        m, works, _ = build_rating_matrix(*self._arrays(), ALSParams())
        assert sorted(m.toarray().ravel().tolist()) == [3.0, 5.0, 9.0, 14.0]

    def test_nothing_left_is_an_error_not_an_empty_fit(self):
        u = np.array([0, 1], dtype=np.int32)
        with pytest.raises(ValueError):
            build_rating_matrix(u, np.array([1, 2]), np.ones(2, dtype=np.float32),
                                np.zeros(2, dtype=np.float32), ALSParams())


class TestFactorSpace:
    def test_gram_is_y_transpose_y(self):
        s = _space()
        Y = s.factors.astype(np.float64)
        assert s.gram == pytest.approx(Y.T @ Y)

    def test_rows_without_raw_factors_are_left_out(self):
        s = FactorSpace.from_rows([(1, [1.0, 0.0], 5), (2, None, 9), (3, [0.0, 1.0], 2)])
        assert s.work_ids.tolist() == [1, 3]

    def test_only_old_factors_is_an_error_that_says_what_to_do(self):
        with pytest.raises(ValueError, match="Refit"):
            FactorSpace.from_rows([(1, None, 5)])


class TestPlaceReader:
    def test_no_usable_ratings_is_no_vector(self):
        s = _space()
        assert place_reader({}, s) == (None, "none")
        assert place_reader({999999: 11.0}, s) == (None, "none")

    def test_the_threshold_is_five(self):
        s = _space()
        assert FULL_SOLVE_FROM == 5
        for n, want in ((1, "few"), (4, "few"), (5, "full"), (30, "full")):
            got = place_reader({100 + j: 9.0 for j in range(n)}, s)[1]
            assert got == want, n

    def test_works_without_factors_do_not_count_towards_the_threshold(self):
        s = _space()
        conf = {100: 9.0, 101: 9.0, 102: 9.0, 103: 9.0, 555555: 9.0, 666666: 9.0}
        assert place_reader(conf, s)[1] == "few"

    def test_the_full_solve_is_hu_koren_volinsky(self):
        s = _space()
        conf = {100 + j: float(3 + 2 * j) for j in range(6)}
        x = place_reader(conf, s)[0].astype(np.float64)
        Y = s.factors.astype(np.float64)
        c, p = np.ones(len(s)), np.zeros(len(s))
        for w, v in conf.items():
            c[s.index[w]], p[s.index[w]] = v, 1.0
        assert ((Y.T * c) @ Y + 0.1 * np.eye(6)) @ x == pytest.approx(Y.T @ (c * p), abs=1e-3)

    def test_it_is_the_function_that_was_measured(self):
        """Same answers as the evaluation's batch solve, both branches."""
        from bookrs.evaluation.coldstart import fold_in

        s = _space()
        for n, gram, value in ((3, None, 3.0), (8, s.gram, None)):
            conf = {100 + j: (value or float(3 + 2 * (j % 5))) for j in range(n)}
            given = csr_matrix(
                (np.array(list(conf.values()), dtype=np.float32),
                 ([0] * n, [s.index[w] for w in conf])), shape=(1, len(s)))
            want = fold_in(given, s.factors, 0.1, gram)[0]
            assert place_reader(conf, s)[0] == pytest.approx(want, abs=1e-4), n

    def test_with_few_ratings_the_stars_are_not_used(self):
        s = _space()
        a = place_reader({100: 3.0, 101: 3.0}, s)[0]
        b = place_reader({100: 14.0, 101: 5.0}, s)[0]
        assert a == pytest.approx(b)


class TestRecommend:
    def test_no_ratings_gives_the_most_rated_works_in_order(self):
        recs, method = recommend(_space(), [], limit=5)
        assert method == "none"
        assert [r.work_id for r in recs] == [100, 101, 102, 103, 104]

    def test_rated_works_are_never_returned(self):
        s = _space()
        ratings = [(100 + j, 5, False) for j in range(12)]
        recs, method = recommend(s, ratings, limit=len(s))
        assert method == "full"
        assert not {r.work_id for r in recs} & {w for w, _, _ in ratings}
        assert len(recs) == len(s) - 12

    def test_a_rated_work_is_excluded_from_the_most_rated_list_too(self):
        s = _space()
        recs, method = recommend(s, [(999999, 5, False), (100, 5, False)], limit=3)
        assert method == "few" and 100 not in [r.work_id for r in recs]

    def test_results_are_in_descending_score_order(self):
        recs, _ = recommend(_space(), [(100 + j, 4, False) for j in range(7)], limit=10)
        scores = [r.score for r in recs]
        assert scores == sorted(scores, reverse=True) and len(recs) == 10

    def test_asking_for_more_than_exists_returns_what_exists(self):
        s = _space(n=6)
        recs, _ = recommend(s, [(100, 5, False)], limit=50)
        assert len(recs) == 5

    def test_a_reader_is_steered_towards_what_they_rated(self):
        """Two groups of works with orthogonal factors: rating group A
        must rank the rest of group A above all of group B."""
        rows = [(j, [1.0 + 0.01 * j, 0.0], 10) for j in range(10)] + \
               [(100 + j, [0.0, 1.0 + 0.01 * j], 10) for j in range(10)]
        s = FactorSpace.from_rows(rows)
        recs, _ = recommend(s, [(j, 5, False) for j in range(6)], limit=4)
        assert {r.work_id for r in recs} == {6, 7, 8, 9}


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


def _library(conn, n_works):
    sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k') "
                       "RETURNING id").fetchone()[0]
    ids = [conn.execute(
        "INSERT INTO works (source_id, source_record_id, title, content_hash, items_hash) "
        "VALUES (%s, %s, %s, 'c', 'i') RETURNING id", (sid, str(j), f"work {j}")
    ).fetchone()[0] for j in range(n_works)]
    conn.commit()
    return sid, ids


class TestAgainstTheDatabase:
    def test_migration_four_added_the_column(self, conn):
        assert conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'work_factors' AND column_name = 'raw_vector'"
        ).fetchone() == ("ARRAY",)

    def test_a_refit_from_ratings_stores_both_forms_of_each_factor(self, conn):
        pytest.importorskip("implicit")
        from bookrs.recommend.pipeline import factorise

        sid, works = _library(conn, 24)
        rng = np.random.default_rng(5)
        for patron in range(120):
            group = patron % 3
            for w in rng.choice(8, size=5, replace=False) + group * 8:
                conn.execute(
                    "INSERT INTO ratings (source_id, patron_ref, work_id, rating, reviewed) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (sid, f"p{patron}", works[int(w)], int(rng.integers(3, 6)), False))
        conn.commit()

        stats = factorise(conn, ALSParams(factors=8, iterations=10), sid, signal="ratings")
        assert (stats.patrons, stats.works, stats.interactions) == (120, 24, 600)

        rows = conn.execute("SELECT work_id, vector, raw_vector, n_patrons, "
                            "factoriser_version, signal FROM work_factors").fetchall()
        assert len(rows) == 24
        for _, vector, raw, n_patrons, version, signal in rows:
            # Recorded with every row: the OPAC heads its similar books
            # "Readers who rated this also rated" from it, not "borrowed".
            assert signal == "ratings"
            assert version == 2 and n_patrons > 0
            assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-5)
            assert normalise(np.array([raw], dtype=np.float32))[0] == \
                pytest.approx(np.array(vector), abs=1e-5)

        # And the stored factors place a new reader in the right group.
        for k, w in enumerate(works[8:14]):
            conn.execute("INSERT INTO ratings (source_id, patron_ref, work_id, rating) "
                         "VALUES (%s, 'newcomer', %s, 5)", (sid, w))
        conn.commit()
        recs, method = recommend_for_patron(conn, sid, "newcomer", limit=2)
        assert method == "full"
        assert {r.work_id for r in recs} == set(works[14:16])

    def test_loans_remain_the_default_signal(self, conn):
        from bookrs.recommend.pipeline import factorise

        sid, _ = _library(conn, 3)
        with pytest.raises(ValueError, match="circulation"):
            factorise(conn, ALSParams(), sid)
        with pytest.raises(ValueError, match="ratings"):
            factorise(conn, ALSParams(), sid, signal="ratings")

    def test_an_unknown_patron_gets_the_most_rated_works(self, conn):
        sid, works = _library(conn, 4)
        for j, w in enumerate(works):
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
                "factoriser_version, n_patrons) VALUES (%s, %s, %s, 2, 2, %s)",
                (w, [1.0, 0.0], [1.0, 0.0], 10 * (j + 1)))
        conn.commit()
        recs, method = recommend_for_patron(conn, sid, "nobody", limit=2)
        assert method == "none"
        assert [r.work_id for r in recs] == [works[3], works[2]]


class TestEditions:
    def _space(self):
        # works 1 and 2 are editions of one book; 3, 4, 5 are distinct
        rows = [(1, [1.0, 0.0], 50), (2, [0.99, 0.01], 40), (3, [0.9, 0.1], 30),
                (4, [0.0, 1.0], 20), (5, [0.1, 0.9], 10)]
        keys = ["catching fire\x1fsuzanne collins"] * 2 + ["a", "b", "c"]
        return FactorSpace.from_rows(rows).with_groups(keys)

    def test_work_key_ignores_case_width_and_spacing(self):
        from bookrs.recommend.personal import work_key

        a = work_key("Catching Fire (The Hunger Games, #2)", ["Suzanne Collins"])
        b = work_key("catching  fire (the hunger games, #2) ", ["SUZANNE COLLINS", "x"])
        assert a == b
        assert a != work_key("Catching Fire (The Hunger Games, #2)", ["Someone Else"])
        assert work_key("Untitled", []) != work_key("Untitled", ["A"])

    def test_two_editions_never_appear_in_one_list(self):
        recs, method = recommend(self._space(), [], limit=3)
        assert method == "none"
        assert [r.work_id for r in recs] == [1, 3, 4]

    def test_rating_one_edition_excludes_the_other(self):
        recs, _ = recommend(self._space(), [(2, 5, False)], limit=5)
        assert {r.work_id for r in recs} == {3, 4, 5}

    def test_without_groups_every_record_is_its_own_book(self):
        s = FactorSpace.from_rows([(1, [1.0, 0.0], 50), (2, [1.0, 0.0], 40)])
        assert [r.work_id for r in recommend(s, [], limit=5)[0]] == [1, 2]

    def test_a_full_list_is_still_returned_when_editions_crowd_the_top(self):
        rng = np.random.default_rng(2)
        rows = [(j, rng.normal(size=4).tolist(), 1000 - j) for j in range(60)]
        # the 20 most-rated records are all editions of one book
        keys = ["same"] * 20 + [str(j) for j in range(20, 60)]
        s = FactorSpace.from_rows(rows).with_groups(keys)
        recs, _ = recommend(s, [], limit=10)
        assert [r.work_id for r in recs] == [0] + list(range(20, 29))

    def test_editions_are_grouped_from_the_database(self, conn):
        sid, works = _library(conn, 4)
        conn.execute("UPDATE works SET title = 'Catching Fire', authors = '{Suzanne Collins}' "
                     "WHERE id = ANY(%s)", (works[:2],))
        for j, w in enumerate(works):
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
                "factoriser_version, n_patrons) VALUES (%s, %s, %s, 2, 2, %s)",
                (w, [1.0, 0.0], [1.0, 0.0], 100 - j))
        conn.commit()
        recs, _ = recommend_for_patron(conn, sid, "nobody", limit=4)
        assert [r.work_id for r in recs] == [works[0], works[2], works[3]]
