"""The revised offline evaluation: several questions, each with its limit.

The thesis's held-out protocol (``thesis.py``) asks one narrow question:
did the model put the exact books a reader went on to rate into their
top 10? It is strict in two ways that keep every score low:

* a book the reader would love but never rated counts as wrong; even
  perfect knowledge of each generated reader's taste scores only 0.29
  on it (``synthetic.py``);
* a reader with two held-out books cannot score above 0.2 on P@10.

So the same model is scored here several ways, each answering a
different question, and each score is shown beside its limit:

1. **Held-out, liked = rated 4 or 5** (the main score), with the
   ceiling every P@10 is capped by, the share of that ceiling reached,
   truncated recall (hits over min(10, held-out)) and MAP@10.
2. **Held-out, any rating**: the thesis's definition, for comparison.
3. **Leave one out**: one liked held-out book per reader, ranked against
   every book the reader has not rated, their other held-out books left
   out. Not capped by how many books a reader has held out.
4. **Genre match**: the share of the top 10 in the reader's own genres
   (those making up 20% or more of their training books), against what
   a random list would get. Fair to the content side, which the held-out
   protocol is not.
5. **Beyond accuracy**: how much of the catalogue gets recommended at
   all, how much from outside the most-rated books, novelty, and
   diversity within a list.
6. **Similar books**: for a sample of books, the share of their ten most
   similar that share their genre, by meaning, by ALS, and by the blend
   the API uses (0.7 meaning + 0.3 ALS over the 50 closest by meaning).

Methods: the most-rated books, content (the thesis's reader profile: the
mean vector of the books they rated), ALS, and the thesis's hybrid.

It runs on the 2,000-book slice (``--slice koha_biblios.tsv``, the
population the thesis and the report used) or on everything the library
holds. On the full catalogue the candidates are the books with at least
one rating (a held-out rating can only ever be on one of those), and the
readers scored are a seeded sample (``--readers``, 20,000 by default).

    docker compose run --rm -T -v "$PWD/data/ucsd:/data:ro" recommend \\
        python -m bookrs.evaluation.revised --source-id 54 --slice /data/koha_biblios.tsv
    docker compose run --rm -T recommend python -m bookrs.evaluation.revised --source-id 54
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy.sparse import csr_matrix

from bookrs.evaluation.thesis import (
    CONFIDENCE, blend, content_scores, fit, load, load_embeddings, restrict,
    slice_works, split,
)

log = logging.getLogger("bookrs.evaluation")

K = 10
DEEP = 50
#: A genre is one of the reader's own when it makes up at least this
#: share of the books they rated in training (and the most frequent
#: always is).
GENRE_SHARE = 0.2
#: The head: the most-rated tenth of the catalogue.
HEAD_SHARE = 0.10

_DISCOUNT = 1.0 / np.log2(np.arange(2, K + 2))
_IDEAL = np.concatenate(([0.0], np.cumsum(_DISCOUNT)))


# -- per-reader arithmetic, on small arrays ---------------------------------

def heldout(top: np.ndarray, rel: np.ndarray) -> dict[str, np.ndarray]:
    """Per-reader scores of ranked lists against held-out books.

    ``top``: (n, >=K) work indices, best first. ``rel``: (n, works) bool.
    Readers with no relevant book are left out (they are counted by the
    caller). Returns arrays of equal length, one entry per reader kept.
    """
    n_rel = rel.sum(axis=1)
    keep = n_rel > 0
    top, rel, n_rel = top[keep], rel[keep], n_rel[keep]
    hit = np.take_along_axis(rel, top[:, :K], axis=1)
    got = hit.sum(axis=1)
    cap = np.minimum(n_rel, K)
    precision_at = np.cumsum(hit, axis=1) / np.arange(1, K + 1)
    first = np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, 0)
    return {
        "p10": got / K,
        "ceiling": cap / K,
        "r10": got / n_rel,
        "trunc_r10": got / cap,
        "map10": (precision_at * hit).sum(axis=1) / cap,
        "mrr": np.divide(1.0, first, out=np.zeros(len(got)), where=first > 0),
        "ndcg10": (hit * _DISCOUNT).sum(axis=1) / _IDEAL[cap],
        "hr10": (got > 0).astype(float),
    }


def loo_rank(scores: np.ndarray, target: np.ndarray, exclude: np.ndarray) -> np.ndarray:
    """0-based rank of each reader's target book among their candidates.

    ``scores``: (n, works), with the reader's training books already at
    -inf. ``exclude``: (n, works) bool, books that are not candidates
    (their other held-out books). Ties are counted in the target's
    favour. Readers whose target is -1 get -1.
    """
    rows = np.arange(len(target))
    ok = target >= 0
    st = np.where(ok, scores[rows, np.maximum(target, 0)], np.inf)
    ahead = (scores > st[:, None]) & ~exclude
    return np.where(ok, ahead.sum(axis=1), -1)


def reader_genres(train_rows: list[np.ndarray], genre: np.ndarray,
                  n_genres: int) -> list[np.ndarray]:
    """Each reader's own genres: those making up GENRE_SHARE or more of
    the books they rated in training, and always the most frequent."""
    out = []
    for books in train_rows:
        g = genre[books]
        g = g[g >= 0]
        if not len(g):
            out.append(np.zeros(0, dtype=np.int32))
            continue
        counts = np.bincount(g, minlength=n_genres)
        own = np.flatnonzero(counts >= GENRE_SHARE * len(g))
        if not len(own):
            own = np.array([counts.argmax()])
        out.append(own.astype(np.int32))
    return out


def genre_match(top10: np.ndarray, own: list[np.ndarray], genre: np.ndarray,
                share: np.ndarray) -> tuple[float, float, int]:
    """(share of the top 10 in the reader's genres, what a random list
    gets, readers counted). ``share`` is each genre's share of the
    catalogue's books with a known genre."""
    matched, expected, n = [], [], 0
    for row, mine in zip(top10, own):
        if not len(mine):
            continue
        matched.append(np.isin(genre[row], mine).mean())
        expected.append(share[mine].sum())
        n += 1
    if not n:
        return float("nan"), float("nan"), 0
    return float(np.mean(matched)), float(np.mean(expected)), n


