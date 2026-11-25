"""Tests for the generated-readers evaluation.

What must hold for its numbers to mean anything: readers read as many
books as real readers do, noise really is taste-free, the stars keep
the library's spread, the truth set is what it says, and a method that
knows the true taste scores 1.0 against it.
"""

import types

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from scipy.sparse import csr_matrix

from bookrs.evaluation import synthetic as S
from bookrs.evaluation.thesis import evaluate


def world(n=400, seed=0):
    rng = np.random.default_rng(seed)
    s = S.Settings()
    genre = rng.dirichlet(np.full(10, 0.4), size=n).astype(np.float32)
    vec = genre @ rng.normal(size=(10, 16)) + 0.5 * rng.normal(size=(n, 16))
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    lp = np.log(rng.pareto(1.2, size=n) + 1)
    return S.World(genre, (rng.normal(size=(n, s.style_dims)) / 4).astype(np.float32),
                   (lp - lp.mean()).astype(np.float32), vec.astype(np.float32),
                   np.array([10, 15, 20, 40, 60]),
                   np.array([0.02, 0.06, 0.2, 0.35, 0.37])), s


def test_readers_read_as_many_books_as_real_readers():
    w, s = world()
    rng = np.random.default_rng(1)
    readers = S.make_readers(300, s, rng)
    u, i, r = S.generate(w, readers, 0.0, s, rng)
    counts = np.bincount(u, minlength=300)
    assert set(np.unique(counts)) <= {10, 15, 20, 40, 60}
    # no book twice for one reader
    assert len(set(zip(u.tolist(), i.tolist()))) == len(u)


def test_stars_keep_the_librarys_spread():
    w, s = world()
    rng = np.random.default_rng(2)
    u, i, r = S.generate(w, S.make_readers(2000, s, rng), 0.0, s, rng)
    share = np.bincount(r, minlength=6)[1:] / len(r)
    assert share == pytest.approx(w.stars, abs=0.03)


def test_stars_follow_liking_without_noise():
    w, s = world()
    rng = np.random.default_rng(3)
    readers = S.make_readers(500, s, rng)
    u, i, r = S.generate(w, readers, 0.0, s, rng)
    util = np.concatenate([S.utility(w, readers, np.array([row]))[0, i[u == row]]
                           for row in range(500)])
    assert np.corrcoef(util, r)[0, 1] > 0.3


def test_noise_reads_are_taste_free():
    """At full noise, what a reader reads says nothing about their taste."""
    w, s = world()
    rng = np.random.default_rng(4)
    readers = S.make_readers(800, s, rng)
    def mean_liking(noise):
        u, i, r = S.generate(w, readers, noise, s, np.random.default_rng(5))
        util = S.utility(w, readers, np.arange(800))
        return util[u, i].mean()
    assert mean_liking(0.0) > 0.5
    assert abs(mean_liking(1.0)) < 0.1


def test_truth_is_the_readers_top_five_percent_among_unseen_books():
    w, s = world(n=400)
    readers = S.make_readers(5, s, np.random.default_rng(6))
    seen = csr_matrix((np.ones(3), ([0, 0, 0], [1, 2, 3])), shape=(5, 400))
    best = S.truth(w, readers, np.arange(5), seen)
    assert (np.diff(best.indptr) == 20).all()          # 5% of 400
    assert not set(best[0].indices) & {1, 2, 3}
    util = S.utility(w, readers, np.array([1]))[0]
    assert set(best[1].indices) == set(np.argsort(-util)[:20])


def test_knowing_the_true_taste_scores_one_against_it():
    w, s = world()
    readers = S.make_readers(50, s, np.random.default_rng(7))
    rows = np.arange(50)
    seen = csr_matrix((50, 400), dtype=np.float32)
    best = S.truth(w, readers, rows, seen)
    sc = evaluate(lambda who: S.utility(w, readers, who), seen, best, rows)
    assert sc.p10 == 1.0 and sc.ndcg10 == pytest.approx(1.0)


def test_the_same_seed_generates_the_same_readers():
    w, s = world()
    a = S.generate(w, S.make_readers(100, s, np.random.default_rng(8)), 0.3, s,
                   np.random.default_rng(9))
    b = S.generate(w, S.make_readers(100, s, np.random.default_rng(8)), 0.3, s,
                   np.random.default_rng(9))
    assert all((x == y).all() for x, y in zip(a, b))


def test_a_whole_level_runs_and_als_beats_popularity_against_truth():
    pytest.importorskip("implicit")
    w, s = world(n=400)
    args = types.SimpleNamespace(readers=1500, scored=400, cold=100, factors=16,
                                 regularization=0.1, iterations=8, seed=1)
    res = S.run_level(w, 0.0, args, s)
    held, truth = res["rows"]["ALS"]
    pop_truth = res["rows"]["most-read books"][1]
    assert truth.p10 > 3 * pop_truth.p10
    assert res["rows"]["true taste (ceiling)"][1].p10 == 1.0
    assert set(res["new"]) == {"most-read books", "content only",
                               "folding-in, as in the thesis", "folding-in, full"}
