#!/usr/bin/env bash
#
# Bring a koha-testing-docker instance up in the state this project
# needs: OAI-PMH enabled, item-level holdings exposed, and both metadata
# formats served.
#
# This exists because that state was reconstructed by hand four times in
# a single session. Three properties of KTD make it necessary.
#
#   1. The entrypoint is not idempotent. It runs koha-create on every
#      start, which fails against a database that already contains the
#      instance ("User kohadev-koha already exists", exit 1). A stopped
#      container cannot be restarted -- it must be REMOVED and recreated.
#      Verified: restart fails, recreate succeeds, and --persistent-db
#      changes neither outcome.
#
#   2. State is split across two lifetimes. The database lives in a
#      volume and survives. Anything written into the container
#      filesystem does not -- including oaiconf.yaml, whose system
#      preference survives in the database and then points at a file
#      that is gone. Koha serves the resulting exception as HTTP 200
#      with a Perl stack trace, so readiness is checked on content.
#
#   3. KTD prints "ready to be enjoyed" while Plack is still starting.
#      That message is not readiness; an OAI envelope from the endpoint
#      is.
#
# Usage:  scripts/provision-koha.sh [--recreate]
#
#   --recreate   remove the koha and memcached containers first. The db
#                container and its volume are never touched, so
#                harvested records survive.
#
# Environment:  KTD_HOME (required), OPAC_URL, KOHA_INSTANCE,
#               OAI_ARCHIVE_ID, TIMEOUT, KTD_ARGS, WIDGET_API,
#               WIDGET_LIMIT, WIDGET_SOURCE_ID

set -euo pipefail

KTD_HOME="${KTD_HOME:?KTD_HOME is not set; export it or source your KTD environment}"
OPAC_URL="${OPAC_URL:-http://localhost:8080}"
OAI="${OPAC_URL}/cgi-bin/koha/oai.pl"
# Two names, deliberately kept as two variables after a real bug: an
# earlier version of this script conflated them and always failed on a
# second proxied instance with "instance directory MISSING".
#
# INSTANCE is the docker-compose PROJECT name (--name to ktd). It
# controls container names -- ${INSTANCE}-koha-1 -- and nothing inside
# the container knows about it.
#
# SITE is the Koha site name INSIDE the container -- the path under
# /etc/koha/sites, the database name, the Linux user koha-create makes.
# KTD hardcodes this to "kohadev" always, regardless of --name,
# confirmed both by the project's own docs ("The default KTD instance
# name is kohadev to match the previous behavior") and by a known
# upstream issue where t/kohasite.t itself hardcodes 'kohadev' deeply
# enough that changing the site name breaks Koha's own test suite. A
# proxied instance named "unimarc" still builds
# /etc/koha/sites/kohadev internally, never /etc/koha/sites/unimarc.
#
# They default to the same string, which is exactly why this went
# unnoticed for as long as it did: on a single default instance
# INSTANCE == SITE == "kohadev", so every call site that used the wrong
# one still happened to produce the right value by coincidence.
INSTANCE="${KOHA_INSTANCE:-kohadev}"
SITE="${KOHA_SITE:-kohadev}"
DB_NAME="koha_${SITE}"
CONF_PATH="/etc/koha/sites/${SITE}/oaiconf.yaml"
ARCHIVE_ID="${OAI_ARCHIVE_ID:-KOHA-OAI-TEST}"

# The recommendation widget is injected through OPACUserJS. Set
# WIDGET_API to the empty string to skip installing it.
WIDGET_API="${WIDGET_API-http://localhost:8000}"
WIDGET_LIMIT="${WIDGET_LIMIT:-6}"
WIDGET_SOURCE_ID="${WIDGET_SOURCE_ID:-1}"

# A warm recreate reaches ready in about a minute. A first boot after a
# host restart does considerably more work and has been observed past
# five minutes, so the default is generous. Override with TIMEOUT=.
TIMEOUT="${TIMEOUT:-900}"

KOHA_C="${INSTANCE}-koha-1"
DB_C="${INSTANCE}-db-1"
MC_C="${INSTANCE}-memcached-1"

say()  { printf '  %s\n' "$*"; }
fail() { printf '  FAILED: %s\n' "$*" >&2; exit 1; }

# Run the client inside the db container. It is 'mariadb', not 'mysql':
# MariaDB 11.8 dropped the compatibility symlink.
dbq() {
  docker exec -i "$DB_C" sh -c \
    'exec mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" "$@"' _ "$@"
}

# --- 1. containers ---------------------------------------------------

if [[ "${1:-}" == "--recreate" ]]; then
  say "removing ${KOHA_C} and ${MC_C} (db and its volume are left alone)"
  docker rm -f "$KOHA_C" "$MC_C" >/dev/null 2>&1 || true
fi

