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
  var HEADING = script.getAttribute("data-heading") || "Readers also borrowed";
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

  function render(results) {
    if (!results.length) { return; }

    var panel = element("div", "bookrs-panel");
    panel.appendChild(element("h3", "bookrs-heading", HEADING));
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
