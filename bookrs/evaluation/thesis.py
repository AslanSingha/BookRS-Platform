"""Re-run the thesis's offline evaluation on what was read from the library.

The BookRS-System thesis reported, for ALS over the UCSD Book Graph,

    P@10 = 0.2259    R@10 = 0.2971    MRR = 0.5826

This module computes the same three numbers from the ``ratings`` table,
which ``bookrs.ingestion.direct`` fills by reading a Koha library's own
database. Nothing here reads a file of interactions: if the numbers come
out, they come out of data that travelled through the library.

**The protocol is the one the thesis's code ran**
(``BookRS-System/experiments/05_implicit_feedback.py``), which is not in
every respect the one its text describes:

* the catalogue is the 2,000 most-rated books, and the readers are those
  with at least ten ratings among them;
* each reader's ratings are split 80/20 at random, seed 42;
* ALS, k=128, lambda=0.1, 10 iterations, on the training part, weighted
  ``c = 1 + 2r + 3d`` (r the star rating, d whether a review was written);
* every held-out book counts as relevant -- the code applies no
  "rated 4 or 5" threshold, though the text mentions one;
* books the reader rated in training are excluded from the ranking;
* the first 2,000 readers are scored, not all of them.

Each of those is reproduced, and each departure is reported beside it
rather than instead of it: all readers as well as the 2,000, the
4-or-5 threshold as well as none, and NDCG, hit rate and recall at
larger cut-offs beside the three the thesis used.

**What cannot be reproduced exactly** is which ratings fall on which
side of the split. The thesis drew it with pandas over rows in file
order; here the rows come out of a database, in whatever order it
returns them, and are put in a fixed order before the draw so that this
module at least agrees with itself from run to run. The split is the
same kind of split, not the same split, so the 2,000-reader figures
should agree within sampling error -- about +/-0.005 on P@10 -- and the
all-reader figures are the ones to quote.

**The ceiling is computed, not assumed.** A reader with three held-out
books cannot score above 0.3 on P@10 however good the model is. The mean
of that bound over readers is printed, because 0.23 means one thing
against a ceiling of 1.0 and another against a ceiling of 0.45.

    docker compose run --rm recommend python -m bookrs.evaluation.thesis --source-id 54
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix

log = logging.getLogger("bookrs.evaluation")

THESIS = {"P@10": 0.2259, "R@10": 0.2971, "MRR": 0.5826}

# Named by what they compute. The thesis lettered its configurations;
# the letters are not reused here because a letter cannot be checked
# against the formula it stands for and a formula can.
CONFIDENCE = {
    "1+2r+3d": lambda r, d: 1.0 + 2.0 * r + 3.0 * d,
    "1+2r": lambda r, d: 1.0 + 2.0 * r,
    "r": lambda r, d: r.astype(np.float32),
    "1": lambda r, d: np.ones(len(r), dtype=np.float32),
}


@dataclass
class Data:
    """Ratings as parallel arrays, in a fixed order."""

    patrons: list[str]          # index -> patron_ref
    works: np.ndarray           # index -> work_id
    u: np.ndarray               # reader index per rating
    i: np.ndarray               # work index per rating
    r: np.ndarray               # 1-5
    d: np.ndarray               # 0/1: also reviewed

    @property
    def n(self) -> int:
        return len(self.u)


def load(conn, source_id: int) -> Data:
    """Every rating for one library, sorted by (reader, work).

    The sort is what makes a run repeatable: a database promises no
    order, and the split below is a function of the order it is given.
    """
    n = conn.execute("SELECT count(*) FROM ratings WHERE source_id = %s",
                     (source_id,)).fetchone()[0]
    if not n:
        raise SystemExit(f"no ratings stored for source {source_id}. Read them "
                         "from the library first (bookrs.ingestion.direct).")
    u = np.empty(n, dtype=np.int32)
    w = np.empty(n, dtype=np.int64)
    r = np.empty(n, dtype=np.int8)
    d = np.empty(n, dtype=np.int8)
    index: dict[str, int] = {}
    k = 0
    with conn.cursor() as cur:
        with cur.copy(
            "COPY (SELECT patron_ref, work_id, rating, reviewed FROM ratings "
            f"WHERE source_id = {int(source_id)}) TO STDOUT"
        ) as copy:
            for ref, work, rating, reviewed in copy.rows():
                u[k] = index.setdefault(ref, len(index))
                w[k], r[k], d[k] = int(work), int(rating), reviewed == "t"
                k += 1
                if k % 4_000_000 == 0:
                    log.info("  loaded %d of %d ratings", k, n)
    u, w, r, d = u[:k], w[:k], r[:k], d[:k]

    # Re-index readers by sorted reference and works by sorted id, so the
    # indices do not depend on the order rows arrived in.
    refs = sorted(index)
    remap = np.empty(len(index), dtype=np.int32)
    for new, ref in enumerate(refs):
        remap[index[ref]] = new
    u = remap[u]
    works, i = np.unique(w, return_inverse=True)
    order = np.lexsort((i, u))
    return Data(refs, works, u[order], i[order].astype(np.int32), r[order], d[order])


def restrict(data: Data, min_per_reader: int) -> Data:
    """Keep readers with at least this many ratings.

    One pass, as in the thesis: books are not re-filtered afterwards.
    On the UCSD fixture this removes nobody, because the fixture is
    already that population; it is here so the harness states its
    population instead of inheriting it.
    """
    counts = np.bincount(data.u, minlength=len(data.patrons))
    keep_reader = counts >= min_per_reader
    if keep_reader.all():
        return data
    keep = keep_reader[data.u]
    new_index = np.cumsum(keep_reader) - 1
    patrons = [p for p, k in zip(data.patrons, keep_reader) if k]
    works, i = np.unique(data.works[data.i[keep]], return_inverse=True)
    return Data(patrons, works, new_index[data.u[keep]].astype(np.int32),
                i.astype(np.int32), data.r[keep], data.d[keep])


def split(u: np.ndarray, n_readers: int, fraction: float = 0.2,
          seed: int = 42) -> np.ndarray:
    """True for the ratings held out for testing.

    Per reader, ``round(fraction * n)`` ratings chosen uniformly -- the
    count pandas' ``groupby().sample(frac=...)`` takes, including its
    round-half-to-even.
    """
    rng = np.random.default_rng(seed)
    order = np.lexsort((rng.random(len(u)), u))
    counts = np.bincount(u, minlength=n_readers)
    starts = np.cumsum(counts) - counts
    su = u[order]
    rank = np.arange(len(u)) - starts[su]
    held = np.zeros(len(u), dtype=bool)
    held[order] = rank < np.rint(fraction * counts).astype(np.int64)[su]
    return held


def fit(train: csr_matrix, factors: int, regularization: float,
        iterations: int, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """ALS on a reader-by-work confidence matrix. Returns (readers, works).

    Fitted on the transpose with the two factor sets swapped back, which
    is exactly what the thesis's script does. The model is the same
    either way round -- ALS treats the two sides alike -- but which side
    is solved first follows from the orientation, so the orientation is
    kept.
    """
    from implicit.als import AlternatingLeastSquares

    model = AlternatingLeastSquares(
        factors=factors, regularization=regularization, iterations=iterations,
        use_gpu=False, calculate_training_loss=False, random_state=seed)
    model.fit(train.T.tocsr(), show_progress=False)
    return (np.asarray(model.item_factors, dtype=np.float32),
            np.asarray(model.user_factors, dtype=np.float32))


@dataclass
class Scores:
    readers: int = 0
    skipped: int = 0            # no relevant held-out book
    p10: float = 0.0
    p10_se: float = 0.0
    r10: float = 0.0
    mrr: float = 0.0
    ndcg10: float = 0.0
    hr10: float = 0.0
    r20: float = 0.0
    r50: float = 0.0
    ceiling: float = 0.0        # best possible P@10 for these readers


_DISCOUNT = 1.0 / np.log2(np.arange(2, 12))
_IDEAL = np.concatenate(([0.0], np.cumsum(_DISCOUNT)))


def evaluate(score_rows, seen: csr_matrix, relevant: csr_matrix,
             readers: np.ndarray, chunk: int = 4096) -> Scores:
    """Rank every work for each reader and score the top of the list.

    ``score_rows(readers)`` returns a dense (len(readers), n_works) array
    of scores. Works the reader rated in training are removed before
    ranking; what is left is compared with ``relevant``.
    """
    n_works = seen.shape[1]
    deep = min(50, n_works)
    cols = {"p": [], "r10": [], "rr": [], "ndcg": [], "hit": [],
            "r20": [], "r50": [], "cap": []}
    skipped = 0

    for at in range(0, len(readers), chunk):
        who = readers[at:at + chunk]
        rel = relevant[who].toarray().astype(bool)
        n_rel = rel.sum(axis=1)
        ok = n_rel > 0
        skipped += int((~ok).sum())
        if not ok.any():
            continue
        who, rel, n_rel = who[ok], rel[ok], n_rel[ok]

        scores = np.array(score_rows(who), dtype=np.float32)
        scores[seen[who].toarray() > 0] = -np.inf

        part = np.argpartition(-scores, deep - 1, axis=1)[:, :deep]
        inner = np.argsort(-np.take_along_axis(scores, part, axis=1),
                           axis=1, kind="stable")
        top = np.take_along_axis(part, inner, axis=1)
        hit = np.take_along_axis(rel, top, axis=1)
        h10 = hit[:, :10]

        got = h10.sum(axis=1)
        first = np.where(h10.any(axis=1), h10.argmax(axis=1) + 1, 0)
        cols["p"].append(got / 10.0)
        cols["r10"].append(got / n_rel)
        cols["rr"].append(np.divide(1.0, first, out=np.zeros(len(who)),
                                    where=first > 0))
        cols["ndcg"].append((h10 * _DISCOUNT[:h10.shape[1]]).sum(axis=1)
                            / _IDEAL[np.minimum(n_rel, 10)])
        cols["hit"].append((got > 0).astype(float))
        cols["r20"].append(hit[:, :20].sum(axis=1) / n_rel)
        cols["r50"].append(hit[:, :50].sum(axis=1) / n_rel)
        cols["cap"].append(np.minimum(n_rel, 10) / 10.0)

    if not cols["p"]:
        return Scores(skipped=skipped)
    c = {k: np.concatenate(v) for k, v in cols.items()}
    n = len(c["p"])
    return Scores(
        readers=n, skipped=skipped,
        p10=float(c["p"].mean()),
        p10_se=float(c["p"].std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0,
        r10=float(c["r10"].mean()), mrr=float(c["rr"].mean()),
        ndcg10=float(c["ndcg"].mean()), hr10=float(c["hit"].mean()),
        r20=float(c["r20"].mean()), r50=float(c["r50"].mean()),
        ceiling=float(c["cap"].mean()),
    )


def thesis_readers(path: str, borrower_map: str, secret: str,
                   patrons: list[str]) -> np.ndarray:
    """Indices of the readers the thesis scored.

    The thesis took the first 2,000 readers in file order. The fixture
    recorded who they were and which Koha borrower each became; the
    borrower number goes through the same HMAC the reader used, which is
    the only way from a borrower to a ``patron_ref`` and the reason the
    secret is needed here.
    """
    from bookrs.ingestion.circulation import patron_reference

    borrower = {}
    with open(borrower_map, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            uid, bn = line.rstrip("\n").split("\t")
            borrower[uid] = bn
    where = {ref: n for n, ref in enumerate(patrons)}
    found, missing = [], 0
    with open(path, encoding="utf-8") as fh:
        for uid in (ln.strip() for ln in fh):
            if not uid:
                continue
            n = where.get(patron_reference(borrower.get(uid, "?"), secret))
            if n is None:
                missing += 1
            else:
                found.append(n)
    if missing:
        raise SystemExit(
            f"{missing} of the thesis's readers were not found among the stored "
            "patrons. The usual cause is a BOOKRS_PATRON_SECRET different from "
            "the one the ratings were read with.")
    return np.array(found, dtype=np.int64)


_HEAD = (f"{'':44}{'readers':>9} {'P@10':>7} {'':>7} {'R@10':>7} {'MRR':>7} "
         f"{'NDCG@10':>8} {'HR@10':>7} {'R@20':>7} {'R@50':>7} {'max P@10':>9}")


def _row(label: str, s: Scores) -> str:
    return (f"{label:44}{s.readers:>9,} {s.p10:>7.4f} {'±%.4f' % s.p10_se:>7} "
            f"{s.r10:>7.4f} {s.mrr:>7.4f} {s.ndcg10:>8.4f} {s.hr10:>7.4f} "
            f"{s.r20:>7.4f} {s.r50:>7.4f} {s.ceiling:>9.4f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bookrs.evaluation.thesis", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--factors", type=int, default=128)
    ap.add_argument("--regularization", type=float, default=0.1)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--confidence", choices=sorted(CONFIDENCE), default="1+2r+3d")
    ap.add_argument("--min-ratings", type=int, default=10,
                    help="Readers with fewer ratings are left out (thesis: 10).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--thesis-readers", default=None,
                    help="thesis_eval_users.txt from the fixture.")
    ap.add_argument("--borrower-map", default=None,
                    help="koha_borrowers.tsv from the fixture.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    import psycopg

    t0 = time.monotonic()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        data = load(conn, a.source_id)
    loaded = data.n
    data = restrict(data, a.min_ratings)
    n_readers, n_works = len(data.patrons), len(data.works)
    log.info("%d ratings, %d readers, %d works (%d ratings left out by the "
             "%d-rating minimum); loaded in %.0fs", data.n, n_readers, n_works,
             loaded - data.n, a.min_ratings, time.monotonic() - t0)

    held = split(data.u, n_readers, 0.2, a.seed)
    tr = ~held
    shape = (n_readers, n_works)
    conf = CONFIDENCE[a.confidence](data.r[tr].astype(np.float32),
                                    data.d[tr].astype(np.float32))
    train = csr_matrix((conf.astype(np.float32), (data.u[tr], data.i[tr])), shape=shape)
    test_all = csr_matrix((np.ones(held.sum(), dtype=np.int8),
                           (data.u[held], data.i[held])), shape=shape)
    liked = held & (data.r >= 4)
    test_liked = csr_matrix((np.ones(liked.sum(), dtype=np.int8),
                             (data.u[liked], data.i[liked])), shape=shape)
    log.info("split: %d train, %d held out (%d of them rated 4 or 5)",
             tr.sum(), held.sum(), liked.sum())

    t1 = time.monotonic()
    readers_f, works_f = fit(train, a.factors, a.regularization, a.iterations, a.seed)
    log.info("ALS k=%d lambda=%g iterations=%d, c = %s: fitted in %.0fs",
             a.factors, a.regularization, a.iterations, a.confidence,
             time.monotonic() - t1)

    everyone = np.arange(n_readers, dtype=np.int64)
    if a.thesis_readers:
        if not a.borrower_map:
            ap.error("--thesis-readers needs --borrower-map")
        secret = os.environ.get("BOOKRS_PATRON_SECRET", "")
        if not secret:
            ap.error("BOOKRS_PATRON_SECRET must be set to find the thesis's readers")
        sample = thesis_readers(a.thesis_readers, a.borrower_map, secret, data.patrons)
        sample_name = f"the thesis's {len(sample):,} readers"
    else:
        sample = np.random.default_rng(a.seed).choice(
            n_readers, size=min(2000, n_readers), replace=False)
        sample_name = f"{len(sample):,} readers at random"

    def als(who):
        return readers_f[who] @ works_f.T

    popularity = np.asarray((train > 0).sum(axis=0), dtype=np.float32).ravel()

    def popular(who):
        return np.tile(popularity, (len(who), 1))

    out = [
        "", f"Source {a.source_id}: {data.n:,} ratings, {n_readers:,} readers, "
            f"{n_works:,} works. ALS k={a.factors}, lambda={a.regularization:g}, "
            f"{a.iterations} iterations, c = {a.confidence}, seed {a.seed}.",
        "", _HEAD,
        f"{'thesis, as reported':44}{2000:>9,} {THESIS['P@10']:>7.4f} {'':>7} "
        f"{THESIS['R@10']:>7.4f} {THESIS['MRR']:>7.4f}",
        "",
        "every held-out book is relevant (the thesis's code)",
        _row(f"  ALS, {sample_name}", evaluate(als, train, test_all, sample)),
        _row("  ALS, all readers", evaluate(als, train, test_all, everyone)),
        _row("  most-rated books, all readers", evaluate(popular, train, test_all, everyone)),
        "",
        "only held-out books rated 4 or 5 are relevant (the thesis's text)",
    ]
    liked_all = evaluate(als, train, test_liked, everyone)
    out += [
        _row(f"  ALS, {sample_name}", evaluate(als, train, test_liked, sample)),
        _row("  ALS, all readers", liked_all),
        _row("  most-rated books, all readers", evaluate(popular, train, test_liked, everyone)),
        "",
        f"random ranking would give P@10 about {test_all.nnz / n_readers / n_works:.4f}. "
        f"'max P@10' is the best any model could score: a reader with three "
        f"held-out books cannot exceed 0.3.",
    ]
    if liked_all.skipped:
        out.append(f"{liked_all.skipped:,} readers have no held-out book rated 4 or 5 "
                   "and are not in the second block.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
