# Koha, running it for BookRS-Platform

One Koha instance backs this project: koha-testing-docker
([ktd](https://gitlab.com/koha-community/koha-testing-docker)), OPAC on
`localhost:8080`, staff interface on `localhost:8081`, database
`koha_kohadev` in the `kohadev-db-1` container. The platform reads that
database, read-only (`bookrs/ingestion/koha_db.py`, `direct.py`); the
BookRS plugin (`koha-plugin/`) puts the recommendations in the OPAC.
This file records how to run the instance and the defects that cost time.

---

## Starting it, with the platform

After a PC restart, or whenever Koha is down. Run the two parts as two
pastes, the second only after the first prints `Koha ready`.

```bash
# 1. The platform, then Koha; the Koha container is recreated once if its
#    start-up stops at "User kohadev-koha already exists" (see below).
cd ~/projects/BookRS-Platform && docker compose up -d && docker compose --profile schedule up -d
koha_up() {
  local T i=0; T=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ktd --persistent-db up -d >/dev/null 2>&1
  while [ $i -lt 180 ]; do
    docker logs --since "$T" kohadev-koha-1 2>&1 | grep -q "started up" && return 0
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/cgi-bin/koha/opac-main.pl)" = 200 ] && return 0
    docker logs --since "$T" kohadev-koha-1 2>&1 | grep -q "User kohadev-koha already exists" && return 1
    [ "$(docker inspect -f '{{.State.Running}}' kohadev-koha-1 2>/dev/null)" = true ] || return 1
    sleep 10; i=$((i+1))
  done
  return 1
}
if koha_up || { echo "Recreating the Koha container"; docker rm -f kohadev-koha-1 >/dev/null && koha_up; }; then
  echo "Koha ready"
else
  echo "Koha did not start:"; docker logs --tail 20 kohadev-koha-1 2>&1
fi
```

```bash
# 2. What a recreate loses: the plugin's files, RESTBasicAuth, the log
#    ownership. Then restart Plack, so the plugin's routes are registered.
D=$(docker exec kohadev-koha-1 bash -c "grep -m1 -o '<pluginsdir>[^<]*' /etc/koha/sites/kohadev/koha-conf.xml | sed 's/<pluginsdir>//'") && \
docker cp koha-plugin/Koha/. "kohadev-koha-1:$D/Koha/" && docker exec kohadev-koha-1 chown -R kohadev-koha: "$D/Koha" && \
docker exec kohadev-koha-1 koha-shell kohadev -c "perl /kohadevbox/koha/misc/devel/install_plugins.pl" && \
docker exec kohadev-koha-1 koha-mysql kohadev -e "UPDATE systempreferences SET value='1' WHERE variable='RESTBasicAuth';" && \
docker restart kohadev-memcached-1 && \
docker exec kohadev-koha-1 chown -R kohadev-koha:kohadev-koha /var/log/koha/kohadev && \
docker exec kohadev-koha-1 koha-plack --restart kohadev && sleep 30 && \
tools/demo.sh check
```

`tools/demo.sh check` ends with `Ready.` when Koha, the plugin, the API and
the scheduler all answer. A plugin `HTTP 000` in the first minute after the
Plack restart is Koha still warming up: check again.

## Bringing it down

There is no safe stop. `ktd down` removes the containers *and the volumes*,
so the database is recreated from scratch on the next `ktd up`: the UCSD
catalogue, its patrons and every rating would be gone. Never use it.

Before anything risky, dump the database, and confirm the database
container is actually running before trusting the dump:

```bash
docker ps --filter 'name=kohadev-db-1' --format '{{.Names}}\t{{.Status}}'
docker exec kohadev-db-1 mariadb-dump -uroot -ppassword koha_kohadev | gzip \
  > ~/koha-backups/koha_kohadev-$(date +%F).sql.gz
ls -lh ~/koha-backups/
```

---

## Defects and workarounds

### `docker start` on a stopped ktd container always fails

**Symptom.** The container exits within a minute of `docker start`, with
exit code 1 or 255. `docker logs` ends at "User kohadev-koha already
exists."

**Cause.** ktd's entrypoint replays the whole provisioning sequence on every
container start, and that sequence is not idempotent. `koha-create` finds
the instance already present, returns non-zero, and the container dies.

**Workaround.** Remove the container and bring it up again against the
existing database, which is what the start-up above does. The database
volume is separate and is not touched.

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

### A system preference changed in the database is served stale

Koha reads system preferences through memcached, so a value changed with
SQL can keep its old effect until the cache is flushed. Restart memcached,
then Plack, after any such change (step 2 above does).

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

# 4. verify the catalogue survived and the OPAC answers
docker exec kohadev-db-1 mariadb -uroot -ppassword -N -e \
  'SELECT COUNT(*) FROM koha_kohadev.biblio; SELECT COUNT(*) FROM koha_kohadev.ratings;'
curl -s -o /dev/null -w 'opac: %{http_code}\n' http://localhost:8080/cgi-bin/koha/opac-main.pl
```

### What these four have in common

Three of the four report a cause that is not the real one: the database is
blamed for a missing flag, `Koha::Patron` for a file permission, and nothing
at all is blamed for the copy that went nowhere. Only the duplicate-user
message says what it means.

That is the ordinary condition of integrating with software of this age, and
it is why this file exists.

### `RESTBasicAuth` does not survive a recreate

Logging in to Koha's REST routes with a username and password (as
`tools/demo.sh list` and `search` do, through the plugin's routes) needs
Koha's `RESTBasicAuth` preference enabled, and recreating the instance
resets it: `ktd --persistent-db up` reuses a database rather than
provisioning one. The symptom is a login refused with a password that
worked yesterday. Step 2 of the start-up sets it again.

```bash
docker exec kohadev-koha-1 koha-mysql kohadev -e \
  "UPDATE systempreferences SET value='1' WHERE variable='RESTBasicAuth';"
docker restart kohadev-memcached-1
docker exec kohadev-koha-1 koha-plack --restart kohadev
```

The memcached restart is not optional: Koha reads preferences through
memcached, so a correct value in the database is served stale until the cache
is cleared. `nc` is not installed in the memcached container, so a `flush_all`
over netcat fails and restarting the container is the reliable form.

This belongs in the restart sequence above, between fixing the log ownership
and verifying the endpoint.
