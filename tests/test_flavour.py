"""Tests for MARC flavour detection.

Fixtures are trimmed from real Koha OAI-PMH responses (see
docs/marc-field-analysis.md). Note that both carry the MARC21/slim
namespace and were served under a metadata prefix named "marc21" --
that is the whole point of the module under test.
"""

import pytest
from xml.etree import ElementTree as ET

from bookrs.ingestion.flavour import (
    Flavour,
    FlavourDetectionError,
    detect_flavour,
    detect_flavour_from_response,
)

OAI = "http://www.openarchives.org/OAI/2.0/"
MX = "http://www.loc.gov/MARC21/slim"


def _envelope(records: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns='{OAI}'><ListRecords>{records}</ListRecords></OAI-PMH>""".encode()


def _record(datafields: str, identifier: str = "KOHA-OAI-TEST:1") -> str:
    return f"""<record><header><identifier>{identifier}</identifier></header>
<metadata><record xmlns='{MX}'><leader>01344cam a22003014i 4500</leader>
{datafields}</record></metadata></record>"""


MARC21_FIELDS = """<datafield tag='100' ind1='1' ind2=' '><subfield code='a'>Heylin, Clinton.</subfield></datafield>
<datafield tag='245' ind1='1' ind2='0'><subfield code='a'>E Street shuffle :</subfield></datafield>
<datafield tag='650' ind1=' ' ind2='0'><subfield code='a'>Rock music</subfield></datafield>"""

# Real UNIMARC record: 100 is general processing data, not an author.
UNIMARC_FIELDS = """<datafield tag='100' ind1=' ' ind2=' '><subfield code='a'>20070130              frey50        </subfield></datafield>
<datafield tag='101' ind1='0' ind2=' '><subfield code='a'>Fre</subfield></datafield>
<datafield tag='200' ind1='1' ind2=' '><subfield code='a'>La Recherche</subfield></datafield>"""


def _first_marc(xml: bytes) -> ET.Element:
    return ET.fromstring(xml).find(f".//{{{MX}}}record")


class TestDetectFlavour:
    def test_marc21_by_245(self):
        assert detect_flavour(_first_marc(_envelope(_record(MARC21_FIELDS)))) is Flavour.MARC21

    def test_unimarc_by_200(self):
        assert detect_flavour(_first_marc(_envelope(_record(UNIMARC_FIELDS)))) is Flavour.UNIMARC

    def test_shared_tag_100_does_not_decide(self):
        """Both fixtures contain tag 100. It must not influence detection."""
        for fields, expected in ((MARC21_FIELDS, Flavour.MARC21), (UNIMARC_FIELDS, Flavour.UNIMARC)):
            record = _first_marc(_envelope(_record(fields)))
            assert "100" in {d.get("tag") for d in record.findall(f"{{{MX}}}datafield")}
            assert detect_flavour(record) is expected

    def test_neither_tag_raises(self):
        fields = "<datafield tag='260' ind1=' ' ind2=' '><subfield code='a'>NY</subfield></datafield>"
        with pytest.raises(FlavourDetectionError, match="neither"):
            detect_flavour(_first_marc(_envelope(_record(fields))))

    def test_both_tags_raises(self):
        with pytest.raises(FlavourDetectionError, match="ambiguous"):
            detect_flavour(_first_marc(_envelope(_record(MARC21_FIELDS + UNIMARC_FIELDS))))


class TestDetectFlavourFromResponse:
    def test_samples_multiple_records(self):
        assert detect_flavour_from_response(_envelope(_record(MARC21_FIELDS) * 5)) is Flavour.MARC21

    def test_single_record_when_sample_size_exceeds_available(self):
        assert detect_flavour_from_response(_envelope(_record(UNIMARC_FIELDS)), sample_size=5) is Flavour.UNIMARC

    def test_one_bad_record_does_not_prevent_detection(self):
        """A malformed record among good ones must not stop the harvest."""
        bad = _record("<datafield tag='260' ind1=' ' ind2=' '><subfield code='a'>x</subfield></datafield>")
        assert detect_flavour_from_response(_envelope(bad + _record(MARC21_FIELDS) * 3)) is Flavour.MARC21

    def test_mixed_flavours_raise(self):
        """A genuinely mixed feed needs per-record handling; never guess."""
        with pytest.raises(FlavourDetectionError, match="disagree"):
            detect_flavour_from_response(_envelope(_record(MARC21_FIELDS) + _record(UNIMARC_FIELDS)))

    def test_oai_error_response_raises(self):
        xml = f"""<?xml version="1.0"?><OAI-PMH xmlns='{OAI}'>
<error code='idDoesNotExist'>Unknown identifier</error></OAI-PMH>""".encode()
        with pytest.raises(FlavourDetectionError, match="no MARCXML records"):
            detect_flavour_from_response(xml)

    def test_html_response_explains_disabled_endpoint(self):
        """The observable signature of OAI-PMH being switched off."""
        html = b"<!DOCTYPE html><html><head><title>404</title></head><body>&nbsp;</body></html>"
        with pytest.raises(FlavourDetectionError, match="OAI-PMH"):
            detect_flavour_from_response(html)

    def test_empty_response_reports_empty(self):
        with pytest.raises(FlavourDetectionError, match="empty"):
            detect_flavour_from_response(b"")
