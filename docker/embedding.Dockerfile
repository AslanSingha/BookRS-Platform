# BookRS-Platform — embedding service.
#
# CPU-only PyTorch. A library's server almost certainly has no GPU, and
# the deployment model is self-hosting on whatever hardware the library
# already runs. The CPU wheel is also far smaller than the CUDA one.
FROM python:3.13-slim

RUN useradd --create-home --uid 1000 bookrs

WORKDIR /app

COPY requirements-embed.txt .
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements-embed.txt

# Model weights are downloaded on first use and cached here. Mounted as
# a volume in compose so a container restart does not re-download.
ENV HF_HOME=/app/.cache/huggingface
RUN mkdir -p /app/.cache/huggingface && chown -R bookrs /app/.cache

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/
COPY pytest.ini .

USER bookrs
