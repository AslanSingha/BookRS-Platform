"""Tests for pre-harvest endpoint verification.

The case that matters most is the silent one: an endpoint that answers
correctly, returns parseable records, and has quietly stopped including
holdings. Every visible signal says success.
"""

import pytest

from bookrs.ingestion.flavour import Flavour
from bookrs.ingestion.harvest import HarvestConfig
from bookrs.ingestion.preflight import PreflightError, preflight

OAI = "http://www.openarchives.org/OAI/2.0/"
MX = "http://www.loc.gov/MARC21/slim"
URL = "http://koha.test/cgi-bin/koha/oai.pl"


def _identify() -> str:
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'><Identify>"
            f"<baseURL>http://koha.test/opac/oai.pl</baseURL>"
            f"<deletedRecord>persistent</deletedRecord></Identify></OAI-PMH>")


def _formats(*prefixes: str) -> str:
    body = "".join(
        f"<metadataFormat><metadataPrefix>{p}</metadataPrefix></metadataFormat>"
        for p in prefixes
    )
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
            f"<ListMetadataFormats>{body}</ListMetadataFormats></OAI-PMH>")


def _records(n: int = 3, *, title_tag: str = "245", item_tag: str | None = "952",
             items_per_record: int = 2) -> str:
    items = "".join(
        f"<datafield tag='{item_tag}' ind1=' ' ind2=' '>"
        f"<subfield code='p'>bc_{i}</subfield></datafield>"
        for i in range(items_per_record)
    ) if item_tag else ""
    records = "".join(
        f"<record><header><identifier>KOHA-OAI-TEST:{i}</identifier></header>"
        f"<metadata><record xmlns='{MX}'>"
        f"<datafield tag='{title_tag}' ind1='1' ind2='0'>"
        f"<subfield code='a'>Title {i}</subfield></datafield>{items}"
        f"</record></metadata></record>"
        for i in range(1, n + 1)
    )
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
            f"<ListRecords>{records}</ListRecords></OAI-PMH>")


def _healthy(mock, **kw):
    mock.add_response(text=_identify())
    mock.add_response(text=_formats("marc21", "marcxml"))
    mock.add_response(text=_records(**kw))


class TestHealthyEndpoint:
    def test_marc21_with_items(self, httpx_mock):
        _healthy(httpx_mock)
        r = preflight(HarvestConfig(base_url=URL))
        assert r.flavour is Flavour.MARC21
        assert r.item_tag == "952" and r.item_fields == 6 and r.has_items

    def test_unimarc_looks_for_995_not_952(self, httpx_mock):
        """Item tag is chosen by detected flavour, not by convention."""
        httpx_mock.add_response(text=_identify())
        httpx_mock.add_response(text=_formats("marc21"))
        httpx_mock.add_response(text=_records(title_tag="200", item_tag="995"))
        r = preflight(HarvestConfig(base_url=URL))
        assert r.flavour is Flavour.UNIMARC
        assert r.item_tag == "995" and r.item_fields == 6

    def test_bib_only_endpoint_is_fine_when_nothing_is_expected(self, httpx_mock):
        """First run against a library that does not publish holdings."""
        _healthy(httpx_mock, item_tag=None)
        r = preflight(HarvestConfig(base_url=URL))
        assert not r.has_items


class TestSilentHoldingsLoss:
    def test_missing_items_raise_when_previous_run_saw_them(self, httpx_mock):
        """The whole point of this module."""
        _healthy(httpx_mock, item_tag=None)
        with pytest.raises(PreflightError, match="OAI-PMH:ConfFile"):
            preflight(HarvestConfig(base_url=URL), expect_items=True)

    def test_items_present_passes_the_same_check(self, httpx_mock):
        _healthy(httpx_mock)
        assert preflight(HarvestConfig(base_url=URL), expect_items=True).has_items

    def test_expect_items_false_permits_genuine_withdrawal(self, httpx_mock):
        _healthy(httpx_mock, item_tag=None)
        assert not preflight(HarvestConfig(base_url=URL), expect_items=False).has_items


class TestConfiguration:
    def test_missing_prefix_names_what_is_available(self, httpx_mock):
        """Setting OAI-PMH:ConfFile drops oai_dc; the inverse can happen too."""
        httpx_mock.add_response(text=_identify())
        httpx_mock.add_response(text=_formats("oai_dc"))
        with pytest.raises(PreflightError, match="does not offer"):
            preflight(HarvestConfig(base_url=URL, metadata_prefix="marc21"))

    def test_disabled_endpoint_is_explained(self, httpx_mock):
        httpx_mock.add_response(status_code=404, text="<!DOCTYPE html><html>..</html>")
        with pytest.raises(PreflightError, match="not enabled"):
            preflight(HarvestConfig(base_url=URL))

    def test_unrecognisable_records_raise(self, httpx_mock):
        httpx_mock.add_response(text=_identify())
        httpx_mock.add_response(text=_formats("marc21"))
        httpx_mock.add_response(text=_records(title_tag="260", item_tag=None))
        with pytest.raises(PreflightError, match="flavour"):
            preflight(HarvestConfig(base_url=URL))
