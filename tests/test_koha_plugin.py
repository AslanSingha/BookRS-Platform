"""The Koha plugin's API route, against the real BookRS-Platform API.

Runs koha-plugin/t/controller.pl, which stands in for Koha's REST layer
and drives the plugin's real controller. The platform it calls is this
repository's API, started here against the test database, with ratings
stored under the reference Python computes. A patron whose ratings come
back therefore proves the Perl hash and the Python hash agree -- the
one thing that, if wrong, would fail silently: every patron would look
like a stranger and get the most-rated list.

Skipped where Perl's Mojolicious is not installed (it is not in the
test image). It is in Koha's own image; see the plugin section of the
README for running it there.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("uvicorn")

from bookrs.ingestion.circulation import patron_reference

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "koha-plugin" / "t" / "controller.pl"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TOKEN, SECRET, BORROWER = "koha-and-bookrs-only", "the-patron-secret", 4242


def _perl_ready() -> bool:
    if not shutil.which("perl"):
        return False
    return subprocess.run(["perl", "-MMojolicious", "-MModern::Perl", "-e", "1"],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _perl_ready(), reason="Perl Mojolicious not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def results():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with conn:
        conn.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k') "
                           "RETURNING id").fetchone()[0]
        ids = [conn.execute(
            "INSERT INTO works (source_id, source_record_id, title, authors, content_hash, "
            "items_hash) VALUES (%s, %s, %s, %s, 'c', 'i') RETURNING id",
            (sid, str(500 + j), f"Book {j}", [f"Author {j}"])).fetchone()[0]
            for j in range(10)]
        for j, w in enumerate(ids):
            v = [1.0, 0.05 * j] if j < 6 else [0.05 * j, 1.0]
            conn.execute(
                "INSERT INTO work_factors (work_id, vector, raw_vector, dimensions, "
                "factoriser_version, n_patrons) VALUES (%s, %s, %s, 2, 2, %s)",
                (w, v, v, 100 - j))
        ref = patron_reference(BORROWER, SECRET)
        for w in ids[6:]:          # this patron reads group B only
            conn.execute("INSERT INTO ratings (source_id, patron_ref, work_id, rating) "
                         "VALUES (%s, %s, %s, 5)", (sid, ref, w))
        conn.commit()

    port = _free_port()
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "BOOKRS_SERVICE_TOKEN": TOKEN}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bookrs.api.main:app", "--port", str(port),
         "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        out = subprocess.run(
            ["perl", str(SCRIPT)], capture_output=True, text=True, timeout=120,
            env={**os.environ, "BOOKRS_TEST_API": f"http://127.0.0.1:{port}/",
                 "BOOKRS_TEST_TOKEN": TOKEN, "BOOKRS_TEST_SECRET": SECRET,
                 "BOOKRS_TEST_SOURCE": str(sid), "BOOKRS_TEST_BORROWER": str(BORROWER)})
    finally:
        server.terminate()
        server.wait(10)
    assert out.returncode == 0, out.stderr
    return {r["name"]: r for r in map(json.loads, out.stdout.splitlines())}, ids


def test_nobody_logged_in_is_refused(results):
    r = results[0]["anonymous"]
    assert r["status"] == 401 and "Log in" in r["body"]["error"]


def test_a_patron_gets_their_own_recommendations(results):
    r, ids = results[0]["patron"], results[1]
    assert r["status"] == 200, r
    assert r["body"]["method"] == "few" and r["body"]["ratings_used"] == 4
    got = [w["id"] for w in r["body"]["results"]]
    assert not set(got) & set(ids[6:])
    assert r["cache_control"] == "no-store, private"


def test_the_perl_and_python_hashes_agree(results):
    """A wrong hash would make the patron a stranger: method 'none'."""
    assert results[0]["patron"]["body"]["method"] != "none"
    assert results[0]["routes"]["reference"] == patron_reference(1234, "s3cret")


def test_limit_is_passed_and_clamped(results):
    assert len(results[0]["limit"]["body"]["results"]) == 2
    assert results[0]["limit_out_of_range"]["status"] == 200
    assert len(results[0]["limit_out_of_range"]["body"]["results"]) == 6


def test_a_patron_with_no_ratings_gets_the_most_rated_list(results):
    r = results[0]["stranger"]
    assert r["status"] == 200 and r["body"]["method"] == "none"


def test_a_different_secret_finds_nobody(results):
    assert results[0]["wrong_secret"]["body"]["method"] == "none"


def test_not_configured_is_503(results):
    r = results[0]["unconfigured"]
    assert r["status"] == 503 and "not configured" in r["body"]["error"]


def test_a_wrong_token_is_a_502_that_reveals_nothing(results):
    r = results[0]["wrong_token"]
    assert r["status"] == 502 and "token" not in r["body"]["error"].lower()


def test_an_unreachable_platform_is_a_502(results):
    assert results[0]["platform_down"]["status"] == 502


def test_the_route_is_registered_where_koha_will_look(results):
    r = results[0]["routes"]
    assert r["namespace"] == "bookrs" and r["resolves"] == 1


def test_the_page_is_told_about_the_patron_route_only_when_it_can_answer(results):
    on = results[0]["opac_js_configured"]["attrs"]
    off = results[0]["opac_js_not_personal"]["attrs"]
    assert on["data-patron-route"] == "/api/v1/contrib/bookrs/recommendations"
    assert "data-patron-route" not in off


def test_the_page_links_are_passed_through(results):
    on = results[0]["opac_js_configured"]["attrs"]
    assert on["data-for-you-url"] == "/cgi-bin/koha/opac-page.pl?page_id=1"
    assert on["data-semantic-url"] == "/cgi-bin/koha/opac-page.pl?page_id=2"
    assert on["src"] == "http://bookrs.test/widget.js"


def test_no_secret_ever_reaches_the_page(results):
    on = results[0]["opac_js_configured"]["attrs"]
    values = " ".join(on.values())
    assert TOKEN not in values and SECRET not in values
