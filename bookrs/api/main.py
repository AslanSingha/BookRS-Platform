"""Public API.

Read-only, and reads only BookRS-Platform's own database. It never
contacts the library's ILS: catalogue data arrives through the
ingestion service on a schedule, and availability here is as fresh as
the last sync.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query
from psycopg_pool import ConnectionPool

from bookrs.api import queries

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=8,
                          open=True)
    yield
    pool.close()


app = FastAPI(
    title="BookRS-Platform",
    description="Recommendations for a library's own catalogue.",
    version="0.1.0",
    lifespan=lifespan,
)


def _summary(work: queries.WorkSummary) -> dict:
    return {
        "id": work.id,
        "title": work.title,
        "authors": work.authors,
        "publisher": work.publisher,
        "publication_year": work.publication_year,
        "languages": work.languages,
        "subjects": work.subjects,
        "isbns": work.isbns,
        "availability": {
            "total": work.copies_total,
            "available": work.copies_available,
            "is_available": work.is_available,
        },
        **({"score": round(work.score, 4)} if work.score is not None else {}),
    }


@app.get("/health")
def health() -> dict:
    """Liveness plus a summary of what is loaded.

    A library's monitoring needs more than 'the process is up' -- an
    empty catalogue or a missing embedding run are the failures worth
    catching, and both are visible here.
    """
    with pool.connection() as conn:
        works, items, vectors = conn.execute(
            "SELECT (SELECT count(*) FROM works WHERE deleted_at IS NULL),"
            "       (SELECT count(*) FROM items),"
            "       (SELECT count(*) FROM embeddings)"
        ).fetchone()
        last = conn.execute("SELECT max(last_harvest) FROM sources").fetchone()[0]
    return {
        "status": "ok",
        "works": works,
        "items": items,
        "embeddings": vectors,
        "unembedded": works - vectors,
        "last_harvest": last.isoformat() if last else None,
    }


@app.get("/works/{work_id}")
def get_work(work_id: int) -> dict:
    with pool.connection() as conn:
        work = queries.get_work(conn, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="No such work")
    return _summary(work)


@app.get("/works/{work_id}/similar")
def similar(
    work_id: int,
    limit: int = Query(10, ge=1, le=50),
    exclude_title_only: bool = Query(
        False,
        description="Skip works whose embedding came from a title alone. "
                    "About a third of a typical catalogue.",
    ),
) -> dict:
    with pool.connection() as conn:
        if queries.get_work(conn, work_id) is None:
            raise HTTPException(status_code=404, detail="No such work")
        results = queries.similar_works(conn, work_id, limit=limit,
                                        exclude_title_only=exclude_title_only)
    return {"work_id": work_id, "count": len(results),
            "results": [_summary(w) for w in results]}


@app.get("/search/exact")
def search_exact(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Deterministic lookup by ISBN, title or author.

    Distinct from semantic search by design: a patron holding an ISBN
    wants that book, not books whose descriptions resemble it.
    """
    with pool.connection() as conn:
        results = queries.search_exact(conn, q, limit=limit)
    return {"query": q, "count": len(results),
            "results": [_summary(w) for w in results]}
