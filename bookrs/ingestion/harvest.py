"""OAI-PMH harvesting.

Implements the ListRecords/resumptionToken loop against an ILS endpoint.
Behaviour here is shaped by what Koha actually does rather than by the
OAI-PMH specification alone; see docs/marc-field-analysis.md.

Three constraints worth knowing before reading the code:

* Koha omits ``completeListSize``, emitting only ``cursor``. The
  harvester cannot know the total in advance, so there is no progress
  percentage and no completion check against an expected count.
* The final page omits the ``resumptionToken`` element entirely rather
  than sending an empty one. Termination is by absence, never by a page
  containing fewer records than the page size -- a full final page is
  possible.
* Tokens are opaque per the specification and are echoed verbatim. They
  contain slashes and must be URL-encoded when sent.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
MARCXML_NS = "http://www.loc.gov/MARC21/slim"

log = logging.getLogger(__name__)



# Characters XML 1.0 forbids outright, and the ISO 2022 escape sequences
# that carry them in practice.
#
# A catalogue converted from MARC-8 keeps its character-set switches:
# ESC ( Q ... ESC ( B around a curly apostrophe, for instance. Koha's
# importer passes them through, the ILS stores them, and its OAI server
# emits them raw inside XML -- where ESC (0x1B) is not a legal character
# at any encoding. One apostrophe in one record then makes a page of a
# hundred records unparseable, and with it the whole harvest.
#
# Stripping them is the conservative reading: the escape sequence is an
# instruction to a MARC-8 reader, not content, and what remains is the
# text a patron would see. The count is logged rather than passed over
# in silence -- a library seeing it has legacy encoding to clean up,
# and that is worth knowing.
_ISO2022_ESCAPE = re.compile(rb"\x1b(?:\$?[()*+][A-Za-z0-9!\"#$%&'-]|[@-_])")
_XML_ILLEGAL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitise_xml(payload: bytes) -> tuple[bytes, int]:
    """Remove bytes no XML parser will accept. Returns (bytes, removed)."""
    cleaned, n = _ISO2022_ESCAPE.subn(b"", payload)
    cleaned, m = _XML_ILLEGAL.subn(b"", cleaned)
    return cleaned, n + m


class HarvestError(Exception):
    """A harvest could not be completed."""


class OAIProtocolError(HarvestError):
    """The endpoint returned an OAI-PMH ``<error>`` element."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"OAI-PMH error [{code}]: {message}")


@dataclass
class HarvestStats:
    pages: int = 0
    records: int = 0
    deleted: int = 0
    retries: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


@dataclass
class HarvestConfig:
    base_url: str
    metadata_prefix: str = "marc21"
    from_: str | None = None
    until: str | None = None
    set_: str | None = None

    # Extra request headers. Needed for endpoints behind a reverse proxy
    # that routes by Host, and the natural place for a User-Agent
    # identifying this harvester to the library's server operators.
    headers: dict[str, str] = field(default_factory=dict)

    timeout: float = 60.0
    max_retries: int = 3
    retry_backoff: float = 2.0

    # A 4,849-record sample corpus took 97 pages at Koha's default page
    # size of 50. A real catalogue of a few hundred thousand records will
    # take thousands. These bounds exist to stop a malformed or looping
    # token running forever, not to limit legitimate harvests.
    max_pages: int = 100_000
    max_seconds: float = 6 * 60 * 60


def _check_oai_error(root: ET.Element) -> None:
    error = root.find(f"{{{OAI_NS}}}error")
    if error is not None:
        raise OAIProtocolError(error.get("code", "unknown"), (error.text or "").strip())