# KTD_ARGS lets a second instance be provisioned without editing this
# line. The UNIMARC instance runs behind KTD's Traefik proxy under its
# own name, which is a different invocation entirely:
#
#   KOHA_INSTANCE=unimarc \
#   OPAC_URL=http://unimarc.localhost \
#   KTD_ARGS="--proxy --name unimarc" \
#   KOHA_MARC_FLAVOUR=unimarc \
#     scripts/provision-koha.sh --recreate
KTD_ARGS="${KTD_ARGS:-}"

say "bringing the instance up${KTD_ARGS:+ (ktd $KTD_ARGS)}"
# shellcheck disable=SC2086  -- word splitting is the point here
( cd "$KTD_HOME" && ktd $KTD_ARGS --persistent-db up -d >/dev/null 2>&1 )

say "waiting for the container to appear"
state=missing
for _ in $(seq 1 15); do
  state=$(docker inspect -f '{{.State.Status}}' "$KOHA_C" 2>/dev/null || echo missing)
  [[ "$state" != "missing" ]] && break
  sleep 2
done
[[ "$state" != "missing" ]] || fail "container ${KOHA_C} was never created"

# --- 2. wait for the application, not for a message ------------------

say "waiting for the Koha instance to be created (up to ${TIMEOUT}s)"
deadline=$(( SECONDS + TIMEOUT ))

# Readiness is an OAI envelope. Not a status code -- a missing conf file
# is served as HTTP 200 with a Perl stack trace. Not <repositoryName>
# either: this instance's Identify response omits it and carries
# <earliestDatestamp> instead, so requiring it would never succeed.
# Wait only until Koha is SERVING, not until it serves valid OAI. On a
# freshly recreated container the conf file does not exist yet, so
# oai.pl throws and returns 500 -- and the file is written in step 4,
# after this loop. Requiring an OAI envelope here would mean waiting for
# a state this script has not yet created. Any HTTP response means Plack
# is up and the configuration steps can run; the envelope is required at
# the end, in step 5, where it is a genuine verification.
# The precondition the configuration steps actually need is the
# instance directory, created by koha-create partway through the
# entrypoint. An HTTP response is not it: Plack answers before
# koha-create finishes, and on the first attempt this loop passed while
# /etc/koha/sites/kohadev/ did not yet exist, so writing the conf file
# failed with "Directory nonexistent".
until docker exec "$KOHA_C" test -d "$(dirname "$CONF_PATH")" 2>/dev/null; do

  # Re-checked every iteration rather than once up front: the entrypoint
  # fails several seconds in, so the container is briefly 'running'
  # before it dies. Checking only before the loop turns a diagnosable
  # error into a full-length timeout.
  state=$(docker inspect -f '{{.State.Status}}' "$KOHA_C" 2>/dev/null || echo missing)
  if [[ "$state" != "running" ]]; then
    printf '\r'
    docker logs --tail 5 "$KOHA_C" 2>&1 | sed 's/^/    /'
    fail "the container exited while starting. If the log ends with 'User ${SITE}-koha already exists', re-run with --recreate: KTD runs koha-create on every start and cannot resume an existing instance."
  fi

  if (( SECONDS >= deadline )); then
    printf '\r'
    fail "OAI-PMH did not become ready within ${TIMEOUT}s. The container is running; inspect it with: docker logs ${KOHA_C}"
  fi

  # Progress, because silence and a hang look identical. The HTTP code
  # disambiguates what the log line cannot: 000 nothing listening,
  # 404 listening but OAI disabled, 500 erroring, 200 responding.
  code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "${OAI}?verb=Identify" || echo 000)
  line=$(docker logs --tail 1 "$KOHA_C" 2>&1 | tr -d '\r' | cut -c1-44)
  printf '\r    %4ds  HTTP %s  %s\033[K' "$SECONDS" "$code" "$line"
  sleep 5
done
printf '\r\033[K'
say "instance directory exists"

# --- 3. system preferences -------------------------------------------

say "applying OAI system preferences"
dbq "$DB_NAME" <<SQL
INSERT INTO systempreferences (variable, value, explanation, type)
VALUES
  ('OAI-PMH',           '1',             'Enable the OAI-PMH server',  'YesNo'),
  ('OAI-PMH:archiveID', '${ARCHIVE_ID}', 'OAI identifier prefix',      'Free'),
  ('OAI-PMH:MaxCount',  '50',            'Records returned per page',  'Integer'),
  ('OAI-PMH:ConfFile',  '${CONF_PATH}',  'OAI-PMH configuration file', 'Free')
ON DUPLICATE KEY UPDATE value = VALUES(value);
SQL

# --- 3b. the OPAC widget -------------------------------------------

