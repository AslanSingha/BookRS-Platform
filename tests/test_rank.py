"""Tests for hybrid ranking.

The properties worth protecting here are mostly about what the ranker
declines to do. Its whole design premise is that the collaborative
signal re-orders content candidates rather than retrieving its own, so
the tests that matter are the ones asserting it cannot promote a work
the content layer never surfaced, cannot rank on factors backed by too
little evidence, and cannot behave differently on a catalogue with no
circulation than the content-only system did.
"""

import pytest

np = pytest.importorskip("numpy")

from bookrs.recommend.rank import Candidate, RankWeights, factor_map, rerank


def _v(*components) -> "np.ndarray":
    """A unit vector. Both signals are L2-normalised in production."""
    vector = np.array(components, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _f(dot) -> "np.ndarray":
    """A unit vector whose dot product with QUERY is exactly ``dot``."""
    return np.array([dot, np.sqrt(max(0.0, 1.0 - dot * dot))], dtype=np.float32)


def _c(work_id, content, factors=None, n_patrons=0) -> Candidate:
    return Candidate(work_id=work_id, content_score=content,
                     factors=factors, n_patrons=n_patrons)


#: Points the same way as _v(1, 0); used as the query throughout.
QUERY = _v(1, 0)


class TestWeightValidation:
    def test_negative_weights_rejected(self):
        with pytest.raises(ValueError):
            RankWeights(content=-1)

    def test_both_weights_zero_rejected(self):
        """Every candidate would score zero and the order would be
        whatever the tie-break happened to produce."""
        with pytest.raises(ValueError):
            RankWeights(content=0, collaborative=0)

    def test_one_weight_zero_is_allowed(self):
        """A deployment may legitimately disable either signal."""
        assert RankWeights(content=0, collaborative=1).collaborative == 1

    def test_evidence_floor_below_one_rejected(self):
        with pytest.raises(ValueError):
            RankWeights(min_patrons=0)

    def test_empty_pool_rejected(self):
        with pytest.raises(ValueError):
            RankWeights(pool=0)


class TestContentOnlyPath:
    """A library with no circulation must get the content-only system's
    behaviour, through the same code path rather than a parallel one."""

    def test_no_query_factors_preserves_content_order(self):
        ranked = rerank([_c(1, 0.9), _c(2, 0.5), _c(3, 0.7)],
                        None, 0, RankWeights(), limit=3)
        assert [r.work_id for r in ranked] == [1, 3, 2]

    def test_no_query_factors_reports_content_signal(self):
        ranked = rerank([_c(1, 0.9)], None, 0, RankWeights(), limit=1)
        assert ranked[0].signal == "content"
        assert ranked[0].collaborative_score is None

    def test_no_candidate_factors_preserves_content_order(self):
        """The query is factorised but nothing it could be compared
        against is -- the common case at low coverage."""
        ranked = rerank([_c(1, 0.9), _c(2, 0.5)], QUERY, 10,
                        RankWeights(), limit=2)
        assert [r.work_id for r in ranked] == [1, 2]
        assert {r.signal for r in ranked} == {"content"}

    def test_query_below_evidence_floor_is_content_only(self):
        candidates = [_c(1, 0.5, _v(0, 1), 10), _c(2, 0.9, _v(1, 0), 10)]
        ranked = rerank(candidates, QUERY, 2, RankWeights(min_patrons=3),
                        limit=2)
        assert [r.work_id for r in ranked] == [2, 1]
        assert {r.signal for r in ranked} == {"content"}


class TestEvidenceFloor:
    """Cosine magnitude carries no information about how much evidence
    backs it. The only collinear pairs in the reference corpus were
    works sharing their sole two borrowers: read naively, a perfect
    match; in fact the weakest possible vectors."""

    def test_thin_candidate_factors_do_not_rank(self):
        candidates = [_c(1, 0.5, _v(1, 0), 2), _c(2, 0.6, _v(0, 1), 2)]
        ranked = rerank(candidates, QUERY, 10, RankWeights(min_patrons=3),
                        limit=2)
        assert [r.work_id for r in ranked] == [2, 1]
        assert {r.signal for r in ranked} == {"content"}

    def test_floor_is_inclusive(self):
        candidates = [_c(1, 0.1, _v(1, 0), 3)]
        ranked = rerank(candidates, QUERY, 3, RankWeights(min_patrons=3),
                        limit=1)
        assert ranked[0].signal == "hybrid"

    def test_lowering_the_floor_admits_thin_factors(self):
        candidates = [_c(1, 0.1, _v(1, 0), 2)]
        ranked = rerank(candidates, QUERY, 2, RankWeights(min_patrons=2),
                        limit=1)
        assert ranked[0].signal == "hybrid"


class TestReranking:
    def test_collaborative_signal_can_reorder(self):
        """A weaker content match with a strong collaborative score
        outranks a stronger one without."""
        candidates = [_c(1, 0.90, _v(0, 1), 10), _c(2, 0.60, _v(1, 0), 10)]
        ranked = rerank(candidates, QUERY, 10,
                        RankWeights(content=0.3, collaborative=0.7), limit=2)
        assert [r.work_id for r in ranked] == [2, 1]

    def test_content_dominant_weights_keep_content_order(self):
        candidates = [_c(1, 0.90, _v(0, 1), 10), _c(2, 0.60, _v(1, 0), 10)]
        ranked = rerank(candidates, QUERY, 10,
                        RankWeights(content=0.99, collaborative=0.01), limit=2)
        assert [r.work_id for r in ranked] == [1, 2]

    def test_both_scores_are_reported(self):
        ranked = rerank([_c(1, 0.5, _v(1, 0), 10)], QUERY, 10,
                        RankWeights(), limit=1)
        assert ranked[0].content_score == pytest.approx(0.5)
        assert ranked[0].collaborative_score == pytest.approx(1.0)
        assert ranked[0].signal == "hybrid"

    def test_score_is_normalised_by_weight_total(self):
        """Scores stay comparable across deployments whose weights sum
        to different totals."""
        candidates = [_c(1, 0.5, _v(1, 0), 10)]
        a = rerank(candidates, QUERY, 10,
                   RankWeights(content=1, collaborative=1), limit=1)
        b = rerank(candidates, QUERY, 10,
                   RankWeights(content=10, collaborative=10), limit=1)
        assert a[0].score == pytest.approx(b[0].score)


class TestRetrievalIsNeverCollaborative:
    """The failure this design exists to prevent: on generated
    circulation the factors carry a positive co-borrowing signal while
    the nearest neighbours of a Springsteen biography are a
    special-education dissemination model and two C++ books. That
    happened because the collaborative layer was the candidate source.
    """

    def test_a_work_outside_the_pool_cannot_be_promoted(self):
        candidates = [_c(i, 1.0 - i / 100) for i in range(1, 40)]
        # Strong collaborative match, but content ranks it 39th.
        candidates.append(_c(99, 0.01, _v(1, 0), 50))
        ranked = rerank(candidates, QUERY, 50,
                        RankWeights(content=0.1, collaborative=0.9, pool=10),
                        limit=10)
        assert 99 not in [r.work_id for r in ranked]

    def test_pool_bounds_what_is_considered(self):
        candidates = [_c(i, 1.0 - i / 100) for i in range(1, 80)]
        ranked = rerank(candidates, None, 0, RankWeights(pool=10), limit=50)
        assert len(ranked) == 10

    def test_limit_is_respected(self):
        candidates = [_c(i, 1.0 - i / 100) for i in range(1, 40)]
        assert len(rerank(candidates, None, 0, RankWeights(), limit=6)) == 6

    def test_empty_candidates(self):
        assert rerank([], QUERY, 10, RankWeights(), limit=6) == []


class TestImputation:
    """An absent collaborative score is imputed with the median of the
    present ones, per query. Zero is neither the centre of the
    collaborative distribution nor a neutral point within it, so it
    would advantage or penalise unfactorised works by nothing more than
    the sparsity of the library's circulation."""

    #: Collaborative scores chosen so that median (0.8), mean (0.68),
    #: max (1.0) and zero each place the unfactorised work at a
    #: different rank. A symmetric fixture would pass under all four
    #: and prove nothing about which imputation is in use.
    SKEWED = (1.0, 0.9, 0.8, 0.7, 0.0)

    def _skewed_ranking(self):
        candidates = [_c(i, 0.5, _f(dot), 9)
                      for i, dot in enumerate(self.SKEWED, start=1)]
        candidates.append(_c(6, 0.5))  # no factors
        ranked = rerank(candidates, QUERY, 9,
                        RankWeights(content=0.0, collaborative=1.0), limit=6)
        return [r.work_id for r in ranked]

    def test_unfactorised_work_lands_at_the_median(self):
        assert self._skewed_ranking().index(6) == 3

    def test_imputation_is_not_zero_mean_or_max(self):
        """Each alternative places it elsewhere: zero last (index 5),
        mean below the 0.7 peer (4), max level with the top (1)."""
        position = self._skewed_ranking().index(6)
        assert position not in (1, 4, 5)

    def test_imputed_work_reports_content_not_hybrid(self):
        """It was placed by an imputation, not by evidence, and a
        library displaying it should not be told otherwise."""
        candidates = [_c(1, 0.5, _v(1, 0), 9), _c(2, 0.5)]
        ranked = rerank(candidates, QUERY, 9, RankWeights(), limit=2)
        by_id = {r.work_id: r for r in ranked}
        assert by_id[2].signal == "content"
        assert by_id[2].collaborative_score is None


class TestDeterminism:
    def test_ties_break_identically_on_both_paths(self):
        """Otherwise the same catalogue returns two different orderings
        depending on whether circulation exists."""
        content = rerank([_c(3, 0.5), _c(1, 0.5), _c(2, 0.5)],
                         None, 0, RankWeights(), limit=3)
        hybrid = rerank([_c(3, 0.5, _v(1, 0), 9),
                         _c(1, 0.5, _v(1, 0), 9),
                         _c(2, 0.5, _v(1, 0), 9)],
                        QUERY, 9, RankWeights(), limit=3)
        assert [r.work_id for r in content] == [r.work_id for r in hybrid]

    def test_input_order_does_not_affect_output(self):
        forward = [_c(1, 0.5), _c(2, 0.9), _c(3, 0.7)]
        ranked_a = rerank(forward, None, 0, RankWeights(), limit=3)
        ranked_b = rerank(list(reversed(forward)), None, 0, RankWeights(),
                          limit=3)
        assert [r.work_id for r in ranked_a] == [r.work_id for r in ranked_b]


class TestFactorMap:
    def test_builds_typed_vectors(self):
        mapped = factor_map([(1, [0.6, 0.8], 4)])
        vector, n_patrons = mapped[1]
        assert vector.dtype == np.float32
        assert n_patrons == 4

    def test_empty_input(self):
        assert factor_map([]) == {}