def _get(client: httpx.Client, url: str, params: dict[str, str], cfg: HarvestConfig,
         stats: HarvestStats) -> ET.Element:
    """Fetch and parse one OAI response, retrying transient failures."""
    last: Exception | None = None
    for attempt in range(cfg.max_retries + 1):
        if attempt:
            delay = cfg.retry_backoff ** attempt
            log.warning("retry %d/%d in %.1fs (%s)", attempt, cfg.max_retries, delay, last)
            time.sleep(delay)
            stats.retries += 1
        try:
            response = client.get(url, params=params, timeout=cfg.timeout,
                                  headers=cfg.headers or None)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            continue

        # 5xx is transient; 4xx is not, and retrying wastes the
        # library's server time on a request that cannot succeed.
        if response.status_code >= 500:
            last = HarvestError(f"HTTP {response.status_code}")
            continue
        if response.status_code >= 400:
            from bookrs.ingestion.flavour import _diagnose_non_xml
            raise HarvestError(
                f"HTTP {response.status_code} from {url}. "
                f"{_diagnose_non_xml(response.content)}"
            )

        try:
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                # Retry once on sanitised bytes before giving up.
                cleaned, removed = _sanitise_xml(response.content)
                if not removed:
                    raise
                root = ET.fromstring(cleaned)
                log.warning(
                    "removed %d character(s) this endpoint emitted that XML "
                    "forbids (legacy MARC-8 escape sequences, most likely); "
                    "the affected records are harvested without them", removed)
        except ET.ParseError as exc:
            from bookrs.ingestion.flavour import _diagnose_non_xml
            raise HarvestError(
                f"Response is not well-formed XML ({exc}). "
                f"{_diagnose_non_xml(response.content)}"
            ) from exc

        _check_oai_error(root)
        return root

    raise HarvestError(
        f"Giving up after {cfg.max_retries} retries against {url}: {last}"
    )


def _extract_token(root: ET.Element) -> str | None:
    """Return the resumption token, or None when the list is exhausted.

    Koha omits the element on the final page; the specification also
    permits an empty one. Both mean the same thing.
    """
    token_el = root.find(f".//{{{OAI_NS}}}resumptionToken")
    if token_el is None:
        return None
    token = (token_el.text or "").strip()
    return token or None


def harvest_records(cfg: HarvestConfig, stats: HarvestStats | None = None
                    ) -> Iterator[ET.Element]:
    """Yield OAI ``<record>`` elements, following resumption tokens.

    Streams rather than accumulating: a real catalogue does not fit
    comfortably in memory, and the caller decides what to retain.

    Deleted records are yielded too. Koha reports ``deletedRecord:
    persistent``, so a record leaving the catalogue arrives as a header
    with ``status="deleted"`` and no metadata. Callers must handle them
    rather than assuming every record carries a payload.
    """
    stats = stats if stats is not None else HarvestStats()
    params: dict[str, str] = {"verb": "ListRecords", "metadataPrefix": cfg.metadata_prefix}
    if cfg.from_:
        params["from"] = cfg.from_
    if cfg.until:
        params["until"] = cfg.until
    if cfg.set_:
        params["set"] = cfg.set_

    with httpx.Client(follow_redirects=True) as client:
        while True:
            if stats.pages >= cfg.max_pages:
                raise HarvestError(
                    f"Exceeded max_pages ({cfg.max_pages}) after {stats.records} "
                    f"records. This suggests a token that is not advancing."
                )
            if stats.elapsed > cfg.max_seconds:
                raise HarvestError(
                    f"Exceeded max_seconds ({cfg.max_seconds:.0f}) after "
                    f"{stats.pages} pages and {stats.records} records."
                )

            root = _get(client, cfg.base_url, params, cfg, stats)
            stats.pages += 1

            page_records = 0
            for record in root.findall(f".//{{{OAI_NS}}}record"):
                page_records += 1
                stats.records += 1
                header = record.find(f"{{{OAI_NS}}}header")
                if header is not None and header.get("status") == "deleted":
                    stats.deleted += 1
                yield record

            token = _extract_token(root)
            log.info("page %d: %d records (total %d), token=%s",
                     stats.pages, page_records, stats.records,
                     "present" if token else "none — list exhausted")
            if token is None:
                return

            # Only the token is sent on subsequent requests; repeating
            # metadataPrefix or from/until alongside it is a protocol error.
            params = {"verb": "ListRecords", "resumptionToken": token}
