"""End-to-end ingestion: verify, harvest, map.

Composes the adapter's modules into a single callable. Nothing here is
new logic; the value is that the sequence exists in one place, with
preflight actually gating the harvest rather than being available to be
forgotten.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from bookrs.ingestion.fieldmap import Work, map_record
from bookrs.ingestion.flavour import Flavour, detect_flavour
from bookrs.ingestion.harvest import (
    MARCXML_NS,
    OAI_NS,
    HarvestConfig,
    HarvestStats,
    harvest_records,
)
from bookrs.ingestion.preflight import PreflightResult, preflight

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    preflight: PreflightResult
    stats: HarvestStats
    works: int = 0
    deleted: int = 0
    unmappable: int = 0
    flavour_changes: int = 0
    errors: list[str] = field(default_factory=list)


def ingest(cfg: HarvestConfig, *, expect_items: bool | None = None,
           ) -> tuple[Iterator[Work], IngestResult]:
    """Verify the endpoint, then stream mapped works from it.

    Returns the iterator and a result object that fills in as the
    iterator is consumed. The caller drives iteration, so a large
    catalogue never has to be held in memory.

    Flavour is established once by preflight and reused, but each
    record is still checked: a feed that changes flavour partway
    through is not something this pipeline can handle correctly, and
    silently applying the wrong map is the failure this whole adapter
    exists to prevent.
    """
    check = preflight(cfg, expect_items=expect_items)
    result = IngestResult(preflight=check, stats=HarvestStats())
    log.info(
        "ingesting: flavour=%s prefix=%s items=%s",
        check.flavour.value, cfg.metadata_prefix,
        "present" if check.has_items else "absent",
    )

    def _stream() -> Iterator[Work]:
        for record in harvest_records(cfg, result.stats):
            identifier_el = record.find(f".//{{{OAI_NS}}}identifier")
            identifier = (identifier_el.text or "") if identifier_el is not None else ""

            header = record.find(f"{{{OAI_NS}}}header")
            if header is not None and header.get("status") == "deleted":
                result.deleted += 1
                yield Work(source_record_id=identifier,
                           flavour=check.flavour, deleted=True)
                continue

            marc = record.find(f".//{{{MARCXML_NS}}}record")
            if marc is None:
                result.unmappable += 1
                result.errors.append(f"{identifier}: no MARCXML payload")
                continue

            try:
                flavour = detect_flavour(marc)
            except Exception as exc:
                # One unreadable record must not abort a harvest of
                # hundreds of thousands, but it is recorded rather than
                # dropped silently.
                result.unmappable += 1
                result.errors.append(f"{identifier}: {exc}")
                continue

            if flavour is not check.flavour:
                result.flavour_changes += 1
                result.errors.append(
                    f"{identifier}: flavour {flavour.value} differs from "
                    f"{check.flavour.value} established at preflight"
                )
                continue

            result.works += 1
            yield map_record(marc, flavour, identifier)

    return _stream(), result
