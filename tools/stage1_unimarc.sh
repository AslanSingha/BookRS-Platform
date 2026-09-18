#!/usr/bin/env bash
# Stage 1, UNIMARC: same slice -> Koha (unimarc, proxied) -> harvest -> embed.
# Gated on the MARC21 leg finishing and on unimarc-koha-1 running.
set -euo pipefail
cd "$(dirname "$0")/.."
MRC=data/ol/slice-unimarc.mrc
KOHA=unimarc-koha-1
OAI_HOST=unimarc.localhost
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

say "waiting for the MARC21 leg to finish"
until grep -q "STAGE 1 (MARC21) DONE" data/ol/stage1.log 2>/dev/null; do sleep 60; done

until [[ "$(docker inspect -f '{{.State.Status}}' "$KOHA" 2>/dev/null)" == "running" ]]; do
  say "waiting for $KOHA -- provision it: KOHA_INSTANCE=unimarc OPAC_URL=http://unimarc.localhost WIDGET_SOURCE_ID=9 KTD_ARGS=\"--proxy --name unimarc\" scripts/provision-koha.sh --recreate"
  sleep 120
done

say "reading item subfields from the instance framework"
FW=$(docker exec "$KOHA" bash -lc "koha-mysql kohadev -N -e \"SELECT kohafield, tagfield, tagsubfield FROM marc_subfield_structure WHERE frameworkcode='' AND kohafield IN ('items.homebranch','items.holdingbranch','items.barcode','items.itype','items.itemcallnumber')\"")
echo "$FW" | sed 's/^/   /'
get() { echo "$FW" | awk -v k="$1" '$1==k{print $3; exit}'; }
ITEM_TAG=$(echo "$FW" | awk '$1=="items.barcode"{print $2; exit}')
[[ -n "$ITEM_TAG" ]] || { say "FAILED: framework has no items.barcode mapping"; exit 1; }

say "writing UNIMARC (items in $ITEM_TAG)"
python tools/ol_to_unimarc.py --item-tag "$ITEM_TAG" \
  --sub-home "$(get items.homebranch)" --sub-hold "$(get items.holdingbranch)" \
  --sub-barcode "$(get items.barcode)" --sub-itype "$(get items.itype)" \
  --sub-callno "$(get items.itemcallnumber)"

say "raising OAI page size to 250"
docker exec "$KOHA" bash -lc "koha-shell kohadev -c \"perl -MC4::Context -e 'C4::Context->set_preference(q{OAI-PMH:MaxCount}, 250)'\""

say "importing into Koha"
docker cp "$MRC" "$KOHA:/tmp/slice-unimarc.mrc"
docker exec "$KOHA" bash -lc "koha-shell kohadev -c 'cd /kohadevbox/koha && perl misc/migration_tools/bulkmarcimport.pl -b -v -m ISO2709 -file /tmp/slice-unimarc.mrc --commit 1000'" \
  2>&1 | tail -12

say "rebuilding Zebra"
docker exec "$KOHA" bash -lc "koha-rebuild-zebra -f -v -b kohadev" 2>&1 | tail -3

say "Koha counts"
docker exec "$KOHA" bash -lc "koha-mysql kohadev -e 'SELECT count(*) AS biblios FROM biblio; SELECT count(*) AS items FROM items;'"

say "harvesting source 9 (unimarc) into BookRS"
docker compose run --rm ingestion python -m bookrs.ingestion.cli \
  --url "http://host.docker.internal/cgi-bin/koha/oai.pl" --header "Host:${OAI_HOST}" \
  --name unimarc --prefix marc21 --timeout 120 -v 2>&1 | tail -8

say "embedding source 9"
docker compose run --rm embedding python -m bookrs.embedding.cli --source-id 9 -v 2>&1 | tail -5

say "result"
curl -s http://localhost:8000/health | python -m json.tool
curl -s "http://localhost:8000/search/semantic?q=r%C3%A9seaux+de+neurones+et+apprentissage+automatique&source_id=9&limit=5" \
  | python -m json.tool | grep -E '"(title|score)"'
say "STAGE 1 (UNIMARC) DONE"
