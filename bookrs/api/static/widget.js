/*
 * BookRS-Platform OPAC widget.
 *
 * Added to a library's catalogue through Koha's OPACUserJS system
 * preference, which injects script into every OPAC page. That avoids
 * patching templates or forking a theme, so the widget survives a Koha
 * upgrade.
 *
 *   <script src="https://bookrs.library.example/widget.js"
 *           data-api="https://bookrs.library.example"
 *           data-source-id="1"></script>
 *
 * Fails silently. A recommendation panel that does not appear is a
 * disappointment; a JavaScript error on a library's catalogue page is a
 * support ticket.
 */
(function () {
  "use strict";

  if (window.__bookrsLoaded) { return; }
  window.__bookrsLoaded = true;

  var script = document.currentScript;
  if (!script) { return; }

  var API = (script.getAttribute("data-api") || "").replace(/\/$/, "");
  var SOURCE_ID = script.getAttribute("data-source-id") || "";
  var LIMIT = parseInt(script.getAttribute("data-limit") || "6", 10);
  /* Two headings, because the panel is not always making the same
   * claim. "Readers also borrowed" is a statement about this library's
   * patrons; when no result carries circulation evidence it is simply
   * false, and a patron cannot tell. The semantic heading is true in
   * every case, so it is the default and the borrowing claim is earned
   * rather than assumed.
   *
   * An explicit data-heading always wins: a library that has chosen its
   * own wording knows its own catalogue. */
  var HEADING = script.getAttribute("data-heading");
  /* Three things differ between library systems, and nothing else does.
   * Defaults are Koha's, so an existing installation is unaffected.
   *
   * PMB, whose OPAC takes the same snippet through its biblio_main_header
   * parameter rather than Koha's OPACUserJS:
   *   data-record-param="id"
   *   data-record-prefix="oai:PMBTEST:"
   *   data-record-url="/pmb/opac_css/index.php?lvl=notice_display&id={id}"
   *   data-mount="#main"
   * The prefix is the installation's OAI identifier, not a constant. */
  var RECORD_PARAM = script.getAttribute("data-record-param") || "biblionumber";
  var RECORD_PREFIX = script.getAttribute("data-record-prefix") || "";
  var RECORD_URL = script.getAttribute("data-record-url")
                || "/cgi-bin/koha/opac-detail.pl?biblionumber={id}";
  var MOUNT = script.getAttribute("data-mount");
  /* Zero-result rescue on the search results page. A query the
   * catalogue's own search cannot match -- natural language, a typo,
   * a concept rather than a title -- is answered by meaning instead of
   * left at "No results found". Never shown when the catalogue did
   * find something: that page belongs to the library's own ranking. */
  var SEARCH_PATH = script.getAttribute("data-search-path") || "opac-search.pl";
  var SEARCH_PARAM = script.getAttribute("data-search-param") || "q";
  var NORESULTS_SEL = script.getAttribute("data-noresults") || "#numresults";
  var RESULTS_SEL = script.getAttribute("data-results") || "#userresults";
  var MIN_SCORE = parseFloat(script.getAttribute("data-min-score") || "0.55");
  var HEADING_RESCUE = script.getAttribute("data-heading-rescue") || "Closest by meaning";
  var HEADING_RELATED = script.getAttribute("data-heading-related") || "Also related by subject";
  var HEADING_CONTENT = "Related in this catalogue";
  var HEADING_BORROWED = "Readers also borrowed";
  if (!API) { return; }

  /* Koha detail pages carry the record id in the query string. Other
   * pages have no biblionumber, so the widget simply does nothing. */
  function biblionumber() {
    var re = new RegExp("[?&]" + RECORD_PARAM + "=(\\d+)");
    var match = window.location.search.match(re);
    return match ? match[1] : null;
  }

  function request(path) {
    return fetch(API + path, { credentials: "omit" }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function card(work) {
    var item = element("li", "bookrs-item");

    var link = element("a", "bookrs-title", work.title || "Untitled");
    link.href = RECORD_URL.replace("{id}", encodeURIComponent(work.biblionumber));
    item.appendChild(link);

    if (work.authors && work.authors.length) {
      item.appendChild(element("span", "bookrs-author", work.authors[0]));
    }

    /* Marked only where circulation actually backs this result. Absent
     * on the rest, which is most of them on a sparse catalogue. */
    if (work.signal === "hybrid") {
      item.appendChild(element("span", "bookrs-signal", "Borrowed together"));
    }

    /* Availability comes from the last catalogue sync, not a live
     * check, so it is described as "on the shelf" rather than promised
     * as current. */
    /* Absent, not zero. A source whose export carries no item fields --
     * PMB's OAI is one -- says nothing about holdings, and rendering
     * that as "No copies" asserts something the library never
     * published. Zero copies from a source that does publish holdings
     * is a real catalogue-only record and still shown. */
    if (work.availability) {
      var availability = work.availability;
      item.appendChild(element(
        "span",
        "bookrs-status " + (availability.is_available ? "in" : "out"),
        availability.total === 0
          ? "No copies"
          : availability.is_available
            ? availability.available + " of " + availability.total + " on the shelf"
            : "All copies on loan"
      ));
    }
    return item;
  }

  /* The borrowing claim holds only if every result shown carries
   * collaborative evidence of its own. A mixed panel gets the semantic
   * heading and marks the individual results that were borrowed
   * together -- overclaiming on behalf of a library's patrons is worse
   * than underclaiming. */
  function heading(results) {
    if (HEADING) { return HEADING; }
    /* The length check is unreachable from render(), which returns
     * early on an empty list. It stays because [].every() is true, so
     * any future caller reaching heading() directly with no results
     * would get the borrowing claim -- the strongest statement, from
     * the weakest evidence. */
    var all = results.length > 0 && results.every(function (work) {
      return work.signal === "hybrid";
    });
    return all ? HEADING_BORROWED : HEADING_CONTENT;
  }

  function render(results) {
    if (!results.length) { return; }

    var panel = element("div", "bookrs-panel");
    panel.appendChild(element("h3", "bookrs-heading", heading(results)));
    var list = element("ul", "bookrs-list");
    results.forEach(function (work) { list.appendChild(card(work)); });
    panel.appendChild(list);
    panel.appendChild(element("p", "bookrs-credit", "Suggestions from this library's own catalogue"));

    /* Several insertion points, because OPAC themes differ and a
     * library may have customised theirs. The last is the page body,
     * which always exists. */
    var host = (MOUNT && document.querySelector(MOUNT))
            || document.getElementById("bookrs-recommendations")
            || document.querySelector(".content_set, #catalogue_detail_biblio, .maincontent")
            || document.querySelector("#main, main")
            || document.body;
    host.appendChild(panel);
  }

  function style() {
    var css = document.createElement("style");
    css.textContent = [
      ".bookrs-panel{margin:1.5em 0;padding:1em 0;border-top:1px solid #ddd}",
      ".bookrs-heading{margin:0 0 .75em;font-size:1.05em}",
      ".bookrs-signal{display:block;font-size:.8em;opacity:.7}",
      ".bookrs-list{list-style:none;margin:0;padding:0;display:grid;",
      "  grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.75em}",
      ".bookrs-item{padding:.6em .7em;border:1px solid #e3e3e3;border-radius:4px;",
      "  display:flex;flex-direction:column;gap:.2em}",
      ".bookrs-title{font-weight:600;text-decoration:none;line-height:1.3}",
      ".bookrs-author{font-size:.87em;color:#555}",
      ".bookrs-status{font-size:.8em}",
      ".bookrs-status.in{color:#1a7f37}",
      ".bookrs-status.out{color:#8a6d00}",
      ".bookrs-credit{margin:.8em 0 0;font-size:.78em;color:#777}",
      ".bookrs-note{margin:0 0 .75em;font-size:.9em;color:#444}"
    ].join("");
    document.head.appendChild(css);
  }

  /* Every value of the search parameter, joined; Koha's advanced search
   * repeats q. CCL index prefixes ("kw,wrdl:") and quotes are stripped
   * because they are instructions to Zebra, not meaning. */
  function searchQuery() {
    var out = [];
    var re = new RegExp("[?&]" + SEARCH_PARAM + "=([^&]*)", "g");
    var m;
    while ((m = re.exec(window.location.search)) !== null) {
      var v = decodeURIComponent(m[1].replace(/\+/g, " "));
      v = v.replace(/\b[a-z-]+(,[a-z-]+)*[:=]/gi, " ").replace(/["']/g, " ")
           .replace(/\s+/g, " ").trim();
      if (v) { out.push(v); }
    }
    return out.join(" ");
  }

  function isSearchPage() {
    return window.location.pathname.indexOf(SEARCH_PATH) !== -1;
  }

  function hadNoResults() {
    if (!document.querySelector(RESULTS_SEL)) { return true; }
    var h = document.querySelector(NORESULTS_SEL);
    return !!(h && /no results/i.test(h.textContent));
  }

  function minScore() {
    var m = window.location.search.match(/[?&]bookrs_min=([0-9.]+)/);
    return m ? parseFloat(m[1]) : MIN_SCORE;
  }

  function renderRescue(query, results) {
    if (!results.length) { return; }
    var panel = element("div", "bookrs-panel bookrs-rescue");
    panel.appendChild(element("h3", "bookrs-heading", HEADING_RESCUE));
    panel.appendChild(element("p", "bookrs-note",
      "Nothing matched \u201c" + query + "\u201d exactly. These are the closest by subject:"));
    var list = element("ul", "bookrs-list");
    results.forEach(function (work) { list.appendChild(card(work)); });
    panel.appendChild(list);
    panel.appendChild(element("p", "bookrs-credit", "Suggestions from this library's own catalogue"));
    var anchor = document.querySelector(NORESULTS_SEL);
    /* After Koha's own "No results found for that in catalog." line,
     * when it is the next element; the heading alone otherwise. */
    if (anchor && anchor.nextElementSibling && anchor.nextElementSibling.tagName === "P") {
      anchor = anchor.nextElementSibling;
    }
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(panel, anchor.nextSibling);
    } else {
      ((MOUNT && document.querySelector(MOUNT)) || document.querySelector("#main, main") || document.body)
        .appendChild(panel);
    }
  }

  /* Record ids the catalogue's own search already put on the page, so
   * the related band only ever adds and never repeats. */
  function shownRecordIds() {
    var ids = {};
    var re = new RegExp("[?&]" + RECORD_PARAM + "=(\\d+)");
    var links = document.querySelectorAll(RESULTS_SEL + " a[href*='" + RECORD_PARAM + "=']");
    for (var i = 0; i < links.length; i++) {
      var m = links[i].getAttribute("href").match(re);
      if (m) { ids[m[1]] = true; }
    }
    return ids;
  }

  function renderRelated(results) {
    if (!results.length) { return; }
    var panel = element("div", "bookrs-panel bookrs-related");
    panel.appendChild(element("h3", "bookrs-heading", HEADING_RELATED));
    var list = element("ul", "bookrs-list");
    results.forEach(function (work) { list.appendChild(card(work)); });
    panel.appendChild(list);
    panel.appendChild(element("p", "bookrs-credit", "Suggestions from this library's own catalogue"));
    var anchor = document.querySelector(RESULTS_SEL);
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(panel, anchor.nextSibling);
    } else {
      ((MOUNT && document.querySelector(MOUNT)) || document.querySelector("#main, main") || document.body)
        .appendChild(panel);
    }
  }

  function rescue() {
    var query = searchQuery();
    if (!query || query.length < 3) { return; }
    var empty = hadNoResults();
    /* Stricter when the catalogue already answered: the band is an
     * addition to a working result list, not a rescue of a failed one. */
    var threshold = empty ? minScore() : Math.min(minScore() + 0.05, 0.95);
    var want = empty ? LIMIT : LIMIT * 3;
    var path = "/search/semantic?q=" + encodeURIComponent(query)
             + "&limit=" + want + "&min_score=" + threshold;
    if (SOURCE_ID) { path += "&source_id=" + encodeURIComponent(SOURCE_ID); }
    request(path)
      .then(function (data) {
        var results = data.results || [];
        style();
        if (empty) { renderRescue(query, results); return; }
        var shown = shownRecordIds();
        renderRelated(results.filter(function (w) { return !shown[w.biblionumber]; }).slice(0, LIMIT));
      })
      .catch(function () { /* silent, as everywhere else */ });
  }

  function start() {
    if (isSearchPage()) { rescue(); return; }
    var id = biblionumber();
    if (!id) { return; }

    var query = "/works/by-record-id/" + encodeURIComponent(RECORD_PREFIX + id);
    if (SOURCE_ID) { query += "?source_id=" + encodeURIComponent(SOURCE_ID); }

    request(query)
      .then(function (work) {
        return request("/works/" + work.id + "/similar?limit=" + LIMIT);
      })
      .then(function (data) {
        style();
        render(data.results || []);
      })
      .catch(function () {
        /* Deliberately silent. The catalogue works without us. */
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
