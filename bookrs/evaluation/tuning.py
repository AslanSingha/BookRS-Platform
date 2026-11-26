"""Are k=128, lambda=0.1, 10 iterations and c = 1 + 2r + 3d the right settings?

The thesis chose them; the platform reproduced the thesis with them.
Neither showed they are the best this data allows. This module checks,
on the real readers, without letting the check flatter itself:

* The test ratings are the thesis's: each reader's 20%, seed 42.
* The settings are chosen on a **validation** slice carved out of the
  training ratings (a further 20% of each reader's training ratings,
  seed 43), scored on 20,000 readers. The test ratings play no part in
  the choice.
* Each setting is varied on its own, the others held at the thesis's
  values. Varying all four together would mean hundreds of fits; one at
  a time shows which setting matters, and how much.
* A setting leaves the thesis's value only if another value beats it on
  validation by more than two standard errors. One value is always
  highest by chance; without this bar a sweep over random data still
  "finds" a better setting for everything.
* The values that clear the bar are combined, and only then -- once --
  the thesis's settings and the combined ones are refitted on all
  training ratings and scored on the test ratings, for every reader.

Confidence is written c = 1 + a(r + 1.5d): a = 2 is the thesis's
1 + 2r + 3d, and a sets how far a rated book stands above an unrated
one -- the part of the formula that the confidence comparison showed
matters (bookrs/evaluation/thesis.py).

    docker compose run --rm -T recommend python -m bookrs.evaluation.tuning --source-id 54
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, replace

import numpy as np
from scipy.sparse import csr_matrix

from bookrs.evaluation.thesis import (Scores, evaluate, fit, load, restrict,
                                      slice_works, split)

log = logging.getLogger("bookrs.evaluation")


@dataclass(frozen=True)
class Setting:
    factors: int = 128
    regularization: float = 0.1
    iterations: int = 10
    a: float = 2.0

    def label(self) -> str:
        return (f"k={self.factors} lambda={self.regularization:g} "
                f"iterations={self.iterations} c=1+{self.a:g}(r+1.5d)")


THESIS = Setting()

GRID = {
    "factors": (32, 64, 128, 256, 512),
    "regularization": (0.01, 0.1, 1.0, 10.0, 100.0),
    "iterations": (5, 10, 20, 30),
    "a": (0.5, 1.0, 2.0, 5.0, 10.0, 20.0),
}


def confidence(r: np.ndarray, d: np.ndarray, a: float) -> np.ndarray:
    return (1.0 + a * (r.astype(np.float32) + 1.5 * d.astype(np.float32))).astype(np.float32)


def candidates(base: Setting = THESIS, grid: dict = GRID) -> list[tuple[str, Setting]]:
    """One at a time, the others at the base. The base appears once."""
    out, seen = [], set()
    for name, values in grid.items():
        for v in values:
            s = replace(base, **{name: v})
            if s not in seen:
                seen.add(s)
                out.append((name, s))
    return out


def combine(base: Setting, results: list[tuple[str, Setting, Scores]],
            bar: float = 2.0) -> Setting:
    """For each setting, the value with the best validation P@10 -- kept
    only if it beats the base's value by more than ``bar`` standard
    errors of the difference."""
    at_base = next((sc for _, s, sc in results if s == base), None)
    best = {}
    for name, s, sc in results:
        if name not in best or sc.p10 > best[name][1].p10:
            best[name] = (getattr(s, name), sc)
    chosen = {}
    for name, (value, sc) in best.items():
        if at_base is None:
            chosen[name] = value
            continue
        se = (sc.p10_se ** 2 + at_base.p10_se ** 2) ** 0.5
        if sc.p10 - at_base.p10 > bar * se:
            chosen[name] = value
    return replace(base, **chosen)


def matrices(u, i, r, d, mask, shape, a):
    return csr_matrix((confidence(r[mask], d[mask], a), (u[mask], i[mask])), shape=shape)


def score_setting(s: Setting, u, i, r, d, fit_mask, eval_mask, shape, readers, seed) -> Scores:
    train = matrices(u, i, r, d, fit_mask, shape, s.a)
    target = csr_matrix((np.ones(int(eval_mask.sum()), np.int8), (u[eval_mask], i[eval_mask])),
                        shape=shape)
    rf, wf = fit(train, s.factors, s.regularization, s.iterations, seed)
    return evaluate(lambda w: rf[w] @ wf.T, train, target, readers)


_ROW = "{:<16}{:<46}{:>8}{:>8}{:>8}{:>8}{:>9}{:>8}"


def _line(what, s: Setting, sc: Scores, secs=None):
    return _ROW.format(what, s.label(), f"{sc.p10:.4f}", f"{sc.r10:.4f}", f"{sc.mrr:.4f}",
                       f"{sc.hr10:.4f}", f"{sc.ndcg10:.4f}", f"{secs:.0f}s" if secs else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bookrs.evaluation.tuning", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--validation-readers", type=int, default=20_000)
    ap.add_argument("--quick", action="store_true",
                    help="A smaller grid, for a first look (about a third of the time).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--slice", default=None, metavar="KOHA_BIBLIOS_TSV",
                    help="Only the fixture slice's books (koha_biblios.tsv), as "
                         "the thesis evaluated, when the library holds more.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        data = restrict(load(conn, a.source_id,
                                 slice_works(conn, a.source_id, a.slice) if a.slice else None), 10)
    n_readers, n_works = len(data.patrons), len(data.works)
    shape = (n_readers, n_works)
    u, i, r, d = data.u, data.i, data.r, data.d

    test = split(u, n_readers, 0.2, a.seed)                    # the thesis's test
    train = ~test
    val = np.zeros(len(u), dtype=bool)
    val[np.flatnonzero(train)] = split(u[train], n_readers, 0.2, a.seed + 1)
    fit_part = train & ~val
    has_val = np.flatnonzero(np.bincount(u[val], minlength=n_readers) > 0)
    readers = np.sort(np.random.default_rng(a.seed + 2).choice(
        has_val, size=min(a.validation_readers, len(has_val)), replace=False))
    log.info("%d ratings: %d to fit on, %d validation, %d test; %d validation readers",
             len(u), fit_part.sum(), val.sum(), test.sum(), len(readers))

    grid = {k: v[1::2] + (THESIS.__dict__[k],) if a.quick else v for k, v in GRID.items()}
    grid = {k: tuple(sorted(set(v))) for k, v in grid.items()}

    head = _ROW.format("varied", "setting", "P@10", "R@10", "MRR", "HR@10", "NDCG@10", "")
    out = ["", f"Source {a.source_id}: {len(u):,} ratings, {n_readers:,} readers, {n_works:,} works.",
           "", f"Chosen on validation ({len(readers):,} readers, ratings carved from training):",
           head]
    results = []
    for name, s in candidates(THESIS, grid):
        t0 = time.monotonic()
        sc = score_setting(s, u, i, r, d, fit_part, val, shape, readers, a.seed)
        secs = time.monotonic() - t0
        results.append((name, s, sc))
        mark = "  <- thesis" if s == THESIS else ""
        out.append(_line(name, s, sc, secs) + mark)
        log.info("%s: P@10 %.4f (%.0fs)", s.label(), sc.p10, secs)

    best = combine(THESIS, results)
    if best == THESIS:
        out += ["", "No setting beat the thesis's value on validation by more than two "
                "standard errors. The thesis's settings stand; the test ratings were not used."]
        print("\n".join(out))
        return 0
    t0 = time.monotonic()
    best_val = score_setting(best, u, i, r, d, fit_part, val, shape, readers, a.seed)
    out.append(_line("combined", best, best_val, time.monotonic() - t0))

    everyone = np.arange(n_readers)
    final = []
    for label, s in (("thesis", THESIS), ("best", best)):
        t0 = time.monotonic()
        final.append((label, s, score_setting(s, u, i, r, d, train, test, shape, everyone, a.seed),
                      time.monotonic() - t0))
    out += ["", f"Then once, on the test ratings (all {n_readers:,} readers), fitted on all training ratings:",
            head]
    for label, s, sc, secs in final:
        out.append(_line(label, s, sc, secs) + f"   +/-{sc.p10_se:.4f}")
    gain = final[1][2].p10 - final[0][2].p10
    se = (final[0][2].p10_se ** 2 + final[1][2].p10_se ** 2) ** 0.5
    out += ["", f"Difference in P@10: {gain:+.4f} (about {abs(gain) / se:.0f} standard errors)."
            if se else ""]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
