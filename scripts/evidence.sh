#!/usr/bin/env bash
#
# Print a one-page evidence summary of what this project integrates with,
# separating what is running right now from what was verified earlier and
# recorded in the repository.
#
# That separation is the point. "I integrated with Koha and PMB" is a
# claim about three instances, and each is probed here rather than
# asserted -- presenting recorded evidence as live evidence is the
# fastest way to lose the room when someone asks to see it. All three
# can run locally; whether they are running right now is what this
# script actually checks.
#
# Usage:  scripts/evidence.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

KOHA_DB="${KOHA_DB_CONTAINER:-kohadev-db-1}"
line() { printf '  %s\n' "$*"; }
rule() { printf '\n%s\n' "  ── $* ─────────────────────────────────────────" | cut -c1-72; }

koha() {
  docker exec "$KOHA_DB" sh -c \
    "mariadb -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -B -e \"$1\"" 2>/dev/null \
    | tr -d '[:space:]'
}

mine() {
  docker compose run --rm -T test python -c "
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    print(c.execute('''$1''').fetchone()[0])
" 2>/dev/null | tail -1 | tr -d '[:space:]'
}

printf '\n  BOOKRS-PLATFORM — INTEGRATION EVIDENCE\n'
printf '  %s\n' "$(date '+%Y-%m-%d %H:%M')"
printf '  repository %s at %s\n' \
  "$(git rev-list --count HEAD 2>/dev/null || echo '?') commits" \
  "$(git log -1 --format=%h 2>/dev/null || echo '?')"

# ---------------------------------------------------------------- live
rule "RUNNING NOW — verifiable in this session"

if docker ps --format '{{.Names}}' | grep -q "^${KOHA_DB}$"; then
  biblios=$(koha "SELECT COUNT(*) FROM koha_kohadev.biblio;")
  items=$(koha   "SELECT COUNT(*) FROM koha_kohadev.items;")
  patrons=$(koha "SELECT COUNT(*) FROM koha_kohadev.borrowers;")
  cur=$(koha     "SELECT COUNT(*) FROM koha_kohadev.issues;")
  old=$(koha     "SELECT COUNT(*) FROM koha_kohadev.old_issues;")

  # The integration surface decides the label, not the container being up.
  if curl -s -m 5 'http://localhost:8080/cgi-bin/koha/oai.pl?verb=Identify' \
       | grep -q '<OAI-PMH'; then oai=yes; else oai=no; fi

  if [ "$oai" = yes ]; then
    line "Koha, MARC21   (koha-testing-docker, live — OAI-PMH answering)"
  else
    line "Koha, MARC21   (koha-testing-docker, DATABASE ONLY — web tier not answering)"
  fi
  line "    counts below read from Koha's database, not over OAI-PMH"
  line "    catalogue records      ${biblios:-?}"
  line "    physical copies        ${items:-?}"
  line "    patron accounts        ${patrons:-?}"
  line "    loans, current         ${cur:-?}"
  line "    loans, returned        ${old:-?}"
  line ""
  if [ "$oai" = yes ]; then
    line "    OAI-PMH endpoint       answering"
    fmts=$(curl -s -m 5 'http://localhost:8080/cgi-bin/koha/oai.pl?verb=ListMetadataFormats' \
           | grep -o '<metadataPrefix>[^<]*' | sed 's/.*>//' | tr '\n' ' ')
    line "    formats offered        ${fmts:-none}"
  else
    line "    OAI-PMH endpoint       NOT answering — harvesting cannot run"
    line "                           start the web tier, then re-run this script"
  fi
else
  line "Koha, MARC21   not running — start with scripts/provision-koha.sh --recreate"
fi

