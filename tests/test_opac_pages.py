"""The OPAC pages a patron sees, through the real widget.js.

Each test builds a Koha page in opac_harness.js, answers the widget's
requests, and checks what was rendered and what was asked. The rules
under test are about honesty and privacy as much as layout:

* the patron's list is asked of Koha, same origin, with the session --
  never of the BookRS API, and never carrying an identifier;
* "Recommended for you" is said only over a list made from the
  patron's own ratings, and the note says how many;
* a visitor is invited to log in only when logging in would help.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "opac_harness.js"
ROUTE = "/api/v1/contrib/bookrs/recommendations"
API = "http://bookrs.test"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def run(path="/", search="", page=(), routes=None, **attributes):
    attrs = {"data-api": API, "data-source-id": "54",
             **{"data-" + k.replace("_", "-"): v for k, v in attributes.items()}}
    payload = json.dumps({"path": path, "search": search, "page": list(page),
                          "routes": routes or {}, "attributes": attrs})
    out = subprocess.run(["node", str(HARNESS), payload], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def books(*titles, score=None):
    out = [{"title": t, "authors": [f"Author of {t}"], "biblionumber": str(50000 + i),
            "availability": {"total": 1, "available": 1, "is_available": True}}
           for i, t in enumerate(titles)]
    if score is not None:
        for i, b in enumerate(out):
            b["score"] = round(score - 0.01 * i, 4)
    return out


def personal(method="full", n=40, titles=("Mine A", "Mine B")):
    return {"status": 200, "body": {"method": method, "ratings_used": n,
                                    "explanation": "...", "results": books(*titles)}}


POPULAR = {f"{API}/works/popular": {"status": 200, "body": {"results": books("Pop A", "Pop B")}}}
MAIN_COLUMN = [{"tag": "div", "class": "maincontent", "name": "main"},
               {"tag": "p", "text": "Library news", "parent": "main", "id": "news"}]


def find(node, cls=None, tag=None, id=None):
    out = []
    if ((cls is None or cls in node.get("class", "").split()) and
            (tag is None or node["tag"] == tag) and (id is None or node.get("id") == id)):
        out.append(node)
    for c in node.get("children", []):
        out += find(c, cls, tag, id)
    return out


def text(node):
    return node.get("text", "") + "".join(text(c) for c in node.get("children", []))


def titles(node):
    return [text(a) for a in find(node, cls="bookrs-title")]


class TestHomepage:
    def test_a_logged_in_patron_sees_their_list_at_the_top(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE,
                  routes={ROUTE: personal(), **POPULAR})
        main = find(out["body"], cls="maincontent")[0]
        assert main["children"][0]["class"] == "bookrs-panel"      # above the news
        panel = main["children"][0]
        assert text(find(panel, cls="bookrs-heading")[0]) == "Recommended for you"
        assert text(find(panel, cls="bookrs-note")[0]) == "Based on your 40 ratings."
        assert titles(panel) == ["Mine A", "Mine B"]

    def test_the_patrons_list_is_asked_of_koha_with_the_session_only(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE, routes={ROUTE: personal(), **POPULAR})
        asked = [r for r in out["requests"] if ROUTE in r["url"]]
        assert asked and all(r["url"].startswith(ROUTE) for r in asked)   # same origin
        assert asked[0]["credentials"] == "same-origin"
        assert not any("popular" in r["url"] for r in out["requests"])
        bookrs = [r for r in out["requests"] if r["url"].startswith(API)]
        assert all(r["credentials"] == "omit" for r in bookrs)

    def test_a_visitor_sees_the_librarys_popular_books(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE,
                  routes={ROUTE: {"status": 401, "body": {"error": "Log in"}}, **POPULAR})
        panel = find(out["body"], cls="bookrs-panel")[0]
        assert text(find(panel, cls="bookrs-heading")[0]) == "Popular in this library"
        assert titles(panel) == ["Pop A", "Pop B"]
        assert any(r["url"] == f"{API}/works/popular?limit=6&source_id=54" for r in out["requests"])

    def test_without_the_patron_route_koha_is_never_asked(self):
        out = run(page=MAIN_COLUMN, routes=POPULAR)
        assert not any(ROUTE in r["url"] for r in out["requests"])
        assert text(find(out["body"], cls="bookrs-heading")[0]) == "Popular in this library"

    def test_a_failing_patron_route_falls_back_to_popular(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE,
                  routes={ROUTE: {"status": 502, "body": {}}, **POPULAR})
        assert text(find(out["body"], cls="bookrs-heading")[0]) == "Popular in this library"

    def test_a_placeholder_decides_where_the_panel_goes(self):
        page = MAIN_COLUMN + [{"tag": "div", "id": "bookrs-home", "text": "Loading...",
                               "parent": "main"}]
        out = run(page=page, patron_route=ROUTE, routes={ROUTE: personal(), **POPULAR})
        holder = find(out["body"], id="bookrs-home")[0]
        assert not find(holder, tag="#text") and holder["children"][0]["class"] == "bookrs-panel"
        assert find(out["body"], cls="maincontent")[0]["children"][0]["id"] == "news"

    def test_the_for_you_page_is_linked_from_a_personal_panel_only(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE, for_you_url="/cgi-bin/koha/opac-page.pl?page_id=1",
                  routes={ROUTE: personal(), **POPULAR})
        assert find(out["body"], cls="bookrs-more")[0]["href"] == "/cgi-bin/koha/opac-page.pl?page_id=1"
        out = run(page=MAIN_COLUMN, patron_route=ROUTE, for_you_url="/x",
                  routes={ROUTE: {"status": 401, "body": {}}, **POPULAR})
        assert not find(out["body"], cls="bookrs-more")

    def test_opac_main_pl_is_the_homepage_too(self):
        out = run(path="/cgi-bin/koha/opac-main.pl", page=MAIN_COLUMN, routes=POPULAR)
        assert find(out["body"], cls="bookrs-panel")

    def test_nothing_is_shown_when_the_service_is_down(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE,
                  routes={ROUTE: {"network_error": True}, f"{API}/works/popular": {"network_error": True}})
        assert not find(out["body"], cls="bookrs-panel")


class TestHomepageUnrated:
    def test_an_unrated_patron_sees_popular_books_said_as_such(self):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE, for_you_url="/x",
                  routes={ROUTE: personal("none", 0), **POPULAR})
        panel = find(out["body"], cls="bookrs-panel")[0]
        assert text(find(panel, cls="bookrs-heading")[0]) == "Popular in this library"
        assert text(find(panel, cls="bookrs-credit")[0]) == "Suggestions from this library's own catalogue"
        assert not find(panel, cls="bookrs-more")


class TestNotes:
    @pytest.mark.parametrize("method,n,expected", [
        ("full", 40, "Based on your 40 ratings."),
        ("few", 1, "Based on your 1 rating so far."),
        ("few", 3, "Based on your 3 ratings so far."),
        ("none", 0, "You have not rated any books yet"),
    ])
    def test_the_note_says_what_the_list_stands_on(self, method, n, expected):
        out = run(page=MAIN_COLUMN, patron_route=ROUTE, routes={ROUTE: personal(method, n), **POPULAR})
        assert text(find(out["body"], cls="bookrs-note")[0]).startswith(expected)


FOR_YOU = [{"tag": "div", "id": "bookrs-for-you", "text": "Loading recommendations..."}]


class TestForYouPage:
    def test_a_patron_sees_up_to_24_of_their_own(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", search="?page_id=1", page=FOR_YOU,
                  patron_route=ROUTE, routes={ROUTE: personal(), **POPULAR})
        holder = find(out["body"], id="bookrs-for-you")[0]
        assert not find(holder, tag="#text")                       # placeholder text replaced
        assert titles(holder) == ["Mine A", "Mine B"]
        assert out["requests"][0]["url"] == ROUTE + "?limit=24"

    def test_the_page_title_is_not_repeated_over_the_patrons_list(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", page=FOR_YOU, patron_route=ROUTE,
                  routes={ROUTE: personal(), **POPULAR})
        holder = find(out["body"], id="bookrs-for-you")[0]
        assert not find(holder, cls="bookrs-heading")
        assert text(find(holder, cls="bookrs-credit")[0]) == "Chosen from your ratings in this library"

    def test_a_patron_with_no_ratings_is_not_told_the_list_is_theirs(self):
        """Logged in, nothing rated: Koha sends the popular list. Neither
        the heading nor the credit may claim it was chosen for them."""
        out = run(path="/cgi-bin/koha/opac-page.pl", page=FOR_YOU, patron_route=ROUTE,
                  routes={ROUTE: personal("none", 0, ("Pop A", "Pop B")), **POPULAR})
        holder = find(out["body"], id="bookrs-for-you")[0]
        assert text(find(holder, cls="bookrs-heading")[0]) == "Popular in this library"
        assert text(find(holder, cls="bookrs-note")[0]).startswith("You have not rated any books yet")
        assert text(find(holder, cls="bookrs-credit")[0]) == "Suggestions from this library's own catalogue"
        assert "Log in" not in text(holder)

    def test_a_visitor_is_invited_to_log_in_and_shown_popular_books(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", page=FOR_YOU, patron_route=ROUTE,
                  routes={ROUTE: {"status": 401, "body": {}}, **POPULAR})
        holder = find(out["body"], id="bookrs-for-you")[0]
        invite = holder["children"][0]
        assert find(invite, tag="a")[0]["href"] == "/cgi-bin/koha/opac-user.pl"
        assert "Log in to see books chosen for you" in text(invite)
        assert titles(holder) == ["Pop A", "Pop B"]

    def test_no_login_invitation_when_logging_in_would_not_help(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", page=FOR_YOU, routes=POPULAR)
        assert "Log in" not in text(find(out["body"], id="bookrs-for-you")[0])

    def test_a_failure_is_said_plainly(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", page=FOR_YOU, patron_route=ROUTE,
                  routes={ROUTE: {"status": 502, "body": {}},
                          f"{API}/works/popular": {"status": 503, "body": {}}})
        assert text(find(out["body"], id="bookrs-for-you")[0]) == \
            "Recommendations are not available right now."


SEMANTIC = [{"tag": "div", "id": "bookrs-semantic-search"}]
SEARCH = {f"{API}/search/semantic": {"status": 200,
                                     "body": {"results": books("Near A", "Near B", "Near C", score=0.8)}}}


class TestSearchByMeaningPage:
    def test_without_a_query_it_offers_the_form_and_asks_nothing(self):
        out = run(path="/cgi-bin/koha/opac-page.pl", search="?page_id=2", page=SEMANTIC)
        holder = find(out["body"], id="bookrs-semantic-search")[0]
        form = find(holder, tag="form")[0]
        assert form["attrs"] == {"method": "get", "action": "/cgi-bin/koha/opac-page.pl"}
        hidden = [c["attrs"] for c in form["children"] if c["attrs"].get("type") == "hidden"]
        assert hidden == [{"type": "hidden", "name": "page_id", "value": "2"}]
        assert find(form, cls="bookrs-search-input")[0]["attrs"]["name"] == "bq"
        assert not out["requests"]

    def test_a_query_is_searched_by_meaning_and_listed(self):
        out = run(path="/cgi-bin/koha/opac-page.pl",
                  search="?page_id=2&bq=a+boy+wizard+at+school", page=SEMANTIC, routes=SEARCH)
        assert out["requests"][0]["url"] == (f"{API}/search/semantic?q=a%20boy%20wizard%20at%20school"
                                             "&limit=24&min_score=0.35&source_id=54")
        holder = find(out["body"], id="bookrs-semantic-search")[0]
        assert titles(holder) == ["Near A", "Near B", "Near C"]
        status = find(holder, cls="bookrs-note")[0]
        assert text(status) == ("The 3 books in this catalogue closest in meaning to "
                                "“a boy wizard at school”, best match first.")
        assert find(holder, cls="bookrs-search-input")[0]["attrs"]["value"] == "a boy wizard at school"
        hidden = [c["attrs"]["name"] for c in find(holder, tag="input") if c["attrs"].get("type") == "hidden"]
        assert hidden == ["page_id"]                               # bq is not duplicated

    def test_a_weak_best_match_is_not_called_close(self):
        """A title the catalogue does not hold still gets a ranked list;
        the page must not call it close."""
        out = run(path="/x", search="?bq=the+art+of+war+by+sun+tzu", page=SEMANTIC,
                  routes={f"{API}/search/semantic": {"status": 200,
                          "body": {"results": books("War and Peace", "The Winds of War", score=0.45)}}})
        status = text(find(out["body"], cls="bookrs-note")[0])
        assert status == ("No book in this catalogue is a close match for "
                          "“the art of war by sun tzu”. These are the nearest it has, "
                          "best match first.")
        assert titles(find(out["body"], id="bookrs-semantic-search")[0]) == ["War and Peace", "The Winds of War"]

    def test_the_bar_for_close_is_the_record_pages_own(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, min_score="0.4",
                  routes={f"{API}/search/semantic": {"status": 200,
                          "body": {"results": books("A", score=0.45)}}})
        assert text(find(out["body"], cls="bookrs-note")[0]).startswith("The 1 book in this catalogue")

    def test_nothing_close_is_said_plainly(self):
        out = run(path="/x", search="?bq=zzzz", page=SEMANTIC,
                  routes={f"{API}/search/semantic": {"status": 200, "body": {"results": []}}})
        assert text(find(out["body"], cls="bookrs-note")[0]) == \
            "Nothing in this catalogue is close in meaning to “zzzz”."

    def test_a_failure_is_said_plainly(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC,
                  routes={f"{API}/search/semantic": {"status": 500, "body": {}}})
        assert text(find(out["body"], cls="bookrs-note")[0]) == \
            "Search by meaning is not available right now."

    def test_the_page_never_asks_for_the_patrons_list(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_route=ROUTE, routes=SEARCH)
        assert not any(ROUTE in r["url"] for r in out["requests"])


SEARCH_ROUTE = "/api/v1/contrib/bookrs/search"


def mine_search(method="full", n=40, titles=("Taste A", "Taste B"), scores=(0.70, 0.80)):
    results = books(*titles)
    for b, sc in zip(results, scores):
        b["score"] = sc
    return {"status": 200, "body": {"query": "q", "method": method, "ratings_used": n,
                                    "count": len(results), "results": results}}


class TestPersonalSearchPage:
    def test_a_patrons_search_is_asked_of_koha_with_the_session(self):
        out = run(path="/x", search="?bq=wizard+school", page=SEMANTIC,
                  patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: mine_search(), **SEARCH})
        first = out["requests"][0]
        assert first["url"] == SEARCH_ROUTE + "?q=wizard%20school&limit=24"
        assert first["credentials"] == "same-origin"
        assert not any("/search/semantic" in r["url"] for r in out["requests"])

    def test_the_patrons_order_is_kept_and_said(self):
        out = run(path="/x", search="?bq=wizard+school", page=SEMANTIC,
                  patron_search_route=SEARCH_ROUTE, routes={SEARCH_ROUTE: mine_search(), **SEARCH})
        holder = find(out["body"], id="bookrs-semantic-search")[0]
        assert titles(holder) == ["Taste A", "Taste B"]
        notes = [text(n) for n in find(holder, cls="bookrs-note")]
        assert notes[0] == "The 2 books in this catalogue closest in meaning to “wizard school”."
        assert notes[1] == ("Ordered by meaning and by your 40 ratings: "
                            "books like the ones you have rated move up.")

    def test_closeness_is_judged_on_the_best_score_not_the_first(self):
        """Taste can put a weaker match first; the page must not then
        call the whole list weak."""
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: mine_search(scores=(0.40, 0.80))})
        assert not text(find(out["body"], cls="bookrs-note")[0]).startswith("No book")

    def test_a_weak_personal_search_does_not_say_best_match_first(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: mine_search(scores=(0.40, 0.45))})
        status = text(find(out["body"], cls="bookrs-note")[0])
        assert status == "No book in this catalogue is a close match for “wizards”. These are the nearest it has."

    def test_books_below_the_page_floor_are_dropped_on_both_paths(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: mine_search(titles=("Near", "Far"), scores=(0.80, 0.10))})
        assert titles(find(out["body"], id="bookrs-semantic-search")[0]) == ["Near"]

    def test_a_patron_with_no_ratings_is_not_told_the_order_is_theirs(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: mine_search(method="none", n=0, scores=(0.8, 0.7))})
        holder = find(out["body"], id="bookrs-semantic-search")[0]
        assert not any("Ordered by" in text(n) for n in find(holder, cls="bookrs-note"))
        assert text(find(holder, cls="bookrs-note")[0]).endswith("best match first.")

    def test_a_visitor_gets_the_plain_search(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: {"status": 401, "body": {}}, **SEARCH})
        assert any("/search/semantic" in r["url"] for r in out["requests"])
        assert titles(find(out["body"], id="bookrs-semantic-search")[0]) == ["Near A", "Near B", "Near C"]

    def test_a_failing_personal_search_falls_back_to_the_plain_one(self):
        out = run(path="/x", search="?bq=wizards", page=SEMANTIC, patron_search_route=SEARCH_ROUTE,
                  routes={SEARCH_ROUTE: {"status": 502, "body": {}}, **SEARCH})
        assert titles(find(out["body"], id="bookrs-semantic-search")[0]) == ["Near A", "Near B", "Near C"]


class TestLinks:
    def test_search_results_link_to_the_same_words_by_meaning(self):
        page = [{"tag": "div", "id": "numresults", "text": "Your search returned 3 results."},
                {"tag": "div", "id": "userresults"}]
        out = run(path="/cgi-bin/koha/opac-search.pl", search="?q=wizard+school", page=page,
                  semantic_url="/cgi-bin/koha/opac-page.pl?page_id=2",
                  routes={f"{API}/search/semantic": {"status": 200, "body": {"results": []}}})
        offer = find(out["body"], cls="bookrs-offer")[0]
        a = find(offer, tag="a")[0]
        assert a["href"] == "/cgi-bin/koha/opac-page.pl?page_id=2&bq=wizard%20school"
        assert text(a) == "Search for “wizard school” by meaning instead"

    def test_the_account_menu_gains_a_link(self):
        page = [{"tag": "div", "id": "menu", "name": "menu"},
                {"tag": "ul", "parent": "menu", "name": "list"},
                {"tag": "li", "parent": "list", "text": "Summary"}]
        out = run(path="/cgi-bin/koha/opac-user.pl", page=page, for_you_url="/for-you")
        items = find(out["body"], tag="li")
        assert [text(i) for i in items] == ["Summary", "Recommended for you"]
        assert find(items[1], tag="a")[0]["href"] == "/for-you"

    def test_no_menu_link_without_a_page_to_link_to(self):
        page = [{"tag": "div", "id": "menu", "name": "menu"}, {"tag": "ul", "parent": "menu"}]
        out = run(path="/cgi-bin/koha/opac-user.pl", page=page)
        assert not find(out["body"], tag="li")

    def test_record_pages_are_unchanged(self):
        out = run(path="/cgi-bin/koha/opac-detail.pl", search="?biblionumber=7",
                  routes={f"{API}/works/by-record-id/7": {"status": 200, "body": {"id": 3}},
                          f"{API}/works/3/similar": {"status": 200, "body": {"results": books("Sim")}}})
        assert [r["url"] for r in out["requests"]] == [
            f"{API}/works/by-record-id/7?source_id=54", f"{API}/works/3/similar?limit=6"]
        assert titles(out["body"]) == ["Sim"]
