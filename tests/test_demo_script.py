"""tools/demo.sh, run against stand-ins for docker and curl.

The script is used live, in front of the supervisor: what it prints, and
what it asks Koha's database, are checked here rather than discovered
there. The stand-ins answer as Koha, its database, the platform's API
and docker compose do.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
user = args[args.index("-u") + 1] if "-u" in args else None
fake = os.environ["FAKE"]
open(os.path.join(fake, "curl.log"), "a").write(" ".join(args) + "\n")
def answer(body, code):
    if "-o" in args:                      # only the status asked for
        sys.stdout.write(str(code))
    else:
        sys.stdout.write(body + ("\n%d" % code if "-w" in args else ""))
if url.endswith("/opac-main.pl"):
    answer("", 200)
elif "/contrib/bookrs/" in url and user is None:
    answer('{"error":"Log in"}', 401)
elif "/contrib/bookrs/" in url and not user.endswith(":Bookrs-Demo-2026"):
    answer('{"error":"Bad"}', 401)
elif "/contrib/bookrs/recommendations" in url:
    answer(open(os.path.join(fake, "list.json")).read(), 200)
elif "/contrib/bookrs/search" in url:
    answer(open(os.path.join(fake, "search.json")).read(), 200)
elif url.endswith("/health"):
    answer(open(os.path.join(fake, "health.json")).read(), 200)
elif url.endswith("/sources"):
    answer(open(os.path.join(fake, "sources.json")).read(), 200)
else:
    answer("", 404)
'''

DOCKER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
fake = os.environ["FAKE"]
if args[0] == "exec" and "mariadb" in args:
    sql = args[len(args) - args[::-1].index("-e")]      # the last -e: mariadb's
    open(os.path.join(fake, "sql.log"), "a").write(sql + "\n--\n")
    if "FROM biblio)" in sql:
        print("2000\t446666\t16283050")
    elif "FROM borrowers b WHERE userid = 'demo2'" in sql:
        print("447665\tD2\tDemo Two\tS\t2")
    elif "FROM borrowers b WHERE userid" in sql:
        pass
    elif "WHERE r.borrowernumber = 447665" in sql:
        print("2026-11-27 01:02:03\t5\tThe Hobbit")
        print("2026-11-27 01:01:00\t3\tEragon")
    elif "LIKE" in sql:
        print("42\t81234\tThe Hunger Games\tSuzanne Collins")
        print("77\t0\tThe Hunger Games (Special Edition)\t")
elif args[:2] == ["compose", "--profile"] and "ps" in args:
    print("api\ndb\n" + os.environ.get("FAKE_RUNNING", "schedule-read\nschedule-embed\nschedule-refit"))
elif args[:2] == ["compose", "exec"]:
    print("ran: " + " ".join(args[2:]))
else:
    sys.exit("unexpected docker " + " ".join(args))
'''


def _result(bn, title):
    return {"biblionumber": str(bn), "title": title, "authors": ["A. Writer"]}


# The development database holds older sources beside the library's own
# (54): totals are not the library's numbers.
SOURCES = {"sources": [
    {"id": 1, "name": "old", "works": 125332, "embedded": 125332, "factorised": 0},
    {"id": 54, "name": "kohadev-db", "works": 2000, "embedded": 1998, "factorised": 1990},
]}

HEALTH = {
    "status": "ok", "works": 127332, "items": 2000, "embeddings": 127330, "unembedded": 2,
    "vectors_held": 127330, "vectors_reloading": False, "factorised_works": 1990,
    "last_factorised": "2026-11-27T00:59:40", "last_harvest": None,
    "jobs": [{"job": "read", "ok": True, "next_due": "01:08"},
             {"job": "embed", "ok": True, "next_due": "01:59"},
             {"job": "refit", "ok": True, "next_due": "tomorrow"}],
}


@pytest.fixture
def demo(tmp_path):
    fake = tmp_path / "fake"
    bin_ = fake / "bin"
    bin_.mkdir(parents=True)
    for name, body in (("curl", CURL), ("docker", DOCKER)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    (fake / "health.json").write_text(json.dumps(HEALTH))
    (fake / "sources.json").write_text(json.dumps(SOURCES))
    project = tmp_path / "project"
    (project / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "tools" / "demo.sh", project / "tools" / "demo.sh")
    (project / ".env").write_text(textwrap.dedent("""\
        BOOKRS_SOURCE_ID="54"
        KOHA_DB_URL=mysql://root:password@kohadev-db-1:3306/koha_kohadev
        BOOKRS_READ_EVERY=10m
        """))

    def run(*args, **env):
        full_env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE": str(fake),
                    "BOOKRS_CACHE_PROBE_SECONDS": "0", "LC_ALL": "C", **env}
        for key in ("KOHA_DB_URL", "DEMO_PASSWORD", "KOHA_OPAC", "BOOKRS_API"):
            if key not in env:
                full_env.pop(key, None)
        return subprocess.run(["bash", str(project / "tools" / "demo.sh"), *args],
                              capture_output=True, text=True, env=full_env, timeout=60)

    run.fake = fake
    run.project = project
    return run


class TestCheck:
    def test_ready(self, demo):
        r = demo("check")
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout
        assert "OPAC" in out and "answers" in out
        assert "installed (asks a logged-out visitor to log in)" in out
        assert "16283050" in out                      # ratings, from Koha
        line = next(ln for ln in out.splitlines() if "books read from Koha" in ln)
        assert "(source 54)" in line and line.endswith(" 2,000")    # the library's own row
        assert "1,998 (2 waiting)" in out
        assert "1,990, last refit" in out
        assert "127,330 of 127,330 embedded, all sources" in out
        assert "read 10m, embed 1h, refit 1d" in out  # .env, then compose's defaults
        assert out.rstrip().endswith("Ready.")

    def test_a_failed_job_or_a_stopped_scheduler_is_not_ready(self, demo):
        health = dict(HEALTH, jobs=[{"job": "refit", "ok": False, "next_due": "01:30"}])
        (demo.fake / "health.json").write_text(json.dumps(health))
        r = demo("check", FAKE_RUNNING="schedule-read")
        assert r.returncode == 1
        assert "job refit" in r.stdout and "FAILED" in r.stdout
        assert "1 of 3 running" in r.stdout
        assert "Not ready" in r.stdout

    def test_a_source_the_platform_does_not_have(self, demo):
        (demo.fake / "sources.json").write_text(json.dumps({"sources": []}))
        r = demo("check")
        assert r.returncode == 1
        assert "check BOOKRS_SOURCE_ID" in r.stdout

    def test_vectors_still_loading(self, demo):
        (demo.fake / "health.json").write_text(json.dumps(dict(HEALTH, vectors_held=None)))
        assert "not loaded yet" in demo("check").stdout


class TestList:
    def test_the_first_list_then_what_changed(self, demo):
        first = {"method": "few", "explanation": "placed from the rated works only",
                 "ratings_used": 3, "results": [_result(1, "Eragon"), _result(2, "Inkheart")]}
        (demo.fake / "list.json").write_text(json.dumps(first))
        r = demo("list", "demo2")
        assert r.returncode == 0, r.stderr
        assert "demo2: Recommended for you" in r.stdout
        assert "holds 3 of their ratings" in r.stdout
        assert "+" not in r.stdout and "left the list" not in r.stdout

        second = dict(first, method="full", ratings_used=5,
                      results=[_result(2, "Inkheart"), _result(3, "Fahrenheit 451")])
        (demo.fake / "list.json").write_text(json.dumps(second))
        # A list a second later may share a name: make the first one older.
        saved = sorted((demo.project / "data" / "demo").glob("demo2-*.json"))
        saved[0].rename(saved[0].with_name("demo2-20260101-000000.json"))
        r = demo("list", "demo2")
        assert "+ new since the list of 00:00:00" in r.stdout
        assert "  1.   Inkheart" in r.stdout
        assert "  2. + Fahrenheit 451" in r.stdout
        assert "left the list: Eragon" in r.stdout

    def test_no_ratings_is_the_popular_list(self, demo):
        (demo.fake / "list.json").write_text(json.dumps(
            {"method": "none", "explanation": "most-rated works (no ratings yet)",
             "ratings_used": 0, "results": [_result(1, "The Hobbit")]}))
        assert "demo2: Popular in this library" in demo("list", "demo2").stdout

    def test_a_wrong_password_says_so(self, demo):
        r = demo("list", "demo2", "nope")
        assert r.returncode == 1
        assert "refused the login demo2" in r.stderr


class TestFindAndPatron:
    def test_find_quotes_the_words_and_links_the_record(self, demo):
        r = demo("find", "O'Brien")
        assert r.returncode == 0, r.stderr
        assert "LIKE '%O''Brien%'" in (demo.fake / "sql.log").read_text()
        assert "81234 ratings  The Hunger Games / Suzanne Collins" in r.stdout
        assert "opac-detail.pl?biblionumber=42" in r.stdout

    def test_patron_and_their_ratings(self, demo):
        r = demo("patron", "demo2")
        assert r.returncode == 0, r.stderr
        assert "Demo Two (demo2): borrower 447665" in r.stdout
        assert "ratings in Koha: 2" in r.stdout
        assert "*****  The Hobbit" in r.stdout
        assert "***    Eragon" in r.stdout

    def test_an_unknown_patron(self, demo):
        r = demo("patron", "nobody")
        assert r.returncode == 1
        assert "No patron with the username nobody" in r.stdout

    def test_only_reads_koha(self, demo):
        demo("check")
        demo("find", "x")
        demo("patron", "demo2")
        sql = (demo.fake / "sql.log").read_text().upper()
        for verb in ("INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP"):
            assert verb not in sql


class TestSearchAndJobs:
    def test_search_says_whether_taste_reordered_it(self, demo):
        (demo.fake / "search.json").write_text(json.dumps(
            {"query": "a boy wizard at school", "method": "full", "reordered": True,
             "count": 1, "results": [dict(_result(5, "Harry Potter"), score=0.593)]}))
        r = demo("search", "demo2", "a", "boy", "wizard", "at", "school")
        assert r.returncode == 0, r.stderr
        assert '"a boy wizard at school": 1 books, reordered by meaning (0.7)' in r.stdout
        assert "best match 0.593" in r.stdout
        assert "q=a boy wizard at school" in (demo.fake / "curl.log").read_text()

    def test_refit_runs_the_job_then_waits_for_the_api(self, demo):
        r = demo("refit")
        assert r.returncode == 0, r.stderr
        assert "ran: -T schedule-refit python -m bookrs.schedule refit --now" in r.stdout
        assert "next list comes from the new model" in r.stdout

    def test_a_job_without_the_scheduler(self, demo):
        r = demo("read", FAKE_RUNNING="")
        assert r.returncode == 1
        assert "The scheduler is not running" in r.stderr

    def test_no_command_shows_the_commands(self, demo):
        r = demo()
        assert r.returncode == 2
        assert "tools/demo.sh refit" in r.stdout
