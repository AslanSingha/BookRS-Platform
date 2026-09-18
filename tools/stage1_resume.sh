#!/usr/bin/env bash
# Resume the MARC21 leg from the harvest (Koha already holds the records).
set -euo pipefail
cd "$(dirname "$0")/.."
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
say "harvesting source 1 (kohadev-marc21) into BookRS"
docker compose run --rm ingestion python -m bookrs.ingestion.cli \
  --url "http://host.docker.internal:8080/cgi-bin/koha/oai.pl" \
  --name kohadev-marc21 --prefix marc21 --timeout 120 -v 2>&1 | tail -8
curl -s http://localhost:8000/health | python -m json.tool | grep -E '"(works|items|unembedded)"'
say "embedding source 1 (CPU; expect 30-60 min for 50k works)"
docker compose run --rm embedding python -m bookrs.embedding.cli --source-id 1 -v 2>&1 | tail -5
say "result"
curl -s http://localhost:8000/health | python -m json.tool
curl -s "http://localhost:8000/search/semantic?q=books+about+how+neural+networks+learn&source_id=1&limit=5" \
  | python -m json.tool | grep -E '"(title|score)"'
say "STAGE 1 (MARC21) DONE"
