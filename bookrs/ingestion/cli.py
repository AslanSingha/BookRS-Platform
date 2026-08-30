"""Command-line entry point for harvesting a catalogue.

    docker compose run --rm ingestion python -m bookrs.ingestion.cli \
        --url https://library.example/cgi-bin/koha/oai.pl \
        --name "Example Library"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import psycopg

from bookrs.db.loader import ensure_source, load_works
from bookrs.db.migrations import apply_migrations
from bookrs.ingestion.harvest import HarvestConfig
from bookrs.ingestion.pipeline import ingest
from bookrs.ingestion.preflight import PreflightError

log = logging.getLogger("bookrs.harvest")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bookrs.ingestion.cli",
        description="Harvest a library catalogue over OAI-PMH.",
    )
    p.add_argument(
        "--url", default=os.environ.get("OAI_BASE_URL"),
        help="OAI-PMH endpoint, usually .../cgi-bin/koha/oai.pl. Give the "
             "URL that works, not the one the endpoint advertises: Koha "
             "reports a baseURL it does not answer on.",
    )
    p.add_argument("--name", default=None,
                   help="Label for this source (default: the URL's host).")
    p.add_argument("--prefix", default="marc21", help="Metadata prefix.")
    p.add_argument("--from", dest="from_", default=None, metavar="DATE",
                   help="Harvest only records changed since this date "
                        "(YYYY-MM-DD). Note that circulation activity bumps "
                        "a record's datestamp, so an incremental harvest "
                        "returns borrowed books whose bibliography is "
                        "unchanged.")
    p.add_argument("--until", default=None, metavar="DATE")
    p.add_argument("--set", dest="set_", default=None,
                   help="Restrict to an OAI set, if the library defines any.")
    p.add_argument(
        "--header", action="append", default=[], metavar="NAME:VALUE",
        help="Extra request header, repeatable. Needed for endpoints "
             "behind a reverse proxy that routes by Host.",
    )
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument(
        "--allow-missing-items", action="store_true",
        help="Proceed even if holdings vanished since the last run. By "
             "default that stops the harvest, because a catalogue losing "
             "every holding overnight is far more likely to be a "
             "configuration regression than a real change.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not args.url:
        print("error: --url is required (or set OAI_BASE_URL)", file=sys.stderr)
        return 2

    headers = {}
    for raw in args.header:
        if ":" not in raw:
            print(f"error: --header expects NAME:VALUE, got {raw!r}",
                  file=sys.stderr)
            return 2
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()

    cfg = HarvestConfig(
        base_url=args.url, metadata_prefix=args.prefix, from_=args.from_,
        until=args.until, set_=args.set_, headers=headers, timeout=args.timeout,
    )
    name = args.name or args.url.split("//")[-1].split("/")[0]

    started = time.monotonic()
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            # schema.sql is an initdb script: it runs once, on an empty
            # database, and nothing re-applies it. Without this, a schema
            # change could only ever reach a fresh install, and a library
            # upgrading would have to drop its database and re-harvest to
            # pick up an added column.
            if applied := apply_migrations(conn):
                log.info("applied schema migrations: %s",
                         ", ".join(str(v) for v in applied))

            # What the previous run saw, so a sudden loss of holdings is
            # treated as a configuration regression rather than accepted.
            previous = conn.execute(
                "SELECT last_had_items FROM sources "
                "WHERE base_url = %s AND metadata_prefix = %s",
                (args.url, args.prefix),
            ).fetchone()
            expect_items = None
            if previous and previous[0] and not args.allow_missing_items:
                expect_items = True

            stream, result = ingest(cfg, expect_items=expect_items)
            source_id = ensure_source(conn, name, args.url, args.prefix,
                                      result.preflight.flavour)
            conn.commit()

            stats = load_works(conn, source_id, stream)
            conn.execute(
                "UPDATE sources SET last_had_items = %s WHERE id = %s",
                (result.preflight.has_items, source_id),
            )
            conn.commit()
    except PreflightError as exc:
        log.error("%s", exc)
        return 1
    except KeyError:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    elapsed = time.monotonic() - started
    log.info(
        "%s: %d new, %d updated, %d unchanged, %d deleted; "
        "holdings rewritten for %d works (%d item rows); %d pages in %.0fs",
        name, stats.inserted, stats.updated, stats.unchanged, stats.deleted,
        stats.items_refreshed, stats.items, result.stats.pages, elapsed,
    )
    if result.errors:
        log.warning("%d records could not be mapped; first: %s",
                    len(result.errors), result.errors[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
