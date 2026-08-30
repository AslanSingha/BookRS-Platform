"""Shared test fixtures.

The test database is created by ``bookrs/db/test-db.sh`` at container
init, from ``schema.sql`` as it stood at that moment. Nothing re-applies
it, and migrations run from the harvest CLI, which the suite does not
invoke. So a database created before a migration was written keeps an
old schema while the tests exercise code that expects the new one --
which is how eleven loader tests failed against a perfectly correct
loader.

Applying migrations once per session fixes that, and does it through the
same code path a deployment uses rather than a test-only shortcut.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_database():
    """Bring the test database's schema up to date before anything runs.

    Silent when TEST_DATABASE_URL is unset: the suite's database-backed
    tests skip in that case, and failing here would turn a clean skip
    into a collection error.
    """
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        return

    try:
        import psycopg

        from bookrs.db.migrations import apply_migrations
    except ImportError:
        return

    with psycopg.connect(dsn) as conn:
        apply_migrations(conn)
        conn.commit()
