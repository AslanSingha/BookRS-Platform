"""Folding-in: a personal vector for one patron, without refitting.

The BookRS-System thesis computes this at query time for a patron with
enough ratings to place them in factor space, without waiting for the
next scheduled fit -- five or more ratings unlocks it there. This module
adapts the same closed-form solve to circulation, which is the only
signal a library produces (see ``confidence.py``).

**The maths is standard implicit-ALS folding-in.** Item factors are held
fixed; the one missing patron vector is the solution to a small
regularised least-squares problem:

    x = (Y^T C Y + lambda I)^-1 Y^T C p

``Y`` is the factor matrix for the works this patron has loaned, drawn
straight from ``work_factors`` -- nothing new to compute there. ``C`` is
a diagonal of confidence values from the existing formula in
``confidence.py``, unchanged. ``p`` is a vector of ones: implicit
feedback records that an interaction occurred, not how much it was
liked, so every observed pair is an equally-weighted target and only the
confidence varies. ``lambda`` is the same regularisation
``ALSParams.regularization`` already carries for the batch fit.

**This module imports numpy and nothing else**, the same boundary
``rank.py`` states explicitly for the same reason: it must be callable
from the API process without pulling in ``implicit``, which needs a C
toolchain the API image does not carry. Folding-in runs inside a
request, so this constraint applies to it even more directly than to
the batch trainer.

**The evidence floor was measured, not copied from the thesis.** The
thesis's "five ratings" was tuned for rating data at a different scale;
copying it here would repeat the mistake ``rank.py``'s own history
already records once. Counting, per patron, how many of their loans
land on works that already carry factors: one patron reached 17, the
next cluster sat at 6-7, a longer tail ran 3-5, and nothing appeared
below 3. ``min_factorised_works`` is set at that break, matching
``rank.py``'s ``min_scored`` both in value and in reasoning -- fewer
than three observations is not a distribution, whichever formula is
built from it.

**What this deliberately does not do.** It is a pure function with no
API route. Folding-in a real patron and exposing the result changes what
kind of system this is: every other endpoint answers the same way to
any caller, and a personalised endpoint would let anyone holding a
patron's hashed reference query a shadow of that person's borrowing
pattern. That is a new kind of exposure pseudonymisation does not cover,
and it needs a real authentication decision before this is wired to
anything a network can reach -- not a decision to make inside a ranking
module. See ``docs/marc-field-analysis.md`` for where that discussion
belongs once it happens.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from bookrs.recommend.confidence import ConfidenceWeights, Interaction, confidence

__all__ = ["FoldedPatron", "fold_in"]


@dataclass(frozen=True)
class FoldedPatron:
    """The result of attempting to fold in one patron.

    ``vector`` is ``None`` exactly when personalisation should not be
    attempted -- the same abstention discipline ``rerank()`` applies to
    the collaborative term generally, reported here as a value rather
    than an exception so a caller can log *why* without a try/except.

    ``n_works`` and ``n_loans`` are reported even on abstention, so a
    caller (or a test) can see how close a patron came to the floor
    rather than only that they did not clear it.
    """

    vector: np.ndarray | None
    n_works: int
    n_loans: int

    @property
    def personalised(self) -> bool:
        return self.vector is not None


def fold_in(
    interactions: Sequence[Interaction],
    item_factors: Mapping[int, np.ndarray],
    weights: ConfidenceWeights,
    regularization: float = 0.1,
    min_factorised_works: int = 3,
) -> FoldedPatron:
    """Compute one patron's vector from their loans, or abstain.

    ``interactions`` must all belong to the same patron -- one call, one
    person. Loans on works with no factors contribute nothing and are
    filtered out before the evidence floor is checked, so a patron with
    ten loans on unfactorised works and two on factorised ones is judged
    on the two, not the ten: the ten carry no information this solve can
    use.

    ``regularization`` defaults to match ``ALSParams.regularization``'s
    default deliberately. The two need not be identical for the solve to
    be well-defined -- ``lambda`` here only controls how far the fitted
    vector shrinks toward zero when evidence is thin -- but using the
    same value by default keeps one fewer independent knob to reason
    about. A deployment that retunes the batch fit's regularisation
    should pass the same value here.
    """
    usable = [i for i in interactions if i.work_id in item_factors]

    if len(usable) < min_factorised_works:
        return FoldedPatron(
            vector=None,
            n_works=len(usable),
            n_loans=sum(i.n_loans for i in usable),
        )

    Y = np.stack(
        [np.asarray(item_factors[i.work_id], dtype=np.float64) for i in usable]
    )
    c = np.array([confidence(i, weights) for i in usable], dtype=np.float64)

    k = Y.shape[1]
    weighted = c[:, None] * Y
    YtCY = Y.T @ weighted
    # p is all ones, so Y^T C p reduces to a confidence-weighted sum of
    # the factor rows rather than a second matrix product.
    YtCp = weighted.sum(axis=0)

    x = np.linalg.solve(YtCY + regularization * np.eye(k), YtCp)

    # Item factors are L2-normalised in als.py so that a dot product is
    # a cosine directly; the folded patron vector must be normalised the
    # same way, or every comparison rerank() makes against it silently
    # stops being a cosine.
    norm = np.linalg.norm(x)
    if norm > 0:
        x = x / norm

    return FoldedPatron(
        vector=x.astype(np.float32),
        n_works=len(usable),
        n_loans=sum(i.n_loans for i in usable),
    )
