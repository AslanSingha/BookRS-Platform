"""Tests for the thesis-protocol evaluation.

The arithmetic is checked on cases small enough to do by hand, and the
whole path on data with a structure planted in it, where a working
harness has to find the structure and a broken one cannot.
"""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from scipy.sparse import csr_matrix

from bookrs.evaluation.thesis import (
    CONFIDENCE, Data, evaluate, restrict, split,
)


def _m(rows, shape):
    r = [a for a, _ in rows]
    c = [b for _, b in rows]
    return csr_matrix((np.ones(len(rows), dtype=np.int8), (r, c)), shape=shape)


class TestSplit:
    def _u(self):
        return np.repeat(np.arange(5), [10, 12, 13, 25, 40]).astype(np.int32)

    def test_each_reader_gives_up_a_fifth_rounded_to_nearest(self):
        u = self._u()
        held = split(u, 5)
        # 0.2 * (10, 12, 13, 25, 40) = 2, 2.4, 2.6, 5, 8
        assert np.bincount(u[held], minlength=5).tolist() == [2, 2, 3, 5, 8]

    def test_rounding_matches_python_round_as_pandas_uses_it(self):
        for n in (2, 3, 5, 13, 15, 62, 63, 1001):
            assert int(split(np.zeros(n, dtype=np.int32), 1).sum()) == round(0.2 * n), n

    def test_the_same_seed_gives_the_same_split(self):
        u = self._u()
        assert (split(u, 5, seed=42) == split(u, 5, seed=42)).all()

    def test_a_different_seed_gives_a_different_split(self):
        u = self._u()
        assert (split(u, 5, seed=42) != split(u, 5, seed=43)).any()

    def test_every_rating_is_on_exactly_one_side(self):
        u = self._u()
        held = split(u, 5)
        assert held.dtype == bool and len(held) == len(u)
        assert held.sum() + (~held).sum() == len(u)

    def test_which_ratings_are_held_out_is_not_positional(self):
        """Not simply each reader's first or last few."""
        u = np.zeros(1000, dtype=np.int32)
        held = split(u, 1)
        assert held.sum() == 200
        assert 20 < held[:500].sum() < 180


class TestEvaluate:
    def test_a_case_done_by_hand(self):
        # 12 works. The reader's held-out books are 3 and 7. The ranking
        # puts 5 first, 7 second, 3 eleventh.
        scores = np.array([[0, 0, 0, 1, 0, 12, 0, 11, 0, 0, 0, 0]], dtype=np.float32)
        scores[0, [0, 1, 2, 4, 6, 8, 9, 10, 11]] = [10, 9, 8, 7, 6, 5, 4, 3, 2]
        s = evaluate(lambda who: scores[who], _m([], (1, 12)),
                     _m([(0, 3), (0, 7)], (1, 12)), np.array([0]))
        assert s.readers == 1
        assert s.p10 == pytest.approx(0.1)          # one of ten
        assert s.r10 == pytest.approx(0.5)          # one of two
        assert s.mrr == pytest.approx(0.5)          # first hit at rank 2
        assert s.hr10 == 1.0
        assert s.r20 == pytest.approx(1.0)          # 3 is at rank 11
        assert s.ceiling == pytest.approx(0.2)      # two books: at best 2/10
        ideal = 1 / np.log2(2) + 1 / np.log2(3)
        assert s.ndcg10 == pytest.approx((1 / np.log2(3)) / ideal)

    def test_a_perfect_ranking_scores_the_ceiling_not_one(self):
        rng = np.random.default_rng(0)
        rel = (rng.random((50, 40)) < 0.1)
        rel[:, 0] = True
        relevant = csr_matrix(rel.astype(np.int8))
        s = evaluate(lambda who: rel[who].astype(np.float32), _m([], (50, 40)),
                     relevant, np.arange(50))
        assert s.p10 == pytest.approx(s.ceiling) and s.ceiling < 1.0
        assert s.mrr == 1.0 and s.ndcg10 == pytest.approx(1.0) and s.hr10 == 1.0

    def test_books_seen_in_training_are_never_recommended(self):
        # Work 0 scores highest and was rated in training; the held-out
        # book is work 1. Without the exclusion the first hit is rank 2.
        scores = np.array([[9, 8, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]], dtype=np.float32)
        s = evaluate(lambda who: scores[who], _m([(0, 0)], (1, 12)),
                     _m([(0, 1)], (1, 12)), np.array([0]))
        assert s.mrr == 1.0

    def test_the_callers_scores_are_not_modified(self):
        scores = np.ones((1, 12), dtype=np.float32)
        evaluate(lambda who: scores[who], _m([(0, 0)], (1, 12)),
                 _m([(0, 1)], (1, 12)), np.array([0]))
        assert np.isfinite(scores).all()

    def test_a_reader_with_nothing_relevant_is_left_out_and_counted(self):
        scores = np.ones((3, 12), dtype=np.float32)
        s = evaluate(lambda who: scores[who], _m([], (3, 12)),
                     _m([(0, 1), (2, 5)], (3, 12)), np.arange(3))
        assert (s.readers, s.skipped) == (2, 1)

    def test_nobody_relevant_is_zeros_not_an_error(self):
        s = evaluate(lambda who: np.ones((len(who), 12), dtype=np.float32),
                     _m([], (2, 12)), _m([], (2, 12)), np.arange(2))
        assert (s.readers, s.skipped, s.p10) == (0, 2, 0.0)

    def test_chunking_does_not_change_the_answer(self):
        rng = np.random.default_rng(1)
        scores = rng.random((300, 60)).astype(np.float32)
        relevant = csr_matrix((rng.random((300, 60)) < 0.08).astype(np.int8))
        seen = csr_matrix((rng.random((300, 60)) < 0.2).astype(np.int8))
        a = evaluate(lambda w: scores[w], seen, relevant, np.arange(300), chunk=7)
        b = evaluate(lambda w: scores[w], seen, relevant, np.arange(300), chunk=4096)
        assert a == b

    def test_only_the_readers_asked_for_are_scored(self):
        scores = np.zeros((4, 12), dtype=np.float32)
        scores[2, 3] = 1.0
        relevant = _m([(0, 1), (1, 1), (2, 3), (3, 1)], (4, 12))
        s = evaluate(lambda w: scores[w], _m([], (4, 12)), relevant, np.array([2]))
        assert (s.readers, s.mrr) == (1, 1.0)


