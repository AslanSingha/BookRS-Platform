"""Does the model find taste that is really there?

The real evaluation (``thesis.py``) scores the model against what real
readers happened to rate. Two things hold that score down for every
model: people rate inconsistently, and a book a reader would have loved
but never met counts as a miss. Neither can be removed from real data.

Here they can. The books are the real 2,000 -- their real Goodreads
genres and the description vectors the platform already holds -- but
the readers are generated, so each one's true taste is known:

* a taste over the ten Goodreads genres (most readers like a few), and
* a hidden "style" preference, over a hidden style each book is given,
  that no text reveals. Real readers like things genres do not capture;
  without this, content-based recommendation would be handed a world
  in which genre is everything.

A reader reads books in proportion to how much they would like them and
how popular they are, then rates them: better-liked books get higher
stars. How many books each reader reads and the spread of stars are
copied from the real ratings in the library. A noise level replaces
that share of each reader's reads with books picked at random and
stars picked at random -- taste-free behaviour, the stand-in for
everything a model cannot know.

Every method is then scored two ways:

* **held-out** -- exactly the real protocol: 80/20 split, rank every
  book, compare with the 20%. Comparable to the real numbers.
* **truth** -- the books this reader would love: their top 5% by true
  taste (100 of 2,000), among those not in their training ratings.
  P@10 is then the share of the ten recommendations the reader would
  love; a perfect model scores 1.0. (Asking for each reader's exact ten
  best of 2,000 would test whether a model can pin down a precise
  order, not whether it finds good books.)

A "true taste" row ranks by the real utility. On truth it is 1.0 by
construction; on held-out it shows the best any model could do at that
noise level -- the ceiling noise puts on the real protocol.

These are generated readers. Their scores say whether the methods work
when the answer is known; they are not a substitute for the scores on
real readers, and are reported beside them, never instead.

    docker compose run --rm -T -v "$PWD/data/ucsd:/data:ro" recommend \\
        python -m bookrs.evaluation.synthetic --source-id 54 \\
        --books /data/books.jsonl --biblios /data/koha_biblios.tsv \\
        --genres /data/goodreads_book_genres_initial.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix

from bookrs.evaluation.thesis import (
    Scores, blend, content_scores, evaluate, fit, split,
)

log = logging.getLogger("bookrs.evaluation")

GENRES = (
    "children", "comics, graphic", "fantasy, paranormal", "fiction",
    "history, historical fiction, biography", "mystery, thriller, crime",
    "non-fiction", "poetry", "romance", "young-adult",
)


@dataclass
class World:
    """The real books, and what is true about them."""

    genre: np.ndarray        # (n, 10) rows sum to 1
    style: np.ndarray        # (n, d) hidden, unit-scale
    log_pop: np.ndarray      # (n,) log of real rating counts, centred
    vectors: np.ndarray      # (n, e) the platform's description vectors
    activity: np.ndarray     # real ratings-per-reader counts, to resample
    stars: np.ndarray        # (5,) real share of 1..5 stars


@dataclass
class Readers:
    taste: np.ndarray        # (m, 10)
    style: np.ndarray        # (m, d)


@dataclass
class Settings:
    style_dims: int = 16
    genre_concentration: float = 0.3   # Dirichlet: low = few favourite genres
    temperature: float = 0.5           # how strongly reading follows liking
    popularity: float = 1.0            # how strongly reading follows popularity
    rating_noise: float = 0.5          # stars follow liking, not exactly


def make_readers(m: int, s: Settings, rng) -> Readers:
    taste = rng.dirichlet(np.full(len(GENRES), s.genre_concentration), size=m)
    style = rng.normal(size=(m, s.style_dims))
    style /= np.linalg.norm(style, axis=1, keepdims=True)
    return Readers(taste.astype(np.float32), style.astype(np.float32))


def utility(world: World, readers: Readers, rows: np.ndarray) -> np.ndarray:
    """How much each reader truly likes each book: genre and style, each
    standardised per reader so neither dominates by scale."""
    g = readers.taste[rows] @ world.genre.T
    h = readers.style[rows] @ world.style.T
    def z(x):
        return (x - x.mean(axis=1, keepdims=True)) / (x.std(axis=1, keepdims=True) + 1e-9)
    return (z(g) + z(h)).astype(np.float32)


def generate(world: World, readers: Readers, noise: float, s: Settings, rng,
             chunk: int = 2000):
    """Reads and stars for every reader. Returns (u, i, r) arrays."""
    m, n = len(readers.taste), len(world.genre)
    counts = np.minimum(rng.choice(world.activity, size=m), n // 2)
    star_values = np.arange(1, 6)
    share = np.asarray(world.stars, dtype=np.float64)
    share = share / share.sum()
    cum = np.cumsum(share)
    us, is_, rs = [], [], []
    for at in range(0, m, chunk):
        rows = np.arange(at, min(at + chunk, m))
        u = utility(world, readers, rows)
        logits = u / s.temperature + s.popularity * world.log_pop
        # Gumbel top-k: a sample without replacement, weighted by exp(logits)
        keys = logits + rng.gumbel(size=logits.shape)
        order = np.argsort(-keys, axis=1)
        for j, row in enumerate(rows):
            k = int(counts[row])
            n_random = int(round(noise * k))
            chosen = order[j, :k - n_random]
            if n_random:
                pool = np.setdiff1d(np.arange(n), chosen, assume_unique=False)
                chosen = np.concatenate([chosen, rng.choice(pool, size=n_random, replace=False)])
            # Stars: rank the read books by liking (plus noise), then cut
            # at the real star shares, so the spread matches the library.
            liked = u[j, chosen] + rng.normal(scale=s.rating_noise, size=len(chosen))
            rank = np.argsort(np.argsort(liked)) / max(len(chosen) - 1, 1)
            stars = star_values[np.searchsorted(cum, rank, side="left").clip(0, 4)]
            if n_random:
                stars[-n_random:] = rng.choice(star_values, size=n_random, p=share)
            us.append(np.full(len(chosen), row, dtype=np.int32))
            is_.append(chosen.astype(np.int32))
            rs.append(stars.astype(np.int8))
    u, i, r = np.concatenate(us), np.concatenate(is_), np.concatenate(rs)
    order = np.lexsort((i, u))
    return u[order], i[order], r[order]


LOVE = 0.05


def truth(world: World, readers: Readers, rows: np.ndarray, seen: csr_matrix,
          share: float = LOVE) -> csr_matrix:
    """The books each reader would love -- their top ``share`` of the
    catalogue by true taste -- among those not in training."""
    k = max(10, int(round(share * world.genre.shape[0])))
    u = utility(world, readers, rows)
    u[seen[rows].toarray() > 0] = -np.inf
    top = np.argpartition(-u, k - 1, axis=1)[:, :k]
    r = np.repeat(rows, k)
    return csr_matrix((np.ones(len(r), dtype=np.int8), (r, top.ravel())),
                      shape=seen.shape)


def gini(x: np.ndarray) -> float:
    x = np.sort(np.asarray(x, dtype=np.float64))
    n = len(x)
    return float((2 * np.arange(1, n + 1) - n - 1) @ x / (n * x.sum())) if x.sum() else 0.0


def run_level(world: World, noise: float, args, s: Settings) -> dict:
    from bookrs.evaluation.coldstart import fold_in, reveal_order
    from bookrs.evaluation.thesis import CONFIDENCE

    rng = np.random.default_rng(args.seed)
    readers = make_readers(args.readers, s, rng)
    t0 = time.monotonic()
    u, i, r = generate(world, readers, noise, s, rng)
    m, n = args.readers, len(world.genre)
    shape = (m, n)
    log.info("noise %.0f%%: %d reads by %d readers in %.0fs", 100 * noise, len(u), m,
             time.monotonic() - t0)

    held = split(u, m, 0.2, args.seed)
    tr = ~held
    conf = CONFIDENCE["1+2r"](r[tr].astype(np.float32), np.zeros(tr.sum(), np.float32))
    train = csr_matrix((conf.astype(np.float32), (u[tr], i[tr])), shape=shape)
    test = csr_matrix((np.ones(held.sum(), np.int8), (u[held], i[held])), shape=shape)
    readers_f, works_f = fit(train, args.factors, args.regularization, args.iterations, args.seed)

    sample = np.sort(np.random.default_rng(args.seed + 1).choice(m, size=min(args.scored, m),
                                                                 replace=False))
    best = truth(world, readers, sample, train)
    binary = (train > 0).astype(np.float32).tocsr()
    n_train = np.asarray(binary.sum(axis=1)).ravel()
    pop = np.asarray(binary.sum(axis=0)).ravel()

    methods = {
        "most-read books": lambda w: np.tile(pop, (len(w), 1)),
        "content only": lambda w: content_scores(binary, n_train, world.vectors, w),
        "ALS": lambda w: readers_f[w] @ works_f.T,
        "hybrid, alpha 0.1": lambda w: blend(0.1, content_scores(binary, n_train, world.vectors, w),
                                             readers_f[w] @ works_f.T),
        "true taste (ceiling)": lambda w: utility(world, readers, w),
    }
    rows = {name: (evaluate(f, train, test, sample), evaluate(f, train, best, sample))
            for name, f in methods.items()}

    # New readers: fit without some, reveal a few of their reads.
    cold = np.sort(np.random.default_rng(args.seed + 2).choice(
        np.flatnonzero(np.bincount(u[tr], minlength=m) >= 20), size=min(args.cold, m // 10),
        replace=False))
    warm = ~np.isin(u, cold)
    _, cold_books = fit(csr_matrix((np.where(tr & warm, 1.0 + 2.0 * r, 0).astype(np.float32),
                                    (u, i)), shape=shape),
                        args.factors, args.regularization, args.iterations, args.seed)
    gram = cold_books.T.astype(np.float64) @ cold_books.astype(np.float64)
    position = reveal_order(u, tr, args.seed + 3)
    cold_truth = truth(world, readers, cold, csr_matrix(
        (np.ones(int((tr & ~warm).sum()), np.int8), (u[tr & ~warm], i[tr & ~warm])), shape=shape))
    slot = np.full(m, -1)
    slot[cold] = np.arange(len(cold))
    new_rows = {}
    for k in (5, 20):
        shown = ~warm & tr & (position < k)
        given = csr_matrix(((1.0 + 2.0 * r[shown]).astype(np.float32), (u[shown], i[shown])),
                           shape=shape)
        gb = (given > 0).astype(np.float32).tocsr()
        gn = np.asarray(gb.sum(axis=1)).ravel()
        as_thesis = given.copy()
        as_thesis.data[:] = 3.0
        x_t = fold_in(as_thesis[cold], cold_books, args.regularization, None)
        x_f = fold_in(given[cold], cold_books, args.regularization, gram)
        for name, f in {
            "most-read books": lambda w: np.tile(pop, (len(w), 1)),
            "content only": lambda w: content_scores(gb, gn, world.vectors, w),
            "folding-in, as in the thesis": lambda w: x_t[slot[w]] @ cold_books.T,
            "folding-in, full": lambda w: x_f[slot[w]] @ cold_books.T,
        }.items():
            new_rows.setdefault(name, []).append(evaluate(f, given, cold_truth, cold))
    return {"reads": len(u), "gini": gini(np.bincount(i, minlength=n)), "rows": rows,
            "new": new_rows, "seconds": time.monotonic() - t0}


def load_world(conn, source_id: int, books_path: str, biblios_path: str, genres_path: str,
               s: Settings, seed: int) -> tuple[World, dict]:
    from bookrs.evaluation.thesis import load_embeddings

    ucsd_to_bib = {}
    with open(biblios_path, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            a, b = line.rstrip("\n").split("\t")
            ucsd_to_bib[a] = b
    gid_to_bib = {}
    for line in open(books_path, encoding="utf-8"):
        b = json.loads(line)
        if b["ucsd_book_id"] in ucsd_to_bib:
            gid_to_bib[b["goodreads_id"]] = ucsd_to_bib[b["ucsd_book_id"]]
    bib_to_work = dict(conn.execute(
        "SELECT regexp_replace(source_record_id, '^.*:', ''), id FROM works "
        "WHERE source_id = %s AND deleted_at IS NULL", (source_id,)).fetchall())

    genre_of: dict[int, np.ndarray] = {}
    wanted = set(gid_to_bib)
    with gzip.open(genres_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            gid = str(rec.get("book_id"))
            if gid not in wanted:
                continue
            v = np.array([float(rec.get("genres", {}).get(g, 0)) for g in GENRES])
            work = bib_to_work.get(gid_to_bib[gid])
            if work is not None and v.sum() > 0:
                genre_of[int(work)] = v / v.sum()

    works = np.array(sorted(genre_of), dtype=np.int64)
    vectors, n_vec, model = load_embeddings(conn, source_id, works)
    counts = dict(conn.execute(
        "SELECT work_id, count(*) FROM ratings WHERE source_id = %s GROUP BY work_id",
        (source_id,)).fetchall())
    pop = np.array([counts.get(int(w), 1) for w in works], dtype=np.float64)
    activity = np.array([c for (c,) in conn.execute(
        "SELECT count(*) FROM ratings WHERE source_id = %s GROUP BY patron_ref",
        (source_id,)).fetchall()], dtype=np.int64)
    hist = dict(conn.execute("SELECT rating, count(*) FROM ratings WHERE source_id = %s "
                             "GROUP BY rating", (source_id,)).fetchall())
    stars = np.array([hist.get(k, 0) for k in range(1, 6)], dtype=np.float64)

    rng = np.random.default_rng(seed + 7)
    style = rng.normal(size=(len(works), s.style_dims)) / np.sqrt(s.style_dims)
    log_pop = np.log(pop)
    world = World(np.array([genre_of[int(w)] for w in works], dtype=np.float32),
                  style.astype(np.float32), (log_pop - log_pop.mean()).astype(np.float32),
                  vectors, activity, stars / stars.sum())
    info = {"works": len(works), "with_vectors": n_vec, "model": model,
            "real_gini": gini(pop), "activity_median": int(np.median(activity))}
    return world, info


def table(title, rows, scores_of):
    head = f"{'':34}{'P@10':>8}{'R@10':>8}{'MRR':>8}{'HR@10':>8}{'NDCG@10':>9}"
    out = ["", title, head]
    for name, s in rows.items():
        sc = scores_of(s)
        out.append(f"  {name:32}{sc.p10:>8.4f}{sc.r10:>8.4f}{sc.mrr:>8.4f}"
                   f"{sc.hr10:>8.4f}{sc.ndcg10:>9.4f}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bookrs.evaluation.synthetic", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--books", required=True)
    ap.add_argument("--biblios", required=True)
    ap.add_argument("--genres", required=True, help="goodreads_book_genres_initial.json.gz")
    ap.add_argument("--readers", type=int, default=50_000)
    ap.add_argument("--scored", type=int, default=5_000)
    ap.add_argument("--cold", type=int, default=2_000)
    ap.add_argument("--noise", default="0,0.1,0.3,0.5")
    ap.add_argument("--factors", type=int, default=128)
    ap.add_argument("--regularization", type=float, default=0.1)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    import psycopg

    s = Settings()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        world, info = load_world(conn, a.source_id, a.books, a.biblios, a.genres, s, a.seed)
    print(f"\nBooks: {info['works']:,} real books with Goodreads genres "
          f"({info['with_vectors']:,} with description vectors, {info['model']}).")
    print(f"Copied from the real library: ratings per reader (median {info['activity_median']}), "
          f"the spread of stars. Real popularity Gini {info['real_gini']:.2f}.")
    print(f"Generated: {a.readers:,} readers; {a.scored:,} scored; {a.cold:,} kept out as new readers. "
          f"ALS k={a.factors}, lambda={a.regularization:g}, {a.iterations} iterations, c = 1 + 2r.")

    for noise in (float(x) for x in a.noise.split(",")):
        res = run_level(world, noise, a, s)
        print(f"\n===== noise {100 * noise:.0f}%: {res['reads']:,} reads, "
              f"generated popularity Gini {res['gini']:.2f} =====")
        for line in table("scored against held-out ratings (the real protocol)",
                          res["rows"], lambda t: t[0]):
            print(line)
        for line in table("scored against true taste (books the reader would love, their top 5%; perfect = 1.0)",
                          res["rows"], lambda t: t[1]):
            print(line)
        print("\nnew readers, scored against true taste: P@10 after 5 and after 20 ratings")
        for name, (five, twenty) in res["new"].items():
            print(f"  {name:32}{five.p10:>8.4f}{twenty.p10:>8.4f}")
    print("\nGenerated readers: these say whether the methods recover taste that is known to be "
          "there. They are reported beside the real results, not instead of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
