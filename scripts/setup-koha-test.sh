#!/usr/bin/env bash
# Configure a koha-testing-docker instance for harvesting.
#
# Koha ships with OAI-PMH disabled and item information excluded, so a
# fresh instance serves nothing useful until both are turned on. This
# does that, plus generates circulation history for testing the
# collaborative layer.
#
# Two of these steps have bitten repeatedly and are worth knowing about:
#
#   * The OAI-PMH:ConfFile preference lives in the database; the file it
#     names lives in the container filesystem. Recreating a container
#     leaves the preference pointing at a file that no longer exists,
#     and Koha then returns HTTP 500 on every OAI request.
#
#   * ktd --persistent-db is for restarting an instance it created. Run
#     against an existing volume it fails with "User kohadev-koha
#     already exists" and the container exits, while --wait-ready still
#     reports READY.
#
# Usage:  scripts/setup-koha-test.sh [--with-loans]
set -euo pipefail

: "${KTD_HOME:?KTD_HOME must point at a koha-testing-docker clone}"
: "${SYNC_REPO:?SYNC_REPO must point at a Koha source clone}"

WITH_LOANS=0
[[ "${1:-}" == "--with-loans" ]] && WITH_LOANS=1

echo "==> enabling OAI-PMH and item output"
ktd --shell --run 'cat > /etc/koha/sites/kohadev/oaiconf.yaml <<'"'"'YAML'"'"'
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
perl -MC4::Context -e "
  C4::Context->set_preference(q{OAI-PMH}, 1);
  C4::Context->set_preference(q{OAI-PMH:ConfFile}, q{/etc/koha/sites/kohadev/oaiconf.yaml});
  C4::Context->set_preference(q{RESTBasicAuth}, 1);
"'

# set_preference rather than a direct UPDATE: Koha caches preferences in
# memcached, and a SQL write leaves the cache stale so the endpoint keeps
# returning 404.
echo "==> verifying"
code=$(curl -s -o /dev/null -w '%{http_code}' \
  'http://localhost:8080/cgi-bin/koha/oai.pl?verb=Identify')
items=$(curl -s 'http://localhost:8080/cgi-bin/koha/oai.pl?verb=ListRecords&metadataPrefix=marc21' \
  | grep -c "tag='952'" || true)
echo "    Identify: HTTP ${code}, 952 fields on page 1: ${items}"
[[ "$code" == "200" ]] || { echo "    FAILED: expected 200"; exit 1; }

if [[ "$WITH_LOANS" == "1" ]]; then
  echo "==> generating circulation history"
  cp "$(dirname "$0")/gen-circulation.pl" "$SYNC_REPO/gen-circulation.pl"
  ktd --shell --run 'perl /kohadevbox/koha/gen-circulation.pl' | tail -2
  rm -f "$SYNC_REPO/gen-circulation.pl"
fi

echo "==> done"
