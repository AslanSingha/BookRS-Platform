"""Where migrations are applied from.

Separate from test_migrations.py because these read source files and
need no database. Left in that module they inherited its skipif on
TEST_DATABASE_URL, so they reported as skipped wherever it was unset --
structural guards that quietly do not run.
"""

import pathlib

import pytest


class TestApplicationPoints:
    """Where migrations run, and why it is more than one place.

    A library upgrading pulls a new version and restarts its services.
    The API comes back immediately; the next harvest may be hours away.
    If only the harvest applied migrations, the API would spend that
    window querying columns that do not exist.
    """

    def _source(self, path):
        return pathlib.Path(path).read_text(encoding="utf-8")

    def test_harvest_cli_applies_migrations(self):
        src = self._source("bookrs/ingestion/cli.py")
        assert "apply_migrations(conn)" in src

    def test_api_applies_migrations_at_startup(self):
        """Not a style preference. Removing this reintroduces an
        upgrade-ordering hazard that no test would otherwise catch,
        because it only appears between a version bump and the next
        harvest."""
        src = self._source("bookrs/api/main.py")
        assert "apply_migrations(conn)" in src, (
            "the API no longer migrates at startup; a library upgrading "
            "would query an unmigrated schema until its next harvest"
        )

    def test_the_api_does_not_import_implicit(self):
        """Migrations pulled bookrs.db into the API's import graph. That
        must not drag in the ALS trainer, whose C extension the API
        image deliberately does not carry.

        Walked statically rather than by poisoning sys.modules and
        reloading: implicit IS installed in the test image and other
        tests import it legitimately, so mutating the module table
        mid-suite tests the suite's ordering rather than this property,
        and reloading a module holding a live connection pool is its own
        hazard.
        """
        import ast

        seen: set[str] = set()
        stack = ["bookrs/api/main.py"]
        while stack:
            path = stack.pop()
            if path in seen:
                continue
            seen.add(path)
            source = pathlib.Path(path)
            if not source.exists():
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    # Both the package and each name imported from it.
                    # "from bookrs.api import queries" resolves to
                    # bookrs/api.py on the module alone, which does not
                    # exist -- so queries.py was never walked, and the
                    # first version of this test passed while missing
                    # most of the graph.
                    names = [node.module] + [
                        f"{node.module}.{a.name}" for a in node.names
                    ]
                for name in names:
                    assert not name.split(".")[0] == "implicit", (
                        f"{path} imports implicit; the API image carries "
                        f"no C toolchain for it"
                    )
                    if name.startswith("bookrs."):
                        stack.append(name.replace(".", "/") + ".py")

        # The graph is a handful of modules; if the walk collapses to
        # just the entry point it is not checking anything.
        assert len(seen) >= 5, f"import walk covered only {len(seen)} files"
