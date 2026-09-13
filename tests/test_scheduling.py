"""Tests for the collaborative refit command and freshness reporting.

The command exists because factorise() previously had no entry point:
the pipeline could not be operated without an inline Python invocation,
which is not something a library should have to write.

The freshness reporting exists because a refit that silently stops is
the failure a library would not otherwise notice. Recommendations keep
being served from increasingly stale factors -- plausibly, and wrongly.
That is the same shape as a positive metric over meaningless output, and
the countermeasure is the same: report the thing that would look
identical if it were broken.
"""

import pathlib

import pytest


SOURCE = pathlib.Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    path = SOURCE / relative
    assert path.exists(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


class TestRefitCommand:
    def test_the_command_exists(self):
        """factorise() is a library function. Without an entry point the
        collaborative layer cannot be run except by hand."""
        assert (SOURCE / "bookrs/recommend/cli.py").exists()

    def test_it_parses_arguments_without_a_database(self):
        from bookrs.recommend.cli import build_parser
        args = build_parser().parse_args(["--dry-run", "--factors", "16"])
        assert args.dry_run is True
        assert args.factors == 16

    def test_factors_defaults_to_chosen_from_the_data(self):
        """A fixed dimensionality does not transfer across scales, so the
        default must not be a number."""
        from bookrs.recommend.cli import build_parser
        assert build_parser().parse_args([]).factors is None

    def test_a_missing_database_url_is_reported_not_raised(self, monkeypatch):
        from bookrs.recommend import cli
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert cli.main(["--dry-run"]) == 2


class TestFreshnessReporting:
    """A library's monitoring must be able to see that the model stopped
    being refitted, not merely that the service is up."""

    def test_health_reports_when_factors_were_last_built(self):
        source = read("bookrs/api/main.py")
        assert "last_factorised" in source, (
            "without this a library cannot tell that retraining stopped; "
            "recommendations keep being served from stale factors"
        )

    def test_health_reports_how_many_works_carry_factors(self):
        assert "factorised_works" in read("bookrs/api/main.py")

    def test_health_still_reports_harvest_freshness(self):
        assert "last_harvest" in read("bookrs/api/main.py")


class TestNoSchedulerIsShipped:
    """The schedule belongs to the library, on its own host, alongside
    the cron jobs Koha already requires.

    A scheduler inside a container fights Docker's one-process model,
    complicates restart semantics, and hides its failures from the
    monitoring a library already operates. These assertions exist so
    that adding one is a deliberate decision rather than a drift.
    """

    def test_no_scheduler_in_the_service_images(self):
        for name in ("recommend.Dockerfile", "api.Dockerfile",
                     "ingestion.Dockerfile", "embedding.Dockerfile"):
            path = SOURCE / "docker" / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8").lower()
            for token in ("cron", "supervisord", "systemd"):
                assert token not in text, f"{name} installs {token}"

    def test_the_readme_documents_scheduling(self):
        """Not shipping a scheduler is only defensible if the library is
        told what to schedule and how often.

        Skipped rather than failed when the README is not present: the
        test image mounts bookrs/, tests/ and docker/, and the README
        sits at the repository root. Asserting on a file the container
        cannot see tests the mount, not the documentation -- and a guard
        that fails for a reason unrelated to what it guards is worse
        than one that says plainly it could not look.
        """
        path = SOURCE / "README.md"
        if not path.exists():
            pytest.skip("README.md is not mounted into this environment")
        readme = path.read_text(encoding="utf-8").lower()
        assert "schedul" in readme
        assert "cron" in readme
