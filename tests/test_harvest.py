"""Tests for the OAI-PMH harvest loop.

Mocked rather than live, so the behaviours that matter are exercised
deterministically: termination, retry policy, error handling, and the
protocol rule that a resumption token travels alone.
"""

import httpx
import pytest

from bookrs.ingestion.harvest import (
    HarvestConfig,
    HarvestError,
    HarvestStats,
    OAIProtocolError,
    harvest_records,
)

OAI = "http://www.openarchives.org/OAI/2.0/"
MX = "http://www.loc.gov/MARC21/slim"
URL = "http://koha.test/cgi-bin/koha/oai.pl"


def _page(n_records: int, token: str | None, start: int = 1) -> str:
    records = "".join(
        f"<record><header><identifier>KOHA-OAI-TEST:{start+i}</identifier></header>"
        f"<metadata><record xmlns='{MX}'>"
        f"<datafield tag='245' ind1='1' ind2='0'>"
        f"<subfield code='a'>Title {start+i}</subfield></datafield>"
        f"</record></metadata></record>"
        for i in range(n_records)
    )
    tok = f"<resumptionToken cursor='{start-1}'>{token}</resumptionToken>" if token else ""
    return (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
            f"<ListRecords>{records}{tok}</ListRecords></OAI-PMH>")


class TestTermination:
    def test_follows_tokens_to_exhaustion(self, httpx_mock):
        httpx_mock.add_response(text=_page(50, "tok/50", 1))
        httpx_mock.add_response(text=_page(50, "tok/100", 51))
        httpx_mock.add_response(text=_page(36, None, 101))
        stats = HarvestStats()
        assert len(list(harvest_records(HarvestConfig(base_url=URL), stats))) == 136
        assert stats.pages == 3

    def test_full_final_page_is_not_a_stop_signal(self, httpx_mock):
        """A page of exactly page_size can still be the last one -- and a
        full page mid-harvest must not be mistaken for the end."""
        httpx_mock.add_response(text=_page(50, "tok/50", 1))
        httpx_mock.add_response(text=_page(50, None, 51))
        assert len(list(harvest_records(HarvestConfig(base_url=URL)))) == 100

    def test_empty_token_terminates_like_a_missing_one(self, httpx_mock):
        """The specification permits an empty element; Koha omits it."""
        httpx_mock.add_response(text=_page(10, "", 1))
        assert len(list(harvest_records(HarvestConfig(base_url=URL)))) == 10

    def test_single_page_harvest(self, httpx_mock):
        httpx_mock.add_response(text=_page(5, None))
        assert len(list(harvest_records(HarvestConfig(base_url=URL)))) == 5


class TestProtocol:
    def test_token_is_sent_alone(self, httpx_mock):
        """Repeating metadataPrefix alongside a token is a badArgument."""
        httpx_mock.add_response(text=_page(2, "tok/2", 1))
        httpx_mock.add_response(text=_page(2, None, 3))
        list(harvest_records(HarvestConfig(base_url=URL)))
        second = httpx_mock.get_requests()[1].url
        assert "resumptionToken=" in str(second)
        assert "metadataPrefix" not in str(second)

    def test_from_until_set_are_passed(self, httpx_mock):
        httpx_mock.add_response(text=_page(1, None))
        list(harvest_records(HarvestConfig(
            base_url=URL, from_="2026-01-01", until="2026-06-30", set_="fiction")))
        url = str(httpx_mock.get_requests()[0].url)
        assert "from=2026-01-01" in url and "until=2026-06-30" in url and "set=fiction" in url

    def test_deleted_records_are_yielded_and_counted(self, httpx_mock):
        """deletedRecord is 'persistent', so deletions arrive as headers
        with no metadata. Dropping them would strand removed works."""
        body = (f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'><ListRecords>"
                f"<record><header status='deleted'>"
                f"<identifier>KOHA-OAI-TEST:7</identifier></header></record>"
                f"</ListRecords></OAI-PMH>")
        httpx_mock.add_response(text=body)
        stats = HarvestStats()
        assert len(list(harvest_records(HarvestConfig(base_url=URL), stats))) == 1
        assert stats.deleted == 1


class TestErrors:
    def test_oai_error_element_raises(self, httpx_mock):
        httpx_mock.add_response(text=f"<?xml version='1.0'?><OAI-PMH xmlns='{OAI}'>"
                                     f"<error code='badArgument'>Bad verb</error></OAI-PMH>")
        with pytest.raises(OAIProtocolError) as exc:
            list(harvest_records(HarvestConfig(base_url=URL)))
        assert exc.value.code == "badArgument"

    def test_5xx_is_retried_then_succeeds(self, httpx_mock):
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(text=_page(3, None))
        stats = HarvestStats()
        cfg = HarvestConfig(base_url=URL, retry_backoff=1.0)
        assert len(list(harvest_records(cfg, stats))) == 3
        assert stats.retries == 1

    def test_4xx_is_not_retried(self, httpx_mock):
        """Retrying a client error wastes the library's server time."""
        httpx_mock.add_response(status_code=404, text="<html><body>not found</body></html>")
        with pytest.raises(HarvestError, match="HTTP 404"):
            list(harvest_records(HarvestConfig(base_url=URL)))
        assert len(httpx_mock.get_requests()) == 1

    def test_disabled_endpoint_is_explained(self, httpx_mock):
        httpx_mock.add_response(status_code=404, text="<!DOCTYPE html><html>...</html>")
        with pytest.raises(HarvestError, match="OAI-PMH is not enabled"):
            list(harvest_records(HarvestConfig(base_url=URL)))

    def test_exhausted_retries_raise(self, httpx_mock):
        for _ in range(4):
            httpx_mock.add_response(status_code=500)
        cfg = HarvestConfig(base_url=URL, max_retries=3, retry_backoff=1.0)
        with pytest.raises(HarvestError, match="Giving up"):
            list(harvest_records(cfg))


class TestBounds:
    """Bounds exist so a malformed or non-advancing token cannot run
    forever. Both tests stop the harvester early by design, leaving
    registered responses unconsumed, so the strict default is relaxed
    here only."""

    @pytest.fixture
    def assert_all_responses_were_requested(self) -> bool:
        return False

    def test_max_pages_stops_a_non_advancing_token(self, httpx_mock):
        """A token that never resolves would otherwise loop forever."""
        # is_reusable: the harvester stops at max_pages, so a fixed queue
        # of responses would be left partly unconsumed.
        httpx_mock.add_response(text=_page(1, "stuck", 1))
        cfg = HarvestConfig(base_url=URL, max_pages=5)
        with pytest.raises(HarvestError, match="max_pages"):
            list(harvest_records(cfg))
        assert len(httpx_mock.get_requests()) == 5

    def test_max_seconds_bounds_a_long_harvest(self, httpx_mock):
        """A slow endpoint must not hold a scheduled sync open forever."""
        httpx_mock.add_response(text=_page(1, "tok", 1))
        cfg = HarvestConfig(base_url=URL, max_seconds=0.0)
        with pytest.raises(HarvestError, match="max_seconds"):
            list(harvest_records(cfg))
