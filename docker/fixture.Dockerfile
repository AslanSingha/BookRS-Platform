# Fixture building: the tools that stock the test library (Koha) with the
# UCSD data. The only image in this project whose work writes to Koha, and
# none of the model's services is built from it.
FROM python:3.12-slim
RUN useradd --create-home --uid 1000 bookrs
WORKDIR /app
COPY requirements-fixture.txt .
RUN pip install --no-cache-dir -r requirements-fixture.txt
USER bookrs
