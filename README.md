# BookRS-Platform

> A recommendation service for libraries running open-source ILS
> platforms — Koha, PMB, and other MARC / OAI-PMH-compliant systems.

**Status: working, not yet piloted.** Catalogue ingestion, embedding,
search and the OPAC widget run end to end against live Koha instances in
both MARC flavours — a patron viewing a record sees related books from
the library's own holdings. No library has deployed it yet, so the
collaborative-filtering half is still unbuilt: it needs circulation data
that only a real deployment produces.

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
| Signals | Explicit ratings | Circulation signals, then ratings |
| Record format | Normalised CSV | MARC21 and UNIMARC |
| Embedding model | English-only | Multilingual |
| Deployment | Local development | Docker Compose, self-hosted |

**Bibliographic / item separation.** Real library data needs two layers:
a `works` record (the intellectual work — title, authors, subject
headings, one embedding) and `items` records (physical copies — branch,
call number, availability). Multiple copies of a book are real
inventory, not duplicates to merge.

**Two sources of preference signal.** Circulation records that a book
was borrowed, not whether it was enjoyed, so implicit signals are
weighted through an extended confidence formula. Koha additionally
provides a native patron star rating feature, enabled by default. How
much rating data exists varies by deployment and is measured at setup
rather than assumed.

**Graceful degradation on sparse metadata.** Roughly a third of a real
catalogue carries nothing beyond a title. The content-based layer is
designed around that constraint rather than assuming rich descriptions.

## What runs today

```
bookrs/ingestion    OAI-PMH harvest → MARC21/UNIMARC → normalised works
bookrs/db           works, items, embeddings, ratings + loader
bookrs/embedding    multilingual encoding → 384-dimensional vectors
bookrs/api          search, similarity, availability, health
```

Four Docker services: `db`, `ingestion`, `embedding`, `api`.

**Endpoints**

| | |
|---|---|
| `GET /works/{id}/similar` | recommendations for a given work |
| `GET /search/exact` | ISBN, title or author lookup |
| `GET /works/{id}` | a single record with availability |
| `GET /works/by-record-id/{id}` | resolve a library's own biblionumber |
| `GET /widget.js` | the OPAC widget |
| `GET /health` | catalogue size, embedding coverage, sync freshness |

**Measured against a 5,285-work catalogue** harvested from two live Koha
instances (436 MARC21, 4,849 UNIMARC):

| | |
|---|---|
| Harvest | ~10 s for 436 records, ~75 s for 4,849 |
| Embedding | 90–175 works/sec on 6 CPU cores, first run only |
| Similarity query | 8–12 ms |
| Exact search | 4–22 ms |

Extrapolating to a 300,000-record catalogue: roughly 30–55 minutes for
the initial embedding run, once. Subsequent syncs touch only records
whose bibliography actually changed.

## Not yet built

**Collaborative filtering.** ALS needs `(patron, work, confidence)`
triples. OAI-PMH carries no patron data by design, and item-level
checkout totals are aggregate — "borrowed 47 times" cannot be
factorised. Both patron ratings and circulation history exist in Koha
and are reachable over its REST API; neither is harvested yet, and
neither can be meaningfully tested without a library that has some.

**Semantic search over free text.** Requires encoding the query, which
means loading the embedding model into the public-facing service. The
stored-vector endpoints above need no model, so this is a deliberate
separate decision rather than an oversight.

**A live PMB harvest.** The translation shim for PMB's
`pmb_xml_unimarc` format is implemented and tested, and the format has
been verified against PMB's current nightly build — its XML output is
unchanged since 2013. What has not happened is harvesting a running PMB
instance end to end.

**Production hardening** — authentication, rate limiting, observability
— which waits for a concrete pilot rather than being built
speculatively.

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

The widget renders a panel of related books on a record's detail page,
with availability drawn from the library's own holdings.

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

**Options.** `data-source-id` is required when more than one library is
configured, because record identifiers are unique within a source rather
than across sources. `data-limit` sets how many suggestions to show
(default 6), and `data-heading` changes the panel title (default
"Readers also borrowed").

**Placement.** The widget appends to the first of `#bookrs-recommendations`,
the theme's main content container, or the page body. A library wanting
control over where the panel appears can add an empty
`<div id="bookrs-recommendations">` to their detail template.

The widget fails silently. If the API is unreachable or the record is
not in the catalogue yet, no panel appears and nothing is logged to the
page — a missing panel is a disappointment, a JavaScript error on a
library's catalogue is a support ticket.

## License

GNU General Public License v3.0 — matching Koha and the wider
open-source ILS ecosystem this integrates with.

---

<div align="center">
  <strong>BookRS-Platform</strong> · 2026<br>
  RIN SINGH · Institute of Technology of Cambodia
</div>
