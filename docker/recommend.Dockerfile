# BookRS-Platform — matrix factorisation.
#
# Multi-stage, because implicit ships no Linux wheels and has to be
# compiled from source: the compiler is needed to build it and has no
# reason to remain in the image that runs it.
FROM python:3.12-slim AS build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-recommend.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-recommend.txt


FROM python:3.12-slim

# implicit's solver is OpenMP-parallel, so libgomp must be present at
# runtime. It arrives with build-essential in the build stage and does
# not survive into a slim runtime -- the compiled extension then imports
# with "libgomp.so.1: cannot open shared object file". Only the runtime
# library is needed here, not the toolchain.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# implicit and OpenBLAS both parallelise, and left alone they
# oversubscribe the CPU and run slower than single-threaded. implicit
# warns about this on import; setting it here is the fix rather than the
# warning.
ENV OPENBLAS_NUM_THREADS=1
ENV MKL_NUM_THREADS=1

RUN useradd --create-home --uid 1000 bookrs
WORKDIR /app

COPY --from=build /install /usr/local

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/
COPY pytest.ini .

USER bookrs
