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
  /* Three headings, because the panel is not always making the same
   * claim. "Readers also borrowed" is a statement about this library's
   * patrons; when no result carries circulation evidence it is simply
   * false, and a patron cannot tell. The same is true when the evidence
   * is star ratings: the factors are then learned from what readers
   * rated, and the heading says so. The semantic heading is true in
   * every case, so it is the default, and either claim about readers is
   * earned rather than assumed.
   *
   * An explicit data-heading always wins: a library that has chosen its
   * own wording knows its own catalogue. */
  var HEADING = script.getAttribute("data-heading");
  /* Where a record page keeps its id, and how to link to one. The
   * defaults are Koha's OPAC; the attributes exist for an OPAC whose
   * pages are laid out differently. */
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
  /* A catalogue that searches by POST has no query in the URL. It does
   * re-fill the search box with the term, so the page still carries it;
   * this selector says where to read it. Koha does not need it. */
  var QUERY_INPUT = script.getAttribute("data-query-input") || "";
  var HEADING_RESCUE = script.getAttribute("data-heading-rescue") || "Closest by meaning";
  var HEADING_RELATED = script.getAttribute("data-heading-related") || "Also related by subject";
  var HEADING_CONTENT = "Related in this catalogue";
  /* What readers did is what the factors were learned from, which the
   * API reports with each answer (collaborative_signal). An answer that
   * does not name it -- an older API, or factors from a refit that did
   * not record it -- earns neither claim. */
  var READERS = {
    loans: { heading: "Readers also borrowed", marker: "Borrowed together" },
    ratings: { heading: "Readers who rated this also rated", marker: "Readers rated both" }
  };
  function readers(evidence) {
    return evidence === "loans" || evidence === "ratings" ? READERS[evidence] : null;
  }
  /* Pages for one patron, and pages that stand on their own.
   *
   * data-patron-route is set by the Koha plugin only when it can
   * answer. It is Koha's own address, same origin as this page, so the
   * patron's session goes with the request and Koha -- not this script
   * -- decides who is asking. Nothing here ever holds a patron id.
   *
   * The two page URLs are Koha pages a librarian created, each holding
   * a placeholder this script fills; set, they are linked from the
   * account menu and the search results. */
  var PATRON_ROUTE = script.getAttribute("data-patron-route") || "";
  /* Same rules: Koha's address, the patron's session, set by the
   * plugin only when it can answer. */
  var PATRON_SEARCH_ROUTE = script.getAttribute("data-patron-search-route") || "";
  var FOR_YOU_URL = script.getAttribute("data-for-you-url") || "";
  var SEMANTIC_URL = script.getAttribute("data-semantic-url") || "";
  /* Lower than data-min-score on purpose: there the suggestions are
   * unasked for and must be close; here the patron asked for a search
   * and expects a list, ranked best first. */
  var PAGE_MIN_SCORE = parseFloat(script.getAttribute("data-page-min-score") || "0.35");
  var LOGIN_URL = "/cgi-bin/koha/opac-user.pl";
  var FOR_YOU_LIMIT = 24;
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

  /* evidence: what the collaborative factors were learned from, on the
   * record page's own panel; absent everywhere else. */
  function card(work, evidence) {
    var item = element("li", "bookrs-item");

    var link = element("a", "bookrs-title", work.title || "Untitled");
    link.href = RECORD_URL.replace("{id}", encodeURIComponent(work.biblionumber));
    item.appendChild(link);

    if (work.authors && work.authors.length) {
      item.appendChild(element("span", "bookrs-author", work.authors[0]));
    }

    /* Marked only where readers' evidence actually backs this result,
     * and in the words of that evidence. Absent on the rest, which is
     * most of them on a sparse catalogue. */
    var backed = work.signal === "hybrid" ? readers(evidence) : null;
    if (backed) {
      item.appendChild(element("span", "bookrs-signal", backed.marker));
    }

    /* Availability comes from the last catalogue sync, not a live
     * check, so it is described as "on the shelf" rather than promised
     * as current. */
    /* Absent, not zero. A source that carries no item fields says
     * nothing about holdings, and rendering
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

  /* A claim about readers holds only if every result shown carries
   * collaborative evidence of its own, and only in the words of that
   * evidence. A mixed panel gets the semantic heading and marks the
   * individual results that readers' evidence backs -- overclaiming on
   * behalf of a library's patrons is worse than underclaiming. */
  function heading(results, evidence) {
    if (HEADING) { return HEADING; }
    /* The length check is unreachable from render(), which returns
     * early on an empty list. It stays because [].every() is true, so
     * any future caller reaching heading() directly with no results
     * would get a claim about readers -- the strongest statement, from
     * the weakest evidence. */
    var all = results.length > 0 && results.every(function (work) {
      return work.signal === "hybrid";
    });
    var backed = all ? readers(evidence) : null;
    return backed ? backed.heading : HEADING_CONTENT;
  }

  function render(results, evidence) {
    if (!results.length) { return; }

    var panel = element("div", "bookrs-panel");
    panel.appendChild(element("h3", "bookrs-heading", heading(results, evidence)));
    var list = element("ul", "bookrs-list");
    results.forEach(function (work) { list.appendChild(card(work, evidence)); });
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

  var styled = false;
  function style() {
    if (styled) { return; }
    styled = true;
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
      ".bookrs-note{margin:0 0 .75em;font-size:.9em;color:#444}",
      ".bookrs-more{display:inline-block;margin-top:.6em;font-size:.9em}",
      ".bookrs-search{display:flex;gap:.5em;margin:0 0 1em;flex-wrap:wrap}",
      ".bookrs-search-input{flex:1 1 18em;padding:.45em .6em;font-size:1em}",
      ".bookrs-offer{margin:.5em 0;font-size:.95em}"
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
    if (!out.length && QUERY_INPUT) {
      var field = document.querySelector(QUERY_INPUT);
      if (field && field.value) { out.push(field.value.trim()); }
    }
    return out.join(" ");
  }

  function isSearchPage() {
    if (window.location.pathname.indexOf(SEARCH_PATH) === -1) { return false; }
    /* A POST-search catalogue serves its results page from the same
     * path as its home page, so the path alone is not enough: require
     * the query itself, wherever this deployment carries it. */
    return !!searchQuery();
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
    var threshold = empty ? minScore() : Math.min(Math.round((minScore() + 0.05) * 100) / 100, 0.95);
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

  function param(name) {
    var match = window.location.search.match(new RegExp("[?&]" + name + "=([^&]*)"));
    return match ? decodeURIComponent(match[1].replace(/\+/g, " ")) : "";
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function link(text, href, className) {
    var a = element("a", className || "bookrs-link", text);
    a.href = href;
    return a;
  }

  /* The patron's own list, asked of Koha with the patron's session.
   * Resolves to the answer, or to null when nobody is logged in;
   * rejects when the route is off or cannot answer. */
  function mine(limit) {
    if (!PATRON_ROUTE) { return Promise.reject(new Error("off")); }
    return fetch(PATRON_ROUTE + "?limit=" + limit, {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      if (r.status === 401) { return null; }
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  function popular(limit) {
    var path = "/works/popular?limit=" + limit;
    if (SOURCE_ID) { path += "&source_id=" + encodeURIComponent(SOURCE_ID); }
    return request(path);
  }

  /* Said plainly, and only what is true: how many of the patron's own
   * ratings the list stands on. */
  function personalNote(data) {
    var n = data.ratings_used || 0;
    if (data.method === "full") { return "Based on your " + n + " ratings."; }
    if (data.method === "few") {
      return "Based on your " + n + (n === 1 ? " rating" : " ratings")
           + " so far. Rate a few more books you have read and this list becomes more yours.";
    }
    return "You have not rated any books yet, so these are popular in this library. "
         + "Rate books you have read and this list will be chosen for you.";
  }

  /* reason: "anonymous" (nobody logged in) or "unavailable" (the
   * personal route is off, or could not answer). Only the first earns
   * an invitation to log in. */
  function personalOrPopular(limit) {
    function fallback(reason) {
      return popular(limit).then(function (data) {
        return { personal: false, reason: reason, heading: "Popular in this library",
                 note: "", results: data.results || [] };
      });
    }
    return mine(limit).then(function (data) {
      if (!data) { return fallback("anonymous"); }
      /* Logged in but nothing rated: Koha sends the popular list. It is
       * not this patron's, so it is not headed or credited as theirs --
       * only the note speaks to them. */
      var own = data.method !== "none";
      return { personal: own, reason: own ? null : "unrated",
               heading: own ? "Recommended for you" : "Popular in this library",
               note: personalNote(data), results: data.results || [] };
    }, function () { return fallback("unavailable"); });
  }

  function listPanel(view, credit, noHeading) {
    var panel = element("div", "bookrs-panel");
    if (!noHeading) { panel.appendChild(element("h3", "bookrs-heading", view.heading)); }
    if (view.note) { panel.appendChild(element("p", "bookrs-note", view.note)); }
    var list = element("ul", "bookrs-list");
    view.results.forEach(function (work) { list.appendChild(card(work)); });
    panel.appendChild(list);
    panel.appendChild(element("p", "bookrs-credit", credit || (view.personal
      ? "Chosen from your ratings in this library"
      : "Suggestions from this library's own catalogue")));
    return panel;
  }

  function isHomePage() {
    var path = window.location.pathname;
    return path === "/" || /\/opac-main\.pl$/.test(path);
  }

  /* Homepage: the patron's list when logged in, the library's popular
   * books otherwise. At the top of the main column, or inside
   * #bookrs-home where a library has placed one. */
  function home() {
    var placeholder = document.getElementById("bookrs-home");
    var host = placeholder || document.querySelector(".maincontent")
            || document.querySelector("#main, main") || document.body;
    personalOrPopular(LIMIT).then(function (view) {
      if (!view.results.length) { return; }
      style();
      var panel = listPanel(view);
      if (view.personal && FOR_YOU_URL) {
        panel.appendChild(link("More recommended for you", FOR_YOU_URL, "bookrs-more"));
      }
      if (placeholder) { clear(placeholder); placeholder.appendChild(panel); }
      else { host.insertBefore(panel, host.firstChild || null); }
    }).catch(function () { /* the homepage works without us */ });
  }

  /* The "Recommended for you" page: a Koha page holding
   * <div id="bookrs-for-you"></div>. */
  function forYouPage(host) {
    personalOrPopular(FOR_YOU_LIMIT).then(function (view) {
      style();
      clear(host);
      if (!view.personal && view.reason === "anonymous") {
        var invite = element("p", "bookrs-note");
        invite.appendChild(link("Log in", LOGIN_URL));
        invite.appendChild(element("span", "", " to see books chosen for you from your own ratings. Until then, these are popular in this library."));
        host.appendChild(invite);
      }
      /* The page's own title already says "Recommended for you"; the
       * panel repeats a heading only when it says something else. */
      if (view.results.length) { host.appendChild(listPanel(view, null, view.personal)); }
      else { host.appendChild(element("p", "bookrs-note", "There are no recommendations yet.")); }
    }).catch(function () {
      clear(host);
      host.appendChild(element("p", "bookrs-note", "Recommendations are not available right now."));
    });
  }

  /* The "Search by meaning" page: a Koha page holding
   * <div id="bookrs-semantic-search"></div>. The query travels in the
   * address (bq), so a result page can be bookmarked or shared; the
   * page's own parameters (page_id, code) are carried on the form so
   * a search returns to the same page. */
  function semanticPage(host) {
    style();
    clear(host);
    var query = param("bq").replace(/^\s+|\s+$/g, "");

    var form = element("form", "bookrs-search");
    form.setAttribute("method", "get");
    form.setAttribute("action", window.location.pathname);
    window.location.search.replace(/^\?/, "").split("&").forEach(function (pair) {
      if (!pair) { return; }
      var parts = pair.split("=");
      var name = decodeURIComponent(parts[0]);
      if (name === "bq") { return; }
      var hidden = element("input");
      hidden.setAttribute("type", "hidden");
      hidden.setAttribute("name", name);
      hidden.setAttribute("value", decodeURIComponent((parts[1] || "").replace(/\+/g, " ")));
      form.appendChild(hidden);
    });
    var input = element("input", "bookrs-search-input");
    input.setAttribute("type", "search");
    input.setAttribute("name", "bq");
    input.setAttribute("value", query);
    input.setAttribute("placeholder", "Describe the book you are looking for");
    input.setAttribute("aria-label", "Search by meaning");
    form.appendChild(input);
    var button = element("button", "bookrs-search-button", "Search by meaning");
    button.setAttribute("type", "submit");
    form.appendChild(button);
    host.appendChild(form);

    if (query.length < 3) {
      host.appendChild(element("p", "bookrs-note",
        "Describe a book in your own words -- a theme, a plot, a feeling -- and the "
        + "catalogue is searched by meaning, not by matching words."));
      return;
    }
    var status = element("p", "bookrs-note", "Searching...");
    host.appendChild(status);
    var path = "/search/semantic?q=" + encodeURIComponent(query)
             + "&limit=" + FOR_YOU_LIMIT + "&min_score=" + PAGE_MIN_SCORE;
    if (SOURCE_ID) { path += "&source_id=" + encodeURIComponent(SOURCE_ID); }

    /* A logged-in patron's search goes to Koha, which orders the same
     * books by their ratings as well as by meaning. Anyone else, or any
     * failure, gets the plain search by meaning. */
    var asked = PATRON_SEARCH_ROUTE
      ? fetch(PATRON_SEARCH_ROUTE + "?q=" + encodeURIComponent(query) + "&limit=" + FOR_YOU_LIMIT, {
          credentials: "same-origin",
          headers: { "Accept": "application/json" }
        }).then(function (r) {
          if (r.status === 401) { return null; }
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.json();
        }).catch(function () { return null; })
      : Promise.resolve(null);

    asked.then(function (mineData) {
      return mineData || request(path).then(function (data) {
        return { method: "public", results: data.results || [] };
      });
    }).then(function (data) {
      /* The same floor for both: the personal route returns its whole
       * candidate pool, the public one is asked for this floor. */
      var results = (data.results || []).filter(function (work) {
        return typeof work.score !== "number" || work.score >= PAGE_MIN_SCORE;
      });
      /* A ranked list always has a first place, whether or not anything
       * is near. Below the bar the catalogue search sets before offering
       * books unasked (data-min-score), the best match is not called
       * close. The best
       * is looked for across the list: a patron's order need not put
       * the closest in meaning first. */
      var best = null;
      results.forEach(function (work) {
        if (typeof work.score === "number" && (best === null || work.score > best)) { best = work.score; }
      });
      var weak = typeof best === "number" && best < minScore();
      /* The order is the patron's only if their taste changed it. The
       * platform leaves a search that found no close match in plain
       * meaning order (reordered false), and the page then says "best
       * match first" like the public search. A platform that does not
       * send the field is taken at its method, as before. */
      var theirs = (data.method === "full" || data.method === "few")
                && data.reordered !== false;
      status.textContent = !results.length
        ? "Nothing in this catalogue is close in meaning to “" + query + "”."
        : weak
          ? "No book in this catalogue is a close match for “" + query
            + "”. These are the nearest it has, best match first."
          : "The " + results.length + (results.length === 1 ? " book" : " books")
            + " in this catalogue closest in meaning to “" + query
            + "”, best match first.";
      if (!results.length) { return; }
      if (theirs) {
        /* No longer "best match first": the patron's taste has moved
         * books up. Say what the order is made of instead. */
        var n = data.ratings_used || 0;
        status.textContent = weak
          ? "No book in this catalogue is a close match for “" + query
            + "”. These are the nearest it has."
          : "The " + results.length + (results.length === 1 ? " book" : " books")
            + " in this catalogue closest in meaning to “" + query + "”.";
        host.appendChild(element("p", "bookrs-note",
          "Ordered by meaning and by your " + n + (n === 1 ? " rating" : " ratings")
          + ": books like the ones you have rated move up."));
      }
      var list = element("ul", "bookrs-list");
      results.forEach(function (work) { list.appendChild(card(work)); });
      host.appendChild(list);
    }).catch(function () {
      status.textContent = "Search by meaning is not available right now.";
    });
  }

  /* From the catalogue's own results, a way across to the same words
   * searched by meaning. */
  function offerSemantic() {
    if (!SEMANTIC_URL) { return; }
    var query = searchQuery();
    if (!query || query.length < 3) { return; }
    var anchor = document.querySelector(NORESULTS_SEL)
              || (MOUNT && document.querySelector(MOUNT)) || document.querySelector("#main, main");
    if (!anchor) { return; }
    style();
    var p = element("p", "bookrs-offer");
    p.appendChild(link("Search for “" + query + "” by meaning instead",
      SEMANTIC_URL + (SEMANTIC_URL.indexOf("?") === -1 ? "?" : "&") + "bq=" + encodeURIComponent(query)));
    anchor.appendChild(p);
  }

  /* The patron's account menu, on the pages that have one. */
  function accountLink() {
    if (!FOR_YOU_URL) { return; }
    var menu = document.querySelector("#menu ul");
    if (!menu) { return; }
    var item = element("li", "bookrs-menu-item");
    item.appendChild(link("Recommended for you", FOR_YOU_URL));
    menu.appendChild(item);
  }

  function start() {
    var forYou = document.getElementById("bookrs-for-you");
    var semantic = document.getElementById("bookrs-semantic-search");
    accountLink();
    if (forYou) { forYouPage(forYou); }
    if (semantic) { semanticPage(semantic); }
    if (forYou || semantic) { return; }
    if (isHomePage()) { home(); return; }
    if (isSearchPage()) { offerSemantic(); rescue(); return; }
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
        render(data.results || [], data.collaborative_signal);
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
