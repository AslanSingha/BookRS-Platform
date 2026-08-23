# BookRS-Platform — ingestion service
#
# Harvests bibliographic records from a library's ILS over OAI-PMH.
# Python 3.13 (not 3.14) because several pinned dependencies do not yet
# publish wheels for 3.14; revisit once they do.
FROM python:3.13-slim

# Non-root by default. This container reaches out to a library's
# production OAI endpoint, so it gets no more privilege than it needs.
RUN useradd --create-home --uid 1000 bookrs

WORKDIR /app

# Dependencies first, so code edits don't invalidate the layer cache.
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/

USER bookrs

# No CMD yet — nothing to run until the harvest entrypoint exists.
# Until then this image is driven via `docker compose run`.
