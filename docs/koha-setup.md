# Koha, running it for BookRS-Platform

Two Koha instances back this project: a MARC21 instance on `localhost:8080`
and a UNIMARC instance on `unimarc.localhost`, both from
[koha-testing-docker](https://gitlab.com/koha-community/koha-testing-docker)
(ktd). This file records how to run them and the defects that cost time.

`scripts/provision-koha.sh` does the setup. This document explains what it
does and what to do when it, or ktd, behaves unexpectedly.

---

## Bringing an instance up

MARC21, the primary instance:

```bash
cd ~/projects/BookRS-Platform
scripts/provision-koha.sh
```

UNIMARC, in ktd's proxy mode so both can run at once:

```bash
ktd_proxy --start
KOHA_INSTANCE=unimarc OPAC_URL=http://unimarc.localhost \
  KTD_ARGS="--proxy --name unimarc" scripts/provision-koha.sh
```

The script brings the containers up, waits for the instance to be created,
applies the OAI system preferences, installs the OPAC widget, writes
`oaiconf.yaml`, and verifies the endpoint answers with both `marc21` and
`marcxml`.

## Bringing one down

There is no safe stop. `ktd down` removes the containers *and the volumes*,
so the database is recreated from scratch on the next `ktd up` — schema
reinstalled, sample data reloaded, the instance back to ktd's defaults.

If there is state worth keeping, dump it first, and confirm the database
container is actually running before trusting the dump:

```bash
docker ps --filter 'name=kohadev-db-1' --format '{{.Names}}\t{{.Status}}'
docker exec kohadev-db-1 mariadb-dump -uroot -ppassword koha_kohadev \
  > ~/koha-kohadev-$(date +%F).sql
ls -lh ~/koha-kohadev-*.sql
```

Reprovisioning is cheap — `scripts/provision-koha.sh` exists precisely so a
lost instance costs one script run — so a dump is insurance, not a
requirement.

---

## Defects and workarounds

### `docker start` on a stopped ktd container always fails

**Symptom.** The container exits within a minute of `docker start`, with
exit code 1 or 255. `docker logs` ends at "User kohadev-koha already
exists."

**Cause.** ktd's entrypoint replays the whole provisioning sequence on every
container start, and that sequence is not idempotent. `koha-create` finds
the instance already present, returns non-zero, and the container dies.

**Workaround.** There isn't one. `docker start` is not a supported way to
resume a ktd instance; use `ktd up`, accepting that the database is
recreated (see above). Do not spend time diagnosing the exit code — it is
the expected outcome.

### MariaDB 11.8 dropped the `mysql` compatibility symlinks

**Symptom.** `docker exec kohadev-db-1 mysql ...` fails with
`exec: "mysql": executable file not found in $PATH`. Same for `mysqldump`.

**Cause.** The MariaDB image moved to 11.8, which removed the `mysql` and
`mysqldump` names in favour of `mariadb` and `mariadb-dump`.

**Workaround.** Use the new names, or use `koha-mysql` inside the Koha
container, which reads the instance config and is unaffected:

```bash
docker exec kohadev-db-1 mariadb -uroot -ppassword -N -e 'SELECT 1;'
docker exec kohadev-koha-1 koha-mysql kohadev -e 'SELECT 1;'
```

`koha-mysql` is the better habit — it does not depend on the client binary's
name or on knowing the root password.

### `oai.pl` returns 404 when the OAI-PMH preference is off or stale

**Symptom.** `curl` against
`/cgi-bin/koha/oai.pl?verb=Identify` returns HTTP 404. It looks like the
script is missing or the URL is wrong. Both are fine.

**Cause.** Koha's `oai.pl` returns 404 rather than an OAI error document
when the `OAI-PMH` system preference is disabled. And the preference is read
through memcached, so a correct value in the database can still be served
stale — `provision-koha.sh` would report success and then fail its own
verification step on work it had done correctly.

**Workaround.** Check the database first, then flush the cache:

```bash
docker exec kohadev-koha-1 koha-mysql kohadev -e \
  "SELECT variable, value FROM systempreferences WHERE variable LIKE 'OAI-PMH%';"

docker exec kohadev-memcached-1 sh -c 'echo flush_all | nc -q1 localhost 11211' \
  || docker restart kohadev-memcached-1
docker exec kohadev-koha-1 koha-plack --restart kohadev
```

`provision-koha.sh` now flushes memcached before restarting Plack, so this
should not recur during provisioning. It remains worth knowing when the
preference is changed by any other route.

### One sample record fails Zebra indexing

**Symptom.** During `ktd up`, Zebra reports
`PCDATA invalid Char value 31` against biblio 369 and exports 435 records
rather than 436.

**Cause.** A control character in that record's control fields. It is a
defect in ktd's sample data, not in the instance.

**Impact.** None on this project. OAI-PMH harvesting reads
`biblio_metadata` directly and returns all 436; only Zebra's search index is
short by one. Ignore it.

---

## What the instance contains

The MARC21 sample catalogue, after provisioning:

| | |
|---|---|
| catalogue records | 436 |
| physical copies | 961 |
| patron accounts | 50 |
| records with MARC 880 | 1 |
| duplicate title pairs | 8 |

Two of those are worth knowing before a demonstration.

**MARC 880 — one record.** Field 880 carries alternate-script versions of a
title, and it is the mechanism behind this project's multilingual claim. The
sample catalogue contains exactly one such record: a Hebrew title where 245
holds the romanisation and 880 the original script. Extraction is complete —
one present, one extracted — but the demonstration rests on a single record
in a language incidental to the project. Loading Khmer records with 880
fields is the way to fix that, and it is cataloguing work rather than code.

**Duplicate titles — eight pairs.** Separate editions of the same work with
different ISBNs, so ISBN-first entity resolution cannot merge them. They
surface as near-identical suggestions in the OPAC widget — including
Knuth's *The Art of Computer Programming* and Stevens' *Advanced Programming
in the UNIX Environment*, which a technical reader is likely to notice.
Title-author matching as a second resolution pass would address it and is
not built.

## Verifying the whole surface

```bash
scripts/evidence.sh
```

Probes all three instances live and reports what each is actually answering,
rather than what was verified once. The Koha section distinguishes the
database being reachable from the OAI-PMH endpoint answering — they are not
the same thing, and an instance can be half up.
