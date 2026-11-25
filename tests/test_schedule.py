"""The built-in scheduler (bookrs/schedule.py).

What it promises a library: a cadence it can set in minutes, hours, days
or weeks; a record of every run, so a restarted container does not run
everything again and a failure is retried the same day; a run that
stops at the first command that fails; and the record shown in /health.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bookrs import schedule
from bookrs.schedule import commands, next_due, parse_every

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
T = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)


class TestCadence:
    @pytest.mark.parametrize("text,seconds", [
        ("30m", 1_800), ("1h", 3_600), ("6h", 21_600), ("1d", 86_400),
        ("2w", 1_209_600), (" 1D ", 86_400), ("12 h", 43_200),
    ])
    def test_a_whole_number_and_a_unit(self, text, seconds):
        assert parse_every(text) == timedelta(seconds=seconds)

    def test_off_runs_nothing(self):
        assert parse_every("off") is None and parse_every("OFF") is None

    @pytest.mark.parametrize("bad", ["", "1", "h", "0h", "1.5h", "1y", "-1d", "1 hour"])
    def test_anything_else_is_refused_with_the_accepted_forms(self, bad):
        with pytest.raises(ValueError, match="30m, 6h, 1d, 2w"):
            parse_every(bad)


class TestWhenDue:
    def test_a_job_that_never_ran_is_due_now(self):
        assert next_due(None, timedelta(days=1)) is None

    def test_after_a_good_run_one_cadence_later(self):
        assert next_due((T, True), timedelta(days=1)) == T + timedelta(days=1)

    def test_a_failed_weekly_refit_is_retried_within_the_hour(self):
        assert next_due((T, False), timedelta(weeks=1)) == T + timedelta(hours=1)

    def test_an_interrupted_run_counts_as_failed(self):
        """finished_at and ok stay NULL when the container stopped mid-run."""
        assert next_due((T, None), timedelta(weeks=1)) == T + timedelta(hours=1)

    def test_a_short_cadence_is_not_slowed_by_the_retry_rule(self):
        assert next_due((T, False), timedelta(minutes=30)) == T + timedelta(minutes=30)


def _modules(cmds):
    return [c[c.index("-m") + 1] for c in cmds]


class TestWhatEachJobRuns:
    def test_read_is_the_catalogue_then_the_ratings(self):
        cmds = commands("read", 54)
        assert _modules(cmds) == ["bookrs.ingestion.koha_db", "bookrs.ingestion.direct"]
        assert all(c[-2:] == ["--source-id", "54"] for c in cmds)

    def test_embed(self):
        assert _modules(commands("embed", 54)) == ["bookrs.embedding.cli"]

    def test_refit_uses_the_settings_the_sweep_validated(self):
        (cmd,) = commands("refit", 54)
        assert " ".join(cmd).endswith(
            "bookrs.recommend.cli --source-id 54 --signal ratings --factors 128 --iterations 10")

    def test_each_runs_with_this_interpreter(self):
        assert commands("embed", 1)[0][0] == sys.executable

    def test_an_unknown_job_is_refused(self):
        with pytest.raises(ValueError, match="read, embed, refit"):
            commands("harvest", 54)


class TestCommandLine:
    def test_a_job_needs_a_cadence(self, monkeypatch):
        monkeypatch.delenv("BOOKRS_REFIT_EVERY", raising=False)
        with pytest.raises(SystemExit):
            schedule.main(["refit", "--source-id", "54"])

    def test_a_bad_cadence_is_refused_before_anything_runs(self, monkeypatch):
        monkeypatch.setenv("BOOKRS_REFIT_EVERY", "fortnightly")
        with pytest.raises(SystemExit):
            schedule.main(["refit", "--source-id", "54"])

    def test_a_job_needs_the_librarys_source(self, monkeypatch):
        monkeypatch.delenv("BOOKRS_SOURCE_ID", raising=False)
        with pytest.raises(SystemExit):
            schedule.main(["read", "--every", "1h"])


# --- against a database ----------------------------------------------------

@pytest.fixture
def connect():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    from bookrs.db.migrations import apply_migrations
    with conn:
        apply_migrations(conn)
        conn.execute("TRUNCATE job_runs")
    return lambda: psycopg.connect(TEST_DATABASE_URL)


def _rows(connect):
    with connect() as conn:
        return conn.execute(
            "SELECT job, source_id, every, finished_at IS NOT NULL, ok, detail "
            "FROM job_runs ORDER BY id").fetchall()


class _Runner:
    """Stands in for subprocess.run: records each command, answers with
    the exit codes given, or raises what it is told to."""

    def __init__(self, *codes, raises=None):
        self.codes, self.raises, self.calls = list(codes), raises, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if self.raises:
            raise self.raises
        return SimpleNamespace(returncode=self.codes.pop(0) if self.codes else 0)


class TestRuns:
    def test_a_run_is_recorded_with_its_outcome(self, connect):
        assert schedule.run_once(connect, "refit", 54, "1d",
                                 commands("refit", 54), runner=_Runner(0))
        ((job, sid, every, finished, ok, detail),) = _rows(connect)
        assert (job, sid, every, finished, ok) == ("refit", 54, "1d", True, True)
        assert detail.startswith("done in ")

    def test_a_failing_command_stops_the_run(self, connect):
        """A refit should not follow a read that broke half way, and the
        second half of the read should not run on its own."""
        runner = _Runner(1)
        assert not schedule.run_once(connect, "read", 54, "1h",
                                     commands("read", 54), runner=runner)
        assert _modules(runner.calls) == ["bookrs.ingestion.koha_db"]
        assert _rows(connect)[0][4:] == (False, "bookrs.ingestion.koha_db exited with 1")

    def test_a_command_that_cannot_start_is_a_failure_not_a_crash(self, connect):
        runner = _Runner(raises=FileNotFoundError("python"))
        assert not schedule.run_once(connect, "embed", 54, "1h",
                                     commands("embed", 54), runner=runner)
        assert _rows(connect)[0][4] is False

    def test_a_stop_is_recorded_and_still_stops(self, connect):
        """docker compose stop during a run: the run is marked as
        interrupted, and the scheduler exits rather than carrying on."""
        with pytest.raises(SystemExit):
            schedule.run_once(connect, "refit", 54, "1d", commands("refit", 54),
                              runner=_Runner(raises=SystemExit(0)))
        finished, ok, detail = _rows(connect)[0][3:]
        assert finished and ok is False and detail.startswith("interrupted: SystemExit")

    def test_a_real_child_process_is_run_and_its_exit_code_read(self, connect):
        assert not schedule.run_once(
            connect, "read", 54, "1h", [[sys.executable, "-c", "raise SystemExit(3)"]],
            runner=subprocess.run)
        assert _rows(connect)[0][5].endswith("exited with 3")


class TestTick:
    def test_a_due_job_runs_then_waits_one_cadence(self, connect):
        runner = _Runner()
        assert schedule.tick(connect, "refit", 54, "1h", commands("refit", 54),
                             runner=runner) == 0
        assert len(runner.calls) == 1
        wait = schedule.tick(connect, "refit", 54, "1h", commands("refit", 54), runner=runner)
        assert 3_500 < wait <= 3_600 and len(runner.calls) == 1

    def test_a_restart_does_not_run_a_job_again(self, connect):
        with connect() as conn:
            conn.execute("INSERT INTO job_runs (job, source_id, every, started_at, "
                         "finished_at, ok) VALUES ('refit', 54, '1d', now() - interval "
                         "'2 hours', now() - interval '2 hours', true)")
        runner = _Runner()
        wait = schedule.tick(connect, "refit", 54, "1d", commands("refit", 54), runner=runner)
        assert runner.calls == [] and 21 * 3_600 < wait <= 22 * 3_600

    def test_a_shorter_cadence_applies_from_the_last_run(self, connect):
        with connect() as conn:
            conn.execute("INSERT INTO job_runs (job, source_id, every, started_at, "
                         "finished_at, ok) VALUES ('refit', 54, '1w', now() - interval "
                         "'2 days', now() - interval '2 days', true)")
        runner = _Runner()
        schedule.tick(connect, "refit", 54, "1d", commands("refit", 54), runner=runner)
        assert len(runner.calls) == 1

    def test_another_sources_runs_do_not_count(self, connect):
        with connect() as conn:
            conn.execute("INSERT INTO job_runs (job, source_id, every, finished_at, ok) "
                         "VALUES ('refit', 9, '1d', now(), true)")
        runner = _Runner()
        schedule.tick(connect, "refit", 54, "1d", commands("refit", 54), runner=runner)
        assert len(runner.calls) == 1

    def test_off_never_runs(self, connect):
        runner = _Runner()
        assert schedule.tick(connect, "refit", 54, "off", commands("refit", 54),
                             runner=runner) == float("inf")
        assert runner.calls == []


class TestHealth:
    def test_health_shows_each_jobs_last_run_and_when_it_is_next_due(self, connect, monkeypatch):
        pytest.importorskip("fastapi")
        pytest.importorskip("psycopg_pool")
        from fastapi.testclient import TestClient

        with connect() as conn:
            conn.execute("INSERT INTO job_runs (job, source_id, every, started_at, "
                         "finished_at, ok, detail) VALUES ('refit', 54, '1d', "
                         "'2026-11-26 03:00+00', '2026-11-26 03:01+00', true, 'done in 42s')")
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        from bookrs.api import main
        with TestClient(main.app) as c:
            jobs = c.get("/health").json()["jobs"]
        (refit,) = [j for j in jobs if j["job"] == "refit"]
        assert refit["ok"] is True and refit["detail"] == "done in 42s"
        assert refit["every"] == "1d" and refit["source_id"] == 54
        assert datetime.fromisoformat(refit["next_due"]) == T + timedelta(days=1)
