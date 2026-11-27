"""Recommendations for one reader, from what the library holds about them.

This is the personalised counterpart to the item-to-item similarity the
OPAC widget already serves. Given a patron reference it reads that
patron's ratings, places them in the factor space (``folding.py``) and
ranks the catalogue.

**No reader vector is stored.** The vector is computed from the
patron's ratings when asked for and discarded with the answer. That
keeps the position ``work_factors`` was designed around -- no behavioural
profile per patron at rest -- and it means a rating made a minute ago
counts now, with no refit.

**Which answer a reader gets** follows what was measured
(``bookrs/evaluation/coldstart.py``), not what was assumed:

==============  =====================================================
ratings         answer
==============  =====================================================
none            the works most readers chose
one to four     a solve over the rated works only -- stays close to
                the popular list, which is the better answer this early
five or more    the full solve against the whole catalogue
==============  =====================================================

Content similarity is not blended in. Measured, the best weight for it
was 0 to 0.1 at every stage and the gain at 0.1 was under a thousandth
on P@10; it earns its place for works nobody has rated, which is a
different question from this one.

**Editions are one book.** A library often holds two records for the
same work -- a hardback and a paperback, a reprint -- and catalogues
them separately, as it should. To a reader they are one book: having
rated one edition, being offered the other is a recommendation of
something already read, and a list with both in it has one place
wasted. So works are grouped by title and first author (``work_key``)
and the group is what is excluded and what is counted. The records
stay as they are; only the answer is collapsed.

The evaluation (``bookrs.evaluation``) does not do this, on purpose: it
reproduces the thesis's protocol, and the thesis scored editions as
separate books.

**This module imports numpy and psycopg only**, so the API process can
use it without ``implicit``.

    docker compose run --rm recommend python -m bookrs.recommend.personal --source-id 54
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

import numpy as np

from bookrs.recommend.confidence import rating_confidence
from bookrs.recommend.folding import FactorSpace, place_reader

log = logging.getLogger("bookrs.recommend.personal")

def work_key(title: str, authors) -> str:
    """What two records must share to count as editions of one book.

    The title and the first author, compared without case, accents of
    width, or spacing. Deliberately strict: two records are merged only
    when they agree exactly on both, so a false merge -- hiding a
    different book -- needs two different books with the same title by
    the same author, which a catalogue's own title rules already
    distinguish.
    """
    import unicodedata

    def norm(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())

    first = authors[0] if authors else ""
    return f"{norm(title)}\x1f{norm(first)}"


METHODS = {
    "none": "most-rated works (no ratings yet)",
    "few": "placed from the rated works only (fewer than five ratings)",
    "full": "placed against the whole catalogue (five or more ratings)",
}


@dataclass(frozen=True)
class Recommendation:
    work_id: int
    score: float


def load_space(conn, source_id: int) -> FactorSpace:
    """The stored factors for one library's works, editions grouped."""
    from bookrs.db.arrays import vector_cursor

    # The factors arrive as float32 arrays rather than Python floats:
    # up to 142,000 books of 128 factors at the full UCSD catalogue.
    with vector_cursor(conn) as cur:
        rows = cur.execute(
            "SELECT f.work_id, f.raw_vector, f.n_patrons, w.title, w.authors "
            "FROM work_factors f JOIN works w ON w.id = f.work_id "
            "WHERE w.source_id = %s AND w.deleted_at IS NULL", (source_id,)).fetchall()
    space = FactorSpace.from_rows([r[:3] for r in rows])
    keys = {r[0]: work_key(r[3], r[4]) for r in rows}
    return space.with_groups([keys.get(int(w), str(w)) for w in space.work_ids])


def load_ratings(conn, source_id: int, patron_ref: str) -> list[tuple[int, int, bool]]:
    """(work_id, rating, reviewed) for one patron, oldest first."""
    return conn.execute(
        "SELECT work_id, rating, reviewed FROM ratings "
        "WHERE source_id = %s AND patron_ref = %s ORDER BY rated_at, work_id",
        (source_id, patron_ref)).fetchall()