# OPACUserJS is a system preference, so it survives in the volume -- but
# only if it was ever set on THIS database. It was not on a database
# recovered from a bare datadir, and the resulting demonstration had a
# working API, correct CORS headers and no panel on the page, with
# nothing to indicate why.
#
# Two details in the snippet are not obvious and were found the hard
# way. OPACUserJS holds JavaScript, not HTML: Koha wraps its contents in
# script tags, so a <script src> placed there becomes inert text. And
# Koha serves a nonce-based CSP, so the loader copies the nonce from the
# block it runs in -- without it an enforcing policy drops the injected
# script with no visible error.
if [[ -n "$WIDGET_API" ]]; then
  say "installing the OPAC widget (WIDGET_API=${WIDGET_API})"
  dbq "$DB_NAME" <<SQL
INSERT INTO systempreferences (variable, value, explanation, type)
VALUES ('OPACUserJS', '(function () {
  var s = document.createElement("script");
  s.src = "${WIDGET_API}/widget.js";
  s.setAttribute("data-api", "${WIDGET_API}");
  s.setAttribute("data-source-id", "${WIDGET_SOURCE_ID}");
  s.setAttribute("data-limit", "${WIDGET_LIMIT}");
  var c = document.currentScript;
  if (c && c.nonce) { s.nonce = c.nonce; }
  document.body.appendChild(s);
})();', 'JavaScript injected into every OPAC page', 'Free')
ON DUPLICATE KEY UPDATE value = VALUES(value);
SQL

  # Koha caches system preferences, so the change is invisible until
  # Plack reloads.
  say "restarting Plack so the preference takes effect"
  docker exec "$KOHA_C" koha-plack --restart "$SITE" >/dev/null 2>&1 || true
fi

# --- 4. the conf file, which does not survive recreation -------------

say "writing ${CONF_PATH}"
docker exec -i "$KOHA_C" sh -c "cat > '${CONF_PATH}'" <<'YAML' \
  || fail "could not write ${CONF_PATH}. If the message says the directory does not exist, koha-create has not finished; check: docker logs ${KOHA_C}"
format:
    marcxml:
      metadataPrefix: marcxml
      metadataNamespace: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim
      schema: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd
      include_items: 0
    marc21:
      metadataPrefix: marc21
      metadataNamespace: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim
      schema: http://www.loc.gov/MARC21/slim http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd
      include_items: 1
YAML

# Parsed with the library Koha itself uses. YAML is whitespace-sensitive
# and this text has crossed two shell layers; a structural check beats
# trusting that the bytes look right.
say "verifying the file parses as Koha will read it"
docker exec "$KOHA_C" perl -MYAML::XS -e '
  my $c = YAML::XS::LoadFile($ARGV[0]);
  for my $f (sort keys %{$c->{format}}) {
    printf qq{    %-8s include_items=%s\n}, $f, $c->{format}{$f}{include_items};
  }' "$CONF_PATH" || fail "the conf file did not parse"

# --- 5. verify on content --------------------------------------------

# Koha is only now being told where its conf file is, and Plack may
# still be serving the previous state, so this is retried rather than
# checked once.
say "verifying the endpoint"
formats=""
for _ in $(seq 1 24); do
  body=$(curl -s -m 10 "${OAI}?verb=ListMetadataFormats" || true)
  # '|| true' on the grep: under pipefail a non-matching grep returns 1
  # and would abort the script before the case below could report why.
  formats=$(printf '%s' "$body" | grep -oE '<metadataPrefix>[^<]*' | sed 's/.*>//' | sort | tr '\n' ' ' || true)
  [[ -n "$formats" ]] && break
  sleep 5
done

case "$formats" in
  *marc21*marcxml*|*marcxml*marc21*)
    say "formats: ${formats}" ;;
  *)
    printf '    %s\n' "$(printf '%s' "$body" | head -4)"
    fail "expected marc21 and marcxml, got: ${formats:-<nothing>}. The first lines of the response are above." ;;
esac

biblios=$(dbq -N -B "$DB_NAME" -e 'SELECT COUNT(*) FROM biblio;' | tr -d '[:space:]')
say "records in the catalogue: ${biblios}"
if [[ "${biblios:-0}" -eq 0 ]]; then
  say "WARNING: the catalogue is empty; a harvest will return nothing"
fi

if [[ -n "$WIDGET_API" ]]; then
  widget_len=$(dbq -N -B "$DB_NAME" -e \
    "SELECT COALESCE(LENGTH(value),0) FROM systempreferences WHERE variable='OPACUserJS';" \
    | tr -d '[:space:]')
  # Checked rather than assumed: a zero here is exactly the state that
  # produces a silent, empty recommendation panel.
  [[ "${widget_len:-0}" -gt 0 ]] || fail "OPACUserJS is empty; the widget will not load"
  say "OPAC widget installed (${widget_len} characters)"
  say "  record page: ${OPAC_URL}/cgi-bin/koha/opac-detail.pl?biblionumber=4"
fi

printf '\n  Ready: %s\n' "$OAI"
