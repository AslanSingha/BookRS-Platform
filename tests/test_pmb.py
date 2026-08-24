"""Tests for the PMB XML translation.

PMB serves UNIMARC under its own element names rather than MARC21slim.
The structure verified here comes from PMB's own
admin/convert/xml_unimarc.class.php, which writes the format.
"""

from xml.etree import ElementTree as ET

import pytest

from bookrs.ingestion.fieldmap import map_record
from bookrs.ingestion.flavour import Flavour, detect_flavour
from bookrs.ingestion.pmb import (
    PMB_PREFIX,
    looks_like_pmb,
    translate_notice,
    translate_response,
)

OAI = "http://www.openarchives.org/OAI/2.0/"
MX = "http://www.loc.gov/MARC21/slim"


def _notice(body: str) -> ET.Element:
    return ET.fromstring(f"<notice>{body}</notice>")


def _response(body: str, *, namespaced: bool = True) -> ET.Element:
    xmlns = f" xmlns='{OAI}'" if namespaced else ""
    return ET.fromstring(
        f"<OAI-PMH{xmlns}><ListRecords><record>"
        f"<header><identifier>oai:pmb:1</identifier></header>"
        f"<metadata><notice>{body}</notice></metadata>"
        f"</record></ListRecords></OAI-PMH>"
    )


FULL = (
    "<rs>c</rs><dt>a</dt><bl>m</bl><hl>0</hl><el>1</el><ru>*</ru>"
    "<f c='001'>12345</f>"
    "<f c='010'><s c='a'>9782070360024</s></f>"
    "<f c='101' ind='0 '><s c='a'>Fre</s></f>"
    "<f c='200' ind='1 '><s c='a'>[La ]mythologie Grecque</s>"
    "<s c='e'>des origines</s><s c='b'>TEXT</s></f>"
    "<f c='210'><s c='a'>Paris</s><s c='c'>Gallimard</s><s c='d'>1998</s></f>"
    "<f c='606'><s c='a'>Mythologie</s><s c='x'>Antiquite</s></f>"
    "<f c='700'><s c='a'>Mira Pons</s><s c='b'>Michele</s><s c='9'>361</s></f>"
    "<f c='995'><s c='f'>bc_9</s><s c='r'>BK</s><s c='c'>CPL</s>"
    "<s c='b'>FFL</s><s c='n'>2026-09-06</s></f>"
)


class TestStructure:
    def test_field_without_subfields_is_a_control_field(self):
        """PMB makes no element-level distinction: an <f> carrying text
        with no <s> children is a control field."""
        record = translate_notice(_notice("<f c='001'>12345</f>"))
        controls = record.findall(f"{{{MX}}}controlfield")
        assert len(controls) == 1
        assert controls[0].get("tag") == "001"
        assert controls[0].text == "12345"
        assert record.findall(f"{{{MX}}}datafield") == []

    def test_field_with_subfields_is_a_data_field(self):
        record = translate_notice(
            _notice("<f c='200' ind='1 '><s c='a'>Titre</s></f>")
        )
        fields = record.findall(f"{{{MX}}}datafield")
        assert len(fields) == 1
        assert fields[0].get("tag") == "200"
        subs = fields[0].findall(f"{{{MX}}}subfield")
        assert [(s.get("code"), s.text) for s in subs] == [("a", "Titre")]

    def test_single_ind_attribute_splits_into_two(self):
        record = translate_notice(
            _notice("<f c='200' ind='1 '><s c='a'>T</s></f>")
        )
        field = record.find(f"{{{MX}}}datafield")
        assert field.get("ind1") == "1" and field.get("ind2") == " "

    def test_missing_ind_becomes_two_spaces(self):
        """MARC uses a space for 'no indicator'; downstream code reads
        both attributes unconditionally."""
        record = translate_notice(_notice("<f c='606'><s c='a'>X</s></f>"))
        field = record.find(f"{{{MX}}}datafield")
        assert field.get("ind1") == " " and field.get("ind2") == " "


