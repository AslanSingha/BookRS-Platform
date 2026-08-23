"""Pre-harvest endpoint verification.

A Koha instance can lose its OAI configuration without any visible
failure. Observed during development: after a restart, the ``OAI-PMH``
and ``OAI-PMH:ConfFile`` preferences had reverted to their defaults. The
endpoint still answered, ``ListRecords`` still returned 200, records
still parsed -- they simply arrived with no holdings at all.

An adapter that verified configuration only at installation would
harvest bibliographic-only records indefinitely and discard every item,
raising nothing. In a library, the equivalent triggers are an upgrade, a
restore from backup, or a reverted configuration change.

See docs/marc-field-analysis.md section 12.1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from bookrs.ingestion.flavour import (
    Flavour,
    FlavourDetectionError,
    detect_flavour_from_response,
)
from bookrs.ingestion.harvest import (
    MARCXML_NS,
    OAI_NS,
    HarvestConfig,
    HarvestError,
    HarvestStats,
    _get,
)

log = logging.getLogger(__name__)

# Koha stores holdings in 952 under MARC21 and 995 under UNIMARC.
ITEM_TAG = {Flavour.MARC21: "952", Flavour.UNIMARC: "995"}


class PreflightError(HarvestError):
    """The endpoint is not in a state fit to harvest from."""


@dataclass
class PreflightResult:
    flavour: Flavour
    metadata_prefixes: list[str]
    records_sampled: int
    item_fields: int
    item_tag: str

    @property
    def has_items(self) -> bool:
        return self.item_fields > 0


def preflight(cfg: HarvestConfig, expect_items: bool | None = None) -> PreflightResult:
    """Verify an endpoint before harvesting from it.

    Checks, in order: the endpoint answers ``Identify``; the requested
    metadata prefix is actually offered; a first page of records parses
    and yields a consistent flavour; and holdings are present.

    ``expect_items`` should carry forward what the previous successful
    run observed. When the previous run saw holdings and this one does
    not, that is treated as a configuration regression and raises --
    because a catalogue genuinely losing every holding overnight is far
    less likely than a preference having been reset, and continuing
    would silently delete every item we hold.
    """
    stats = HarvestStats()
    try:
        with httpx.Client(follow_redirects=True) as client:
            _get(client, cfg.base_url, {"verb": "Identify"}, cfg, stats)

            root = _get(client, cfg.base_url,
                        {"verb": "ListMetadataFormats"}, cfg, stats)
            prefixes = [
            (el.text or "").strip()
                for el in root.findall(f".//{{{OAI_NS}}}metadataPrefix")
            ]
            if cfg.metadata_prefix not in prefixes:
                raise PreflightError(
                    f"The endpoint does not offer metadataPrefix "
                    f"'{cfg.metadata_prefix}'. Available: {prefixes or 'none'}. "
                    f"Note that setting OAI-PMH:ConfFile restricts the offered "
                    f"formats to those the file declares."
                )

            page = _get(
                client, cfg.base_url,
                {"verb": "ListRecords", "metadataPrefix": cfg.metadata_prefix},
                cfg, stats,
            )
    except PreflightError:
        raise
    except HarvestError as exc:
        # Transport, HTTP and parse failures reach here. Re-raised as
        # PreflightError so a caller can distinguish "this endpoint is not
        # fit to harvest from" -- typically a configuration problem an
        # operator can fix -- from a failure partway through a harvest.
        raise PreflightError(f"Endpoint is not ready to harvest: {exc}") from exc

    try:
        flavour = detect_flavour_from_response(
            ET.tostring(page, encoding="utf-8")
        )
    except FlavourDetectionError as exc:
        raise PreflightError(f"Could not determine MARC flavour: {exc}") from exc

    item_tag = ITEM_TAG[flavour]
    marc_records = page.findall(
        f".//{{{OAI_NS}}}record/{{{OAI_NS}}}metadata/{{{MARCXML_NS}}}record"
    )
    item_fields = sum(
        1
        for record in marc_records
        for field in record.findall(f"{{{MARCXML_NS}}}datafield")
        if field.get("tag") == item_tag
    )

    result = PreflightResult(
        flavour=flavour,
        metadata_prefixes=prefixes,
        records_sampled=len(marc_records),
        item_fields=item_fields,
        item_tag=item_tag,
    )

    if expect_items and not result.has_items:
        raise PreflightError(
            f"No {item_tag} holdings in the first {result.records_sampled} "
            f"records, but the previous run found some. This usually means "
            f"OAI-PMH:ConfFile has been unset or no longer enables "
            f"include_items for prefix '{cfg.metadata_prefix}'. Refusing to "
            f"harvest, because proceeding would remove every item currently "
            f"held. Restore the configuration, or pass expect_items=False if "
            f"the holdings were genuinely withdrawn."
        )

    log.info(
        "preflight ok: flavour=%s prefixes=%s sampled=%d %s fields=%d",
        flavour.value, prefixes, result.records_sampled, item_tag, item_fields,
    )
    return result
