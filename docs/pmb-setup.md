# Standing up a PMB instance for harvesting

PMB 7.3.7, self-hosted via Docker, serving OAI-PMH to
`bookrs.ingestion.cli`. This is the third live integration target
alongside the two Koha instances.

Written because none of it is obvious and three parts of it are
outright broken in PMB 7.3.7 — rediscovering this from scratch cost
most of a working session.

## Why self-hosted rather than a public demo

Earlier verification ran against PMB's own public nightly instance. That
was recorded in commit messages but its URL was never written down, and
a server belonging to someone else can change or vanish between
demonstrations. A local instance is controllable and reproducible.

## Bringing it up

```bash
git clone https://github.com/ced42/pmb-docker.git ~/projects/pmb-docker
cd ~/projects/pmb-docker
```

Two edits are needed before the first build.

**Port.** `docker-compose.yml` publishes the web container on `8080`,
which Koha's OPAC already owns. Remap it:

```bash
sed -i 's/8080:80/8090:80/' docker-compose.yml
```

**Dead package mirrors.** The image is `php:7.3-apache`, based on Debian
bullseye. Bullseye's individual `.deb` files have been pruned from the
live mirrors while the package *index* is still served, so `apt update`
succeeds and `apt install wget unzip` then fails with a 404. The
Dockerfile joins its install steps with `;` rather than `&&`, so the
build reports success anyway and the container dies at runtime with
`wget: command not found`.

Point apt at the permanent archive, and drop `debian-security`
entirely — that suite is not mirrored on `archive.debian.org`, and
nothing this build needs is security-only:

```dockerfile
FROM php:7.3-apache

RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list \
 && sed -i '/security.debian.org/d' /etc/apt/sources.list \
 && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt update -y
... (rest unchanged)
```

Then `docker compose up -d`. The web container downloads and extracts
PMB on every start, so a restart takes a few seconds longer than the
others and the container is briefly unready.

## Installing

`http://localhost:8090/pmb/tables/install.php`

- **Charset: utf-8.** Not `iso-8859-1`, which cannot represent Khmer at
  all — the wrong choice here would defeat the point of the project.
- **Database host `db`**, not `localhost`: the installer runs inside the
  web container, where `localhost` means that container.
- User `root`, password `password`, database `pmb`.
- Check **"Insert the operational test case data"** — PMB ships no
  sample catalogue otherwise, and this loads 48 records plus baskets.

Afterwards, Administration → Tools → Database update, and click through
until it reports v5.33. The version the installer leaves behind is
older than the code expects.

## Two broken admin forms

Both accept input, return HTTP 200, redirect normally, and save
nothing. No error appears anywhere — not on the page, not in Apache's
log, not in PHP's. The cause is the same in both: JavaScript that should
reveal a required field never runs, so the form submits an incomplete
payload that the server-side code silently declines.

Confirmed by reading the request payload in DevTools and then reading
PMB's own PHP source. Both are worked around with direct SQL.

**Do not hand-write the serialised config.** PHP's `serialize()` format
prefixes every string with its exact byte length, and `unserialize()`
returns `false` on a mismatch rather than raising — producing exactly
the silent half-save this whole page is about. Generate it with PHP:

```bash
docker exec pmb-docker-web-1 php -r '$c = [...]; echo serialize($c);'
```

### The set

`Administration → Connectors → Sets for the outcoming connectors`.
Selecting a type should reveal a basket picker; it does not.

```sql
INSERT INTO connectors_out_sets
  (connector_out_set_caption, connector_out_set_type, connector_out_set_config)
VALUES
  ('full-catalogue', 1,
   'a:2:{s:16:"included_caddies";a:0:{}s:17:"include_full_base";b:1;}');

INSERT INTO connectors_out_setcaches (connectors_out_setcache_setnum)
VALUES (LAST_INSERT_ID());
```

`include_full_base = true` exports the whole catalogue. To export one
basket instead, set it false and list the basket's `idcaddie` in
`included_caddies` — PMB's own code empties that array whenever
full-base is on, so mirror that rather than leaving a stale value.

The cache starts empty. Populate it from the UI's "Manual update"
button, or directly:

```
http://localhost:8090/pmb/admin.php?categ=connecteurs&sub=out_sets&action=manual_update&id=1
```

### The OAI source

`Administration → Connectors → Outcoming connectors → Connecteur qui
exporte un entrepot OAI`. Same failure.

Connector number `3` is the OAI connector — taken from PMB's own
`clean_out_oai_deleted_records()`, which queries
`connectors_out_sources_connectornum = 3`.

```sql
INSERT INTO connectors_out_sources
  (connectors_out_sources_connectornum, connectors_out_source_name,
   connectors_out_source_comment, connectors_out_source_config)
VALUES (3, 'BookRS export', '', '<serialized config>');
```

The config is a 17-key array; generate it with PHP. The keys come from
`oai_source::update_config_from_form()` in
`admin/connecteurs/out/oai/oai.class.php`. `included_sets` holds the set
IDs to export — `[1]` for the set created above. `baseURL` can be left
empty; PMB computes it from the source ID when blank.

