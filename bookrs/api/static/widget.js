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
  var HEADING_CONTENT = "Related in this catalogue";
  var HEADING_BORROWED = "Readers also borrowed";
  if (!API) { return; }

  /* Koha detail pages carry the record id in the query string. Other
   * pages have no biblionumber, so the widget simply does nothing. */
  function biblionumber() {
    var match = window.location.search.match(/[?&]biblionumber=(\d+)/);
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
    link.href = "/cgi-bin/koha/opac-detail.pl?biblionumber=" + work.biblionumber;
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
    var availability = work.availability || {};
    var status = element(
      "span",
      "bookrs-status " + (availability.is_available ? "in" : "out"),
      availability.total === 0
        ? "No copies"
        : availability.is_available
          ? availability.available + " of " + availability.total + " on the shelf"
          : "All copies on loan"
    );
    item.appendChild(status);
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
    var host = document.getElementById("bookrs-recommendations")
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
      ".bookrs-credit{margin:.8em 0 0;font-size:.78em;color:#777}"
    ].join("");
    document.head.appendChild(css);
  }

  function start() {
    var id = biblionumber();
    if (!id) { return; }

    var query = "/works/by-record-id/" + encodeURIComponent(id);
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