class TestRestrict:
    def _data(self):
        u = np.array([0, 0, 0, 1, 2, 2], dtype=np.int32)
        i = np.array([0, 1, 2, 2, 0, 3], dtype=np.int32)
        return Data(["a", "b", "c"], np.array([10, 20, 30, 40]), u, i,
                    np.array([5, 4, 3, 2, 1, 5], dtype=np.int8),
                    np.array([1, 0, 0, 0, 0, 1], dtype=np.int8))

    def test_thin_readers_are_removed_and_indices_stay_dense(self):
        d = restrict(self._data(), 2)
        assert d.patrons == ["a", "c"]
        assert d.u.tolist() == [0, 0, 0, 1, 1]
        assert d.works[d.i].tolist() == [10, 20, 30, 10, 40]
        assert d.r.tolist() == [5, 4, 3, 1, 5] and d.d.tolist() == [1, 0, 0, 0, 1]

    def test_nobody_removed_returns_the_same_data(self):
        data = self._data()
        assert restrict(data, 1) is data


class TestConfidence:
    def test_the_thesis_formula(self):
        r = np.array([1, 5, 5], dtype=np.float32)
        d = np.array([0, 0, 1], dtype=np.float32)
        assert CONFIDENCE["1+2r+3d"](r, d).tolist() == [3.0, 11.0, 14.0]

    def test_every_formula_gives_positive_confidence(self):
        r = np.arange(1, 6, dtype=np.float32)
        d = np.zeros(5, dtype=np.float32)
        for name, f in CONFIDENCE.items():
            assert (np.asarray(f(r, d)) > 0).all(), name


class TestEndToEnd:
    def test_planted_taste_groups_are_recovered(self):
        """Readers in a group rate that group's books. ALS must beat
        popularity by a wide margin, or something between the split and
        the score is wrong."""
        pytest.importorskip("implicit")
        from bookrs.evaluation.thesis import fit

        rng = np.random.default_rng(7)
        groups, per_group, n_readers = 8, 25, 1600
        n_works = groups * per_group
        u, i = [], []
        for reader in range(n_readers):
            g = reader % groups
            own = rng.choice(per_group, size=15, replace=False) + g * per_group
            u += [reader] * 15
            i += own.tolist()
        u, i = np.array(u, dtype=np.int32), np.array(i, dtype=np.int32)
        order = np.lexsort((i, u))
        u, i = u[order], i[order]

        held = split(u, n_readers)
        shape = (n_readers, n_works)
        train = csr_matrix((np.full((~held).sum(), 5.0, dtype=np.float32),
                            (u[~held], i[~held])), shape=shape)
        test = csr_matrix((np.ones(held.sum(), dtype=np.int8),
                           (u[held], i[held])), shape=shape)
        rf, wf = fit(train, factors=16, regularization=0.1, iterations=10)
        assert rf.shape == (n_readers, 16) and wf.shape == (n_works, 16)

        everyone = np.arange(n_readers)
        als = evaluate(lambda w: rf[w] @ wf.T, train, test, everyone)
        pop = np.asarray((train > 0).sum(axis=0), dtype=np.float32).ravel()
        popular = evaluate(lambda w: np.tile(pop, (len(w), 1)), train, test, everyone)

        # 3 held-out books among the 13 unseen in the group: a model that
        # knows the group and nothing else expects 10 * 3/13 hits.
        assert als.ceiling == pytest.approx(0.3)
        assert als.p10 > 0.2
        assert als.p10 > 4 * popular.p10