## The endpoint

```
http://localhost:8090/pmb/ws/connector_out.php?source_id=1
```

Note it is a **query string**, not a path — unlike Koha's
`/cgi-bin/koha/oai.pl`. The harvester appends `&verb=...` correctly, but
anything constructing URLs by hand needs to account for the existing
`?`.

### oai_dc is broken; use pmb_xml_unimarc

`ListMetadataFormats` advertises both `oai_dc` and `pmb_xml_unimarc`.
Only the second works — `oai_dc` returns **HTTP 500 with a zero-byte
body** on every request. `Identify`, `ListSets` and
`ListMetadataFormats` all work, so the fault is specific to Dublin Core
record rendering.

Not worth chasing: `pmb.py` parses UNIMARC, so the working format is the
one the pipeline wants anyway. Recorded here so the 500 is recognised
rather than re-diagnosed.

## Harvesting

```bash
cd ~/projects/BookRS-Platform
docker compose run --rm ingestion python -m bookrs.ingestion.cli \
  --url "http://host.docker.internal:8090/pmb/ws/connector_out.php?source_id=1" \
  --name pmb-test \
  --prefix pmb_xml_unimarc
```

`host.docker.internal` because the ingestion container reaches PMB
through the host, as with Koha.

Flavour detection correctly reports `UNIMARC` without help, despite
PMB's non-standard `<f c="200">` field encoding — `pmb.py` normalises it
before detection. Items are absent (`995 fields=0`): the sample
catalogue carries no holdings, and `include_items` is off in the source
config.

A second harvest reports the unchanged records as unchanged, so the
two-hash change detection works against PMB as it does against Koha.

---

## Circulation: what PMB exposes, and what it does not

BookRS-Platform harvests cataloguing from PMB and circulation from Koha only.
That asymmetry is PMB's configuration rather than a gap in the integration, and
this section records how that was established — by reading what the running
instance publishes, not by assuming PMB behaves like Koha.

### Where the answer lives

PMB ships eight out-connectors under `admin/connecteurs/out/`. Six are
bibliographic export formats; two are general-purpose APIs:

* `apijsonrpc` — JSON-RPC over `ws/connector_out.php?source_id=N`
* `apisoap` — SOAP, same endpoint

Neither defines any method itself. Both are transports: the method inventory
comes from `es_catalog`, which parses `external_services/catalog.xml` and loads
one service group per registered `<item>`. The connector's configuration form
then lets an administrator pick which of those methods a given source exports.

Two consequences follow. First, whatever PMB can expose is enumerated in that
one XML file. Second, exposure is opt-in per function and per source — unlike
Koha's REST API, which answers under Basic auth once enabled, a PMB
installation publishes nothing until an administrator selects it.

### What is registered

`external_services/catalog.xml` registers 39 groups. `pmbesEmpr` (id 30) is
among them, and its manifest publishes full borrower management:

    fetch_empr · create_empr · update_empr · delete_empr · empr_list
    statut_list · categ_list · codestat_list · groupe_list · abt_list …

So PMB does publish patron data over JSON-RPC, read and write.

### What is not

Three groups are present on disk, complete with manifests, and commented out in
the catalogue:

```xml
<!-- <item name="pmbesResas" id="24"/>
<item name="pmbesLoans" id="25"/>
<item name="pmbesReaders" id="26"/> -->
```

`external_services/pmbesLoans/` contains `pmbesLoans.class.php`, localised
messages and a manifest declaring `listLoansReaders`, `listLoansGroups`,
`filterLoansReaders` and `exportCSV`. None of it is reachable: `es_catalog`
never loads the group, so its methods never appear in the connector's
exported-functions list, so no source can enable them.

The catalogue file carries `$Id: catalog.xml,v 1.21.6.1 2020/04/22`. Whether
the three groups were deprecated deliberately or left mid-migration is not
determinable from the shipped files. What is determinable is the effective
behaviour: **loans, reservations and reader records are not exposed by this
build of PMB.**

### Why the platform does not request borrower records either

`pmbesEmpr` is available, so patron identities could be harvested. They are
not, deliberately. A collaborative recommender needs interactions, not
identities — borrower records without loans produce no signal at all. Fetching
them would mean holding personal data the system has no use for, which
contradicts the data-minimisation position the rest of the platform takes
(circulation from Koha is pseudonymised at ingestion precisely so that
identities are never stored).

### The resulting claim

> Cataloguing integrates with Koha and PMB. Circulation integrates with Koha
> only, because PMB ships its loan services disabled at
> `external_services/catalog.xml` ids 24, 25 and 26. Borrower services are
> available but not used, since identities without interactions carry no
> recommendation signal and would be personal data held without purpose.

### What would change this

Two things, neither attempted:

1. **A newer PMB.** This instance is a 2020-era build. If current PMB
   re-registers those groups, the finding is version-specific rather than
   permanent. Checking that means reading upstream's `catalog.xml`, not this
   container.