def recommend(space: FactorSpace, ratings, limit: int = 10,
              regularization: float = 0.1) -> tuple[list[Recommendation], str]:
    """Rank the catalogue for a reader with these ratings.

    Works the reader has already rated are never returned. The second
    value names the method used -- a key of ``METHODS`` -- so a page can
    say honestly why it is showing what it shows.
    """
    rated = {int(w) for w, _, _ in ratings}
    groups = space.groups if space.groups is not None else np.arange(len(space))
    rated_groups = {int(groups[space.index[w]]) for w in rated if w in space.index}
    x, method = place_reader(
        {int(w): float(rating_confidence(r, 1.0 if d else 0.0)) for w, r, d in ratings},
        space, regularization)

    if x is None:
        scores = space.n_patrons.astype(np.float32)
    else:
        scores = space.factors @ x
    scores = scores.astype(np.float32, copy=True)
    # Every edition of a book the reader rated, not only the record they
    # rated.
    if rated_groups:
        scores[np.isin(groups, list(rated_groups))] = -np.inf

    # Best first, one record per book. Editions are few, so the top few
    # times `limit` almost always holds enough distinct books; the full
    # sort is the fallback, not the path.
    finite = int(np.isfinite(scores).sum())
    out: list[Recommendation] = []
    taken: set[int] = set()
    for depth in (min(finite, 4 * limit), finite):
        if depth <= 0:
            break
        cand = np.argpartition(-scores, depth - 1)[:depth] if depth < len(scores) \
            else np.arange(len(scores))
        cand = cand[np.argsort(-scores[cand], kind="stable")]
        out, taken = [], set()
        for j in cand:
            if not np.isfinite(scores[j]) or int(groups[j]) in taken:
                continue
            taken.add(int(groups[j]))
            out.append(Recommendation(int(space.work_ids[j]), float(scores[j])))
            if len(out) == limit:
                return out, method
    return out, method


def recommend_for_patron(conn, source_id: int, patron_ref: str, limit: int = 10,
                         space: FactorSpace | None = None
                         ) -> tuple[list[Recommendation], str]:
    """Read one patron's ratings and rank the catalogue for them."""
    space = space or load_space(conn, source_id)
    return recommend(space, load_ratings(conn, source_id, patron_ref), limit)


def _titles(conn, work_ids) -> dict[int, str]:
    if not work_ids:
        return {}
    rows = conn.execute("SELECT id, title, authors FROM works WHERE id = ANY(%s)",
                        (list(work_ids),)).fetchall()
    return {i: (t[:58] + ("  --  " + a[0][:22] if a else "")) for i, t, a in rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bookrs.recommend.personal",
        description="Show what one reader would be recommended as the library "
                    "learns more about them.",
    )
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--patron", default=None,
                    help="A patron_ref. Default: one chosen at random among "
                         "readers with 25 to 60 ratings.")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--steps", default="0,3,20,all",
                    help="How many of the reader's ratings to use at each step.")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        space = load_space(conn, a.source_id)
        patron = a.patron
        if patron is None:
            if a.seed is not None:
                conn.execute("SELECT setseed(%s)", ((a.seed % 1000) / 1000.0,))
            # TABLESAMPLE keeps this from scanning sixteen million rows
            # to pick one person.
            row = conn.execute(
                "SELECT patron_ref FROM (SELECT patron_ref FROM ratings "
                "TABLESAMPLE SYSTEM (0.05) WHERE source_id = %s) s "
                "JOIN LATERAL (SELECT count(*) n FROM ratings r WHERE r.source_id = %s "
                "AND r.patron_ref = s.patron_ref) c ON c.n BETWEEN 25 AND 60 "
                "ORDER BY random() LIMIT 1", (a.source_id, a.source_id)).fetchone()
            if row is None:
                raise SystemExit("no reader with 25 to 60 ratings was sampled; "
                                 "run again or pass --patron")
            patron = row[0]
        ratings = load_ratings(conn, a.source_id, patron)
        if not ratings:
            raise SystemExit(f"patron {patron} has no ratings in source {a.source_id}")

        # A fixed shuffle, so "the first three" is not "the three oldest":
        # every rating in the fixture carries the same load timestamp.
        import zlib
        order = np.random.default_rng(zlib.crc32(patron.encode()) if a.seed is None
                                      else a.seed).permutation(len(ratings))
        ratings = [ratings[j] for j in order]

        print(f"\nReader {patron[:12]}...  {len(ratings)} ratings in the library. "
              f"{len(space):,} works have factors.")
        print("This reader was part of the fit, so this shows how the answer "
              "changes with what is used, not a test.\n")
        for step in a.steps.split(","):
            n = len(ratings) if step.strip() == "all" else min(int(step), len(ratings))
            used = ratings[:n]
            recs, method = recommend(space, used, a.limit)
            names = _titles(conn, [w for w, _, _ in used] + [r.work_id for r in recs])
            rest = {w for w, _, _ in ratings[n:]}
            print(f"--- using {n} rating{'s' if n != 1 else ''}: {METHODS[method]}")
            if used:
                shown = sorted(used, key=lambda t: -t[1])[:5]
                print("    rated: " + "; ".join(
                    f"{names.get(w, w)[:34].rstrip()} ({r}*)" for w, r, _ in shown)
                    + (f"; and {n - 5} more" if n > 5 else ""))
            for k, rec in enumerate(recs, 1):
                mark = "  <- rated by this reader, not shown to the model" \
                    if rec.work_id in rest else ""
                print(f"    {k:>2}. {names.get(rec.work_id, rec.work_id)}{mark}")
            hits = sum(r.work_id in rest for r in recs)
            if rest:
                print(f"    {hits} of {len(recs)} are books this reader did rate.")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
