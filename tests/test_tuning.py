"""Tests for the hyperparameter sweep's own logic."""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from bookrs.evaluation import tuning as T
from bookrs.evaluation.thesis import CONFIDENCE, Scores


def test_a_equal_two_is_the_thesis_formula():
    r = np.array([1, 5, 5], dtype=np.int8)
    d = np.array([0, 0, 1], dtype=np.int8)
    assert T.confidence(r, d, 2.0).tolist() == CONFIDENCE["1+2r+3d"](r, d).tolist()


def test_one_setting_varies_at_a_time_and_the_thesis_appears_once():
    grid = {"factors": (64, 128), "regularization": (0.1, 1.0)}
    got = T.candidates(T.THESIS, grid)
    settings = [s for _, s in got]
    assert settings.count(T.THESIS) == 1
    assert len(settings) == 3
    for name, s in got:
        changed = [k for k in ("factors", "regularization", "iterations", "a")
                   if getattr(s, k) != getattr(T.THESIS, k)]
        assert changed in ([], [name])


def _sc(p, se=0.001):
    return Scores(readers=1, p10=p, p10_se=se)


def test_combine_takes_a_better_value_that_clears_the_bar():
    res = [("factors", T.THESIS, _sc(0.120)),
           ("factors", T.Setting(factors=64), _sc(0.100)),
           ("factors", T.Setting(factors=256), _sc(0.200)),
           ("a", T.Setting(a=5.0), _sc(0.150))]
    best = T.combine(T.THESIS, res)
    assert (best.factors, best.a, best.regularization, best.iterations) == (256, 5.0, 0.1, 10)


def test_a_win_within_noise_does_not_move_a_setting():
    """The random-data failure: one value is always highest by chance."""
    res = [("factors", T.THESIS, _sc(0.1000)),
           ("factors", T.Setting(factors=256), _sc(0.1020)),     # 1.4 SE: noise
           ("a", T.Setting(a=1.0), _sc(0.1040))]                 # 2.8 SE: real
    best = T.combine(T.THESIS, res)
    assert best.factors == 128 and best.a == 1.0


def test_nothing_clearing_the_bar_keeps_the_thesis():
    res = [("factors", T.THESIS, _sc(0.1000)),
           ("factors", T.Setting(factors=64), _sc(0.1010)),
           ("regularization", T.Setting(regularization=1.0), _sc(0.0990))]
    assert T.combine(T.THESIS, res) == T.THESIS


def test_the_test_ratings_play_no_part_in_the_choice():
    """Validation is carved from training only: no rating is in both."""
    from bookrs.evaluation.thesis import split
    u = np.repeat(np.arange(200), 30).astype(np.int32)
    test = split(u, 200, 0.2, 42)
    train = ~test
    val = np.zeros(len(u), dtype=bool)
    val[np.flatnonzero(train)] = split(u[train], 200, 0.2, 43)
    assert not (val & test).any()
    assert val.sum() == pytest.approx(0.2 * train.sum(), rel=0.05)


def test_a_setting_is_scored_end_to_end():
    pytest.importorskip("implicit")
    rng = np.random.default_rng(3)
    groups, per, m = 6, 20, 600
    u, i = [], []
    for reader in range(m):
        own = rng.choice(per, size=12, replace=False) + (reader % groups) * per
        u += [reader] * 12
        i += sorted(own.tolist())
    u, i = np.array(u, np.int32), np.array(i, np.int32)
    r = np.full(len(u), 5, np.int8)
    d = np.zeros(len(u), np.int8)
    from bookrs.evaluation.thesis import split
    held = split(u, m, 0.2, 1)
    sc = T.score_setting(T.Setting(factors=8, iterations=8), u, i, r, d, ~held, held,
                         (m, groups * per), np.arange(m), 1)
    assert sc.p10 > 0.15 and sc.readers == m
