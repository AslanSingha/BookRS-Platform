"""Command-line entry point for refitting the collaborative model.

    docker compose run --rm recommend python -m bookrs.recommend.cli

Reads current circulation, weights it by the confidence formula, fits
the factorisation, and replaces the stored factors wholesale.

**Wholesale, not incrementally.** A refit produces a new factor space,
and vectors from two different runs are not comparable -- ALS is
rotation-invariant, so an old vector and a new one describe the same
work in coordinate systems that share no axes. Mixing them returns
confident nonsense rather than an error, which is why the stored
version is checked and the whole set is replaced.

**Why this is a separate command rather than part of the harvest.**
The three stages run on different natural cadences. Cataloguing changes
daily and is cheap to sync. Embedding follows the catalogue and is
expensive only for genuinely new records. Factorisation is a batch job
whose output is meaningless in small increments -- one new loan does not
change the shape of a factor space -- so it wants a slower cadence than
either. Coupling them would force the slowest onto all three.

**Scheduling is deliberately not built in.** See the README: the
schedule belongs to the library, on the host, alongside the cron jobs
Koha already requires. A scheduler inside a container fights Docker's
one-process model and hides its failures from the monitoring a library
already has.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from bookrs.recommend.als import ALSParams
from bookrs.recommend.pipeline import factorise

log = logging.getLogger("bookrs.recommend")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bookrs.recommend.cli",
        description="Refit the collaborative model from current circulation.",
    )
    p.add_argument(
        "--factors", type=int, default=None,
        help="Latent dimensions. Omitted means chosen from the data, which "
             "is the default and the recommended setting: a fixed value does "
             "not transfer across scales.",
    )
    p.add_argument(
        "--iterations", type=int, default=None,
        help="ALS iterations. Omitted uses the library default.",
    )
    p.add_argument(
        "--source-id", type=int, default=None,
        help="Restrict to one configured library. Omitted means all of them.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report what circulation is available and stop, without "
             "touching the stored factors.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )

    try:
        dsn = os.environ["DATABASE_URL"]
    except KeyError:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn:
        if args.dry_run:
            loans, patrons, works = conn.execute(
                """
                SELECT count(*), count(DISTINCT patron_ref), count(DISTINCT work_id)
                FROM loans
                WHERE (%s::int IS NULL OR source_id = %s::int)
                """,
                (args.source_id, args.source_id),
            ).fetchone()
            log.info("%d loans from %d patrons across %d works",
                     loans, patrons, works)
            if loans == 0:
                log.warning("no circulation: a refit would produce no factors")
            return 0

        params = ALSParams()
        if args.factors is not None:
            params = ALSParams(**{**vars(params), "factors": args.factors})
        if args.iterations is not None:
            params = ALSParams(**{**vars(params), "iterations": args.iterations})

        stats = factorise(conn, params=params, source_id=args.source_id)
        conn.commit()

    # Reported rather than merely logged as a count, because these are the
    # numbers that say whether the result means anything. A refit over 87
    # interactions completes successfully and produces factors nobody
    # should rely on; the density figure is what makes that visible.
    log.info(
        "%d interactions from %d patrons across %d works "
        "(%d patrons and %d works excluded); density %.2f%%; %.1fs",
        stats.interactions, stats.patrons, stats.works,
        stats.excluded_patrons, stats.excluded_works,
        # als.py already scales this to a percentage. Multiplying again
        # produced "density 1160.00%", which is impossible and therefore
        # noticeable -- a wrong number in the plausible range would not
        # have been.
        stats.density, stats.seconds,
    )

    if stats.works == 0:
        log.warning(
            "no works were factorised: recommendations will fall back to "
            "the content layer entirely"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
