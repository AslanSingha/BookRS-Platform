"""How well can a reader be served after one, three, five, ten, twenty ratings?

``thesis.py`` scores readers the model was trained on, each with at
least eight ratings to learn from. That is the case where collaborative
filtering is strongest and content adds almost nothing (measured: +0.0007
on P@10). The thesis's four-stage cold start exists for the opposite
case, and this module measures it.

**The readers here are new in the only sense that matters: the model has
never seen them.** A sample is removed before fitting, the model is
fitted on everyone else, and each removed reader is then shown to it a
few ratings at a time. Nothing about them is in the reader factors,
because they have none.

**Design.**

* Each cold reader's ratings are split 80/20 with the same function the
  main evaluation uses. The 20% is the target at every step, so a row
  can be read left to right: the same people, the same books to find,
  more known about them each time.
* From the 80%, the first 1, 3, 5, 10 and 20 ratings in a fixed random
  order are revealed. Only readers with at least 20 to reveal are used,
  so every column is the same cohort.
* Books a reader rated but that were neither revealed nor held out are
  simply unknown to the model. Recommending one earns nothing, which
  makes every number here an underestimate of the same size for every
  method.

**Methods.**

* *Most-rated books* -- what the thesis serves at zero ratings.
* *Content* -- nearest books to the mean vector of the revealed ones.
* *Folding-in, as in the thesis* -- solve for a reader vector against
  the frozen book factors using only the revealed books
  (``BookRS-System/app/services/recommender.py``, ``_compute_user_vector``,
  with every rating passed as 1.0, as that code passes it).
* *Folding-in, full* -- the same solve as Hu, Koren and Volinsky state
  it, and as ALS itself performs it for a reader during training: the
  books the reader did not rate enter as weak evidence of no interest
  rather than being left out. One extra matrix, computed once.
* *The thesis's stages* -- content below five ratings; from five,
  0.7 * content + 0.3 * folding-in; from twenty, 0.3 * content + 0.7.
* *The platform's stages* -- what the OPAC serves
  (``bookrs/recommend/personal.py``): below five ratings the rated-only
  solve, with the ratings' real confidences; from five, the full solve.
* *Best blend* -- alpha chosen per column on half the cold readers and
  scored, like every other row, on the other half.

**Measures.** Beside the thesis's P@10, MRR and HR@10 (any held-out
rating counts), the revised evaluation's (``revised.py``): P@10 with
liked = rated 4 or 5, as a share of the best any model could reach;
leave one out (one liked held-out book per reader); and genre match,
the share of the top 10 in the genres of the books the reader has
revealed so far, as a multiple of what a random list gets.

    docker compose run --rm recommend python -m bookrs.evaluation.coldstart --source-id 54
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import numpy as np
from scipy.sparse import csr_matrix

from bookrs.evaluation.revised import genre_match, load_genres, reader_genres, score_method
from bookrs.evaluation.thesis import (
    CONFIDENCE, blend, content_scores, evaluate, fit, load, load_embeddings,
    restrict, slice_works, split,
)

log = logging.getLogger("bookrs.evaluation")

STEPS = (1, 3, 5, 10, 20)
ALPHAS = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0)


def thesis_stage_alpha(n: int) -> float:
    """Weight on content at ``n`` ratings, as the thesis's recommender sets it."""
    if n < 5:
        return 1.0
    return 0.7 if n < 20 else 0.3


def reveal_order(u: np.ndarray, available: np.ndarray, seed: int) -> np.ndarray:
    """Position of each rating in its reader's reveal order (0 = first).

    ``available`` marks the ratings that may be revealed; the others get
    a position past any step. The order is random per reader and fixed
    by the seed, so "the first five" always contains "the first three".
    """
    rng = np.random.default_rng(seed)
    key = rng.random(len(u))
    key[~available] = 2.0                       # sorts after every real key
    order = np.lexsort((key, u))
    counts = np.bincount(u)
    starts = np.cumsum(counts) - counts
    position = np.empty(len(u), dtype=np.int64)
    position[order] = np.arange(len(u)) - starts[u[order]]
    position[~available] = np.iinfo(np.int64).max
    return position


