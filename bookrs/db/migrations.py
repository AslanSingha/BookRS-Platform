"""Schema migrations.

``schema.sql`` is mounted as a Docker initdb script, so it runs exactly
once, when the database is created empty. Nothing re-applies it. That is
fine for a fresh install and useless for an existing one: without a
migration path, a library that deployed six months ago and pulls a new
version would have to drop its database and re-harvest the entire
catalogue to pick up a single added column.

For a system whose deployment model is "a library self-hosts this and
runs it for years", that is not acceptable, so migrations are applied at
startup rather than left to the operator.

The design is deliberately small.

- **Forward only.** No down-migrations. A library rolling back would
  restore a backup, not un-apply DDL, and a wrong down-migration
  destroys data that a wrong up-migration merely fails to add.
- **Recorded, not inferred.** Applied versions are stored in a table
  rather than detected by inspecting the schema. Inspection guesses;
  a record states.
- **Each migration is idempotent anyway.** Belt and braces: the ledger
  should prevent re-running, and if it ever fails to, `ADD COLUMN IF NOT
  EXISTS` means the second run is harmless rather than fatal.
- **One transaction per migration.** A partially applied migration
  would leave the ledger disagreeing with the schema, which is the
  state hardest to recover from.

Migrations live in this file rather than as separate .sql files so that
the version, the statement and the reason travel together. If the list
grows past a couple of dozen, move it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

log = logging.getLogger(__name__)

__all__ = ["MIGRATIONS", "apply_migrations", "applied_versions"]


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="works.title_alternate for linked 880 script",
        statements=(
            # A record catalogued in a non-Latin script commonly carries
            # its title twice: a romanisation in 245 and the original in
            # a linked 880. Reading only 245 stored the transliteration
            # and discarded the language, which the multilingual
            # embedding model was chosen to use.
            "ALTER TABLE works ADD COLUMN IF NOT EXISTS "
            "title_alternate TEXT NOT NULL DEFAULT ''",
            # Indexed on exactly the same terms as works_title_trgm_idx.
            #
            # public.immutable_unaccent rather than unaccent(): the
            # latter is STABLE, because its dictionary can be changed at
            # runtime, and PostgreSQL refuses STABLE functions in index
            # expressions. schema.sql defines the wrapper and says so;
            # this migration reproduced the error by not reading it.
            #
            # lower() matters too -- without it the alternate script
            # would be the one field in the catalogue where search is
            # case-sensitive.
            "CREATE INDEX IF NOT EXISTS works_title_alternate_trgm_idx "
            "ON works USING GIN "
            "(public.immutable_unaccent(lower(title_alternate)) "
            "gin_trgm_ops)",
        ),
    ),
    Migration(
        version=2,
        description="sources.request_headers for proxy-routed endpoints",
        statements=(
            # An endpoint behind a reverse proxy that routes by Host is
            # unreachable without that header, and the header was only
            # ever a command-line argument. A scheduled harvest run by
            # cron months later has no way to know it is needed: the
            # request does not fail, it times out, which reads as the
            # library being down rather than as a missing header.
            #
            # Stored per source so the source carries its own routing.
            # An explicit --header still overrides what is stored, since
            # the operator in front of the terminal knows more than the
            # row does.
            "ALTER TABLE sources ADD COLUMN IF NOT EXISTS "
            "request_headers JSONB NOT NULL DEFAULT '{}'::jsonb",
        ),
    ),
    Migration(
        version=3,
        description="ratings.reviewed: whether the patron also wrote a review",
        statements=(
            # The thesis weighted an interaction as c = 1 + 2r + 3d, where
            # d flags a written review. The rating r had a column here; d
            # did not, so the second term could not be computed from what
            # this service stored.
            #
            # Koha keeps reviews in their own table. Only the fact of one
            # is carried over -- a boolean, never the text. What a patron
            # wrote is theirs and the library's, and the confidence
            # formula has no use for it.
            #
            # DEFAULT false is also the true value for every row written
            # before this migration: nothing read reviews until now.
            "ALTER TABLE ratings ADD COLUMN IF NOT EXISTS "
            "reviewed BOOLEAN NOT NULL DEFAULT false",
        ),
    ),
    Migration(
        version=4,
        description="work_factors.raw_vector: factors as fitted, for folding-in",
        statements=(
            # work_factors.vector is L2-normalised so that a dot product
            # between two works is a cosine, which is what "readers of
            # this also read" needs. Placing a new reader in the factor
            # space is a different calculation: a least-squares solve
            # against the factors as the fit produced them, lengths
            # included. A work's length in that space carries how much
            # evidence stands behind it, and normalising throws it away.
            #
            # Measured on 10,000 readers withheld from training
            # (bookrs/evaluation/coldstart.py): solving against the
            # whole catalogue reaches P@10 0.179 from twenty ratings,
            # against 0.090 for a solve over the rated books alone. The
            # first needs these.
            #
            # Nullable: rows written before this migration have none,
            # and the next refit replaces every row anyway.
            "ALTER TABLE work_factors ADD COLUMN IF NOT EXISTS raw_vector REAL[]",
        ),
    ),
    Migration(
        version=5,
        description="work_factors.signal: what the factors were learned from",
        statements=(
            # The OPAC heads a record page's similar books "Readers also
            # borrowed" when every result carries ALS evidence. That was
            # true while factors came only from loans. A refit can now
            # learn them from star ratings (--signal ratings), and the
            # same heading over ratings claims borrowing that never
            # happened. What the factors stand on is recorded with them,
            # so the page can say which.
            #
            # Nullable: rows written before this migration do not say,
            # and the page then claims neither, falling back to the
            # weaker heading until the next refit records it. Recorded,
            # not inferred: a source holding loans may still have been
            # refitted from ratings.
            "ALTER TABLE work_factors ADD COLUMN IF NOT EXISTS signal TEXT "
            "CHECK (signal IN ('loans', 'ratings'))",
        ),
    ),
    Migration(
        version=6,
        description="job_runs: what the built-in scheduler ran, and how it went",
        statements=(
            # One row per run of a scheduled job (bookrs/schedule.py). The
            # scheduler reads it to decide when a job is next due, so a
            # restarted container does not run everything again at once;
            # /health reads it so a library can see that reading Koha and
            # refitting are still happening, not only that the API is up.
            #
            # `every` is the cadence the run was started under, kept so
            # /health can say when the next run is due without knowing the
            # scheduler's settings. A row whose finished_at is NULL is a
            # run that was interrupted: the container stopped mid-job.
            """
            CREATE TABLE IF NOT EXISTS job_runs (
                id          BIGSERIAL   PRIMARY KEY,
                job         TEXT        NOT NULL,
                source_id   INTEGER,
                every       TEXT        NOT NULL,
                started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                ok          BOOLEAN,
                detail      TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS job_runs_job_idx "
            "ON job_runs (job, source_id, started_at DESC)",
        ),
    ),
    Migration(
        version=7,
        description="sources: where the last ratings read reached, and the last full one",
        statements=(
            # A read of changes (bookrs.ingestion.direct --changes) asks
            # Koha for ratings stamped since the last read. "Since" is
            # Koha's own NOW() at the start of that read, kept as Koha
            # reported it -- a timestamp without zone, in the clock that
            # stamps the ratings -- so nothing converts between clocks.
            # It is written in the same transaction as the ratings it
            # covers: a watermark that moved without them would skip
            # what they missed.
            "ALTER TABLE sources ADD COLUMN IF NOT EXISTS ratings_watermark TIMESTAMP",
            # When the last full read finished, this service's clock: a
            # read of changes cannot see what was removed, so --full-every
            # reads everything again once this is old enough.
            "ALTER TABLE sources ADD COLUMN IF NOT EXISTS ratings_full_read_at TIMESTAMPTZ",
        ),
    ),
    Migration(
        version=8,
        description="sources: where the last catalogue read reached; items: collection code",
        statements=(
            # The catalogue's counterpart of migration 7. A read of changes
            # (bookrs.ingestion.koha_db --changes) asks Koha for records
            # whose record, publication details or copies were stamped
            # since; a catalogue of 883,000 records is too large to read
            # whole every ten minutes.
            "ALTER TABLE sources ADD COLUMN IF NOT EXISTS catalogue_watermark TIMESTAMP",
            # When the last full catalogue read finished: the only read that
            # sees a record removed from Koha, and marks it deleted here.
            "ALTER TABLE sources ADD COLUMN IF NOT EXISTS catalogue_full_read_at TIMESTAMPTZ",
            # Koha's items.ccode: where a library records genre on the
            # shelf, and where the UCSD fixture put the thesis's genres.
            "ALTER TABLE items ADD COLUMN IF NOT EXISTS collection VARCHAR(80)",
        ),
    ),
)

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER     PRIMARY KEY,
    description TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def applied_versions(conn: psycopg.Connection) -> set[int]:
    conn.execute(_LEDGER)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(conn: psycopg.Connection) -> list[int]:
    """Apply every migration not yet recorded. Returns those applied.

    Safe to call on every startup, and safe to call concurrently: the
    ledger's primary key means a second process attempting the same
    version fails its insert and rolls back that migration alone.
    """
    done = applied_versions(conn)
    applied: list[int] = []

    for migration in MIGRATIONS:
        if migration.version in done:
            continue
        # One transaction per migration. A migration that half-applies
        # leaves the ledger disagreeing with the schema, which is the
        # hardest state to recover from.
        with conn.transaction():
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description) "
                "VALUES (%s, %s)",
                (migration.version, migration.description),
            )
        log.info("applied migration %d: %s",
                 migration.version, migration.description)
        applied.append(migration.version)

    return applied
