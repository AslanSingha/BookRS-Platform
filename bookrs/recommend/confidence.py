"""Confidence weighting for implicit circulation signals.

Follows Hu, Koren and Volinsky (2008): an implicit-feedback matrix has
no negative examples, so every observed interaction is treated as a
positive preference with a *confidence* that scales with the strength of
the evidence. Unobserved pairs get confidence 1, meaning "no reason to
believe either way".

The BookRS-System thesis used ``c = 1 + 2r + 3d`` over Goodreads data,
where ``r`` is a 1-5 star rating and ``d`` flags a written review. That
formula cannot be carried over: library circulation has neither term, so
every entry would evaluate to exactly 1 and the weighting would silently
do nothing.

What circulation does supply is **intensity** -- how much a patron
engaged -- rather than **valence** -- whether they liked it. That
distinction matters, and the thesis measured its cost directly:
Experiment 6, Config D removed the star rating and kept only a binary
engagement flag, scoring P@10 = 0.1641 at full scale, 22.1% below the
rating-weighted baseline. Library data is structurally close to that
configuration, so a drop of similar size should be the expectation until
measured otherwise.

The terms here are the signals that plausibly carry some valence,
ordered by how much:

* **Repeat borrowing** -- the same patron taking the same work out
  again. Rare, and about as close to an endorsement as circulation gets.
* **Renewal** -- post-consumption and deliberate: the patron was still
  reading and chose to keep it.
* **The loan itself** -- pure intensity. A patron chose this book off a
  shelf, which is not nothing, but says nothing about what they thought
  of it.

Weights are configuration rather than constants, because which values
are right is an open empirical question that needs real circulation data
to answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceWeights:
    """Per-signal weights for the confidence formula.

    ``c = 1 + loan*n_loans + renewal*n_renewals + repeat*(n_loans - 1)``

    The defaults are a starting point, not a finding. They encode an
    ordering -- a repeat borrow counts for more than a renewal, which
    counts for more than a bare loan -- that follows from how much
    valence each signal plausibly carries, and nothing stronger. The
    grid search that would justify specific values needs real
    circulation history.
    """

    loan: float = 1.0
    renewal: float = 2.0
    repeat: float = 4.0

    # A single patron borrowing one work forty times should not dominate
    # the factorisation. Real circulation contains such cases -- a
    # teacher renewing a class set, a data-entry error -- and ALS is
    # sensitive to extreme confidence values.
    ceiling: float = 40.0

    def __post_init__(self) -> None:
        if min(self.loan, self.renewal, self.repeat) < 0:
            raise ValueError("weights must not be negative")
        if self.ceiling <= 1:
            raise ValueError("ceiling must exceed the baseline confidence of 1")


@dataclass(frozen=True)
class Interaction:
    """One patron's complete history with one work."""

    patron_ref: str
    work_id: int
    n_loans: int
    n_renewals: int

    @property
    def n_repeats(self) -> int:
        """Times the work was borrowed again after the first."""
        return max(0, self.n_loans - 1)


def confidence(interaction: Interaction, weights: ConfidenceWeights) -> float:
    """Confidence that this patron prefers this work.

    Never below 1: that is the baseline for an unobserved pair, and an
    observed one cannot carry less evidence than no observation at all.
    """
    value = (
        1.0
        + weights.loan * interaction.n_loans
        + weights.renewal * interaction.n_renewals
        + weights.repeat * interaction.n_repeats
    )
    return min(max(value, 1.0), weights.ceiling)
