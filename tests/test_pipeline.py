"""Tests for the end-to-end ingestion pipeline.

Mocked, so the composition is exercised deterministically: that
preflight gates the harvest, that a bad record does not abort a run, and
that a flavour change partway through is caught.
"""

import pytest

from bookrs.ingestion.flavour import Flavour
from bookrs.ingestion.harvest import HarvestConfig
from bookrs.ingestion.pipeline import ingest
from bookrs.ingestion.preflight import PreflightError

@pytest.fixture
def assert_all_responses_were_requested() -> bool:
    """Several tests stop consuming partway through by design, leaving
    registered responses unused. What each run actually did is asserted
    through IngestResult rather than through response consumption."""
    return False


OAI = "http://www.openarchives.org/OAI/2.0/"
MX = "http://www.loc.gov/MARC21/slim"
URL = "http://koha.test/cgi-bin/koha/oai.pl"


def _identify() -> str:
    return f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'><Identify/></OAI-PMH>"


def _formats(*p: str) -> str:
    inner = "".join(f"<metadataFormat><metadataPrefix>{x}</metadataPrefix></metadataFormat>" for x in p)
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
            f"<ListMetadataFormats>{inner}</ListMetadataFormats></OAI-PMH>")


def _marc21(i: int, items: int = 1) -> str:
    it = "".join(f"<datafield tag='952' ind1=' ' ind2=' '>"
                 f"<subfield code='p'>bc{i}</subfield></datafield>" for _ in range(items))
    return (f"<record><header><identifier>KOHA-OAI-TEST:{i}</identifier></header>"
            f"<metadata><record xmlns='{MX}'>"
            f"<datafield tag='245' ind1='1' ind2='0'>"
            f"<subfield code='a'>Title {i}</subfield></datafield>{it}"
            f"</record></metadata></record>")


def _unimarc(i: int) -> str:
    return (f"<record><header><identifier>KOHA-OAI-TEST:{i}</identifier></header>"
            f"<metadata><record xmlns='{MX}'>"
            f"<datafield tag='200' ind1='1' ind2=' '>"
            f"<subfield code='a'>Titre {i}</subfield></datafield>"
            f"</record></metadata></record>")


def _deleted(i: int) -> str:
    return (f"<record><header status='deleted'>"
            f"<identifier>KOHA-OAI-TEST:{i}</identifier></header></record>")


def _no_payload(i: int) -> str:
    return (f"<record><header><identifier>KOHA-OAI-TEST:{i}</identifier>"
            f"</header><metadata></metadata></record>")


def _unreadable(i: int) -> str:
    """Neither 245 nor 200: flavour cannot be determined."""
    return (f"<record><header><identifier>KOHA-OAI-TEST:{i}</identifier></header>"
            f"<metadata><record xmlns='{MX}'>"
            f"<datafield tag='260' ind1=' ' ind2=' '>"
            f"<subfield code='a'>NY</subfield></datafield>"
            f"</record></metadata></record>")


def _list(*records: str) -> str:
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
            f"<ListRecords>{''.join(records)}</ListRecords></OAI-PMH>")


def _setup(mock, *pages: str, formats: tuple[str, ...] = ("marc21",),
           sample: str | None = None):
    """Register a full exchange: Identify, ListMetadataFormats, then the
    page preflight samples, then the harvest pages.

    preflight is a real gate -- it fetches and inspects a ListRecords
    page of its own before the harvest starts. Tests exercising
    malformed records must still hand it a healthy sample, or they fail
    at preflight and never reach the behaviour under test.
    """
    mock.add_response(text=_identify())
    mock.add_response(text=_formats(*formats))
    mock.add_response(text=sample if sample is not None else pages[0])
    for page in pages:
        mock.add_response(text=page)


