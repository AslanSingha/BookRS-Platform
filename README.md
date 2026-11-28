# BookRS-Platform

> Book recommendations inside a Koha library's own OPAC, learned from
> Koha's own data, which the platform only reads.

**Status: working on a test library, not yet piloted.** A
koha-testing-docker Koha holds the UCSD Goodreads catalogue used in the
thesis: the 2,000-book slice with 446,664 readers and 16.3 million
ratings, with the full 883,468-book catalogue being loaded. A patron
logged in to the OPAC gets recommendations from their own ratings, search
by meaning, and similar books on every record page; a new patron's
ratings change what they are shown within minutes. See
[Evaluation](#evaluation) for what is measured.

---

## What this is

BookRS-Platform is **software a library self-hosts beside its Koha**. It
reads Koha's database, learns from it, and puts the results into Koha's
own OPAC through a Koha plugin. Nothing leaves the library.

**It only reads.** Every statement it sends to Koha's database is a
SELECT, and a deployment gives it an account with SELECT on the tables it
reads and nothing else:

| Koha table | What is read |
|---|---|
| `biblio`, `biblioitems` | each book: title, author, description, ISBN, year, publisher |
| `items` | copies, availability, collection (the genre, in the test library) |
| `ratings` | each patron's star ratings, given in the OPAC |
| `reviews` | only whether a patron also wrote a review (the δ in the confidence) |
| `issues`, `old_issues` | loans, optionally (`koha_db --loans`) |

Patrons are never stored by number: each borrower number is replaced by
a keyed hash (`bookrs/ingestion/patrons.py`).

## Prior work

Building on **[BookRS-System](https://github.com/AslanSingha/BookRS-System)**,
RIN SINGH's Engineering thesis at the Institute of Technology of
Cambodia (2026), supervised by M. SOK Kimheng. That system combined
SBERT semantic embeddings with ALS collaborative filtering, behind a
Vue prototype.

BookRS-Platform is a **separate codebase**, with its own schema,
integration layer and deployment model. The thesis repository is
reference only.

## Differences from BookRS-System

| | BookRS-System | BookRS-Platform |
|---|---|---|
| Data source | Static research dataset | Koha's database, read-only |
| Interface | Vue prototype | Koha's own OPAC, through a plugin |
| Schema | Books table | Works / items separation |
| Signal | Ratings, c = 1 + 2r + 3δ | The same ratings, read from Koha |
| New readers | Four stages | Three stages, chosen by measurement |
| Embedding model | English-only | Multilingual |
| Updating | Manual | A scheduler: read, embed, retrain on set cadences |

**Works and items.** A `works` record is the book (title, authors,
description, one embedding); `items` are its physical copies (branch,
availability, collection). Several copies of a book are real inventory,
not duplicates to merge.

**Readers' ratings reorder; they do not retrieve.** Similar books are
drawn by meaning, then reordered by ALS (0.7 meaning, 0.3 ALS). A book
the embeddings did not consider related cannot be promoted by ratings
alone. Factors resting on very few readers are excluded, and where too
few candidates carry any, the ratings layer abstains.

## What runs today

```
bookrs/ingestion    Koha's database, read-only → works, items, ratings, review flags
bookrs/db           the platform's schema and its migrations
bookrs/embedding    multilingual encoding → 384-dimensional vectors
bookrs/recommend    confidence weighting → ALS → factors; folding-in per patron
bookrs/evaluation   the thesis's protocol, cold start, generated readers, tuning
bookrs/api          recommendations, search by meaning, similar books, health
bookrs/schedule.py  read, embed and retrain on cadences set in .env
koha-plugin/        the OPAC pages, and the patron's routes inside Koha
```

Docker services: `db`, `ingestion`, `embedding`, `recommend`, `api`; the
scheduler's `schedule-read`, `schedule-embed`, `schedule-refit` (profile
`schedule`); `test`; and `fixture` (profile `fixture`, the only one that
writes to Koha: it loads the UCSD test data).

**Endpoints**

| | |
|---|---|
| `GET /patrons/me/recommendations` | a patron's list, asked by Koha's plugin on their behalf |
| `GET /patrons/me/search` | a patron's search by meaning, reordered by their ratings |
| `GET /works/{id}/similar` | similar books, with the signal behind each |
| `GET /works/popular` | the most-rated books |
| `GET /search/semantic` | free-text search by meaning, with a score floor |
| `GET /search/exact` | ISBN, title or author lookup |
| `GET /works/{id}` | a single record with availability |
| `GET /works/by-record-id/{id}` | resolve Koha's biblionumber |
| `GET /widget.js` | the OPAC script |
| `GET /sources` | per-library counts: books read, embedded, factorised |
| `GET /health` | totals, the vectors held, and each scheduled job's last run |

## Not yet built

- **The thesis's other homepage sections:** Trending Now, Because you
  rated, Because you searched, Readers like you, Popular in your
  favourite genre; and browsing by genre.
- **New books nobody has rated** get no ALS signal; only content can
  place them. Not yet evaluated.
- **One book under two titles** (*Philosopher's Stone* / *Sorcerer's
  Stone*) still appears twice: editions are grouped by title and first
  author.
- **Retraining is not seeded**, so lists shift slightly between refits.
- **Production hardening**: rate limiting and observability wait for a
  pilot library.

## Evaluation

Measured on the 2,000-book slice read from Koha (446,664 readers):

| Evaluation | Result |
|---|---|
| The thesis's protocol (80/20 per reader) | P@10 **0.2225** on the thesis's 2,000 readers, against 0.2259 reported; 0.1988 over all readers; 2.2× the most-rated baseline |
| Cold start (readers withheld, 1–20 ratings revealed) | Full folding-in 0.179 at 20 ratings, against 0.090 for the thesis's version |
| Generated readers with a known taste | ALS recommends books the reader would love 91% of the time with no noise, 70% at 50% noise |
| Hyperparameter sweep | The thesis's k = 128, λ = 0.1, 10 iterations, c = 1 + 2r + 3δ confirmed |

The held-out protocol is strict: a book the reader would love but never
rated counts as wrong, so even perfect knowledge of every reader's taste
scores only 0.29 on it. `bookrs.evaluation.revised` therefore scores the
same model several ways, each beside its limit:

1. held-out books, liked = rated 4 or 5 (the main score), with the
   ceiling P@10 is capped by, truncated recall and MAP@10;
2. held-out books, any rating (the thesis's definition);
3. leave one out: one liked book per reader, not capped by how many a
   reader has held out;
4. genre match: the share of the top 10 in the reader's own genres,
   against a random list;
5. beyond accuracy: catalogue coverage, recommendations outside the
   most-rated tenth, novelty, diversity within a list;
6. similar books: the share of a book's ten most similar that share its
   genre, by meaning, by ALS and by the API's blend.

It runs on the thesis's slice or on everything the library holds:

```bash
# the 2,000-book slice the thesis and the report used
docker compose run --rm -T -v "$PWD/data/ucsd:/data" recommend python -m bookrs.evaluation.revised \
  --source-id 54 --slice /data/koha_biblios.tsv --json /data/revised-slice.json
# everything the library holds (a sample of 20,000 readers, seeded)
docker compose run --rm -T -v "$PWD/data/ucsd:/data" recommend python -m bookrs.evaluation.revised \
  --source-id 54 --json /data/revised-full.json
# the thesis's own protocol and numbers, reproduced
docker compose run --rm -T -v "$PWD/data/ucsd:/data:ro" recommend python -m bookrs.evaluation.thesis \
  --source-id 54 --slice /data/koha_biblios.tsv \
  --thesis-readers /data/thesis_eval_users.txt --borrower-map /data/koha_borrowers.tsv
```

## Running it

Koha first: [`docs/koha-setup.md`](docs/koha-setup.md) starts the
instance, installs the plugin and checks everything with
`tools/demo.sh check`. Then the platform:

```bash
git clone https://github.com/AslanSingha/BookRS-Platform.git
cd BookRS-Platform
cp .env.example .env     # KOHA_DB_URL, BOOKRS_SOURCE_ID, secrets, cadences
docker compose up -d db api
docker compose --profile schedule up -d
```

The scheduler reads Koha, embeds and retrains on its own (see
[Keeping it up to date](#keeping-it-up-to-date)). The embedding service
downloads its model on first run and caches it in a named volume.

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
page. The defaults are Koha's.

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

Without a query in the URL (or, with `data-query-input`, in a search
box) the widget does nothing on a search page.

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

**Recommendations for the logged-in patron (plugin 0.2.0).** The plugin
adds one route to Koha's own REST API,
`/api/v1/contrib/bookrs/recommendations`. Koha decides who is asking from
the patron's OPAC session; the plugin turns the borrower number into the
keyed reference BookRS-Platform stores (HMAC-SHA256 with
`BOOKRS_PATRON_SECRET`, as `bookrs/ingestion/circulation.py` computes it)
and asks this service server to server, with `BOOKRS_SERVICE_TOKEN`. The
borrower number never leaves Koha and no browser holds a patron
reference. Configure three fields on the plugin's form -- the API URL as
Koha's server reaches it, the service token, the patron secret -- and
restart Plack so Koha registers the route
(`koha-plack --restart kohadev`). `koha-plugin/t/controller.pl` tests
the route without Koha; `tests/test_koha_plugin.py` runs it against this
API.

**A Koha enforcing CSP needs `connect-src` too**, not only `script-src`:
the widget *fetches* from this service. Either add its origin to
`csp_header_value`, or proxy the API under the OPAC's own hostname so
the request is same-origin.

The widget fails silently. If the API is unreachable or the record is
not in the catalogue yet, no panel appears and nothing is logged to the
page — a missing panel is a disappointment, a JavaScript error on a
library's catalogue is a support ticket.

## Keeping it up to date

Three jobs keep the platform in step with the library. They run on
different natural cadences, which is why they are three jobs rather than
one.

| Job | Commands | Suggested cadence | Why |
|---|---|---|---|
| `read` | `python -m bookrs.ingestion.koha_db --source-id N --changes --full-every 1d`, then `python -m bookrs.ingestion.direct --source-id N --changes --full-every 1d` (ingestion image; needs `KOHA_DB_URL` and `BOOKRS_PATRON_SECRET`) | Every 10 minutes | Reads the catalogue records, then the ratings and review flags, changed since the last read, from Koha's database, read-only: seconds. Once a day each reads everything instead (12 minutes for 16 million ratings), which is the only read that sees a record, rating or review that was removed. A patron's new rating counts for their own recommendations as soon as it is read: their place in the model is computed from their ratings at each request |
| `embed` | `python -m bookrs.embedding.cli --source-id N` (embedding image) | After each read | Only new or changed records are encoded, so this is usually seconds |
| `refit` | `python -m bookrs.recommend.cli --source-id N --signal ratings --factors 128 --iterations 10` (recommend image) | Daily or weekly | Relearns every book's factors from all ratings: everyone's results shift, including patrons who did nothing. One new rating does not change the shape of a factor space, so this needs a slower cadence than the read |

Each is safe to re-run: a read of an unchanged library writes nothing,
and an embedding run with nothing stale does nothing.

### The built-in scheduler

`bookrs/schedule.py` runs each job on a cadence the library sets: a
whole number and a unit (`30m`, `6h`, `1d`, `2w`), or `off`. It is
optional and starts only with its compose profile. Set the library's
source and the cadences in `.env`:

```bash
BOOKRS_SOURCE_ID=54
KOHA_DB_URL=mysql://<read-only user>:<password>@<koha db host>:3306/<koha db>
BOOKRS_READ_EVERY=10m
BOOKRS_FULL_READ_EVERY=1d
BOOKRS_EMBED_EVERY=1h
BOOKRS_REFIT_EVERY=1d
```

Between full reads, `read` asks Koha only for what changed since the
last read, by Koha's own clock (`bookrs/ingestion/direct.py` explains
the margin and what only a full read can see). A read of changes that
finds more than 200,000 changed ratings, as after a bulk load, reads
everything instead.

then start, follow and stop it:

```bash
docker compose --profile schedule up -d
docker compose --profile schedule logs -f schedule-read schedule-embed schedule-refit
docker compose --profile schedule stop schedule-read schedule-embed schedule-refit
```

Each job runs as one plain process in the image that carries its
libraries; no daemon is installed in any image. Every run is recorded in
the `job_runs` table. A job is due one cadence after its last run
started, or within an hour after a run that failed or was interrupted,
so restarting a container does not run everything again and a failed
weekly refit is retried the same day. After changing a cadence in
`.env`, run `docker compose --profile schedule up -d` again.

To run a job now rather than wait for it, in its running container:

```bash
docker compose exec schedule-read  python -m bookrs.schedule read  --now
docker compose exec schedule-refit python -m bookrs.schedule refit --now
```

A run by hand is recorded like any other, so the next scheduled run is
one cadence after it. Two runs of one job never overlap: `--now` waits
for a run in progress to finish first, and a scheduled run that finds
one started by hand looks again a minute later.

### The full UCSD catalogue (fixture)

The test library starts as the thesis's 2,000-book slice. `tools/ucsd_full.py`
extends it, in place, to the thesis's whole catalogue after its entity
resolution: 883,468 books, 666,085 readers and the 29.9 million ratings on
those books. What is already in Koha stays; only what is new is added, and
every count is checked. It runs in the `fixture` container, the only one
whose work writes to Koha. Set where the sources are, in `.env` (absolute
paths; both are mounted read-only):

```bash
UCSD_DATAPREP_DIR=/home/<you>/projects/BookRS-DataPrep/outputs
UCSD_THESIS_DATA_DIR=/home/<you>/projects/BookRS-System/data
```

```bash
docker compose --profile schedule stop schedule-read schedule-embed schedule-refit
docker compose --profile fixture build fixture
docker compose --profile fixture run --rm fixture python tools/ucsd_full.py prepare
docker compose --profile fixture run --rm fixture python tools/ucsd_full.py marc

# Import each chunk into Koha without indexing; one full reindex at the end.
for f in data/ucsd-full/marc/ucsd-full-*.mrc; do
  n=$(basename "$f" .mrc); [ -f "data/ucsd-full/marc/$n.done" ] && continue
  echo "== $n $(date +%T)"
  docker cp "$f" "kohadev-koha-1:/kohadevbox/$n.mrc" &&
  docker exec kohadev-koha-1 koha-shell kohadev -c \
    "perl /kohadevbox/koha/misc/migration_tools/bulkmarcimport.pl -b -file /kohadevbox/$n.mrc --commit 1000 -si" \
    > "data/ucsd-full/marc/$n.log" 2>&1 &&
  docker exec kohadev-koha-1 rm -f "/kohadevbox/$n.mrc" && touch "data/ucsd-full/marc/$n.done" ||
  { echo "stopped at $n: see data/ucsd-full/marc/$n.log"; break; }
done
docker exec kohadev-koha-1 koha-rebuild-zebra -b -f -v kohadev

docker compose --profile fixture run --rm fixture python tools/ucsd_full.py load --check
docker compose --profile fixture run --rm fixture python tools/ucsd_full.py load
```

Then the platform reads it. Start the scheduler and run each job once
now, one after the other. Expect the read to take about half an hour
(the 881,545 new records, then every rating, since 13.9 million changed
at once), the embedding a little over an hour (about 200 books a second),
and the refit some minutes.

```bash
docker compose --profile schedule up -d
docker compose exec schedule-read  python -m bookrs.schedule read  --now
docker compose exec schedule-embed python -m bookrs.schedule embed --now
docker compose exec schedule-refit python -m bookrs.schedule refit --now
```

The API needs no restart: it takes in the new books while they are
embedded (see "How quickly the API sees a change" below).

Never pass `-d` to `bulkmarcimport.pl`: it deletes every record in Koha
first. A chunk that stopped part-way is not imported again (its first
records would be doubled): `ucsd_full.py marc --missing-only` writes the
books Koha does not hold yet into `data/ucsd-full/marc-missing/`, and those
are imported the same way. `load --undo` removes the borrowers, ratings
and review flags `load` added; the records stay.

### Seeing it update

What a patron sees changes as the library's data does, and nothing has
to be restarted. With the scheduler running:

1. In Koha's staff interface, add a patron (Patrons → New patron) with
   an OPAC username and password.
2. Log in to the OPAC as them. The homepage shows **Popular in this
   library**, with the note that they have not rated any books yet.
3. Rate three books with the stars on their record pages.
4. Read them: `docker compose exec schedule-read python -m bookrs.schedule read --now`,
   or wait for the next scheduled read. The log reports `3 ratings given
   or changed`.
5. Reload the homepage: **Recommended for you**, *based on your 3
   ratings so far*. No refit happened: a patron's place among the books
   is computed from their ratings at each request.
6. Rate two more and read again: the list changes, and from five
   ratings the note reads *Based on your 5 ratings*.
7. Retrain: `docker compose exec schedule-refit python -m bookrs.schedule refit --now`.
   Every book's factors are relearned from all ratings, the new
   patron's included. The API picks up the new factors within 30
   seconds, and `/health` shows the run under `jobs`.

`tools/demo.sh` does the terminal side of this (`check`, `find`,
`patron`, `list`, `read`, `refit`, `search`), and `docs/demo.md` is the
run sheet for showing it.

### How quickly the API sees a change

The API holds every book's embedding in memory, and the factors, to
search and compare with: about 1.4 GB for the full UCSD catalogue. It
asks the database what changed at most once every
`BOOKRS_CACHE_PROBE_SECONDS` (30 by default), and takes in only that:

- books embedded since its last look are added, or overwritten where
  they are, so it keeps up during an embedding run;
- books withdrawn from the catalogue are left out, and put back if they
  return;
- a refit's factors replace the old ones.

It reloads every embedding only when rows have left the embeddings
table, when the vectors change size (a new model), and once every
`BOOKRS_VECTORS_FULL_RELOAD_HOURS` (24; 0 for never) as a safety net.
That reload runs in the background while the vectors already held keep
answering, and needs about twice the memory for that minute.
`/health` shows `vectors_held` beside `embeddings`, and
`vectors_reloading`.

### Or the host's cron

A library that already monitors its cron jobs can schedule the same
commands there instead, and leave the profile unstarted. Adjust the path,
the source and the times:

```cron
# BookRS-Platform: read Koha, embed, refit
*/10 * * * * cd /opt/bookrs && docker compose run --rm -T ingestion sh -c 'python -m bookrs.ingestion.koha_db --source-id 54 --changes --full-every 1d && python -m bookrs.ingestion.direct --source-id 54 --changes --full-every 1d' >> /var/log/bookrs-read.log 2>&1
35 * * * *  cd /opt/bookrs && docker compose run --rm -T embedding python -m bookrs.embedding.cli --source-id 54 >> /var/log/bookrs-embed.log 2>&1
30 3 * * *  cd /opt/bookrs && docker compose run --rm -T recommend python -m bookrs.recommend.cli --source-id 54 --signal ratings --factors 128 --iterations 10 >> /var/log/bookrs-refit.log 2>&1
```

`KOHA_DB_URL` and `BOOKRS_PATRON_SECRET` must then be in `.env` (or the
crontab's environment) for the read.

### Check that it is still happening

`GET /health` reports `last_factorised` and `factorised_works`, and under
`jobs` the built-in scheduler's last run of each job: when it started
and finished, whether it succeeded, and when it is next due. A refit
that silently stops is the failure worth watching for: recommendations
continue to be served from increasingly stale factors, plausibly and
wrongly, with nothing in the output to suggest the model has not been
rebuilt since the catalogue doubled. Alerting on the age of
`last_factorised` costs nothing and catches it.

Before running the refit for the first time, `--dry-run` reports what it
would learn from without touching anything.

## Running the tests

```bash
docker compose run --rm test python -m pytest
```

The `test` service exists because the suite needs something the services
do not. The OPAC widget is JavaScript, and its tests drive it through a
stub DOM to check what a patron actually sees — which needs a JavaScript
runtime that a Python service has no business shipping. Rather than add
one to a production image, the test environment is its own image.

The image carries the API's packages and Perl's Mojolicious too, so the
API's tests and the Koha plugin's run there as well: nothing should be
reported skipped. `python -m pytest -rs` names any test that is, and why.

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
