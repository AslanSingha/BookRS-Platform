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
| patron accounts | 53 |
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

---

## Restarting an instance that already has data

Four defects sit between a host restart and a working Koha. Each was met in
sequence on 31 October 2026, and three of the four report a cause that is not
the real one.

### `ktd up` destroys the premise, then refuses

Plain `ktd up` means *fresh instance, fresh database*. Against an instance that
already holds a catalogue it fails:

```
Database is not empty! at /kohadevbox/misc4dev/do_all_you_can_do.pl line 89.
koha-1 exited with code 11
```

The message describes the database as the problem. The database is fine; the
startup script is the one making an assumption no longer true.

**Use `ktd --persistent-db up`.** That flag pulls in
`compose/persistent_db.yml`, which sets `USE_EXISTING_DB: yes` in the
container's environment, and `run.sh` passes `--use-existing-db` through to
`do_all_you_can_do.pl`. The startup then reports `Reusing existing database`
and carries on.

Setting `USE_EXISTING_DB=yes` in `env/defaults.env` does **not** work. The
variable is read there by nothing; it reaches the container only through the
compose overlay. `docker inspect kohadev-koha-1` shows it empty despite the
file saying otherwise, which is how to tell the difference.

### A failed attempt poisons the next one

A run that fails after `koha-create` has partially executed leaves
`/etc/koha/sites/kohadev` and the `kohadev-koha` system user inside the
container. The next run — even with the right flag — fails differently:

```
User kohadev-koha already exists.
koha-1 exited with code 1
```

This is the same non-idempotent provisioning that makes `docker start` fail,
reached by another route. The container has to be removed, not restarted:

```bash
docker rm -f kohadev-koha-1
ktd --persistent-db up
```

The database volume (`kohadev_db-data`) is separate and is not touched by
removing the container.

### Two log files come back owned by root

Apache creates `opac-error.log` and `intranet-error.log` before ktd's
`[logs] Chowning logs` step runs, so those two end up root-owned while every
other file in the directory belongs to `kohadev-koha`.

Nothing fails at startup. The failure arrives later, from anything running
under `koha-shell`:

```
{UNKNOWN}: Can't open /var/log/koha/kohadev/opac-error.log (Permission denied)
at /usr/share/perl5/Log/Log4perl/Appender/File.pm line 151.
at /kohadevbox/koha/Koha/Patron.pm line 446
```

The trace points at `Koha::Patron`, which is not where the problem is.

```bash
docker exec kohadev-koha-1 chown -R kohadev-koha:kohadev-koha /var/log/koha/kohadev/
```

Worth running after every recreate, before anything that uses `koha-shell`.

### `docker cp` into `/tmp` reports success and writes nowhere

This is the worst of the four, because it does not fail.

```bash
docker cp plan.tsv kohadev-koha-1:/tmp/plan.tsv
# Successfully copied 11.4kB (transferred 13.3kB) to kohadev-koha-1:/tmp/plan.tsv
docker exec kohadev-koha-1 ls -la /tmp/plan.tsv
# ls: cannot access '/tmp/plan.tsv': No such file or directory
```

`/tmp` in the ktd container is a tmpfs mount:

```
tmpfs on /tmp type tmpfs (rw,nosuid,nodev,noexec,relatime)
```

`docker cp` writes into the container's own filesystem layer, and the mount
hides what it wrote. The copy succeeded; the file is simply not visible at
that path to anything inside the container.

**Use `/kohadevbox`.** It is a real path, and a file copied there lands owned
by `kohadev-koha`, which is the user `koha-shell` becomes:

```bash
docker cp plan.tsv kohadev-koha-1:/kohadevbox/plan.tsv
docker exec kohadev-koha-1 ls -la /kohadevbox/plan.tsv
# -rw-r--r-- 1 kohadev-koha kohadev-koha 11444 /kohadevbox/plan.tsv
```

The symptom of getting this wrong is a script that cannot open a file you
watched being copied successfully.

### The sequence that works

```bash
# 1. remove any half-provisioned container, keeping the database volume
docker rm -f kohadev-koha-1 2>/dev/null

# 2. bring the instance up against its existing database
cd ~/projects/koha-testing-docker && ktd --persistent-db up

# 3. once "koha-testing-docker has started up", fix the log ownership
docker exec kohadev-koha-1 chown -R kohadev-koha:kohadev-koha /var/log/koha/kohadev/

# 4. verify the catalogue survived and the endpoint answers
docker exec kohadev-db-1 mariadb -uroot -ppassword -N -e \
  'SELECT COUNT(*) FROM koha_kohadev.biblio; SELECT COUNT(*) FROM koha_kohadev.issues;'
curl -s -o /dev/null -w 'oai: %{http_code}\n' \
  'http://localhost:8080/cgi-bin/koha/oai.pl?verb=Identify'
```

Expect a Zebra parse error on biblio 369 during startup — that is the MARC-8
control-character record documented in `docs/marc-field-analysis.md`, and it
costs one record out of the Zebra index rather than anything in the harvest.

### What these four have in common

Three of the four report a cause that is not the real one: the database is
blamed for a missing flag, `Koha::Patron` for a file permission, and nothing
at all is blamed for the copy that went nowhere. Only the duplicate-user
message says what it means.

That is the ordinary condition of integrating with software of this age, and
it is why this file exists.
