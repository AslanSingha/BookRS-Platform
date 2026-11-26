"""Run one of the platform's jobs on a schedule the library sets.

    python -m bookrs.schedule read  --every 1h --source-id 54
    python -m bookrs.schedule embed --every 1h --source-id 54
    python -m bookrs.schedule refit --every 1d --source-id 54

Three jobs keep the platform in step with the library:

* ``read``  -- the catalogue (``bookrs.ingestion.koha_db``), then the
  ratings and review flags changed since the last read
  (``bookrs.ingestion.direct --changes``), with a full read once
  ``BOOKRS_FULL_READ_EVERY`` has passed (default ``1d``) to catch what
  was removed. Read-only, as every read of the library is.
* ``embed`` -- vectors for books that are new or changed
  (``bookrs.embedding.cli``); nothing to do when nothing changed.
* ``refit`` -- the ALS factors (``bookrs.recommend.cli``), by default
  with the settings the thesis used and the sweep validated: ratings,
  k = 128, 10 iterations.

A cadence is a whole number and a unit -- ``30m``, ``6h``, ``1d``,
``2w`` -- or ``off``. ``--every`` may be left out when
``BOOKRS_<JOB>_EVERY`` is set (``BOOKRS_REFIT_EVERY=1d``), and
``--source-id`` when ``BOOKRS_SOURCE_ID`` is.

**When a job runs.** Every run is recorded in ``job_runs`` (migration 6).
A job is next due ``every`` after its last run started; after a run that
failed or was interrupted, one hour after it at most, so a weekly refit
that failed is retried the same day. A job that never ran is due at
once. Because the record lives in the database, a container that
restarts does not run everything again, and a changed cadence applies
from the last run.

**What a run is.** Each command runs as a child process, exactly as an
operator would type it, with that module's own logging; its output is
this container's log. A run fails at the first command that exits
non-zero, and the next command is not started: a refit should not follow
a read that broke half way.

**Why there is a scheduler at all.** Until 26 November 2026 the platform
shipped none: the schedule belonged to the host's cron (README). The
supervisor asked for a retrain cadence that can be set in hours, days or
weeks, and cron does not run by default where this is developed (WSL).
What argued for cron is kept: each job is one plain process in its own
container, with no daemon installed in any image; failures show in
``docker compose logs`` and in ``/health``; and nothing runs unless the
library starts the ``schedule`` profile. Host cron remains a fine
choice, and the README shows both.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

log = logging.getLogger("bookrs.schedule")

JOBS = ("read", "embed", "refit")

_UNITS = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}

# How soon a failed or interrupted run is tried again, unless the job's
# own cadence is sooner. A week is too long to wait to learn that a
# refit still fails; a minute would hammer a library's database that is
# down for maintenance.
RETRY_AFTER_FAILURE = timedelta(hours=1)


def parse_every(text: str) -> timedelta | None:
    """'30m', '6h', '1d', '2w' as a timedelta; 'off' as None."""
    t = (text or "").strip().lower()
    if t == "off":
        return None
    m = re.fullmatch(r"(\d+)\s*([mhdw])", t)
    if not m or int(m.group(1)) == 0:
        raise ValueError(
            f"cadence {text!r}: expected a whole number and m, h, d or w "
            "(30m, 6h, 1d, 2w), or off")
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


def commands(job: str, source_id: int, *, signal_: str = "ratings",
             factors: int = 128, iterations: int = 10,
             full_every: str = "1d") -> list[list[str]]:
    """The commands one run of a job consists of, in order."""
    py, sid = sys.executable, str(source_id)
    if job == "read":
        return [[py, "-m", "bookrs.ingestion.koha_db", "--source-id", sid],
                [py, "-m", "bookrs.ingestion.direct", "--source-id", sid,
                 "--changes", "--full-every", full_every]]
    if job == "embed":
        return [[py, "-m", "bookrs.embedding.cli", "--source-id", sid]]
    if job == "refit":
        return [[py, "-m", "bookrs.recommend.cli", "--source-id", sid,
                 "--signal", signal_, "--factors", str(factors),
                 "--iterations", str(iterations)]]
    raise ValueError(f"unknown job {job!r}; expected one of {', '.join(JOBS)}")


def last_run(conn, job: str, source_id: int | None):
    """(started_at, ok) of the job's most recent run, or None."""
    return conn.execute(
        "SELECT started_at, ok FROM job_runs "
        "WHERE job = %s AND source_id IS NOT DISTINCT FROM %s "
        "ORDER BY started_at DESC LIMIT 1",
        (job, source_id)).fetchone()


def next_due(last, every: timedelta) -> datetime | None:
    """When a job is next due after ``last`` = (started_at, ok).

    None means now: the job has never run. ``ok`` is NULL for a run that
    never finished, which counts as a failure.
    """
    if last is None:
        return None
    started, ok = last
    return started + (every if ok else min(every, RETRY_AFTER_FAILURE))


def _module(cmd: list[str]) -> str:
    return cmd[cmd.index("-m") + 1] if "-m" in cmd else cmd[0]


