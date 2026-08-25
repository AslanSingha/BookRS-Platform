"""Persist circulation history.

The join between a loan and a work is the risky part. Koha's REST API
identifies a record by biblionumber; the OAI harvest identifies the same
record as "KOHA-OAI-TEST:345", prefixed by the operator-configurable
archiveID. Two identifier spaces for one record.

That is the same shape as the defect documented in section 4 of
docs/marc-field-analysis.md, and the consequence would be the same: a
join that silently attaches loans to the wrong works, producing
recommendations that look plausible and are wrong. So the mapping is
built explicitly and verified, rather than assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from bookrs.ingestion.circulation import Loan

log = logging.getLogger(__name__)


@dataclass
class LoanLoadStats:
    inserted: int = 0
    updated: int = 0
    unresolved: int = 0
    resolved_works: int = 0


def build_biblio_map(conn: psycopg.Connection, source_id: int) -> dict[int, int]:
    """Map the library's own record numbers to our work ids.

    Keyed on the suffix of the OAI identifier, which is the biblionumber
    for Koha. Records whose suffix is not numeric are excluded rather
    than coerced -- PMB, for instance, uses "oai:pmbtest:1", where the
    suffix is numeric, but another repository might not be.

    Duplicate suffixes within one source would make the mapping
    ambiguous. That cannot happen for Koha, where the suffix is the
    primary key, but it is checked rather than assumed.
    """
    rows = conn.execute(
        """
        SELECT id, split_part(source_record_id, ':', -1) AS suffix
        FROM works
        WHERE source_id = %s AND deleted_at IS NULL
        """,
        (source_id,),
    ).fetchall()

    mapping: dict[int, int] = {}
    collisions = 0
    for work_id, suffix in rows:
        if not suffix or not suffix.isdigit():
            continue
        key = int(suffix)
        if key in mapping:
            collisions += 1
            continue
        mapping[key] = work_id

    if collisions:
        log.warning(
            "%d record numbers appear more than once in source %s; those "
            "loans cannot be attributed and will be skipped",
            collisions, source_id,
        )
    return mapping


def load_loans(conn: psycopg.Connection, source_id: int,
               loans: Iterable[Loan]) -> LoanLoadStats:
    """Persist loans, resolving each to a work.

    A loan whose work is not in the catalogue is counted and dropped.
    That is expected rather than exceptional: a library circulates items
    the harvest has not caught up with, and records get deleted while
    their loan history remains.
    """
    stats = LoanLoadStats()
    biblio_map = build_biblio_map(conn, source_id)
    stats.resolved_works = len(biblio_map)
    log.info("resolved %d record numbers to works in source %s",
             len(biblio_map), source_id)

    seen_works: set[int] = set()
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO loans (source_id, work_id, patron_ref,
                                   source_loan_id, checked_out_at,
                                   checked_in_at, renewals)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, source_loan_id) DO UPDATE SET
                    checked_in_at = EXCLUDED.checked_in_at,
                    renewals      = EXCLUDED.renewals,
                    last_seen     = now()
                """,
                batch,
            )
        batch.clear()

    for loan in loans:
        work_id = biblio_map.get(loan.biblio_id)
        if work_id is None:
            stats.unresolved += 1
            continue
        seen_works.add(work_id)
        batch.append((source_id, work_id, loan.patron_ref, loan.loan_id,
                      loan.checkout_date, loan.checkin_date, loan.renewals))
        stats.inserted += 1
        if len(batch) >= 500:
            flush()
    flush()
    conn.commit()
    return stats
