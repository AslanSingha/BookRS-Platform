# BookRS-Platform

> A recommendation service for libraries running open-source ILS
> platforms — Koha, PMB, and other MARC / OAI-PMH-compliant systems.

**Status: working, not yet piloted.** Catalogue ingestion, embedding,
semantic search and the OPAC widget run end to end against three live
library systems — Koha in both MARC flavours and a self-hosted PMB instance
(`docs/pmb-setup.md`) — and a patron viewing a record sees related books
from the library's own holdings. Collaborative filtering is built and
wired in, including per-patron folding-in, but it has only ever run on
generated circulation: no library has deployed this yet, and nothing
here demonstrates recommendation *quality*. That needs real borrowing
history, which only a pilot produces. Search quality *is* measured:
see [Evaluation](#evaluation).

---

## What this is

BookRS-Platform is **software a library self-hosts**, not a service
operated by a third party. A library's IT staff clone this repository,
point it at their own ILS, and run it on their own infrastructure via
Docker Compose. Catalogue data never leaves the library's control.

The service harvests bibliographic records from the library's existing
system over OAI-PMH, builds semantic recommendation models from them,
and exposes those through an API a library can call from its public
catalogue.

**Integration is read-only.** BookRS-Platform is never given write —
or even direct read — access to a library's production database.

## Prior work

Building on research and findings from
**[BookRS-System](https://github.com/AslanSingha/BookRS-System)** —
RIN SINGH's Engineering thesis at the Institute of Technology of
Cambodia (2026), supervised by M. SOK Kimheng. That system combined
SBERT semantic embeddings with ALS collaborative filtering.

BookRS-Platform is a **separate codebase**, with its own schema,
integration layer and deployment model.

## Differences from BookRS-System

| | BookRS-System | BookRS-Platform |
|---|---|---|
| Data source | Static research dataset | Live ILS via OAI-PMH |
| Schema | Books table | Bibliographic / item separation |
| Signals | Explicit ratings | Implicit circulation intensity |
| Record format | Normalised CSV | MARC21, UNIMARC, PMB XML |
| Embedding model | English-only | Multilingual |
| Deployment | Local development | Docker Compose, self-hosted |

**Bibliographic / item separation.** Real library data needs two layers:
a `works` record (the intellectual work — title, authors, subject
headings, one embedding) and `items` records (physical copies — branch,
call number, availability). Multiple copies of a book are real
inventory, not duplicates to merge.

**The preference signal is circulation, not ratings.** Circulation
records that a book was borrowed, not whether it was enjoyed, so
implicit signals are weighted through a confidence formula built from
borrowing *intensity* — loans, renewals, repeat borrows — rather than
direction. Its weights are deployment configuration, because the right
values are an empirical question no synthetic data can answer. Koha does
collect patron star ratings natively, but offers no way to read them
(see "Not yet built"), so they remain schema-ready and unused.

**Graceful degradation on sparse metadata.** Roughly a third of a real
catalogue carries nothing beyond a title. The content-based layer is
designed around that constraint rather than assuming rich descriptions.

**Circulation re-ranks; it does not retrieve.** Recommendations are
drawn from the semantic layer, and borrowing history reorders them. A
work the embeddings did not consider related cannot be promoted into the
results by co-borrowing alone — which is what put two C++ books beside a
Springsteen biography when the factors were queried directly. The trade
is deliberate and costly: works that share readers but not vocabulary
are exactly what this cannot surface, and the candidate pool is
configurable so a library with real circulation can widen it.

The collaborative layer also declines to act on too little evidence.
Factors resting on very few borrowers are excluded, and where too few
candidates carry any, the layer abstains rather than imputing a value
from one or two observations. A catalogue with no circulation gets the
content-only behaviour through the same code path, not a separate one.
See [`docs/marc-field-analysis.md`](docs/marc-field-analysis.md) §15.

## What runs today

```
bookrs/ingestion    OAI-PMH harvest → MARC21/UNIMARC/PMB → normalised works
                    circulation history → Koha REST API → pseudonymised loans
bookrs/db           works, items, embeddings, ratings, loans, factors + loaders
bookrs/embedding    multilingual encoding → 384-dimensional vectors
bookrs/recommend    confidence weighting → ALS → per-work latent factors
                    hybrid ranking → content candidates, reordered
bookrs/api          search, similarity, availability, health
```

Five Docker services: `db`, `ingestion`, `embedding`, `recommend`, `api`.

**Endpoints**

| | |
|---|---|
| `GET /works/{id}/similar` | recommendations, with the signal behind each |
| `GET /search/semantic` | free-text search by meaning, with a score floor |
| `GET /search/exact` | ISBN, title or author lookup |
| `GET /works/{id}` | a single record with availability |
| `GET /works/by-record-id/{id}` | resolve a library's own biblionumber |
| `GET /widget.js` | the OPAC widget |
| `GET /sources` | per-source harvest and coverage figures |
| `GET /health` | catalogue size, embedding coverage, sync freshness |

**Measured against a 125,333-work catalogue** harvested from three live
instances — Koha MARC21 (50,436), Koha UNIMARC (54,849) and PMB
(20,048), the bibliographic records drawn from Open Library:

| | |
|---|---|
| Harvest | ~155 s for 20,048 records over 201 OAI pages |
| Embedding | 41 works/sec on 6 CPU cores for records carrying summaries; 90–175/sec for title-and-subject records. First run only |
| Similarity query | 8–12 ms |
| Exact search | 4–22 ms |

Extrapolating to a 300,000-record catalogue: roughly 30–55 minutes for
the initial embedding run, once. Subsequent syncs touch only records
whose bibliography actually changed.

## Not yet built

**Any evidence about recommendation quality.** Factorisation has only
ever run on generated circulation. An earlier generator drew loans by
popularity alone and produced neighbours no reader would accept, because
it had no notion of subject — see
[`docs/marc-field-analysis.md`](docs/marc-field-analysis.md) §14.6–14.7.
The generator now gives each patron one to three subject
interests drawn from the catalogue's own headings, and loans execute
through Koha's own `AddIssue`, so co-borrowing reflects shared subjects
rather than shared popularity. On the 50,436-work MARC21 catalogue that
yields 6,390 interactions from 801 patrons over 1,924 works — about 4%
of the catalogue, which is the real cold-start position of any library
on day one, and why the content layer carries the system. The path runs end to end; that it recommends
usefully is not established and cannot be until a library provides real
borrowing history.

**Duplicate record handling.** A catalogue's second copy of a work is
often a second record, and the nearest neighbour of a book can be
itself. No suppression rule is shipped: embeddings, ISBN, title-plus-
author and publication year each fail on a case the others handle, and
what separates a second *volume* from a second *edition* is `245$n`/`$p`,
which ingestion does not yet extract. Hiding a real book is worse than
showing a duplicate, so nothing is hidden. §15.4.

**Patron ratings.** Koha collects them natively and enables them by
default, but its REST API exposes no rating or review route. Reading
them would require direct database access, which this design rules out,
or a Koha plugin. The `ratings` table is present and unused.

**Semantic search over free text.** Requires encoding the query, which
means loading the embedding model into the public-facing service. The
stored-vector endpoints above need no model, so this is a deliberate
separate decision rather than an oversight.

**Production hardening** — authentication, rate limiting, observability
— which waits for a concrete pilot rather than being built
speculatively.

## Evaluation

Recommendation *quality* still needs a pilot. **Search quality does
not**, and it is measured.

Catalogue keyword search fails in recognisable ways, so the query set
was built from the catalogue's own subject headings — independent of the
recommender by construction — in the three ways it fails: a question in
ordinary words, a topic phrase no title carries, and a transposed-letter
typo. 120 queries, 40 of each, run through Koha's own Zebra search and
through `/search/semantic` at the deployed 0.55 floor
(`tools/build_query_set.py`, `tools/evaluate.py`).

| Query class | Queries | Zebra found nothing | BookRS answered |
|---|---|---|---|
| Natural language | 40 | 19 | 19 (100%) |
| Concept phrase | 40 | 20 | 20 (100%) |
| Typo | 40 | 38 | 30 (79%) |
| **All** | **120** | **77 (64%)** | **69 (90%)** |

Two things that number does not say:

- **Typos are the weak class, and that is expected.** A transposed
  letter damages the embedding too; semantic search is not a spell
  checker. The two techniques are complements — fuzzy matching for
  misspellings, embeddings for meaning — which is what the trigram path
  in `/search/exact` is for.
- **A score floor does not protect against a meaningless query.**
  `trsut` returns three short foreign-language titles at 0.91, 0.80 and
  0.77: high scores, useless results. The floor separates weak matches
  to a sensible question from strong ones. It cannot separate a question
  from a non-question.

**The threshold was calibrated, not chosen.** Across twenty queries run
against the live catalogue, every sensible top result scored 0.579 or
above and the two wrong ones scored 0.534 and 0.500. The floor sits
between them.

**Relevance is not scored here.** `tools/evaluate.py` writes a
judgement sheet of query-suggestion pairs for a librarian to mark; a
generated label would be the embedding grading its own output.

## Evidence base

Design decisions here are grounded in measurements against running Koha
instances rather than documentation alone:

**[`docs/marc-field-analysis.md`](docs/marc-field-analysis.md)** — field
coverage, identifier semantics, holdings structure, circulation
behaviour and model comparison, measured across both MARC flavours.

Findings that shape the architecture:

- **Metadata prefix and XML namespace do not identify MARC flavour.** A
  UNIMARC Koha serves UNIMARC records under a prefix named `marc21`, in
  the `MARC21/slim` namespace. Flavour is detected from record structure.
- **Tag numbers collide between standards.** `100`, `300` and `020`
  exist in both with unrelated meanings; a MARC21 field map applied to a
  UNIMARC feed produces plausible nonsense rather than an error.
- **Roughly a third of records have nothing but a title to embed** —
  29.4% of MARC21 and 38.5% of UNIMARC records in the reference corpora.
- **A legacy MARC-8 escape sequence makes an OAI page unparseable.** A
  catalogue converted from MARC-8 keeps its character-set switches —
  `ESC ( Q … ESC ( B` around a curly apostrophe. XML forbids `ESC` at
  any encoding, so one apostrophe in one record aborted a
  20,048-record harvest. The harvester now strips what XML forbids,
  retries the parse, and logs how many characters it removed.
- **An ILS can be incompatible with a current database default.** PMB
  7.3.7 cannot insert into its own `notices` table under MariaDB 11.8's
  default `STRICT_TRANS_TABLES`: a column with no default value. A 2020
  application meeting a 2025 database default.
- **Availability is a ranking problem, not only a display one.** Of
  5,994 planned loans in the circulation generator, 2,044 could not
  execute because the only copy was already out. A popular title is
  unavailable precisely when it is popular — something no offline
  metric captures, and the reason availability belongs in the score
  rather than only in the card.
- **Circulation activity drives incremental sync volume**, not
  cataloguing. A checkout updates a record's OAI datestamp while leaving
  its bibliographic content unchanged.
- **A multilingual embedding model costs nothing on English.** Measured
  by whether works sharing a subject heading embed closer together than
  unrelated works: +0.604 against `all-MiniLM-L6-v2`'s +0.597 on
  English, and +0.301 against +0.169 on French.
- **Patron ratings exist but are not in the MARC record**, so reaching
  them requires a second read channel alongside OAI-PMH.

## Running it

```bash
git clone https://github.com/AslanSingha/BookRS-Platform.git
cd BookRS-Platform
docker compose up -d db api
```

Then harvest a catalogue and embed it:

```bash
OAI_BASE_URL=https://your-library.example/cgi-bin/koha/oai.pl \
  docker compose run --rm ingestion python -m bookrs.ingestion.cli
docker compose run --rm embedding python -m bookrs.embedding.cli
```

The embedding service downloads a 471 MB model on first run and caches
it in a named volume.

**Before harvesting, the library's Koha needs OAI-PMH enabled** — it is
off by default — and, for holdings, an `OAI-PMH:ConfFile` declaring
`include_items: 1`. Note that declaring a conf file also restricts the
formats the endpoint offers, which can remove `oai_dc` for other
harvesters. See `docs/marc-field-analysis.md` §3.

## Adding recommendations to the catalogue

The widget does three things, in increasing order of how much it
interferes with the catalogue's own behaviour:

1. **Related books on a record page** — a panel of neighbours drawn from
   the library's own holdings, with availability.
2. **Zero-result rescue on a search page** — when the catalogue's own
   search returns nothing, the widget answers the same query by meaning
   under a labelled heading. It never appears when the catalogue found
   something, so it cannot compete with the librarian's ranking.
3. **A related band under real results** — appended *below* the
   catalogue's own results, at a stricter score floor, containing only
   works the catalogue did not already show. The catalogue's ordering is
   never touched.

Tiers 2 and 3 need to know where the query and the results live on the
page, which differs between systems. The defaults are Koha's; PMB's
values are given below.

**1. Allow the OPAC's origin.** The widget runs on the catalogue, which
is a different origin from this service, so the browser will not call it
otherwise. The default is empty — an unconfigured deployment refuses
cross-origin requests rather than allowing every site on the internet.

```bash
BOOKRS_ALLOWED_ORIGINS=https://catalogue.your-library.example \
  docker compose up -d api
```

**2. Add the loader to Koha.** In the staff interface, under
Administration → System preferences → **OPACUserJS**:

```javascript
(function () {
  var s = document.createElement('script');
  s.src = 'https://bookrs.your-library.example/widget.js';
  s.setAttribute('data-api', 'https://bookrs.your-library.example');
  s.setAttribute('data-source-id', '1');
  s.setAttribute('data-limit', '6');
  var c = document.currentScript;
  if (c && c.nonce) { s.nonce = c.nonce; }
  document.body.appendChild(s);
})();
```

Two details in that snippet are not obvious and were found the hard way:

- **`OPACUserJS` holds JavaScript, not HTML.** Koha wraps its contents
  in `<script>` tags, so a `<script src="...">` placed there becomes
  inert text inside a script block and never runs. Hence creating the
  element in code.
- **Koha serves a nonce-based Content-Security-Policy**, so the loader
  copies the nonce from the block it runs in. Without it, an enforcing
  policy drops the injected script with no visible error.

**If your CSP is enforcing rather than report-only**, `script-src`
must name this service's origin. Koha's default is `script-src 'self'`,
which does not cover another host, and the widget will be blocked
regardless of the nonce.

**Search-page options.** `data-search-path` (default `opac-search.pl`)
identifies the results page, `data-search-param` (default `q`) the query
parameter, `data-noresults` (default `#numresults`) the element carrying
the catalogue's own "no results" message, and `data-results` (default
`#userresults`) the container holding its results. `data-min-score`
(default `0.55`) is the cosine floor for the rescue band; the related
band uses that value plus 0.05.

**A catalogue that searches by POST** — PMB is one — puts the query
nowhere in the URL. Such systems do re-fill their search box with the
term, so `data-query-input` names a selector to read it from
(`input[name=user_query]` for PMB). Without a query from either source
the widget does nothing, which is also how it tells a results page from
a home page on systems where both share a path.

**Options.** `data-source-id` is required when more than one library is
configured, because record identifiers are unique within a source rather
than across sources. `data-limit` sets how many suggestions to show
(default 6), and `data-heading` sets the panel title.

**The default title depends on the data.** A panel headed "Readers also
borrowed" is a claim about this library's patrons, so the widget only
makes it when every suggestion shown is backed by circulation; otherwise
it reads "Related in this catalogue", and the individual suggestions
that borrowing does back are marked. Setting `data-heading` overrides
that judgement and fixes one wording for every panel — reasonable if
your catalogue has circulation throughout, less so if it has none.
Alternatives that stay true in either case: `Similar books`, `You might
also like`.

**Placement.** The widget appends to the first of `#bookrs-recommendations`,
the theme's main content container, or the page body. A library wanting
control over where the panel appears can add an empty
`<div id="bookrs-recommendations">` to their detail template.

### Installing as a Koha plugin instead

`OPACUserJS` is fine for a trial, but it is a system preference a
librarian has to paste JavaScript into. `koha-plugin/` builds a `.kpz`
that installs from the staff interface and is configured from a form:

```bash
tools/build-kpz.sh        # -> dist/koha-plugin-bookrs-widget-v0.1.0.kpz
```

Upload it under Administration → Plugins, then **Configure**: API URL,
source id, suggestions per panel, minimum similarity, optional heading.
The plugin emits the loader through Koha's `opac_js` hook and carries
the per-request CSP nonce, so it keeps working where a policy is
enforced. Clear `OPACUserJS` after installing — otherwise both loaders
run, and only the widget's own guard stops the panel appearing twice.

**A Koha enforcing CSP needs `connect-src` too**, not only `script-src`:
the widget *fetches* from this service. Either add its origin to
`csp_header_value`, or proxy the API under the OPAC's own hostname so
the request is same-origin.

### PMB

PMB has no plugin system and no `OPACUserJS`. The loader goes in the
`biblio_main_header` OPAC parameter, which accepts HTML and renders it
on record pages. PMB is **content-only by design**: its OAI export
carries no item fields and it exposes no circulation API, so neither
availability nor collaborative filtering is possible there. That is a
property of the ILS, not a limitation of this service, and the widget
degrades to content suggestions without announcing an absence. See
[`docs/pmb-setup.md`](docs/pmb-setup.md).

The widget fails silently. If the API is unreachable or the record is
not in the catalogue yet, no panel appears and nothing is logged to the
page — a missing panel is a disappointment, a JavaScript error on a
library's catalogue is a support ticket.

## Keeping it up to date

Four stages need to run again as a library's catalogue and circulation
change. They run on different natural cadences, which is why they are
four commands rather than one.

| Stage | Command | Suggested cadence | Why |
|---|---|---|---|
| Harvest | `docker compose run --rm ingestion python -m bookrs.ingestion.cli` | Nightly | Catalogue edits are frequent and the sync is cheap — only records whose bibliography actually changed are reprocessed |
| Embed | `docker compose run --rm embedding python -m bookrs.embedding.cli` | After each harvest | Only new or changed records are encoded, so this is usually seconds |
| Circulation | `docker compose run --rm ingestion python -m bookrs.ingestion.circulation --source-id N` | Nightly | Loans accumulate daily and the refit is only as current as its last harvest. Koha only: PMB exposes no circulation API. Requires `KOHA_REST_URL`, `KOHA_REST_USER`, `KOHA_REST_PASSWORD` and `BOOKRS_PATRON_SECRET` |
| Refit | `docker compose run --rm recommend python -m bookrs.recommend.cli` | Weekly | A batch job whose output is meaningless in small increments — one new loan does not change the shape of a factor space |

Each is safe to re-run. A harvest over an unchanged catalogue reports
everything unchanged and writes nothing; an embedding run with nothing
stale does nothing.

**No scheduler is included, deliberately.** The schedule belongs on the
library's host, alongside the cron jobs Koha already requires. A cron
daemon inside a container fights Docker's one-process-per-container
model, complicates restart behaviour, and — most importantly — hides its
failures from the monitoring a library already operates. A failed host
cron job reaches the same administrator as every other failed job.

A worked example, adjusting the path and the times:

```cron
# BookRS-Platform — catalogue sync and model refit
30 2 * * *  cd /opt/bookrs && docker compose run --rm ingestion python -m bookrs.ingestion.cli >> /var/log/bookrs-harvest.log 2>&1
45 2 * * *  cd /opt/bookrs && docker compose run --rm embedding  python -m bookrs.embedding.cli  >> /var/log/bookrs-embed.log   2>&1
15 3 * * *  cd /opt/bookrs && docker compose run --rm ingestion python -m bookrs.ingestion.circulation --source-id 1 >> /var/log/bookrs-circulation.log 2>&1
30 3 * * 0  cd /opt/bookrs && docker compose run --rm recommend  python -m bookrs.recommend.cli  >> /var/log/bookrs-refit.log   2>&1
```

**Check that it is still happening.** `GET /health` reports
`last_harvest`, `last_factorised` and `factorised_works`. A refit that
silently stops is the failure worth watching for: recommendations
continue to be served from increasingly stale factors, plausibly and
wrongly, with nothing in the output to suggest the model has not been
rebuilt since the catalogue doubled. Alerting on the age of
`last_factorised` costs nothing and catches it.

Before running the refit for the first time, `--dry-run` reports what
circulation is available without touching anything.

## Running the tests

```bash
docker compose run --rm test python -m pytest
```

The `test` service exists because the suite needs something the services
do not. The OPAC widget is JavaScript, and its tests drive it through a
stub DOM to check what a patron actually sees — which needs a JavaScript
runtime that a Python service has no business shipping. Rather than add
one to a production image, the test environment is its own image.

Running the suite in a service image instead is not an error, but the
widget tests will skip there, and a skipped test is not a passing one.

## License

GNU General Public License v3.0 — matching Koha and the wider
open-source ILS ecosystem this integrates with.

---

<div align="center">
  <strong>BookRS-Platform</strong> · 2026<br>
  RIN SINGH · Institute of Technology of Cambodia
</div>
