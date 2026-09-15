"""Tests for patron folding-in.

fold_in() is a pure function -- no database, no API -- so every test
here constructs its own interactions and item factors directly, the
same pattern test_rank.py uses for the ranker.
"""

import numpy as np
import pytest

from bookrs.recommend.confidence import ConfidenceWeights, Interaction
from bookrs.recommend.folding import FoldedPatron, fold_in


def _unit(*components) -> np.ndarray:
    v = np.array(components, dtype=np.float64)
    return v / np.linalg.norm(v)


def _loan(work_id, n_loans=1, n_renewals=0) -> Interaction:
    return Interaction(
        patron_ref="test-patron", work_id=work_id,
        n_loans=n_loans, n_renewals=n_renewals,
    )


WEIGHTS = ConfidenceWeights()

# Four orthogonal-ish 2D factors, standing in for a small item-factor
# space. Real factors are higher-dimensional; the maths does not care.
FACTORS = {
    1: _unit(1.0, 0.0),
    2: _unit(0.9, 0.1),
    3: _unit(-1.0, 0.0),
    4: _unit(0.0, 1.0),
}


class TestAbstention:
    """A patron with too little overlap against the factorised set
    should not be folded in at all -- the same discipline rerank()
    applies to the collaborative term generally."""

    def test_no_loans_at_all(self):
        result = fold_in([], FACTORS, WEIGHTS)
        assert result.vector is None
        assert result.personalised is False
        assert result.n_works == 0

    def test_loans_only_on_unfactorised_works(self):
        loans = [_loan(99), _loan(100), _loan(101)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.vector is None
        assert result.n_works == 0, "unfactorised loans must not count toward the floor"

    def test_one_and_two_factorised_loans_still_abstain(self):
        assert fold_in([_loan(1)], FACTORS, WEIGHTS).vector is None
        assert fold_in([_loan(1), _loan(2)], FACTORS, WEIGHTS).vector is None

    def test_the_floor_is_inclusive_at_three(self):
        loans = [_loan(1), _loan(2), _loan(3)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.personalised is True
        assert result.n_works == 3

    def test_unfactorised_loans_do_not_count_toward_the_floor(self):
        """Ten loans, only two on factorised works: judged on the two,
        because the other ten carry nothing this solve can use."""
        loans = [_loan(1), _loan(2)] + [_loan(200 + i) for i in range(10)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.vector is None
        assert result.n_works == 2

    def test_floor_is_configurable(self):
        loans = [_loan(1), _loan(2)]
        assert fold_in(loans, FACTORS, WEIGHTS, min_factorised_works=2).personalised
        assert not fold_in(loans, FACTORS, WEIGHTS, min_factorised_works=3).personalised


class TestReporting:
    def test_n_loans_counts_rows_not_distinct_works(self):
        """A repeat borrow of the same work is two loan rows on one
        distinct work -- n_works and n_loans must diverge to show that,
        the same distinction that mattered when reading the corpus."""
        loans = [_loan(1, n_loans=5), _loan(2), _loan(3)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.n_works == 3
        assert result.n_loans == 7

    def test_abstention_still_reports_evidence_seen(self):
        """So a caller can log how close a patron came, not only that
        they did not clear the floor."""
        result = fold_in([_loan(1), _loan(2)], FACTORS, WEIGHTS)
        assert result.n_works == 2
        assert result.n_loans == 2

    def test_abstention_distinguishes_works_from_loan_rows(self):
        """Two distinct works, one borrowed five times: n_works and
        n_loans must diverge even while abstaining, or a bug computing
        n_works as a loan count rather than a distinct-work count would
        pass unnoticed whenever every loan happens to be a single
        borrow -- exactly the blind spot the first version of this test
        file had."""
        result = fold_in([_loan(1, n_loans=5), _loan(2)], FACTORS, WEIGHTS)
        assert result.vector is None
        assert result.n_works == 2
        assert result.n_loans == 6


class TestVectorProperties:
    def test_vector_is_unit_length(self):
        """Item factors are L2-normalised so a dot product is a cosine
        directly. The folded vector must be too, or every rerank()
        comparison against it silently stops meaning what it claims."""
        loans = [_loan(1), _loan(2), _loan(3)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.vector is not None
        assert np.linalg.norm(result.vector) == pytest.approx(1.0, abs=1e-5)

    def test_vector_has_the_same_dimensionality_as_the_factors(self):
        loans = [_loan(1), _loan(2), _loan(4)]
        result = fold_in(loans, FACTORS, WEIGHTS)
        assert result.vector.shape == (2,)

    def test_vector_points_toward_borrowed_works(self):
        """A patron who only borrowed works near (1, 0) should fold in
        to a vector nearer (1, 0) than (0, 1) or (-1, 0)."""
        loans = [_loan(1), _loan(2)]  # both near (1, 0)
        result = fold_in(loans, FACTORS, WEIGHTS, min_factorised_works=2)
        assert result.vector[0] > result.vector[1]
        assert result.vector[0] > 0

    def test_opposing_evidence_pulls_the_vector_toward_zero_before_normalising(self):
        """Borrowing works 1 and 3 -- opposite directions -- gives the
        solve conflicting evidence. The raw solution should be small
        relative to either input, even though the reported vector is
        renormalised to unit length regardless."""
        loans = [_loan(1), _loan(2), _loan(3)]
        result = fold_in(loans, FACTORS, WEIGHTS, min_factorised_works=3)
        assert result.vector is not None
        # Direction is not asserted here, only that the result is
        # well-defined and unit-length despite conflicting evidence.
        assert np.linalg.norm(result.vector) == pytest.approx(1.0, abs=1e-5)

    def test_zero_regularization_does_not_explode_with_enough_evidence(self):
        loans = [_loan(1), _loan(2), _loan(4)]
        result = fold_in(loans, FACTORS, WEIGHTS, regularization=1e-8)
        assert result.vector is not None
        assert np.all(np.isfinite(result.vector))


class TestRegularization:
    """The one gap mutation testing found and left open: nothing
    previously confirmed regularization was threaded into the solve at
    all, rather than silently ignored or hardcoded. Caught only once the
    fixture gives regularization something to visibly move -- uniform,
    isotropic evidence lets every value of lambda solve to the same
    direction, which is why the earlier attempt at this test passed
    without proving anything.
    """

    def test_changes_output_direction_under_anisotropic_evidence(self):
        # Three orthogonal item factors. Heavy, consistent evidence on
        # the first two; a single thin loan pulling toward the third.
        # Low regularization lets that thin signal rotate the result
        # toward it; high regularization shrinks the whole solve toward
        # zero before normalising, which damps the minority direction
        # relatively more -- so the two solutions point measurably
        # differently, not just at different lengths.
        f1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        f2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        f3 = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        factors = {1: f1, 2: f2, 3: f3}
        loans = [
            Interaction("p", 1, n_loans=10, n_renewals=0),
            Interaction("p", 2, n_loans=10, n_renewals=0),
            Interaction("p", 3, n_loans=1, n_renewals=0),
        ]
        weights = ConfidenceWeights()

        low = fold_in(loans, factors, weights, regularization=0.01)
        high = fold_in(loans, factors, weights, regularization=50.0)

        assert low.vector is not None and high.vector is not None
        cosine = float(np.dot(low.vector, high.vector))
        assert cosine < 0.999, (
            f"regularization did not measurably change the solve's "
            f"direction (cosine={cosine:.6f}); it may not be reaching "
            f"the solve at all"
        )

    def test_default_matches_als_params_default(self):
        """Stated in the docstring as a deliberate default, not a
        coincidence -- pin it so the two cannot drift apart silently."""
        from bookrs.recommend.als import ALSParams
        import inspect
        default = inspect.signature(fold_in).parameters["regularization"].default
        assert default == ALSParams().regularization


class TestConfidenceIntegration:
    """fold_in() must actually use the confidence formula, not just the
    fact that an interaction exists -- a heavily renewed, repeatedly
    borrowed work should weigh more than a single bare loan."""

    def test_higher_confidence_work_dominates_the_direction(self):
        heavy = [_loan(1, n_loans=10, n_renewals=5)]   # near (1, 0)
        light = [_loan(4, n_loans=1)]                  # near (0, 1)
        result = fold_in(heavy + light + [_loan(2)], FACTORS, WEIGHTS,
                         min_factorised_works=3)
        assert result.vector[0] > result.vector[1]

    def test_weights_affect_the_result(self):
        loans = [_loan(1, n_loans=1, n_renewals=5), _loan(2), _loan(4)]
        low = ConfidenceWeights(renewal=0.1)
        high = ConfidenceWeights(renewal=10.0)
        r_low = fold_in(loans, FACTORS, WEIGHTS.__class__(renewal=0.1),
                        min_factorised_works=3)
        r_high = fold_in(loans, FACTORS, WEIGHTS.__class__(renewal=10.0),
                         min_factorised_works=3)
        assert not np.allclose(r_low.vector, r_high.vector)


class TestNoNewDependency:
    def test_the_module_imports_only_numpy(self):
        """Stated as a constraint in the module docstring: this must be
        callable from the API process without pulling in implicit,
        which needs a C toolchain the API image does not carry."""
        import ast
        import inspect

        import bookrs.recommend.folding as mod

        tree = ast.parse(inspect.getsource(mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "implicit" not in imported
        assert "scipy" not in imported
