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

# The API's caches ask the database what changed at most every 30 seconds
# (bookrs/api/vectors.py). Tests change it between one request and the
# next, so here they ask every time; tests of the interval set their own.
# Set before any test module imports the API, which reads it.
os.environ.setdefault("BOOKRS_CACHE_PROBE_SECONDS", "0")


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


@pytest.fixture(autouse=True)
def _api_holds_nothing_yet():
    """Each test starts with the API's vectors unloaded, as after a
    restart. Held vectors outlive a test's data otherwise, and a reload
    they then need runs in the background, after the test has asserted."""
    try:
        from bookrs.api import vectors
    except ImportError:             # an image without the API's packages
        yield
        return
    vectors.CATALOGUE = vectors.CatalogueVectors()
    yield