2. **Uncommenting them locally.** Technically possible and deliberately not
   done. Editing a library's ILS to obtain data is the opposite of the
   read-only, unmodified-infrastructure position the platform is built on, and
   a result obtained that way would not describe what a real PMB library could
   offer.

---

## Installing the OPAC widget

Koha publishes a system preference, `OPACUserJS`, whose entire purpose is
injecting a library's own JavaScript into every OPAC page. PMB has no
equivalent, and the reasonable first conclusion is that reaching PMB's patron
interface means editing its templates — which the read-only,
unmodified-infrastructure position of this project forbids.

It doesn't. PMB has a parameter that does the same job, and finding it took
enough probing to be worth recording.

### What works

`biblio_main_header`, in PMB's `parametres` table under `type_param='opac'`,
section `b_aff_general`. It holds HTML, it renders on record detail pages as
well as the main page, and a `<script>` tag placed inside it executes.

It is set through PMB's own administrative interface — Administration,
Parameters, OPAC — so installing the widget is configuration in exactly the
sense it is on Koha. No PMB file is modified.

The value shipped by default is `<h3>Des services pour PMB</h3>`, which renders
as a caption over the header images. Anything added is appended to that, so a
real library keeps whatever they already have there and adds the snippet after
it:

```html
<h3>Des services pour PMB</h3>
<script src="http://bookrs.library.example/widget.js"
        data-api="http://bookrs.library.example"
        data-source-id="10"
        data-record-param="id"
        data-record-prefix="oai:PMBTEST:"
        data-record-url="/pmb/opac_css/index.php?lvl=notice_display&id={id}"
        data-mount="#main_hors_footer"
        data-limit="6"></script>
```

Four of those attributes exist because PMB differs from Koha, and each defaults
to Koha's value so an existing Koha installation is untouched:

| Attribute | Koha default | PMB |
|---|---|---|
| `data-record-param` | `biblionumber` | `id` |
| `data-record-prefix` | *(empty)* | the installation's OAI identifier prefix |
| `data-record-url` | `/cgi-bin/koha/opac-detail.pl?biblionumber={id}` | `/pmb/opac_css/index.php?lvl=notice_display&id={id}` |
| `data-mount` | *(theme fallback chain)* | `#main_hors_footer` |

`data-record-prefix` is not a constant. `oai:PMBTEST:` is what this development
instance emits; a real installation sets its own OAI archive identifier, and
the prefix must match what `works.source_record_id` actually holds. Check it:

```sql
SELECT source_record_id FROM works WHERE source_id = <n> LIMIT 1;
```

`#main_hors_footer` — "main without footer" — is the container holding the page
content but excluding the footer. Mounting on `#main` instead puts the panel
below "Mentions légales", which is where it first appeared.

### What does not work, and why it looks like it should

`script_analytics` is the obvious candidate. Its description reads *"Code
Javascript d'analyse d'audience (Par exemple pour Google Analytics, XiTi,..)"*,
which is precisely the right shape: a free-text JavaScript field applied to
OPAC pages.

Setting it produces nothing in the rendered page. The reason is in
`opac_css/includes/javascript/script_analytics.js`: the value is wrapped in a
cookie-consent mechanism, injected only after a visitor consents, via an
element with id `script_analytics`.

Two reasons not to pursue it even if the consent path were enabled.
Recommendations are not audience analytics, and putting them behind a tracking
consent prompt misrepresents what they are. And a mechanism that depends on
visitor consent means the panel appears for some patrons and not others, which
is worse than not having it.

### Other requirements

**CORS.** The platform's `BOOKRS_ALLOWED_ORIGINS` must include PMB's origin.
A missing origin fails entirely in the browser with no server-side trace: the
API answers normally and the browser discards the response, so the panel simply
never appears.

**Source id.** `data-source-id` must be PMB's source, not another library's.
The widget looks the record up by `source_record_id` scoped to that source, and
`/works/{id}/similar` returns neighbours from the querying work's own catalogue
— so a wrong source id produces a panel of another library's books under a
footer reading "from this library's own catalogue".

**No availability line.** PMB's OAI export carries no item fields, so the
platform holds no holdings data for PMB records and the widget renders no
status line for them. That is deliberate: reporting "No copies" would assert
something PMB never published. Koha records, whose export does carry items,
still show availability.

### Verifying

```bash
# the snippet is in the page
curl -s 'http://localhost:8090/pmb/opac_css/index.php?lvl=notice_display&id=1' \
  | grep -o 'widget.js'

# the API permits PMB's origin
curl -s -H 'Origin: http://localhost:8090' -D- -o /dev/null \
  'http://localhost:8000/works/<pmb-work-id>/similar?limit=6' \
  | grep -i access-control
```

Both passing and the panel still absent means the browser blocked or errored on
something else; the console will say. The widget fails silently by design — a
panel that does not appear is a disappointment, a JavaScript error on a
library's catalogue page is a support ticket.

One thing to disable when demonstrating: browser translation. Chrome offers to
translate a French OPAC into English, which turns *Bac en poche* into *High
school diploma in hand* and makes it look as though the platform is translating
records. It isn't.
