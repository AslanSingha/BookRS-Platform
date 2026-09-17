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
