# BookRS-Platform — public API.
#
# The library's OPAC widget calls this. It reads the local database and
# never touches the ILS.
FROM python:3.13-slim

RUN useradd --create-home --uid 1000 bookrs
WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

COPY bookrs/ ./bookrs/
COPY tests/ ./tests/
COPY pytest.ini .

USER bookrs
EXPOSE 8000
CMD ["uvicorn", "bookrs.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
