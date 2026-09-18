#!/usr/bin/env bash
# Stage 1, PMB: 20k of the slice through PMB's importer -> harvest source 10 -> embed.
# Gated on the UNIMARC leg and on a passing probe.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
say "waiting for the UNIMARC leg and the PMB probe"
until grep -q "STAGE 1 (UNIMARC) DONE" data/ol/stage1-unimarc.log 2>/dev/null && [[ -f data/ol/pmb-probe.ok ]]; do sleep 120; done
say "writing 20,000 UNIMARC records for PMB"
python tools/ol_to_unimarc.py --slice data/ol/slice.jsonl --out data/ol/slice-pmb.mrc --limit 20000
say "importing into PMB (expect one to three hours)"
python tools/pmb_import.py --file data/ol/slice-pmb.mrc --location pmb
docker exec pmb-docker-db-1 sh -c 'mariadb -uroot -ppassword pmb -N -e "SELECT count(*) AS notices FROM notices; SELECT count(*) AS staging_left FROM import_marc"'
say "harvesting source 10 (pmb-test)"
docker compose run --rm ingestion python -m bookrs.ingestion.cli \
  --url "http://host.docker.internal:8090/pmb/ws/connector_out.php?source_id=1" \
  --name pmb-test --prefix pmb_xml_unimarc --allow-missing-items --timeout 120 -v 2>&1 | tail -8
say "embedding source 10"
docker compose run --rm embedding python -m bookrs.embedding.cli --source-id 10 -v 2>&1 | tail -5
say "result"
curl -s http://localhost:8000/health | python -m json.tool
curl -s "http://localhost:8000/search/semantic?q=histoire+de+la+r%C3%A9volution+fran%C3%A7aise&source_id=10&limit=5" | python -m json.tool | grep -E '"(title|score)"'
say "STAGE 1 (PMB) DONE"
