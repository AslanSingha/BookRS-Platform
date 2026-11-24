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

__all__ = ["FoldedPatron", "fold_in", "FactorSpace", "place_reader",
           "FULL_SOLVE_FROM"]


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


# --------------------------------------------------------------------
# Placing a reader, as measured.
#
# fold_in() above solves over the works a patron has interacted with and
# nothing else. bookrs/evaluation/coldstart.py measured that solve
# against the one Hu, Koren and Volinsky state -- the one ALS itself
# performs for each reader during training -- on 10,000 readers withheld
# from the fit, revealing their ratings a few at a time (P@10):
#
#     ratings revealed        1      3      5     10     20
#     most-rated works     .090   .091   .092   .096   .102
#     observed works only  .099   .098   .099   .097   .090
#     whole catalogue      .056   .085   .102   .131   .179
#
# The observed-only solve does not improve as it is told more, and is
# worse at twenty ratings than at one. With k=128 and a handful of
# observations it is fitted to those few points and free everywhere
# else; what it returns is close to a popularity list. The full solve
# counts every work the reader did not choose as weak evidence, which is
# what ties the answer to the rest of the catalogue.
#
# Below five observations the order reverses: one or three points are
# too few to locate anyone, and the popularity-like answer is the better
# one. The crossover sits at five, which is where the thesis put its own
# threshold.
#
# That measurement was on ratings. fold_in() is left as it is for the
# loan path, where nothing has been measured, and the functions below
# are what a personalised answer should be built on.
# --------------------------------------------------------------------

#: Observations from which the whole-catalogue solve is used.
FULL_SOLVE_FROM = 5

#: Confidence given to each observation in the observed-only solve. The
#: thesis's recommender passes every rating as 1.0, so every confidence
#: is 1 + 2 * 1. Kept, because this is the setting that was measured.
_FEW_CONFIDENCE = 3.0


@dataclass(frozen=True)
class FactorSpace:
    """The fitted work factors, unnormalised, and what is derived from them.

    ``gram`` is ``Y^T Y`` over every work. It is what lets the solve
    account for the whole catalogue without visiting it: k by k, computed
    once per fit, however many works there are.
    """

    work_ids: np.ndarray        # (n,)
    factors: np.ndarray         # (n, k), as fitted
    n_patrons: np.ndarray       # (n,)
    gram: np.ndarray            # (k, k)
    index: Mapping[int, int]    # work_id -> row
    # Rows with the same number are editions of one book (personal.py's
    # work_key). None: every row its own book.
    groups: np.ndarray | None = None

    def with_groups(self, keys) -> "FactorSpace":
        """A copy with rows sharing a key given the same group number."""
        from dataclasses import replace

        numbers: dict[str, int] = {}
        return replace(self, groups=np.array(
            [numbers.setdefault(k, len(numbers)) for k in keys], dtype=np.int64))

    @classmethod
    def from_rows(cls, rows) -> "FactorSpace":
        """From ``(work_id, raw_vector, n_patrons)`` rows."""
        rows = [r for r in rows if r[1] is not None]
        if not rows:
            raise ValueError(
                "no raw factors stored. Refit with this version "
                "(python -m bookrs.recommend.cli); factors written before "
                "migration 4 carry only the normalised form.")
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        Y = np.array([r[1] for r in rows], dtype=np.float32)
        Yd = Y.astype(np.float64)
        return cls(ids, Y, np.array([r[2] for r in rows], dtype=np.int64),
                   Yd.T @ Yd, {int(w): n for n, w in enumerate(ids)})

    def __len__(self) -> int:
        return len(self.work_ids)


def place_reader(confidences: Mapping[int, float], space: FactorSpace,
                 regularization: float = 0.1) -> tuple[np.ndarray | None, str]:
    """A reader's vector in the factor space, and which solve produced it.

    ``confidences`` maps each work the reader has rated to the
    confidence of that rating. Works with no factors are ignored.

    Returns ``(None, "none")`` for a reader with nothing usable: there is
    no vector to compute, and the caller should show what most readers
    chose. Otherwise the vector ``x`` is such that ``space.factors @ x``
    scores every work -- unnormalised on purpose, because it is used for
    ranking one reader's list, not compared across readers.
    """
    known = [(space.index[w], c) for w, c in confidences.items() if w in space.index]
    if not known:
        return None, "none"

    rows = np.array([r for r, _ in known], dtype=np.int64)
    B = space.factors[rows].astype(np.float64)
    eye = regularization * np.eye(B.shape[1])

    if len(known) < FULL_SOLVE_FROM:
        c = np.full(len(known), _FEW_CONFIDENCE)
        x = np.linalg.solve((B.T * c) @ B + eye, B.T @ c)
        return x.astype(np.float32), "few"

    c = np.array([c for _, c in known], dtype=np.float64)
    x = np.linalg.solve(space.gram + (B.T * (c - 1.0)) @ B + eye, B.T @ c)
    return x.astype(np.float32), "full"
