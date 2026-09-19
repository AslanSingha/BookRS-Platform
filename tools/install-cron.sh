#!/usr/bin/env bash
# Install the four operational stages as host cron jobs.
#
# Deliberately on the host and not inside a container: a cron daemon in a
# container fights Docker's one-process-per-container model and hides its
# failures from the monitoring a library already runs. A failed host cron
# job reaches the same administrator as every other failed job.
#
# Times are close together here so the whole pipeline can be observed in
# one night; a library would space them by how long each stage takes.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$REPO/data/cron"
mkdir -p "$LOGS"

BLOCK=$(cat <<CRON
# --- BookRS-Platform (installed by tools/install-cron.sh) ---
30 2 * * *  cd $REPO && docker compose run --rm ingestion python -m bookrs.ingestion.cli --url "http://host.docker.internal:8080/cgi-bin/koha/oai.pl" --name kohadev-marc21 --prefix marc21 >> $LOGS/harvest.log 2>&1
45 2 * * *  cd $REPO && docker compose run --rm embedding python -m bookrs.embedding.cli >> $LOGS/embed.log 2>&1
15 3 * * *  cd $REPO && docker compose run --rm ingestion python -m bookrs.ingestion.circulation --source-id 1 >> $LOGS/circulation.log 2>&1
30 3 * * 0  cd $REPO && docker compose run --rm recommend python -m bookrs.recommend.cli >> $LOGS/refit.log 2>&1
# --- end BookRS-Platform ---
CRON
)

( crontab -l 2>/dev/null | sed '/--- BookRS-Platform/,/--- end BookRS-Platform ---/d'; echo "$BLOCK" ) | crontab -
echo "installed:"; crontab -l | sed -n '/BookRS-Platform/,/end BookRS-Platform/p'
echo; echo "logs: $LOGS"