class TestHappyPath:
    def test_maps_every_record(self, httpx_mock):
        _setup(httpx_mock, _list(*(_marc21(i) for i in range(1, 4))))
        stream, res = ingest(HarvestConfig(base_url=URL))
        works = list(stream)
        assert len(works) == 3 and res.works == 3
        assert res.unmappable == 0 and res.flavour_changes == 0
        assert [w.title for w in works] == ["Title 1", "Title 2", "Title 3"]

    def test_result_fills_in_as_the_stream_is_consumed(self, httpx_mock):
        """The caller drives iteration; a large catalogue is never held."""
        _setup(httpx_mock, _list(*(_marc21(i) for i in range(1, 4))))
        stream, res = ingest(HarvestConfig(base_url=URL))
        assert res.works == 0
        next(stream)
        assert res.works == 1

    def test_preflight_flavour_is_reported(self, httpx_mock):
        _setup(httpx_mock, _list(_marc21(1)))
        _, res = ingest(HarvestConfig(base_url=URL))
        assert res.preflight.flavour is Flavour.MARC21
        assert res.preflight.item_tag == "952"


class TestDeletedRecords:
    def test_deleted_records_are_yielded_not_dropped(self, httpx_mock):
        """The caller must remove them; swallowing them here would
        strand deleted works in the database forever."""
        _setup(httpx_mock, _list(_marc21(1), _deleted(2), _marc21(3)))
        stream, res = ingest(HarvestConfig(base_url=URL))
        works = list(stream)
        assert len(works) == 3
        assert [w.deleted for w in works] == [False, True, False]
        assert res.deleted == 1 and res.works == 2

    def test_deleted_record_carries_its_identifier(self, httpx_mock):
        _setup(httpx_mock, _list(_deleted(7)), sample=_list(_marc21(1)))
        stream, _ = ingest(HarvestConfig(base_url=URL))
        assert next(iter(stream)).source_record_id == "KOHA-OAI-TEST:7"


class TestBadRecords:
    def test_one_bad_record_does_not_abort_the_run(self, httpx_mock):
        _setup(httpx_mock, _list(_marc21(1), _unreadable(2), _marc21(3)))
        stream, res = ingest(HarvestConfig(base_url=URL))
        assert len(list(stream)) == 2
        assert res.unmappable == 1 and len(res.errors) == 1

    def test_missing_payload_is_counted(self, httpx_mock):
        _setup(httpx_mock, _list(_marc21(1), _no_payload(2)))
        stream, res = ingest(HarvestConfig(base_url=URL))
        list(stream)
        assert res.unmappable == 1
        assert "no MARCXML payload" in res.errors[0]

    def test_errors_name_the_record(self, httpx_mock):
        _setup(httpx_mock, _list(_unreadable(42)), sample=_list(_marc21(1)))
        stream, res = ingest(HarvestConfig(base_url=URL))
        list(stream)
        assert "KOHA-OAI-TEST:42" in res.errors[0]


class TestFlavourConsistency:
    def test_flavour_change_mid_harvest_is_caught(self, httpx_mock):
        """Preflight samples five records; a mixed feed would pass it.
        Applying the wrong map yields plausible nonsense, so every
        record is checked."""
        # The mixed record must appear AFTER the page preflight samples.
        # Preflight already refuses a mixed first page; this guards the
        # case where flavour changes on, say, page 40 of 97.
        _setup(httpx_mock, _list(_marc21(1), _unimarc(2), _marc21(3)),
               sample=_list(_marc21(1)))
        stream, res = ingest(HarvestConfig(base_url=URL))
        works = list(stream)
        assert len(works) == 2
        assert res.flavour_changes == 1
        assert "UNIMARC" in res.errors[0] and "MARC21" in res.errors[0]


class TestPreflightGates:
    def test_missing_prefix_stops_before_harvesting(self, httpx_mock):
        httpx_mock.add_response(text=_identify())
        httpx_mock.add_response(text=_formats("oai_dc"))
        with pytest.raises(PreflightError, match="does not offer"):
            ingest(HarvestConfig(base_url=URL))
        assert len(httpx_mock.get_requests()) == 2   # never reached ListRecords

    def test_lost_holdings_stop_the_run(self, httpx_mock):
        """The silent-configuration-loss guard, in the real code path."""
        _setup(httpx_mock, _list(_marc21(1, items=0)))
        with pytest.raises(PreflightError, match="OAI-PMH:ConfFile"):
            ingest(HarvestConfig(base_url=URL), expect_items=True)