def beyond(top10: np.ndarray, popularity: np.ndarray, n_readers: int,
           genre: np.ndarray, unit: np.ndarray | None) -> dict[str, float]:
    """Beyond accuracy, over every scored reader's top 10."""
    n_works = len(popularity)
    head = np.zeros(n_works, dtype=bool)
    head[np.argsort(-popularity, kind="stable")[:max(1, int(HEAD_SHARE * n_works))]] = True
    flat = top10.ravel()
    p = np.maximum(popularity[flat], 1) / max(n_readers, 1)
    out = {
        "coverage": len(np.unique(flat)) / n_works,
        "outside_head": float((~head[flat]).mean()),
        "mean_ratings": float(popularity[flat].mean()),
        "novelty": float((-np.log2(p)).mean()),
        "genres_per_list": float(np.mean([len(np.unique(g[g >= 0]))
                                          for g in genre[top10]])),
    }
    if unit is not None and unit.shape[1]:
        v = unit[top10]                                  # (n, K, d)
        sims = v @ v.transpose(0, 2, 1)                  # (n, K, K)
        k = top10.shape[1]
        off = (sims.sum(axis=(1, 2)) - np.trace(sims, axis1=1, axis2=2)) / (k * (k - 1))
        out["diversity"] = float((1.0 - off).mean())
    return out