line ""
w=$(mine  "SELECT count(*) FROM works")
e=$(mine  "SELECT count(*) FROM embeddings")
l=$(mine  "SELECT count(*) FROM loans")
f=$(mine  "SELECT count(*) FROM work_factors")
a=$(mine  "SELECT count(*) FROM works WHERE length(title_alternate) > 0")
line "BookRS-Platform — what was extracted from it"
line "    works                  ${w:-?}"
line "    embeddings             ${e:-?}   (384 dimensions each)"
line "    pseudonymised loans    ${l:-?}"
line "    works with factors     ${f:-?}"
# The source's own 880 count, so a low number reads as the catalogue's
# content rather than as a shortfall in extraction.
k880=$(koha "SELECT COUNT(*) FROM koha_kohadev.biblio_metadata WHERE ExtractValue(metadata,'//datafield[@tag=\"880\"]') <> '';" 2>/dev/null)
line "    alternate-script works ${a:-?}"
line "      880 records in source ${k880:-not probed}   (Koha MARC21 only)"
# Different editions of one work, with different ISBNs, so ISBN-first
# resolution cannot merge them. Measured rather than discovered in a demo.
dup=$(mine "SELECT count(*) FROM (SELECT lower(title) FROM works WHERE source_id=1 AND length(title)>0 GROUP BY 1 HAVING count(*)>1) d")
line "      duplicate titles       ${dup:-not probed} pairs   (Koha MARC21; different editions, different ISBNs)"

# --------------------------------------------- other live instances
# Koha UNIMARC and PMB both run locally now (see docs/pmb-setup.md and
# scripts/provision-koha.sh). They are probed rather than asserted: an
# instance that is merely usually running is recorded evidence, not live
# evidence, and the difference matters when someone asks to see it.

rule "OTHER INSTANCES — probed now"

# Koha UNIMARC sits behind KTD's Traefik proxy, routed by Host header,
# so it is reached through the proxy rather than a published port.
uni_ip=$(docker inspect proxy-proxy-1 \
         --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
         2>/dev/null)
if [[ -n "$uni_ip" ]] && curl -s -m 5 -H 'Host: unimarc.localhost' \
     "http://${uni_ip}/cgi-bin/koha/oai.pl?verb=Identify" | grep -q '<OAI-PMH'; then
  line "Koha, UNIMARC  answering (via ktd_proxy at ${uni_ip})"
else
  line "Koha, UNIMARC  not answering — ktd_proxy --start, then"
  line "               KOHA_INSTANCE=unimarc OPAC_URL=http://unimarc.localhost \\"
  line "               KTD_ARGS=\"--proxy --name unimarc\" scripts/provision-koha.sh"
fi
line "    a different record set from the MARC21 one, not a translation"
line ""

# PMB serves OAI from a query-string URL, not a path, and only
# pmb_xml_unimarc works -- oai_dc returns HTTP 500 on 7.3.7.
PMB_OAI="${PMB_OAI:-http://localhost:8090/pmb/ws/connector_out.php?source_id=1}"
if curl -s -m 5 "${PMB_OAI}&verb=Identify" | grep -q '<OAI-PMH'; then
  pmb_n=$(curl -s -m 10 "${PMB_OAI}&verb=ListRecords&metadataPrefix=pmb_xml_unimarc" \
          | grep -c '<record>')
  line "PMB            answering — ${pmb_n} records over pmb_xml_unimarc"
  line "    UNIMARC-derived XML, reverse-engineered field by field:"
  line "    no adequate specification is published"
  line "    setup: docs/pmb-setup.md"
else
  line "PMB            not answering — cd ~/projects/pmb-docker && docker compose up -d"
  line "    setup and known defects: docs/pmb-setup.md"
fi

# ---------------------------------------------------------------- code
rule "IN THE REPOSITORY"

printf '  %-26s %s\n' "commits" "$(git rev-list --count HEAD 2>/dev/null)"
t=$(docker compose run --rm -T test python -m pytest -q 2>&1 | grep -oE "[0-9]+ passed" | head -1); printf "  %-26s %s\n" "automated tests" "${t:-not run}"
printf '  %-26s %s\n' "evidence document" "docs/marc-field-analysis.md, $(grep -c '^## ' docs/marc-field-analysis.md 2>/dev/null) sections"
printf '  %-26s %s\n' "stated limitations" "$(sed -n '/^## 17. Limitations/,/^## 18/p' docs/marc-field-analysis.md 2>/dev/null | grep -cE '^[0-9]+\.')"

rule "WHAT THIS DOES NOT SHOW"

line "The circulation data is generated, not real. It demonstrates that"
line "harvesting, storage and factorisation run. It supports no claim"
line "about recommendation quality, which needs a pilot library."
printf '\n'
