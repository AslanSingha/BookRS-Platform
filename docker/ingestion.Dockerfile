# BookRS-Platform — ingestion service
#
# Reads a Koha library's catalogue, ratings and review flags from its
# database, read-only (bookrs/ingestion/koha_db.py, direct.py).
# Python 3.13 (not 3.14) because several pinned dependencies do not yet
# publish wheels for 3.14; revisit once they do.
FROM python:3.13-slim

# Non-root by default. This container reaches a library's production
# database, so it gets no more privilege than it needs.
RUN useradd --create-home --uid 1000 bookrs

WORKDIR /app

# Dependencies first, so code edits don't invalidate the layer cache.
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/

USER bookrs

# No CMD: the scheduler (bookrs/schedule.py) or `docker compose run`
# says what to run.
