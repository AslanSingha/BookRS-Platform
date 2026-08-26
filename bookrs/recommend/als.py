"""Matrix factorisation over circulation history.

Alternating Least Squares for implicit feedback, per Hu, Koren and
Volinsky (2008). The BookRS-System thesis established the parameters
used here as defaults -- k=128 factors, lambda=0.1, and ALS over BPR at
both the 10% and full-scale evaluations.

What this produces, and what it does not:

**Item factors** are the useful output. Two works are close in factor
space when the same patrons borrowed both, which gives an item-to-item
similarity grounded in behaviour rather than text. That is what the OPAC
widget serves, and it needs no patron identity at request time.

**User factors** are computed and discarded. Personalised
recommendations would need to know which patron is asking, and this
service is anonymous by design -- the widget calls it without
credentials. Storing user factors would mean holding a behavioural
profile per patron for a feature that does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import psycopg
from scipy.sparse import csr_matrix

from bookrs.recommend.confidence import ConfidenceWeights, Interaction, confidence

log = logging.getLogger(__name__)

# Bump when the factorisation changes in a way that makes stored factors
# incomparable to new ones: different dimensionality, a different
# confidence formula, a different algorithm. Same guard as the embedder
# and mapper versions, one layer along.
FACTORISER_VERSION = 1


@dataclass
class ALSParams:
    """Hyperparameters.

    Defaults follow the BookRS-System thesis, which tuned them over
    26 million interactions. Whether they transfer to a single library's
    circulation -- three orders of magnitude smaller and differently
    shaped -- is untested.
    """

    # None means "choose from the data". A fixed value does not
    # transfer across scales: the BookRS-System thesis tuned k=128 over
    # 26 million interactions, and on 177 interactions that same value
    # measurably degrades the factors. Scored by whether co-borrowed
    # works embed closer than unrelated ones, k=8 gave a gap of +0.478
    # against k=128's +0.289, declining monotonically in between --
    # 128 factors against a 21x57 matrix is more parameters than
    # observations, so the surplus dimensions fit noise.
    #
    # Where the crossover lies for a real library, somewhere between
    # these two scales, is unmeasured.
    factors: int | None = None
    regularization: float = 0.1
    iterations: int = 15
    weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)

    # Works borrowed by only one patron contribute nothing to
    # item-to-item similarity: with no co-occurrence there is no
    # evidence linking them to anything. They are excluded from
    # training rather than fitted to noise, and fall back to the
    # embedding layer.
    min_patrons_per_work: int = 2

    # A patron with a single loan cannot express a preference pattern.
    min_works_per_patron: int = 2

    def __post_init__(self) -> None:
        if self.factors is not None and self.factors < 1:
            raise ValueError("factors must be positive")
        if self.min_patrons_per_work < 2:
            raise ValueError(
                "a work borrowed by one patron carries no co-occurrence "
                "signal; the minimum is 2"
            )


@dataclass
class TrainingStats:
    interactions: int = 0
    patrons: int = 0
    works: int = 0
    excluded_works: int = 0
    excluded_patrons: int = 0
    density: float = 0.0
    seconds: float = 0.0


def load_interactions(conn: psycopg.Connection, source_id: int | None = None
                      ) -> list[Interaction]:
    """Aggregate loans into one interaction per (patron, work).

    A patron who borrowed the same work three times is one interaction
    with n_loans=3, not three interactions -- the repetition is the
    signal, and the confidence formula reads it.
    """
    rows = conn.execute(
        """
        SELECT patron_ref, work_id, count(*) AS n_loans,
               coalesce(sum(renewals), 0) AS n_renewals
        FROM loans
        WHERE (%(source_id)s::int IS NULL OR source_id = %(source_id)s::int)
        GROUP BY patron_ref, work_id
        """,
        {"source_id": source_id},
    ).fetchall()
    return [Interaction(patron_ref=r[0], work_id=r[1],
                        n_loans=int(r[2]), n_renewals=int(r[3]))
            for r in rows]


def _filter(interactions: list[Interaction], params: ALSParams
            ) -> tuple[list[Interaction], int, int]:
    """Drop patrons and works too sparse to contribute.

    Applied repeatedly: removing thin works can leave a patron below the
    threshold, and vice versa. Two passes settle it in practice; the loop
    guards against the cases where they do not.
    """
    kept = interactions
    excluded_works = excluded_patrons = 0

    for _ in range(5):
        work_counts: dict[int, int] = {}
        patron_counts: dict[str, int] = {}
        for i in kept:
            work_counts[i.work_id] = work_counts.get(i.work_id, 0) + 1
            patron_counts[i.patron_ref] = patron_counts.get(i.patron_ref, 0) + 1

        remaining = [
            i for i in kept
            if work_counts[i.work_id] >= params.min_patrons_per_work
            and patron_counts[i.patron_ref] >= params.min_works_per_patron
        ]
        if len(remaining) == len(kept):
            break
        kept = remaining

    final_works = {i.work_id for i in kept}
    final_patrons = {i.patron_ref for i in kept}
    excluded_works = len({i.work_id for i in interactions}) - len(final_works)
    excluded_patrons = len({i.patron_ref for i in interactions}) - len(final_patrons)
    return kept, excluded_works, excluded_patrons


def build_matrix(interactions: list[Interaction], params: ALSParams
                 ) -> tuple[csr_matrix, list[str], list[int], TrainingStats]:
    """Build the confidence-weighted patron-by-work matrix."""
    kept, ex_works, ex_patrons = _filter(interactions, params)
    if not kept:
        raise ValueError(
            "No interactions survive filtering. A catalogue needs works "
            "borrowed by at least two patrons before collaborative "
            "filtering has anything to learn from."
        )

    patrons = sorted({i.patron_ref for i in kept})
    works = sorted({i.work_id for i in kept})
    patron_index = {p: n for n, p in enumerate(patrons)}
    work_index = {w: n for n, w in enumerate(works)}

    rows = np.fromiter((patron_index[i.patron_ref] for i in kept), dtype=np.int32)
    cols = np.fromiter((work_index[i.work_id] for i in kept), dtype=np.int32)
    data = np.fromiter((confidence(i, params.weights) for i in kept),
                       dtype=np.float32)

    matrix = csr_matrix((data, (rows, cols)), shape=(len(patrons), len(works)))
    stats = TrainingStats(
        interactions=len(kept),
        patrons=len(patrons),
        works=len(works),
        excluded_works=ex_works,
        excluded_patrons=ex_patrons,
        density=100.0 * len(kept) / (len(patrons) * len(works)),
    )
    return matrix, patrons, works, stats


def choose_factors(matrix: csr_matrix) -> int:
    """Pick a factor count the data can support.

    A rank-k factorisation of an m-by-n matrix with z observations fits
    k*(m+n) parameters. Keeping that comfortably below z is what stops
    the surplus dimensions modelling noise; the divisor of 4 is a
    convention rather than a derived constant.

    Bounded at 16 and 128: below 16 there is too little capacity to
    separate anything, and 128 is where the thesis settled at a scale
    far beyond anything a single library reaches.

    Worked examples, to make the behaviour concrete:

    ======================  ===========  ====
    matrix                  observations    k
    ======================  ===========  ====
    25 x 30 (this corpus)            87    16
    1,000 x 1,000 @ 10%         100,000    16
    50,000 x 20,000 @ 0.1%    1,000,000    16
    5,000 x 5,000 @ 5%        1,250,000    31
    5,000 x 5,000 @ 20%       5,000,000   125
    ======================  ===========  ====

    The third row is worth noting: a million observations spread across
    70,000 rows and columns still only supports the floor, because
    k*(m+n) parameters at k=16 already exceeds the observation count.
    That is a real property of factorising very sparse matrices rather
    than a quirk of the divisor -- but the divisor of 4 is a convention
    and has not been tuned against anything.
    """
    observations = matrix.nnz
    rows, cols = matrix.shape
    supported = max(1, observations // (4 * (rows + cols)))
    return int(min(128, max(16, supported)))


def train(matrix: csr_matrix, params: ALSParams) -> np.ndarray:
    """Fit ALS and return the item factors, L2-normalised.

    Normalising at training time means a dot product between two rows is
    cosine similarity, matching how the embedding vectors are stored and
    letting the two be blended without rescaling.
    """
    from implicit.als import AlternatingLeastSquares

    # implicit expects users on rows for fit(); item factors come back
    # as model.item_factors.
    factors_k = params.factors if params.factors is not None else choose_factors(matrix)
    log.info("factorising with k=%d over %d observations", factors_k, matrix.nnz)

    model = AlternatingLeastSquares(
        factors=factors_k,
        regularization=params.regularization,
        iterations=params.iterations,
        use_gpu=False,
        calculate_training_loss=False,
    )
    model.fit(matrix, show_progress=False)

    factors = np.asarray(model.item_factors, dtype=np.float32)
    norms = np.linalg.norm(factors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return factors / norms