def fold_in(given: csr_matrix, book_factors: np.ndarray, regularization: float,
            gram: np.ndarray | None) -> np.ndarray:
    """Solve for one vector per row of ``given`` against frozen book factors.

    ``given`` holds a confidence for each revealed book.

    With ``gram`` (``Y^T Y`` over the whole catalogue) this is the
    implicit-feedback solve in full:

        (Y^T Y + Y_u^T (C_u - I) Y_u + lambda I) x = Y_u^T C_u 1

    Without it, only the revealed books take part:

        (Y_u^T C_u Y_u + lambda I) x = Y_u^T C_u 1

    which is what the thesis's recommender and this service's
    ``recommend/folding.py`` both do. The difference is whether "did not
    rate" counts for anything. With five revealed books and 128
    dimensions the second system is fitted to five points and says
    nothing reliable about the rest of the catalogue; the first is
    anchored by all of it.
    """
    k = book_factors.shape[1]
    eye = regularization * np.eye(k)
    base = eye if gram is None else gram.astype(np.float64) + eye
    out = np.zeros((given.shape[0], k), dtype=np.float32)
    Y = book_factors.astype(np.float64)
    for row in range(given.shape[0]):
        lo, hi = given.indptr[row], given.indptr[row + 1]
        if lo == hi:
            continue
        B = Y[given.indices[lo:hi]]
        c = given.data[lo:hi].astype(np.float64)
        w = c if gram is None else c - 1.0
        out[row] = np.linalg.solve(base + (B.T * w) @ B, B.T @ c)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bookrs.evaluation.coldstart", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--cold-readers", type=int, default=20_000,
                    help="Readers withheld from training. Half choose alpha, "
                         "half are scored.")
    ap.add_argument("--factors", type=int, default=128)
    ap.add_argument("--regularization", type=float, default=0.1)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--confidence", choices=sorted(CONFIDENCE), default="1+2r+3d")
    ap.add_argument("--min-ratings", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--slice", default=None, metavar="KOHA_BIBLIOS_TSV",
                    help="Only the fixture slice's books (koha_biblios.tsv), as "
                         "the thesis evaluated, when the library holds more.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        data = restrict(load(conn, a.source_id,
                                 slice_works(conn, a.source_id, a.slice) if a.slice else None), a.min_ratings)
        vectors, n_vectors, model = load_embeddings(conn, a.source_id, data.works)
        genre, genre_names = load_genres(conn, data.works)
    if not n_vectors:
        raise SystemExit(
            f"no content vectors for source {a.source_id}. Create them with:\n"
            "  docker compose run --rm embedding python -m bookrs.embedding.cli "
            f"--source-id {a.source_id}")
    n_readers, n_works = len(data.patrons), len(data.works)
    conf = CONFIDENCE[a.confidence](data.r.astype(np.float32),
                                    data.d.astype(np.float32)).astype(np.float32)

    held = split(data.u, n_readers, 0.2, a.seed)
    position = reveal_order(data.u, ~held, a.seed + 2)
    can_reveal = np.bincount(data.u[~held], minlength=n_readers)
    eligible = np.flatnonzero(can_reveal >= max(STEPS))
    rng = np.random.default_rng(a.seed + 3)
    cold = np.sort(rng.choice(eligible, size=min(a.cold_readers, len(eligible) // 2),
                              replace=False))
    is_cold = np.zeros(n_readers, dtype=bool)
    is_cold[cold] = True
    shuffled = rng.permutation(cold)
    tuning, scored = np.sort(shuffled[:len(cold) // 2]), np.sort(shuffled[len(cold) // 2:])
    log.info("%d readers, %d works; %d readers have %d or more ratings to reveal; "
             "%d withheld from training (%d choose alpha, %d scored)",
             n_readers, n_works, len(eligible), max(STEPS), len(cold),
             len(tuning), len(scored))

    shape = (n_readers, n_works)
    warm = ~is_cold[data.u]
    t0 = time.monotonic()
    _, book_f = fit(csr_matrix((conf[warm], (data.u[warm], data.i[warm])), shape=shape),
                    a.factors, a.regularization, a.iterations, a.seed)
    gram = book_f.T.astype(np.float64) @ book_f.astype(np.float64)
    log.info("ALS fitted without them in %.0fs", time.monotonic() - t0)

    popularity = np.bincount(data.i[warm], minlength=n_works).astype(np.float32)
    cold_held = held & ~warm
    target = csr_matrix((np.ones(int(cold_held.sum()), dtype=np.int8),
                         (data.u[cold_held], data.i[cold_held])), shape=shape)
    cold_liked = cold_held & (data.r >= 4)
    liked = csr_matrix((np.ones(int(cold_liked.sum()), dtype=np.int8),
                        (data.u[cold_liked], data.i[cold_liked])), shape=shape)
    # One liked held-out book per cold reader, for leave one out.
    loo = np.full(n_readers, -1, dtype=np.int64)
    lu, li = data.u[cold_liked], data.i[cold_liked]
    pick = np.lexsort((rng.random(len(lu)), lu))
    first_of = np.ones(len(pick), dtype=bool)
    first_of[1:] = lu[pick][1:] != lu[pick][:-1]
    loo[lu[pick][first_of]] = li[pick][first_of]
    known = genre >= 0
    share = (np.bincount(genre[known], minlength=len(genre_names)).astype(float)
             / max(known.sum(), 1))
    chunk = int(max(16, min(4096, 50_000_000 // max(n_works, 1))))

    results: dict[str, list] = {}
    chosen: list[float] = []

    for n in STEPS:
        shown = ~warm & (position < n)
        given = csr_matrix((conf[shown], (data.u[shown], data.i[shown])), shape=shape)
        binary = (given > 0).astype(np.float32).tocsr()
        count = np.asarray(binary.sum(axis=1)).ravel()

        # As the thesis's recommender calls it: every rating passed as
        # 1.0, so every confidence is 1 + 2 * 1.
        as_thesis = given.copy()
        as_thesis.data[:] = 3.0

        rows = cold                                   # solve once for all cold readers
        slot = np.full(n_readers, -1, dtype=np.int64)
        slot[rows] = np.arange(len(rows))
        x_thesis = fold_in(as_thesis[rows], book_f, a.regularization, None)
        x_full = fold_in(given[rows], book_f, a.regularization, gram)
        # The platform: the rated-only solve with real confidences below
        # five ratings, the full solve from five.
        x_platform = (x_full if n >= 5 else
                      fold_in(given[rows], book_f, a.regularization, None))

        def content(who):
            return content_scores(binary, count, vectors, who)

        def folded(x):
            return lambda who: x[slot[who]] @ book_f.T

        def mix(alpha, cf):
            if alpha == 0.0:
                return cf
            if alpha == 1.0:
                return content
            return lambda who: blend(alpha, content(who), cf(who))

        def popular(who):
            return np.tile(popularity, (len(who), 1))

        sweep = [evaluate(mix(al, folded(x_full)), given, target, tuning).p10
                 for al in ALPHAS]
        best = ALPHAS[int(np.argmax(sweep))]
        chosen.append(best)

        own = (reader_genres([binary[r].indices for r in scored], genre, len(genre_names))
               if genre_names else [])
        for name, rows_fn in (
                ("most-rated books", popular),
                ("content only", content),
                ("folding-in, as in the thesis", folded(x_thesis)),
                ("folding-in, full", folded(x_full)),
                ("the thesis's stages", mix(thesis_stage_alpha(n), folded(x_thesis))),
                ("the platform's stages", folded(x_platform)),
                ("best blend, content + full folding-in", mix(best, folded(x_full)))):
            m, top10 = score_method(name, rows_fn, scored, given, liked, target, loo, chunk)
            if genre_names:
                match, expected, _ = genre_match(top10, own, genre, share)
                m.genre = {"match": match, "random": expected}
            results.setdefault(name, []).append(m)
        log.info("%2d revealed: done (alpha %.1f chosen)", n, best)

    first = next(iter(results.values()))[0]
    head = f"{'ratings revealed':40}" + "".join(f"{n:>9}" for n in STEPS)
    out = [
        "", f"Cold start. Source {a.source_id}: {len(scored):,} readers the model was not "
            f"trained on, each with a fixed set of held-out books to find.",
        f"ALS k={a.factors}, lambda={a.regularization:g}, {a.iterations} iterations, "
        f"c = {a.confidence}. Vectors: {model}.",
    ]

    def table(title, pick):
        rows = ["", title, head]
        for name, row in results.items():
            rows.append(f"  {name:38}" + "".join(
                f"{v:>9.4f}" if np.isfinite(v := pick(m)) else f"{'-':>9}" for m in row))
        return rows

    def get(d, k):
        return float(d.get(k, float("nan")))

    out += table(f"P@10, any held-out rating (best possible "
                 f"{get(first.heldout_any, 'ceiling'):.4f})",
                 lambda m: get(m.heldout_any, "p10"))
    out += table("MRR, any held-out rating", lambda m: get(m.heldout_any, "mrr"))
    out += table("HR@10, any held-out rating (at least one found)",
                 lambda m: get(m.heldout_any, "hr10"))
    out += table(f"P@10, liked = 4 or 5, as a share of the best possible "
                 f"({get(first.heldout_liked, 'ceiling'):.4f})",
                 lambda m: get(m.heldout_liked, "of_ceiling"))
    out += table("Leave one out: HR@10 (one liked held-out book in the top 10)",
                 lambda m: get(m.loo, "hr10"))
    if genre_names:
        out += table("Genre match: share of the top 10 in the revealed books' genres, "
                     "as a multiple of a random list",
                     lambda m: get(m.genre, "match") / get(m.genre, "random"))
    out += ["", f"{'alpha chosen for the best blend':40}"
            + "".join(f"{al:>9.1f}" for al in chosen),
            f"{'alpha in the thesis at that stage':40}"
            + "".join(f"{thesis_stage_alpha(n):>9.1f}" for n in STEPS),
            f"standard error on P@10 is about {get(first.heldout_any, 'p10_se'):.4f} "
            "throughout."]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
