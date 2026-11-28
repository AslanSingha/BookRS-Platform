"""Tests for the cold-start evaluation's own pieces.

The scoring and the split are thesis.py's and are tested there. What is
new here is the order ratings are revealed in and the two ways of
solving for a reader the model has never seen.
"""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from scipy.sparse import csr_matrix

from bookrs.evaluation.coldstart import fold_in, reveal_order, thesis_stage_alpha


class TestRevealOrder:
    def _case(self):
        u = np.repeat(np.arange(3), [6, 4, 8]).astype(np.int32)
        available = np.ones(len(u), dtype=bool)
        available[[1, 4, 7, 12, 13]] = False
        return u, available

    def test_each_readers_available_ratings_are_numbered_from_zero(self):
        u, available = self._case()
        p = reveal_order(u, available, seed=1)
        for reader in range(3):
            mine = p[(u == reader) & available]
            assert sorted(mine.tolist()) == list(range(len(mine)))

    def test_unavailable_ratings_are_never_revealed(self):
        u, available = self._case()
        p = reveal_order(u, available, seed=1)
        assert (p[~available] > 10**9).all()

    def test_a_larger_step_contains_every_smaller_one(self):
        u, available = self._case()
        p = reveal_order(u, available, seed=1)
        assert set(np.flatnonzero(p < 2)) < set(np.flatnonzero(p < 4))

    def test_the_order_is_random_not_file_order(self):
        u = np.zeros(200, dtype=np.int32)
        p = reveal_order(u, np.ones(200, dtype=bool), seed=1)
        assert not (p == np.arange(200)).all()

    def test_the_same_seed_reveals_the_same_ratings(self):
        u, available = self._case()
        assert (reveal_order(u, available, 5) == reveal_order(u, available, 5)).all()


class TestFoldIn:
    rng = np.random.default_rng(0)
    Y = rng.normal(size=(30, 6)).astype(np.float32)

    def _given(self):
        # reader 0: works 2, 5, 9 with confidences 3, 11, 14; reader 1: nothing
        return csr_matrix((np.array([3.0, 11.0, 14.0], dtype=np.float32),
                           ([0, 0, 0], [2, 5, 9])), shape=(2, 30))

    def test_rated_books_only_satisfies_its_normal_equations(self):
        x = fold_in(self._given(), self.Y, 0.1, None)[0].astype(np.float64)
        B, c = self.Y[[2, 5, 9]].astype(np.float64), np.array([3.0, 11.0, 14.0])
        lhs = (B.T * c) @ B + 0.1 * np.eye(6)
        assert lhs @ x == pytest.approx(B.T @ c, abs=1e-4)

    def test_the_full_solve_satisfies_hu_koren_volinsky(self):
        gram = self.Y.T.astype(np.float64) @ self.Y.astype(np.float64)
        x = fold_in(self._given(), self.Y, 0.1, gram)[0].astype(np.float64)
        # Written out over the whole catalogue: confidence 1 and
        # preference 0 for every book not revealed.
        c = np.ones(30)
        c[[2, 5, 9]] = [3.0, 11.0, 14.0]
        p = np.zeros(30)
        p[[2, 5, 9]] = 1.0
        Y = self.Y.astype(np.float64)
        lhs = (Y.T * c) @ Y + 0.1 * np.eye(6)
        assert lhs @ x == pytest.approx(Y.T @ (c * p), abs=1e-4)

    def test_the_two_solves_differ(self):
        gram = self.Y.T.astype(np.float64) @ self.Y.astype(np.float64)
        a = fold_in(self._given(), self.Y, 0.1, None)[0]
        b = fold_in(self._given(), self.Y, 0.1, gram)[0]
        assert not np.allclose(a, b, atol=1e-3)

    def test_a_reader_with_nothing_revealed_gets_a_zero_vector(self):
        assert not fold_in(self._given(), self.Y, 0.1, None)[1].any()

    def test_revealed_books_score_above_the_catalogue_average(self):
        gram = self.Y.T.astype(np.float64) @ self.Y.astype(np.float64)
        scores = fold_in(self._given(), self.Y, 0.1, gram)[0] @ self.Y.T
        assert scores[[2, 5, 9]].mean() > scores.mean()


