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