class TestLeader:
    def test_named_elements_become_leader_positions(self):
        """PMB emits no leader. Record type and bibliographic level --
        leader positions 6 and 7 -- distinguish books from sound
        recordings and serials, so they must survive."""
        record = translate_notice(_notice(FULL))
        leader = record.find(f"{{{MX}}}leader").text
        assert len(leader) == 24
        assert leader[6] == "a"   # dt: textual
        assert leader[7] == "m"   # bl: monograph

    def test_asterisk_means_space(self):
        record = translate_notice(_notice("<rs>*</rs><dt>a</dt>"))
        assert record.find(f"{{{MX}}}leader").text[5] == " "

    def test_absent_elements_leave_spaces(self):
        record = translate_notice(_notice("<f c='001'>1</f>"))
        assert record.find(f"{{{MX}}}leader").text == " " * 24


class TestDetection:
    def test_namespaced_notice_is_detected(self):
        """PMB writes records without a namespace, but the OAI envelope's
        default xmlns applies to every unprefixed descendant."""
        assert looks_like_pmb(_response(FULL, namespaced=True))

    def test_bare_notice_is_detected(self):
        assert looks_like_pmb(_response(FULL, namespaced=False))

    def test_marcxml_is_not_pmb(self):
        root = ET.fromstring(
            f"<OAI-PMH xmlns='{OAI}'><ListRecords><record><metadata>"
            f"<record xmlns='{MX}'><datafield tag='245'/></record>"
            f"</metadata></record></ListRecords></OAI-PMH>"
        )
        assert not looks_like_pmb(root)


class TestEndToEnd:
    @pytest.fixture
    def work(self):
        root = _response(FULL)
        translate_response(root)
        marc = root.find(f".//{{{MX}}}record")
        return map_record(marc, detect_flavour(marc), "oai:pmb:1")

    def test_flavour_resolves_through_the_existing_detector(self, work):
        assert work.flavour is Flavour.UNIMARC

    def test_unimarc_field_map_applies_unchanged(self, work):
        """The point of translating rather than writing a new parser:
        PMB uses the same UNIMARC tag numbers."""
        assert work.provenance["title"] == "200$ae"
        assert work.provenance["authors"] == "700$ab"
        assert work.provenance["isbns"] == "010$a"
        assert work.provenance["publication"] == "210"
        assert work.provenance["languages"] == "101$a"

    def test_non_filing_brackets_stripped(self, work):
        assert work.title == "La mythologie Grecque : des origines"

    def test_medium_designator_excluded_from_title(self, work):
        """200$b is the medium designator, not a subtitle."""
        assert "TEXT" not in work.title

    def test_authority_link_excluded_from_author(self, work):
        assert work.authors == ["Mira Pons Michele"]

    def test_language_case_folded(self, work):
        assert work.languages == ["fre"]

    def test_items_extracted_from_995(self, work):
        assert work.item_count == 1
        item = work.items[0]
        assert item.barcode == "bc_9" and item.item_type == "BK"
        assert item.owning_branch == "CPL" and item.holding_branch == "FFL"
        assert item.due_date == "2026-09-06" and item.on_loan


class TestIdempotence:
    def test_marcxml_response_is_left_alone(self):
        """Translation runs on every record; a Koha response must pass
        through untouched."""
        root = ET.fromstring(
            f"<OAI-PMH xmlns='{OAI}'><ListRecords><record><metadata>"
            f"<record xmlns='{MX}'><leader>x</leader>"
            f"<datafield tag='245' ind1='1' ind2='0'>"
            f"<subfield code='a'>T</subfield></datafield></record>"
            f"</metadata></record></ListRecords></OAI-PMH>"
        )
        before = ET.tostring(root)
        translate_response(root)
        assert ET.tostring(root) == before

    def test_prefix_constant(self):
        assert PMB_PREFIX == "pmb_xml_unimarc"