def run_once(connect: Callable, job: str, source_id: int | None, every: str,
             cmds: list[list[str]], runner: Callable = subprocess.run) -> bool:
    """Run the commands once, recording the run. True if all succeeded.

    The database connection is not held open while the commands run: a
    read of sixteen million ratings takes minutes, and an idle connection
    held that long is one the library's administrators did not plan for.
    """
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO job_runs (job, source_id, every) VALUES (%s, %s, %s) "
            "RETURNING id", (job, source_id, every)).fetchone()[0]
    started = time.monotonic()
    ok, detail = True, None
    try:
        for cmd in cmds:
            log.info("%s: running %s", job, _module(cmd))
            code = runner(cmd).returncode
            if code != 0:
                ok, detail = False, f"{_module(cmd)} exited with {code}"
                break
    except BaseException as exc:        # stopped, or the command not found
        ok, detail = False, f"interrupted: {type(exc).__name__}: {exc}"[:500]
        _finish(connect, run_id, ok, detail)
        if isinstance(exc, Exception):
            log.error("%s: %s", job, detail)
            return False
        raise
    took = time.monotonic() - started
    detail = detail or f"done in {took:.0f}s"
    _finish(connect, run_id, ok, detail)
    (log.info if ok else log.error)("%s: %s", job, detail)
    return ok


def _finish(connect: Callable, run_id: int, ok: bool, detail: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE job_runs SET finished_at = now(), ok = %s, detail = %s "
            "WHERE id = %s", (ok, detail, run_id))


def tick(connect: Callable, job: str, source_id: int | None, every_text: str,
         cmds: list[list[str]], *, now: Callable = None,
         runner: Callable = subprocess.run) -> float:
    """Run the job if it is due. Returns how long to sleep before the next
    look: 0 after a run, so the next due time is read from the record."""
    every = parse_every(every_text)
    if every is None:
        return float("inf")
    now = now or (lambda: datetime.now(timezone.utc))
    with connect() as conn:
        due = next_due(last_run(conn, job, source_id), every)
    wait = 0.0 if due is None else (due - now()).total_seconds()
    if wait > 0:
        log.info("%s: next run at %s (every %s)", job,
                 due.astimezone().strftime("%Y-%m-%d %H:%M"), every_text)
        return wait
    run_once(connect, job, source_id, every_text, cmds, runner=runner)
    return 0.0


def serve(connect: Callable, job: str, source_id: int | None, every_text: str,
          cmds: list[list[str]], sleep: Callable = time.sleep) -> None:
    """Run the job on its cadence until the container stops."""
    if parse_every(every_text) is None:
        # Stay up rather than exit: under `restart: unless-stopped` an
        # exiting container is started again at once, for ever.
        log.info("%s: off; nothing will run until a cadence is set", job)
        while True:
            sleep(86_400)
    log.info("%s: every %s for source %s", job, every_text, source_id)
    while True:
        wait = tick(connect, job, source_id, every_text, cmds)
        if wait > 0:
            # Woken at least hourly to look again: the record may have
            # changed (a run by hand, a cadence changed and restarted).
            sleep(min(wait, 3_600))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bookrs.schedule", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", choices=JOBS)
    ap.add_argument("--every", default=None,
                    help="30m, 6h, 1d, 2w or off. Default: BOOKRS_<JOB>_EVERY.")
    ap.add_argument("--source-id", type=int, default=None,
                    help="The library's source. Default: BOOKRS_SOURCE_ID.")
    ap.add_argument("--signal", choices=("ratings", "loans"), default="ratings",
                    help="refit only: what the factors are learned from.")
    ap.add_argument("--factors", type=int, default=128, help="refit only.")
    ap.add_argument("--iterations", type=int, default=10, help="refit only.")
    ap.add_argument("--full-every", default=None,
                    help="read only: read everything, not just changes, once "
                         "this has passed since the last full read (30m, 6h, "
                         "1d, 2w or off). Default: BOOKRS_FULL_READ_EVERY, else 1d.")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    every = a.every or os.environ.get(f"BOOKRS_{a.job.upper()}_EVERY", "")
    if not every:
        ap.error(f"--every or BOOKRS_{a.job.upper()}_EVERY must be set")
    try:
        parse_every(every)
    except ValueError as exc:
        ap.error(str(exc))
    full_every = a.full_every or os.environ.get("BOOKRS_FULL_READ_EVERY") or "1d"
    try:
        parse_every(full_every)
    except ValueError as exc:
        ap.error(f"--full-every: {exc}")
    source_id = a.source_id
    if source_id is None and os.environ.get("BOOKRS_SOURCE_ID"):
        source_id = int(os.environ["BOOKRS_SOURCE_ID"])
    if source_id is None:
        ap.error("--source-id or BOOKRS_SOURCE_ID must be set")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    import psycopg

    from bookrs.db.migrations import apply_migrations

    def connect():
        return psycopg.connect(dsn)

    with connect() as conn:
        apply_migrations(conn)

    # As PID 1 in a container a process ignores SIGTERM unless it handles
    # it, and `docker compose stop` would wait ten seconds to kill it. A
    # run in progress is recorded as interrupted on the way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    serve(connect, a.job, source_id, every,
          commands(a.job, source_id, signal_=a.signal, factors=a.factors,
                   iterations=a.iterations, full_every=full_every))
    return 0


if __name__ == "__main__":
    sys.exit(main())
