"""Harvest circulation history from a library's ILS.

OAI-PMH carries no patron data -- deliberately, since that is the
privacy-sensitive part of a library's records. Item-level checkout
totals do arrive (MARC 952$l), but they are aggregate: "borrowed 47
times" cannot be factorised into a user-item matrix. Collaborative
filtering needs per-patron history, and that comes from Koha's REST API.

Patron identifiers are never stored. Each one is replaced by an HMAC
keyed on a per-deployment secret, so the same patron always maps to the
same reference and the factorisation works identically, while this
service holds nothing that identifies a person.

This is pseudonymisation, not anonymisation: anyone holding both the
secret and the library's own database could re-identify. It removes
casual exposure and bounds the damage from a leak of this database
alone. A library's privacy policy still has to cover holding borrowing
history at all, and many libraries deliberately purge it.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

import httpx

log = logging.getLogger(__name__)

# Koha embeds the full bibliographic record when asked for item.biblio,
# which is wasteful given we already hold it from the OAI harvest -- but
# it is the only way to get a biblio_id without a second request per
# item, and at a few hundred bytes per row that trade is worth making.
EMBED = "item.biblio"


class CirculationError(Exception):
    """Circulation history could not be harvested."""


@dataclass
class CirculationConfig:
    base_url: str
    username: str
    password: str

    # Per-deployment secret for the patron HMAC. Without it the
    # references cannot be correlated back to the library's records.
    # A deployment that has not set one must fail rather than fall back
    # to a default, which would make every installation's references
    # identical and therefore correlatable across libraries.
    patron_secret: str

    page_size: int = 100
    timeout: float = 60.0
    max_pages: int = 100_000
    include_current: bool = True
    include_history: bool = True

    def __post_init__(self) -> None:
        if not self.patron_secret:
            raise CirculationError(
                "patron_secret is required. It keys the HMAC that replaces "
                "patron identifiers; without one this service would store "
                "identifiers that link directly to named library members."
            )


@dataclass
class Loan:
    """One patron borrowing one work."""

    # The ILS's own identifier for this loan. Not the item id: a copy is
    # borrowed many times, so keying on it would make two patrons'
    # separate loans of the same copy collide and overwrite each other.
    loan_id: int
    patron_ref: str
    biblio_id: int
    item_id: int | None
    checkout_date: datetime | None
    checkin_date: datetime | None
    renewals: int = 0

    @property
    def is_current(self) -> bool:
        return self.checkin_date is None

    @property
    def days_held(self) -> float | None:
        """How long the patron kept it.

        A candidate signal for the confidence formula: a book returned
        the next day and one kept for six weeks are different
        endorsements. Unverified -- it is an intuition, not a
        measurement.
        """
        if not self.checkout_date or not self.checkin_date:
            return None
        return (self.checkin_date - self.checkout_date).total_seconds() / 86400


@dataclass
class CirculationStats:
    requests: int = 0
    loans: int = 0
    current: int = 0
    historical: int = 0
    skipped_no_biblio: int = 0
    errors: list[str] = field(default_factory=list)


def patron_reference(patron_id: int | str, secret: str) -> str:
    """Map a patron identifier to an opaque, stable reference.

    HMAC rather than a plain hash: a bare SHA-256 of a small integer
    space is trivially reversible by enumeration, since a library has
    at most a few hundred thousand patrons and computing every hash
    takes seconds.
    """
    digest = hmac.new(secret.encode("utf-8"), str(patron_id).encode("utf-8"), sha256)
    return digest.hexdigest()[:32]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fetch_page(client: httpx.Client, cfg: CirculationConfig, *,
                checked_in: bool, page: int,
                stats: CirculationStats) -> tuple[list[dict], bool]:
    """Return one page of checkouts and whether more follow."""
    params = {"_page": page, "_per_page": cfg.page_size}
    if checked_in:
        params["checked_in"] = "true"

    response = client.get(
        f"{cfg.base_url.rstrip('/')}/api/v1/checkouts",
        params=params,
        headers={"x-koha-embed": EMBED},
        auth=(cfg.username, cfg.password),
        timeout=cfg.timeout,
    )
    stats.requests += 1

    if response.status_code == 401:
        raise CirculationError(
            "Authentication failed. Check the credentials, and that Koha's "
            "RESTBasicAuth system preference is enabled."
        )
    if response.status_code == 403:
        raise CirculationError(
            "Access forbidden. The API user needs the circulate permission "
            "to read checkout history."
        )
    if response.status_code >= 400:
        raise CirculationError(
            f"HTTP {response.status_code} from the checkouts endpoint: "
            f"{response.text[:200]}"
        )

    rows = response.json()
    # Koha returns RFC 5988 Link headers. A rel="next" is the only
    # reliable signal that more pages exist -- a short page is not,
    # since the last page can be exactly page_size long.
    has_next = 'rel="next"' in response.headers.get("link", "")
    return rows, has_next


def _to_loan(row: dict, secret: str) -> Loan | None:
    """Convert one API row, or None when it cannot be used."""
    patron_id = row.get("patron_id")
    biblio = (row.get("item") or {}).get("biblio") or {}
    biblio_id = biblio.get("biblio_id")

    # Patron, work and loan identifier are all required. A loan without a
    # patron cannot contribute to a user-item matrix; one without a work
    # cannot be attributed to anything the recommender knows about; and
    # one without its own identifier cannot be upserted, so a re-harvest
    # would duplicate or overwrite it.
    if patron_id is None or biblio_id is None:
        return None

    loan_id = row.get("checkout_id")
    if loan_id is None:
        return None

    return Loan(
        loan_id=int(loan_id),
        patron_ref=patron_reference(patron_id, secret),
        biblio_id=int(biblio_id),
        item_id=row.get("item_id"),
        checkout_date=_parse_datetime(row.get("checkout_date")),
        checkin_date=_parse_datetime(row.get("checkin_date")),
        renewals=int(row.get("renewals_count") or 0),
    )


def harvest_loans(cfg: CirculationConfig,
                  stats: CirculationStats | None = None) -> Iterator[Loan]:
    """Stream every loan the API will give us, current and historical."""
    stats = stats if stats is not None else CirculationStats()

    with httpx.Client(follow_redirects=True) as client:
        for checked_in in ([False] if cfg.include_current else []) + \
                          ([True] if cfg.include_history else []):
            page = 1
            while page <= cfg.max_pages:
                rows, has_next = _fetch_page(client, cfg, checked_in=checked_in,
                                             page=page, stats=stats)
                for row in rows:
                    loan = _to_loan(row, cfg.patron_secret)
                    if loan is None:
                        stats.skipped_no_biblio += 1
                        continue
                    stats.loans += 1
                    if loan.is_current:
                        stats.current += 1
                    else:
                        stats.historical += 1
                    yield loan

                if not has_next:
                    break
                page += 1
            else:
                raise CirculationError(
                    f"Exceeded max_pages ({cfg.max_pages}); the endpoint may "
                    f"not be advancing."
                )
