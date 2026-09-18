#!/usr/bin/env bash
# Stage 1, MARC21: Open Library slice -> Koha (kohadev) -> harvest -> embed.
# Safe to start early: waits for the slice, refuses a stopped container.
set -euo pipefail
cd "$(dirname "$0")/.."
MRC=data/ol/slice-marc21.mrc
KOHA=kohadev-koha-1
OAI="http://localhost:8080/cgi-bin/koha/oai.pl"
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

say "waiting for the slice (tail -f data/ol/slice.log to watch)"
until [[ -s "$MRC" ]] && grep -q "wrote .* records to $MRC" data/ol/slice.log 2>/dev/null; do sleep 60; done
say "slice ready: $(grep -o 'wrote [0-9]* records' data/ol/slice.log | tail -1)"

[[ "$(docker inspect -f '{{.State.Status}}' "$KOHA" 2>/dev/null)" == "running" ]] \
  || { say "FAILED: $KOHA is not running. Run: scripts/provision-koha.sh --recreate"; exit 1; }

say "sanity-reading the MARC file"
python - <<'PY'
from pymarc import MARCReader
n = 0
for r in MARCReader(open("data/ol/slice-marc21.mrc", "rb"), to_unicode=True):
    n += 1
    if n == 1:
        print("  first record:", r.title(), "|", r["020"]["a"], "|", r["008"].data[35:38])
print("  records:", n)
PY

say "raising OAI page size to 250 (cache-safe, via set_preference)"
docker exec "$KOHA" bash -lc "koha-shell kohadev -c \"perl -MC4::Context -e 'C4::Context->set_preference(q{OAI-PMH:MaxCount}, 250)'\""

say "importing into Koha (existing records are kept; 952 fields become items)"
docker cp "$MRC" "$KOHA:/tmp/slice-marc21.mrc"
docker exec "$KOHA" bash -lc "koha-shell kohadev -c 'cd /kohadevbox/koha && perl misc/migration_tools/bulkmarcimport.pl -b -v -m ISO2709 -file /tmp/slice-marc21.mrc --commit 1000'" \
  2>&1 | tail -12

say "rebuilding Zebra (the OPAC search index)"
docker exec "$KOHA" bash -lc "koha-rebuild-zebra -f -v -b kohadev" 2>&1 | tail -3

say "Koha counts"
docker exec "$KOHA" bash -lc "koha-mysql kohadev -e 'SELECT count(*) AS biblios FROM biblio; SELECT count(*) AS items FROM items;'"
printf '  OAI identifiers on page 1: '
curl -s "${OAI}?verb=ListIdentifiers&metadataPrefix=marc21" | grep -c "<identifier>" || true

say "harvesting source 1 (kohadev-marc21) into BookRS"
docker compose run --rm ingestion python -m bookrs.ingestion.cli \
  --url "http://host.docker.internal:8080/cgi-bin/koha/oai.pl" \
  --name kohadev-marc21 --prefix marc21 --timeout 120 -v 2>&1 | tail -8

say "embedding source 1 (CPU; expect 30-60 min for 50k works)"
docker compose run --rm embedding python -m bookrs.embedding.cli --source-id 1 -v 2>&1 | tail -5

say "result"
curl -s http://localhost:8000/health | python -m json.tool
curl -s "http://localhost:8000/search/semantic?q=books+about+how+neural+networks+learn&source_id=1&limit=5" \
  | python -m json.tool | grep -E '"(title|score)"'
say "STAGE 1 (MARC21) DONE"
