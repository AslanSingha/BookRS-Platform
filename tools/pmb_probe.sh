#!/usr/bin/env bash
# 25-record proof of the PMB importer. Waits for the slice; writes data/ol/pmb-probe.ok on success.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
until [[ -s data/ol/slice.jsonl ]] && grep -q "wrote .* records to data/ol/slice.jsonl" data/ol/slice.log 2>/dev/null; do sleep 60; done
before=$(docker exec pmb-docker-db-1 sh -c 'mariadb -uroot -ppassword pmb -N -e "SELECT count(*) FROM notices"')
say "notices before: $before"
python tools/ol_to_unimarc.py --slice data/ol/slice.jsonl --out data/ol/pmb-probe.mrc --limit 25
python tools/pmb_import.py --file data/ol/pmb-probe.mrc --location pmb --debug-dir data/ol/pmb-debug
after=$(docker exec pmb-docker-db-1 sh -c 'mariadb -uroot -ppassword pmb -N -e "SELECT count(*) FROM notices"')
say "notices after: $after"
docker exec pmb-docker-db-1 sh -c 'mariadb -uroot -ppassword pmb -e "SELECT notice_id, tit1, code FROM notices ORDER BY notice_id DESC LIMIT 5; SELECT count(*) AS staging_left FROM import_marc"'
if (( after > before )); then touch data/ol/pmb-probe.ok; say "PMB PROBE OK (+$((after-before)))"; else say "PMB PROBE FAILED: no new notices"; exit 1; fi