class TestStages:
    def test_the_thresholds_are_the_thesis_recommenders(self):
        assert [thesis_stage_alpha(n) for n in (1, 4, 5, 19, 20, 500)] == \
               [1.0, 1.0, 0.7, 0.7, 0.3, 0.3]


class TestEndToEnd:
    def test_a_new_reader_is_placed_in_their_taste_group_from_three_books(self):
        """Readers in a group rate that group's books. Fit without some
        readers, reveal three of each one's books, and both solves must
        find the group -- far better than the most-rated list can."""
        pytest.importorskip("implicit")
        from bookrs.evaluation.thesis import evaluate, fit

        rng = np.random.default_rng(11)
        groups, per_group, n_readers, n_cold = 8, 25, 1600, 200
        n_works = groups * per_group
        u, i = [], []
        for reader in range(n_readers):
            own = rng.choice(per_group, size=15, replace=False) + (reader % groups) * per_group
            u += [reader] * 15
            i += sorted(own.tolist())
        u, i = np.array(u, dtype=np.int32), np.array(i, dtype=np.int32)
        shape = (n_readers, n_works)

        cold = np.arange(n_readers - n_cold, n_readers)
        warm = u < n_readers - n_cold
        _, books = fit(csr_matrix((np.full(warm.sum(), 5.0, dtype=np.float32),
                                   (u[warm], i[warm])), shape=shape),
                       factors=16, regularization=0.1, iterations=10)
        gram = books.T.astype(np.float64) @ books.astype(np.float64)

        position = reveal_order(u, np.ones(len(u), dtype=bool), seed=1)
        shown = ~warm & (position < 3)
        rest = ~warm & (position >= 3)
        given = csr_matrix((np.full(shown.sum(), 5.0, dtype=np.float32),
                            (u[shown], i[shown])), shape=shape)
        target = csr_matrix((np.ones(rest.sum(), dtype=np.int8),
                             (u[rest], i[rest])), shape=shape)
        slot = np.full(n_readers, -1)
        slot[cold] = np.arange(n_cold)

        pop = np.bincount(i[warm], minlength=n_works).astype(np.float32)
        popular = evaluate(lambda w: np.tile(pop, (len(w), 1)), given, target, cold)
        for gram_or_none in (None, gram):
            x = fold_in(given[cold], books, 0.1, gram_or_none)
            got = evaluate(lambda w: x[slot[w]] @ books.T, given, target, cold)
            # 12 of the 22 unseen books in the group are targets.
            assert got.p10 > 0.4, gram_or_none is None
            assert got.p10 > 3 * popular.p10


class TestTheWholeRun:
    def test_new_readers_with_the_revised_measures(self, capsys, monkeypatch):
        """Readers who each read one genre, shown to a model that never saw
        them: every policy that uses their ratings must find their genre,
        and the table must carry the revised measures."""
        from test_evaluation_revised import DATABASE_URL, planted_library

        from bookrs.evaluation import coldstart

        conn, sid = planted_library(per_reader=28)
        conn.close()
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        assert coldstart.main(["--source-id", str(sid), "--factors", "16",
                               "--cold-readers", "200"]) == 0
        text = capsys.readouterr().out
        for title in ("P@10, any held-out rating", "P@10, liked = 4 or 5",
                      "Leave one out: HR@10", "Genre match", "the platform's stages"):
            assert title in text, title
        genre_rows = text.split("Genre match")[1].split("\n")
        platform = next(r for r in genre_rows if "the platform's stages" in r)
        popular = next(r for r in genre_rows if "most-rated books" in r)
        # A multiple of a random list: the platform finds the genre from
        # three books on; the most-rated list knows no genre.
        assert float(platform.split()[-1]) > 3.0 > 1.5 > float(popular.split()[-1])
