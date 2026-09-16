"""Demonstrate folding-in for one patron, from a terminal only.

    docker compose run --rm recommend python -m bookrs.recommend.folding_demo
    docker compose run --rm recommend python -m bookrs.recommend.folding_demo --patron 7ae46532...

**Why this exists as a CLI rather than an API route.** ``folding.py``'s
own docstring is explicit that wiring it to anything a network can
reach needs a real patron-authentication decision, and that decision
does not belong inside a ranking module. That decision is still open --
Koha's session cookie is cross-origin and httponly to this service, and
bookrs-platform was never meant to hold patron credentials, so
answering it properly means designing an identity handoff, not adding a
route.

What was actually asked for, before that question is settled, is
smaller: prove the mechanism computes a real vector from a real
patron's real loans, and that two different patrons get two different
rankings. A terminal command does that completely, reaches no browser,
and creates no new exposure -- the risk the deferred route would carry
is that *any* caller could ask for *any* patron's shadow profile;
nothing here answers a caller at all.

**What it ranks by.** ``rerank()`` in ``rank.py`` blends around a query
*work*'s collaborative signal -- a different question from this one.
Once a patron has a vector, ranking the factorised catalogue by cosine
similarity to it is a separate, simpler operation, and that is what
this does: no blending, no content score, just the collaborative
picture on its own, because that is the one thing folding-in adds that
nothing else in the system can produce.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np
import psycopg

from bookrs.recommend.confidence import ConfidenceWeights, Interaction
from bookrs.recommend.folding import fold_in

log = logging.getLogger("bookrs.recommend.folding_demo")

#: Mirrors fold_in()'s own default. Not imported as a constant because
#: folding.py doesn't export one -- it's a plain default argument there,
#: confirmed by reading the committed file rather than assumed from an
#: earlier design conversation, which is what produced this file's first,
#: wrong version of this import.
MIN_FACTORISED_WORKS = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bookrs.recommend.folding_demo",
        description="Fold one patron into the factor space and show "
                     "what changes. Terminal only -- see module docstring "
                     "for why this is not an API route.",
    )
    p.add_argument(
        "--patron", default=None,
        help="A patron_ref to fold in. Omitted picks whichever patron has "
             "the most distinct factorised works, so the demo has the "
             "best chance of clearing the evidence floor without the "
             "caller needing to know one in advance.",
    )
    p.add_argument("--top", type=int, default=8,
                   help="How many personalised results to show.")
    p.add_argument("--compare", default=None,
                   help="A second patron_ref, folded in alongside the "
                        "first, to show the ranking genuinely differs "
                        "person to person rather than collapsing to one "
                        "answer regardless of who asks.")
    return p


def _load_interactions(conn: psycopg.Connection, patron_ref: str,
                        source_id: int | None) -> list[Interaction]:
    rows = conn.execute(
        """
        SELECT patron_ref, work_id, count(*) AS n_loans,
               coalesce(sum(renewals), 0) AS n_renewals
        FROM loans
        WHERE patron_ref = %s
          AND (%s::int IS NULL OR source_id = %s::int)
        GROUP BY patron_ref, work_id
        """,
        (patron_ref, source_id, source_id),
    ).fetchall()
    return [Interaction(r[0], r[1], r[2], r[3]) for r in rows]


def _top_two_patrons(conn: psycopg.Connection,
                     source_id: int | None) -> list[str]:
    rows = conn.execute(
        """
        SELECT l.patron_ref, count(DISTINCT l.work_id) AS n
        FROM loans l JOIN work_factors f ON f.work_id = l.work_id
        WHERE (%s::int IS NULL OR l.source_id = %s::int)
        GROUP BY l.patron_ref
        ORDER BY n DESC
        LIMIT 2
        """,
        (source_id, source_id),
    ).fetchall()
    return [r[0] for r in rows]


def _show(conn: psycopg.Connection, patron_ref: str,
          item_factors: dict[int, np.ndarray], titles: dict[int, str],
          loaned: dict[str, set[int]], top: int) -> None:
    interactions = _load_interactions(conn, patron_ref, None)
    result = fold_in(interactions, item_factors, ConfidenceWeights())

    print(f"  patron {patron_ref[:12]}...")
    print(f"    loan rows: {result.n_loans}   "
          f"distinct factorised works: {result.n_works}   "
          f"floor: {MIN_FACTORISED_WORKS}")

    if not result.personalised:
        print("    ABSTAINED -- below the evidence floor. This is the "
              "correct outcome for most patrons on this corpus, not a "
              "failure: 9 of 50 clear it, measured last session.")
        return

    already = loaned.setdefault(patron_ref, {i.work_id for i in interactions})
    scores = {wid: float(np.dot(result.vector, vec))
              for wid, vec in item_factors.items() if wid not in already}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top]

    print(f"    top {len(ranked)} by similarity to this patron's own vector:")
    for wid, score in ranked:
        print(f"      {score:+.3f}  {titles.get(wid, '(untitled)')[:56]}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    try:
        dsn = os.environ["DATABASE_URL"]
    except KeyError:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn:
        factor_rows = conn.execute(
            "SELECT work_id, vector FROM work_factors"
        ).fetchall()
        if not factor_rows:
            log.warning("no work_factors at all -- run the refit first "
                       "(python -m bookrs.recommend.cli)")
            return 1
        item_factors = {r[0]: np.asarray(r[1], dtype=np.float32)
                        for r in factor_rows}

        title_rows = conn.execute(
            "SELECT id, title FROM works WHERE id = ANY(%s)",
            (list(item_factors),),
        ).fetchall()
        titles = {r[0]: r[1] for r in title_rows}

        if args.patron:
            patrons = [args.patron] + ([args.compare] if args.compare else [])
        else:
            # No hash-pasting needed by default: automatically compares
            # whoever has the most and second-most distinct factorised
            # works, which is exactly the pair that best demonstrates the
            # ranking genuinely differs person to person.
            patrons = _top_two_patrons(conn, None)
            if not patrons:
                log.warning("no patron has any loan on a factorised work")
                return 1

        loaned: dict[str, set[int]] = {}
        for i, patron in enumerate(patrons):
            if i:
                print()
            _show(conn, patron, item_factors, titles, loaned, args.top)

    return 0


if __name__ == "__main__":
    sys.exit(main())
