"""Hybrid ranking: collaborative re-ranking over a content-based candidate set.

This module imports numpy and nothing else. It is deliberately separate
from ``als.py``, which imports ``implicit``: the API service ranks but
never fits, and ``implicit`` publishes no Linux wheels, so requiring it
in the API image would add a C toolchain for code that never runs there.

**Collaborative filtering re-ranks; it does not retrieve.** Candidates
are drawn from the content layer, and the collaborative signal reorders
them. It cannot introduce a work the embeddings did not already consider
related.

That is a response to a measured failure rather than a preference. On
generated circulation the factors carry a positive co-borrowing signal
(+0.221 between co-borrowed pairs and unrelated ones) while the nearest
neighbours of a Springsteen biography are a special-education
dissemination model and two C++ books -- see
``docs/marc-field-analysis.md`` 14.7. That happened because the
collaborative layer was used as the candidate source. A re-ranker
bounds the damage: with a noisy signal it shuffles plausible results,
where a retriever surfaces implausible ones.

The cost is real and worth naming. Collaborative retrieval's distinctive
value is finding works that share readers but not vocabulary, and a
re-ranker cannot surface those. ``pool`` is configuration precisely so a
deployment with real circulation can widen the candidate set toward that
behaviour once there is evidence to justify it.

Three findings from the reference corpus shape the rest:

1. **Both signals are unit vectors**, so both scores are cosines and a
   linear blend is scale-safe. Factors are L2-normalised in ``als.py``;
   embeddings at generation.

2. **Their distributions differ.** Across the factorised works,
   collaborative pair scores centre near zero (mean +0.024, sd 0.199)
   while content scores centre well above it (mean +0.207, sd 0.150) --
   library books share vocabulary, so semantic relatedness has a high
   floor. Imputing a fixed constant for an absent collaborative score
   would therefore place unfactorised works at an arbitrary point in a
   distribution they are not part of. The imputation is per-query and
   defined below.

3. **Cosine magnitude says nothing about how much evidence backs it.**
   The only two collinear pairs in the corpus (cosine 1.0000) are works
   borrowed by an identical pair of patrons -- ALS had no information to
   separate them, so it did not. Read naively that is the strongest
   possible endorsement; it is in fact the weakest. ``min_patrons``
   exists to keep such vectors out of the ranking entirely.

None of these weights is tuned. Doing so would require circulation that
reflects real borrowing, and the generated data explicitly does not.
They are configuration for the same reason the confidence weights are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

__all__ = ["RankWeights", "Candidate", "Ranked", "rerank"]


@dataclass(frozen=True)
class RankWeights:
    """Deployment configuration for the hybrid ranker.

    ``content`` and ``collaborative`` need not sum to 1; only their ratio
    affects the ordering. They are expressed as a pair anyway, because a
    reader comparing two deployments should be able to see the balance
    without arithmetic.
    """

    content: float = 0.7
    collaborative: float = 0.3

    #: Minimum distinct borrowers before a work's factors may rank.
    #: Below this the vector is an artefact of too little data: two works
    #: sharing their only two borrowers are collinear at cosine 1.0000,
    #: which a ranker would read as a perfect match.
    min_patrons: int = 3

    #: Minimum scored candidates before the collaborative term is used
    #: at all. Fewer than this and the per-query median is not a
    #: summary of anything.
    min_scored: int = 3

    #: Content candidates considered before re-ranking. Wider means the
    #: collaborative signal can reach further down the semantic ranking;
    #: narrower keeps it closer to pure content-based behaviour.
    pool: int = 50

    def __post_init__(self) -> None:
        if self.content < 0 or self.collaborative < 0:
            raise ValueError("weights must not be negative")
        if self.content == 0 and self.collaborative == 0:
            raise ValueError("at least one weight must be non-zero")
        if self.min_patrons < 1:
            raise ValueError("min_patrons must be at least 1")
        if self.pool < 1:
            raise ValueError("pool must be at least 1")
        if self.min_scored < 1:
            raise ValueError("min_scored must be at least 1")


@dataclass(frozen=True)
class Candidate:
    """A content-layer result, optionally carrying collaborative factors."""

    work_id: int
    content_score: float
    factors: np.ndarray | None = None
    n_patrons: int = 0


@dataclass(frozen=True)
class Ranked:
    """A ranked result and the signals that produced it.

    ``signal`` is reported rather than inferred by the caller because
    "these two works share readers" and "these two works are about the
    same thing" are different claims, and a library displaying them
    should be able to tell which it is making.
    """

    work_id: int
    score: float
    signal: str  # "content" or "hybrid"
    content_score: float
    collaborative_score: float | None = None


def _usable(candidate: Candidate, weights: RankWeights) -> bool:
    return (
        candidate.factors is not None
        and candidate.n_patrons >= weights.min_patrons
    )


def rerank(
    candidates: Sequence[Candidate],
    query_factors: np.ndarray | None,
    query_n_patrons: int,
    weights: RankWeights,
    limit: int,
) -> list[Ranked]:
    """Reorder content candidates by blending in a collaborative score.

    Returns at most ``limit`` results, highest score first.

    The collaborative term is skipped entirely -- and the content
    ordering returned unchanged -- when the query work has no factors,
    when its factors rest on too few borrowers, or when no candidate
    carries usable factors. A deployment with no circulation history
    therefore behaves exactly as the content-only system did, with no
    separate code path to keep working.

    **Absent collaborative scores are imputed with the median of the
    present ones**, computed per query. Zero would be wrong: it is
    neither the centre of the collaborative distribution nor a neutral
    point within it, so unfactorised works would be systematically
    advantaged or penalised by nothing more than the sparsity of the
    library's circulation. The median puts them in the middle of the
    candidates that do have evidence, so only an actual signal -- above
    or below its peers -- moves a work from where content placed it.
    """
    # Ties broken on work_id, not left to sort stability over the
    # caller's input order: otherwise the same catalogue returns two
    # different orderings depending on whether circulation exists, and
    # equal-scoring works reshuffle for no reason a reader could see.
    ordered = sorted(candidates, key=lambda c: (-c.content_score, c.work_id))
    ordered = ordered[: weights.pool]

    query_usable = (
        query_factors is not None and query_n_patrons >= weights.min_patrons
    )
    scored = [c for c in ordered if _usable(c, weights)] if query_usable else []

    # A median needs enough observations to be one. With a single
    # scored candidate the "median" is that work's own score, which
    # every unscored candidate then inherits -- a constant added to
    # every result, incapable of changing the order and capable of
    # moving every score by an arbitrary amount. Measured on the
    # reference corpus: a pool of 50 around the highest-evidence query
    # contained exactly one eligible work, and its -0.123 was applied
    # to all six results.
    #
    # Below the floor the collaborative layer abstains rather than
    # contributing an offset it cannot justify.
    if len(scored) < weights.min_scored:
        scored = []

    if not scored:
        return [
            Ranked(
                work_id=c.work_id,
                score=c.content_score,
                signal="content",
                content_score=c.content_score,
            )
            for c in ordered[:limit]
        ]

    assert query_factors is not None  # implied by query_usable
    collaborative = {
        c.work_id: float(np.dot(query_factors, c.factors)) for c in scored
    }
    fallback = float(np.median(list(collaborative.values())))

    total = weights.content + weights.collaborative
    results = [
        Ranked(
            work_id=c.work_id,
            score=(
                weights.content * c.content_score
                + weights.collaborative
                * collaborative.get(c.work_id, fallback)
            )
            / total,
            signal="hybrid" if c.work_id in collaborative else "content",
            content_score=c.content_score,
            collaborative_score=collaborative.get(c.work_id),
        )
        for c in ordered
    ]

    # Ties are broken on the content score, which is defined for every
    # candidate, rather than left to sort stability over an ordering the
    # caller cannot see.
    results.sort(key=lambda r: (-r.score, -r.content_score, r.work_id))
    return results[:limit]


def factor_map(
    rows: Iterable[tuple[int, Sequence[float], int]],
) -> dict[int, tuple[np.ndarray, int]]:
    """Build a lookup from ``(work_id, vector, n_patrons)`` rows."""
    return {
        work_id: (np.asarray(vector, dtype=np.float32), n_patrons)
        for work_id, vector, n_patrons in rows
    }
