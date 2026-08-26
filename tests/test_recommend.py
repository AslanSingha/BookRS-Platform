"""Tests for confidence weighting and factorisation."""

import pytest

from bookrs.recommend.confidence import (
    ConfidenceWeights,
    Interaction,
    confidence,
)

np = pytest.importorskip("numpy")
sparse = pytest.importorskip("scipy.sparse")

from bookrs.recommend.als import ALSParams, _filter, build_matrix, choose_factors


def _i(patron="p", work=1, loans=1, renewals=0) -> Interaction:
    return Interaction(patron_ref=patron, work_id=work,
                       n_loans=loans, n_renewals=renewals)


class TestConfidence:
    def test_baseline_is_never_below_one(self):
        """1 is the confidence of an unobserved pair; an observed one
        cannot carry less evidence than no observation."""
        w = ConfidenceWeights(loan=0, renewal=0, repeat=0)
        assert confidence(_i(), w) == 1.0

    def test_a_loan_raises_confidence(self):
        assert confidence(_i(loans=1), ConfidenceWeights()) > 1.0

    def test_renewal_counts_for_more_than_a_bare_loan(self):
        w = ConfidenceWeights()
        assert confidence(_i(loans=1, renewals=1), w) > confidence(_i(loans=1), w)

    def test_repeat_borrow_counts_for_most(self):
        """Borrowing the same work twice is the closest circulation gets
        to an endorsement."""
        w = ConfidenceWeights()
        assert confidence(_i(loans=2), w) > confidence(_i(loans=1, renewals=1), w)

    def test_ceiling_bounds_pathological_cases(self):
        w = ConfidenceWeights(ceiling=10.0)
        assert confidence(_i(loans=500), w) == 10.0

    def test_repeats_derive_from_loans(self):
        assert _i(loans=1).n_repeats == 0
        assert _i(loans=3).n_repeats == 2

    def test_negative_weights_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            ConfidenceWeights(loan=-1)

    def test_ceiling_below_baseline_rejected(self):
        with pytest.raises(ValueError, match="ceiling"):
            ConfidenceWeights(ceiling=0.5)


class TestFiltering:
    def test_work_with_one_borrower_is_dropped(self):
        """No co-occurrence means no evidence linking it to anything;
        fitting it would be fitting noise."""
        interactions = [_i("a", 1), _i("b", 1), _i("a", 2), _i("b", 2), _i("c", 9)]
        kept, ex_works, _ = _filter(interactions, ALSParams())
        assert 9 not in {i.work_id for i in kept}
        assert ex_works == 1

    def test_patron_with_one_work_is_dropped(self):
        interactions = [_i("a", 1), _i("a", 2), _i("b", 1), _i("b", 2), _i("solo", 1)]
        kept, _, ex_patrons = _filter(interactions, ALSParams())
        assert "solo" not in {i.patron_ref for i in kept}
        assert ex_patrons == 1

    def test_filtering_cascades(self):
        """Removing a thin work can push a patron below threshold, and
        the reverse, so filtering repeats until it settles."""
        interactions = [_i("a", 1), _i("a", 2), _i("b", 1), _i("b", 2),
                        _i("c", 3), _i("c", 1)]
        kept, _, _ = _filter(interactions, ALSParams())
        assert 3 not in {i.work_id for i in kept}

    def test_threshold_below_two_rejected(self):
        with pytest.raises(ValueError, match="co-occurrence"):
            ALSParams(min_patrons_per_work=1)


class TestMatrix:
    def _pairs(self):
        return [_i("a", 1), _i("a", 2), _i("b", 1), _i("b", 2)]

    def test_shape_is_patrons_by_works(self):
        matrix, patrons, works, _ = build_matrix(self._pairs(), ALSParams())
        assert matrix.shape == (len(patrons), len(works)) == (2, 2)

    def test_values_are_confidences_not_counts(self):
        matrix, _, _, _ = build_matrix(self._pairs(), ALSParams())
        assert matrix.data.min() > 1.0

    def test_confidence_scales_with_evidence(self):
        pairs = self._pairs() + [_i("a", 1, loans=3, renewals=2)]
        matrix, _, _, _ = build_matrix(pairs, ALSParams())
        assert matrix.data.max() > matrix.data.min()

    def test_empty_after_filtering_raises(self):
        with pytest.raises(ValueError, match="two patrons"):
            build_matrix([_i("a", 1)], ALSParams())

    def test_stats_report_exclusions(self):
        pairs = self._pairs() + [_i("c", 99)]
        _, _, _, stats = build_matrix(pairs, ALSParams())
        assert stats.excluded_works == 1 and stats.interactions == 4


class TestFactorChoice:
    """A fixed factor count does not transfer across scales: k=128 was
    right over 26 million interactions and degrades the factors over a
    few hundred."""

    def test_small_matrix_gets_the_floor(self):
        m = sparse.csr_matrix(np.ones((10, 10), dtype=np.float32))
        assert choose_factors(m) == 16

    def test_ceiling_caps_a_very_dense_matrix(self):
        """Constructed rather than randomised: sparse.random deduplicates
        coordinates, so its nnz lands near but under the nominal
        density, and the assertion would depend on the draw."""
        rows, cols, per_row = 5000, 5000, 2000
        indptr = np.arange(0, rows * per_row + 1, per_row, dtype=np.int32)
        indices = np.tile(np.arange(per_row, dtype=np.int32), rows)
        data = np.ones(rows * per_row, dtype=np.float32)
        m = sparse.csr_matrix((data, indices, indptr), shape=(rows, cols))
        assert m.nnz == 10_000_000
        assert choose_factors(m) == 128

    def test_very_sparse_matrices_get_the_floor_despite_volume(self):
        """A million observations across 70,000 rows and columns still
        supports only 16 factors: k*(m+n) parameters already exceeds the
        observations. A property of sparse factorisation, not a quirk."""
        rows, cols, per_row = 50_000, 20_000, 20
        indptr = np.arange(0, rows * per_row + 1, per_row, dtype=np.int32)
        indices = np.tile(np.arange(per_row, dtype=np.int32), rows)
        data = np.ones(rows * per_row, dtype=np.float32)
        m = sparse.csr_matrix((data, indices, indptr), shape=(rows, cols))
        assert m.nnz == 1_000_000
        assert choose_factors(m) == 16

    def test_scales_with_observations(self):
        def dense(rows, cols, per_row):
            indptr = np.arange(0, rows * per_row + 1, per_row, dtype=np.int32)
            indices = np.tile(np.arange(per_row, dtype=np.int32), rows)
            data = np.ones(rows * per_row, dtype=np.float32)
            return sparse.csr_matrix((data, indices, indptr), shape=(rows, cols))

        assert choose_factors(dense(2000, 2000, 800)) >= choose_factors(dense(2000, 2000, 40))