def _unit_rows(x: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(x, axis=1, keepdims=True)
    return np.divide(x, norm, out=np.zeros_like(x), where=norm > 0)


def _top(scores: np.ndarray, deep: int) -> np.ndarray:
    part = np.argpartition(-scores, deep - 1, axis=1)[:, :deep]
    inner = np.argsort(-np.take_along_axis(scores, part, axis=1), axis=1, kind="stable")
    return np.take_along_axis(part, inner, axis=1)


# -- loading ----------------------------------------------------------------

def _works_table(conn, works: np.ndarray) -> None:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _revised_works (work_id bigint PRIMARY KEY)")
    conn.execute("TRUNCATE _revised_works")
    with conn.cursor() as cur:
        with cur.copy("COPY _revised_works (work_id) FROM STDIN") as cp:
            for wid in works:
                cp.write_row((int(wid),))


def load_genres(conn, works: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Each work's genre: its copies' collection (Koha's items.ccode),
    -1 where none is recorded."""
    _works_table(conn, works)
    rows = conn.execute(
        "SELECT i.work_id, min(i.collection) FROM items i "
        "JOIN _revised_works s ON s.work_id = i.work_id "
        "WHERE i.collection IS NOT NULL AND i.collection <> '' "
        "GROUP BY i.work_id").fetchall()
    names = sorted({r[1] for r in rows})
    code = {n: k for k, n in enumerate(names)}
    where = {int(w): n for n, w in enumerate(works)}
    genre = np.full(len(works), -1, dtype=np.int32)
    for work_id, name in rows:
        genre[where[int(work_id)]] = code[name]
    return genre, names


# -- the run ------------------------------------------------------------------

@dataclass
class Method:
    name: str
    heldout_liked: dict = field(default_factory=dict)
    heldout_any: dict = field(default_factory=dict)
    loo: dict = field(default_factory=dict)
    genre: dict = field(default_factory=dict)
    beyond: dict = field(default_factory=dict)


def _summary(cols: dict[str, list]) -> dict:
    if not cols or not cols.get("p10"):
        return {"readers": 0}
    c = {k: np.concatenate(v) for k, v in cols.items()}
    n = len(c["p10"])
    out = {"readers": n}
    for k, v in c.items():
        out[k] = float(v.mean())
    out["p10_se"] = float(c["p10"].std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    out["of_ceiling"] = out["p10"] / out["ceiling"] if out["ceiling"] else float("nan")
    return out


def score_method(name, score_rows, readers, seen, liked, anyheld, target,
                 chunk) -> tuple[Method, np.ndarray]:
    """Rank once per reader; every held-out score and the leave-one-out
    rank come from that one ranking. Returns the method and the top 10s."""
    liked_cols: dict[str, list] = {}
    any_cols: dict[str, list] = {}
    ranks, top10s = [], []
    for at in range(0, len(readers), chunk):
        who = readers[at:at + chunk]
        scores = np.array(score_rows(who), dtype=np.float32)
        scores[seen[who].toarray() > 0] = -np.inf
        top = _top(scores, min(DEEP, scores.shape[1]))
        top10s.append(top[:, :K])
        for rel_m, cols in ((liked, liked_cols), (anyheld, any_cols)):
            for k, v in heldout(top, rel_m[who].toarray() > 0).items():
                cols.setdefault(k, []).append(v)
        exclude = anyheld[who].toarray() > 0
        t = target[who]
        exclude[np.arange(len(who))[t >= 0], t[t >= 0]] = False
        ranks.append(loo_rank(scores, t, exclude))
    rank = np.concatenate(ranks)
    rank = rank[rank >= 0]
    loo = {"readers": int(len(rank))}
    if len(rank):
        loo.update(hr10=float((rank < K).mean()),
                   ndcg10=float(np.where(rank < K, 1.0 / np.log2(rank + 2), 0.0).mean()),
                   mrr=float((1.0 / (rank + 1)).mean()),
                   median_rank=float(np.median(rank) + 1))
    m = Method(name, _summary(liked_cols), _summary(any_cols), loo)
    return m, np.concatenate(top10s)


def similar_books(sample, unit_vectors, unit_factors, genre, chunk=256) -> dict[str, dict]:
    """Genre agreement of the ten most similar books, three ways."""
    known = genre >= 0
    share = np.bincount(genre[known]).astype(float) / max(known.sum(), 1)
    agree = {"meaning": [], "ALS": [], "API blend (0.7 meaning + 0.3 ALS)": []}
    for at in range(0, len(sample), chunk):
        q = sample[at:at + chunk]
        rows = np.arange(len(q))
        cs = unit_vectors[q] @ unit_vectors.T
        fs = unit_factors[q] @ unit_factors.T
        cs[rows, q] = -np.inf
        fs[rows, q] = -np.inf
        pool = _top(cs, min(DEEP, cs.shape[1]))
        blend_scores = (0.7 * np.take_along_axis(cs, pool, axis=1)
                        + 0.3 * np.take_along_axis(fs, pool, axis=1))
        order = np.argsort(-blend_scores, axis=1, kind="stable")[:, :K]
        lists = {"meaning": pool[:, :K], "ALS": _top(fs, K),
                 "API blend (0.7 meaning + 0.3 ALS)": np.take_along_axis(pool, order, axis=1)}
        for name, top in lists.items():
            agree[name].append((genre[top] == genre[q][:, None]).mean(axis=1))
    random = float(share[genre[sample]].mean())
    return {name: {"books": int(len(sample)), "agreement10": float(np.concatenate(v).mean()),
                   "random": random} for name, v in agree.items()}


# -- printing -----------------------------------------------------------------

def _f(x, w=8, p=4):
    return f"{x:>{w}.{p}f}" if isinstance(x, float) and np.isfinite(x) else f"{'-':>{w}}"


def report(header: list[str], methods: list[Method], similar: dict | None,
           genre_names: list[str]) -> str:
    out = header + [""]
    for title, attr in (
            ("1. Held-out books, liked = rated 4 or 5 (the main score)", "heldout_liked"),
            ("2. Held-out books, any rating (the thesis's definition)", "heldout_any")):
        out += [title,
                f"{'':30}{'readers':>9}{'P@10':>8}{'±':>8}{'ceiling':>9}{'of ceil.':>9}"
                f"{'R@10':>8}{'trunc R':>8}{'MAP@10':>8}{'MRR':>8}{'NDCG@10':>8}{'HR@10':>8}"]
        for m in methods:
            s = getattr(m, attr)
            if not s.get("readers"):
                continue
            out.append(f"  {m.name:28}{s['readers']:>9,}{_f(s['p10'])}{_f(s['p10_se'])}"
                       f"{_f(s['ceiling'], 9)}{_f(s['of_ceiling'], 9, 3)}{_f(s['r10'])}"
                       f"{_f(s['trunc_r10'])}{_f(s['map10'])}{_f(s['mrr'])}"
                       f"{_f(s['ndcg10'])}{_f(s['hr10'])}")
        out.append("")
    out += ["3. Leave one out: one liked held-out book per reader, against every book "
            "they have not rated",
            f"{'':30}{'readers':>9}{'HR@10':>8}{'NDCG@10':>8}{'MRR':>8}{'median rank':>12}"]
    for m in methods:
        s = m.loo
        if s.get("readers"):
            out.append(f"  {m.name:28}{s['readers']:>9,}{_f(s['hr10'])}{_f(s['ndcg10'])}"
                       f"{_f(s['mrr'])}{s['median_rank']:>12,.0f}")
    out.append("")
    if genre_names:
        out += [f"4. Genre match: share of the top 10 in the reader's own genres "
                f"({len(genre_names)} genres)",
                f"{'':30}{'readers':>9}{'match':>8}{'random':>8}{'lift':>8}"]
        for m in methods:
            g = m.genre
            if g.get("readers"):
                out.append(f"  {m.name:28}{g['readers']:>9,}{_f(g['match'])}"
                           f"{_f(g['random'])}{_f(g['match'] / g['random'], 8, 2)}")
        out.append("")
    else:
        out += ["4. Genre match: skipped, no genre recorded for these books (Koha's "
                "items.ccode, read by a full catalogue read)", ""]
    out += ["5. Beyond accuracy (each reader's top 10)",
            f"{'':30}{'coverage':>9}{'outside head':>13}{'mean ratings':>13}"
            f"{'novelty':>9}{'diversity':>10}{'genres/list':>12}"]
    for m in methods:
        b = m.beyond
        if b:
            out.append(f"  {m.name:28}{_f(b['coverage'], 9)}{_f(b['outside_head'], 13)}"
                       f"{b['mean_ratings']:>13,.0f}{_f(b['novelty'], 9, 2)}"
                       f"{_f(b.get('diversity', float('nan')), 10)}"
                       f"{_f(b['genres_per_list'], 12, 2)}")
    out.append("")
    if similar:
        books = next(iter(similar.values()))["books"]
        out += [f"6. Similar books: share of the 10 most similar sharing the book's genre "
                f"({books:,} books)",
                f"{'':36}{'agreement':>10}{'random':>8}{'lift':>8}"]
        for name, s in similar.items():
            out.append(f"  {name:34}{_f(s['agreement10'], 10)}{_f(s['random'])}"
                       f"{_f(s['agreement10'] / s['random'], 8, 2)}")
        out.append("")
    out += [
        "Reading it:",
        "  ceiling  the best P@10 any model could reach: a reader with 2 held-out books cannot",
        "           pass 0.2. 'of ceil.' is P@10 as a share of it.",
        "  trunc R  hits / min(10, held-out books): 1.0 is a perfect top 10 for every reader.",
        "  random   what a list drawn at random from the catalogue would get.",
        "  head     the most-rated tenth of the catalogue; 'outside head' is the share of",
        "           recommendations from the other nine tenths.",
        "  novelty  mean -log2(share of readers who rated the book): higher is less obvious.",
        "  diversity  mean 1 - cosine similarity of the texts of two books in one list.",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bookrs.evaluation.revised", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--slice", default=None, metavar="KOHA_BIBLIOS_TSV",
                    help="Only the fixture slice's books (koha_biblios.tsv).")
    ap.add_argument("--readers", type=int, default=20_000,
                    help="Readers scored, drawn at random with the seed (0: all).")
    ap.add_argument("--similar-books", type=int, default=2_000,
                    help="Books sampled for the similar-books check (0: skip).")
    ap.add_argument("--factors", type=int, default=128)
    ap.add_argument("--regularization", type=float, default=0.1)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--confidence", choices=sorted(CONFIDENCE), default="1+2r+3d")
    ap.add_argument("--alpha", type=float, default=0.1,
                    help="The hybrid's content weight (thesis: 0.1).")
    ap.add_argument("--min-ratings", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", default=None, help="Also write every number here.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    import psycopg

    t0 = time.monotonic()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        data = load(conn, a.source_id,
                    slice_works(conn, a.source_id, a.slice) if a.slice else None)
        data = restrict(data, a.min_ratings)
        vectors, n_vectors, _ = load_embeddings(conn, a.source_id, data.works)
        genre, genre_names = load_genres(conn, data.works)
    n_readers, n_works = len(data.patrons), len(data.works)
    log.info("%d ratings, %d readers, %d books, %d with a vector, %d with a genre; "
             "loaded in %.0fs", data.n, n_readers, n_works, n_vectors,
             int((genre >= 0).sum()), time.monotonic() - t0)

    held = split(data.u, n_readers, 0.2, a.seed)
    tr = ~held
    shape = (n_readers, n_works)
    conf = CONFIDENCE[a.confidence](data.r[tr].astype(np.float32),
                                    data.d[tr].astype(np.float32))
    train = csr_matrix((conf.astype(np.float32), (data.u[tr], data.i[tr])), shape=shape)
    anyheld = csr_matrix((np.ones(held.sum(), dtype=np.int8),
                          (data.u[held], data.i[held])), shape=shape)
    liked_mask = held & (data.r >= 4)
    liked = csr_matrix((np.ones(liked_mask.sum(), dtype=np.int8),
                        (data.u[liked_mask], data.i[liked_mask])), shape=shape)

    t1 = time.monotonic()
    readers_f, works_f = fit(train, a.factors, a.regularization, a.iterations, a.seed)
    log.info("ALS fitted in %.0fs", time.monotonic() - t1)

    rng = np.random.default_rng(a.seed)
    readers = (np.arange(n_readers) if not a.readers or a.readers >= n_readers
               else np.sort(rng.choice(n_readers, size=a.readers, replace=False)))
    # One liked held-out book per reader for leave-one-out, drawn with the seed.
    target = np.full(n_readers, -1, dtype=np.int64)
    lu, li = data.u[liked_mask], data.i[liked_mask]
    pick = np.lexsort((rng.random(len(lu)), lu))
    first = np.ones(len(pick), dtype=bool)
    first[1:] = lu[pick][1:] != lu[pick][:-1]
    target[lu[pick][first]] = li[pick][first]

    seen = (train > 0).astype(np.int8).tocsr()
    popularity = np.asarray(seen.sum(axis=0)).ravel().astype(np.float32)
    train_binary = seen.astype(np.float32)
    n_train = np.asarray(train_binary.sum(axis=1)).ravel()
    chunk = int(max(16, min(4096, 50_000_000 // max(n_works, 1))))

    methods_rows = [
        ("most-rated books", lambda who: np.tile(popularity, (len(who), 1))),
        ("ALS", lambda who: readers_f[who] @ works_f.T),
    ]
    if n_vectors:
        methods_rows.insert(1, ("content (SBERT profile)",
                                lambda who: content_scores(train_binary, n_train, vectors, who)))
        methods_rows.append((f"hybrid, alpha {a.alpha:g}",
                             lambda who: blend(a.alpha,
                                               content_scores(train_binary, n_train, vectors, who),
                                               readers_f[who] @ works_f.T)))

    known = genre >= 0
    share = (np.bincount(genre[known], minlength=len(genre_names)).astype(float)
             / max(known.sum(), 1))
    own = (reader_genres([seen[r].indices for r in readers], genre, len(genre_names))
           if genre_names else [])
    unit_vectors = _unit_rows(vectors) if n_vectors else None

    methods = []
    for name, rows in methods_rows:
        t = time.monotonic()
        m, top10 = score_method(name, rows, readers, seen, liked, anyheld, target, chunk)
        if genre_names:
            match, expected, n = genre_match(top10, own, genre, share)
            m.genre = {"readers": n, "match": match, "random": expected}
        m.beyond = beyond(top10, popularity, n_readers, genre, unit_vectors)
        methods.append(m)
        log.info("%s scored in %.0fs", name, time.monotonic() - t)

    similar = None
    if a.similar_books and n_vectors and genre_names:
        usable = np.flatnonzero(known & (np.abs(vectors).sum(axis=1) > 0))
        sample = np.sort(rng.choice(usable, size=min(a.similar_books, len(usable)),
                                    replace=False))
        similar = similar_books(sample, unit_vectors, _unit_rows(works_f), genre)

    scope = "the thesis's 2,000-book slice" if a.slice else "everything the library holds"
    header = [
        f"Revised evaluation. Source {a.source_id}, {scope}: {data.n:,} ratings, "
        f"{n_readers:,} readers, {n_works:,} books with ratings.",
        f"{len(readers):,} readers scored (seed {a.seed}). ALS k={a.factors}, "
        f"lambda={a.regularization:g}, {a.iterations} iterations, c = {a.confidence}. "
        f"Content vectors for {n_vectors:,} books; a genre for {int(known.sum()):,}.",
    ]
    print(report(header, methods, similar, genre_names), flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"source_id": a.source_id, "slice": bool(a.slice),
                       "ratings": data.n, "readers": n_readers, "books": n_works,
                       "scored_readers": len(readers), "seed": a.seed,
                       "methods": [asdict(m) for m in methods], "similar": similar},
                      fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
