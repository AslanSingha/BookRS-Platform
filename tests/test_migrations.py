"""Tests for schema migrations.

These run against the test database, because a migration that passes
against a mock proves nothing about the DDL it emits.
"""

import os

import psycopg
import pytest

from bookrs.db.migrations import (
    MIGRATIONS, apply_migrations, applied_versions,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL is not set"
)


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


class TestLedger:
    def test_ledger_is_created_on_first_call(self, conn):
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        assert applied_versions(conn) == set()

    def test_versions_are_recorded(self, conn):
        apply_migrations(conn)
        recorded = applied_versions(conn)
        assert {m.version for m in MIGRATIONS} <= recorded


class TestApplication:
    def test_applying_twice_is_a_no_op(self, conn):
        """Called on every startup, so the second call must do nothing
        rather than fail on an existing column."""
        apply_migrations(conn)
        assert apply_migrations(conn) == []

    def test_from_scratch_applies_everything(self, conn):
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        applied = apply_migrations(conn)
        assert applied == [m.version for m in MIGRATIONS]

    def test_statements_are_individually_idempotent(self, conn):
        """The ledger should prevent re-running, but if it ever fails to,
        a second execution must be harmless rather than fatal."""
        for migration in MIGRATIONS:
            for statement in migration.statements:
                conn.execute(statement)
                conn.execute(statement)


class TestOutcome:
    def test_title_alternate_exists_and_defaults_empty(self, conn):
        apply_migrations(conn)
        row = conn.execute("""
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'works' AND column_name = 'title_alternate'
        """).fetchone()
        assert row is not None, "the column was not created"
        data_type, nullable, default = row
        assert data_type == "text"
        assert nullable == "NO"
        assert default is not None

    def test_existing_rows_get_the_default(self, conn):
        """A library upgrading has rows already. They must not become
        NULL, which would break every consumer expecting a string."""
        apply_migrations(conn)
        nulls = conn.execute(
            "SELECT count(*) FROM works WHERE title_alternate IS NULL"
        ).fetchone()[0]
        assert nulls == 0

    def test_factor_signal_exists_and_is_nullable(self, conn):
        """Factors refitted before migration 5 do not say what they were
        learned from, and must not be made to claim either."""
        apply_migrations(conn)
        row = conn.execute("""
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'work_factors' AND column_name = 'signal'
        """).fetchone()
        assert row == ("text", "YES")

    def test_sources_record_where_the_last_ratings_read_reached(self, conn):
        """The watermark keeps Koha's clock as Koha gave it: no zone, so
        nothing converts it between the library's clock and this one."""
        apply_migrations(conn)
        cols = dict(conn.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = 'sources'
              AND column_name IN ('ratings_watermark', 'ratings_full_read_at')
        """).fetchall())
        assert cols == {"ratings_watermark": "timestamp without time zone",
                        "ratings_full_read_at": "timestamp with time zone"}

    def test_job_runs_records_what_the_scheduler_ran(self, conn):
        """bookrs/schedule.py decides when a job is next due from it, and
        /health shows it; an unfinished run keeps NULL in finished_at."""
        apply_migrations(conn)
        cols = dict(conn.execute("""
            SELECT column_name, is_nullable FROM information_schema.columns
            WHERE table_name = 'job_runs'
        """).fetchall())
        assert {"job", "source_id", "every", "started_at", "finished_at",
                "ok", "detail"} <= set(cols)
        assert cols["finished_at"] == "YES" and cols["ok"] == "YES"
        assert cols["job"] == "NO" and cols["every"] == "NO"

    def test_factor_signal_accepts_only_known_evidence(self, conn):
        """The OPAC chooses its heading from this value. A misspelt one
        would quietly fall back to the weaker heading, so the database
        refuses it instead."""
        apply_migrations(conn)
        sid = conn.execute("INSERT INTO sources (name, base_url) "
                           "VALUES ('m', 'mysql://m') RETURNING id").fetchone()[0]
        wid = conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, content_hash, "
            "items_hash) VALUES (%s, 'm1', 'w', 'c', 'i') RETURNING id", (sid,)
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, dimensions, "
                "factoriser_version, signal) "
                "VALUES (%s, ARRAY[1.0]::real[], 1, 2, 'clicks')", (wid,))


class TestDefinitions:
    def test_versions_are_unique(self):
        versions = [m.version for m in MIGRATIONS]
        assert len(versions) == len(set(versions))

    def test_versions_are_ordered(self):
        """Applied in list order, so an out-of-order list would apply
        migrations in a sequence their author did not intend."""
        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions)

    def test_every_migration_describes_itself(self):
        """The description is written into the ledger, where it is the
        only clue an operator has about what a version number did."""
        for migration in MIGRATIONS:
            assert migration.description.strip()
            assert migration.statements

