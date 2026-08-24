"""Command-line entry point for embedding a catalogue.

    docker compose run --rm embedding python -m bookrs.embedding.cli

Embeds only what needs it: a work with no vector, one whose bibliography
has changed since its vector was made, or one embedded by a different
model or text composition. A second run over an unchanged catalogue does
nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import psycopg

from bookrs.embedding.encoder import DEFAULT_MODEL, Encoder
from bookrs.embedding.pipeline import embed_stale

log = logging.getLogger("bookrs.embed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bookrs.embedding.cli",
        description="Generate embeddings for works that need them.",
    )
    p.add_argument(
        "--model", default=os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL),
        help="Sentence-transformer model. The default is multilingual: "
             "measured on real catalogue data it matches the English-only "
             "model on English and is substantially better otherwise. "
             "Changing this re-embeds the whole catalogue, since vectors "
             "from different models are not comparable.",
    )
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--source-id", type=int, default=None,
                   help="Restrict to one source (default: all).")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report how many works need embedding and stop.",
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

    if args.dry_run:
        with psycopg.connect(dsn) as conn:
            pending, total = conn.execute(
                """
                SELECT count(*) FILTER (
                           WHERE e.work_id IS NULL
                              OR e.source_hash IS DISTINCT FROM w.content_hash
                              OR e.model <> %s),
                       count(*)
                FROM works w
                LEFT JOIN embeddings e ON e.work_id = w.id
                WHERE w.deleted_at IS NULL
                """,
                (args.model,),
            ).fetchone()
        log.info("%d of %d works need embedding with %s",
                 pending, total, args.model.split("/")[-1])
        return 0

    # Loading the model downloads weights on first use, so report before
    # rather than after: a library's first run otherwise sits silent for
    # a minute with no indication anything is happening.
    log.info("loading %s", args.model)
    encoder = Encoder(args.model, batch_size=args.batch_size)
    log.info("model ready: %d dimensions, %d token limit",
             encoder.dimensions, encoder.max_tokens)

    started = time.monotonic()
    with psycopg.connect(dsn) as conn:
        stats = embed_stale(conn, encoder, source_id=args.source_id)
    elapsed = time.monotonic() - started

    rate = stats.embedded / elapsed if elapsed else 0
    log.info("%d embedded (%d title-only, %d skipped) in %.0fs (%.0f/sec)",
             stats.embedded, stats.title_only, stats.skipped_empty,
             elapsed, rate)
    if stats.skipped_empty:
        log.info("skipped works have neither title nor subject headings; "
                 "encoding an empty string would give them all the same "
                 "vector and make them mutually similar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
