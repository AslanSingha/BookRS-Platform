# BookRS-Platform — the test environment.
#
# Separate from the service images deliberately. The widget is
# JavaScript and its tests need a JS runtime; the services are Python
# and ship without one. Installing node into recommend so that tests
# could run would put a toolchain into a production image for a test's
# convenience — the same thing the recommend/embedding split exists to
# avoid.
#
# The build stage below is identical to recommend.Dockerfile's, so
# BuildKit reuses those layers rather than compiling implicit twice.
# That identity is load-bearing and is asserted by
# tests/test_dockerfiles.py: if the two drift, the suite stops
# exercising what the service actually runs, and nothing else would
# notice.
FROM python:3.12-slim AS build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-recommend.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-recommend.txt


FROM python:3.12-slim

# libgomp1 for implicit's OpenMP solver, as in recommend.Dockerfile.
#
# nodejs for the widget harness. Without --no-install-recommends this
# pulls npm, which is the larger half of the install and buys nothing:
# the harness deliberately has no package dependencies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 nodejs \
 && rm -rf /var/lib/apt/lists/*

ENV OPENBLAS_NUM_THREADS=1
ENV MKL_NUM_THREADS=1

RUN useradd --create-home --uid 1000 bookrs
WORKDIR /app

COPY --from=build /install /usr/local

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/
COPY pytest.ini .

USER bookrs
