"""Tests for MARC field mapping.

The fixtures below are trimmed from real records in both a MARC21 and a
UNIMARC Koha corpus. The point of most of these tests is that the same
tag number means something different in each flavour.
"""

from xml.etree import ElementTree as ET

import pytest

from bookrs.ingestion.fieldmap import Flavour, map_record

MX = "http://www.loc.gov/MARC21/slim"


def _rec(body: str) -> ET.Element:
    return ET.fromstring(f"<record xmlns='{MX}'>{body}</record>")


def _df(tag: str, **subs: str) -> str:
    inner = "".join(f"<subfield code='{c}'>{v}</subfield>" for c, v in subs.items())
    return f"<datafield tag='{tag}' ind1=' ' ind2=' '>{inner}</datafield>"


MARC21_REC = _rec(
    "<controlfield tag='008'>120829t20132012nyu      bk   001 0ceng  </controlfield>"
    + _df("020", a="9780670026623 (alk. paper)")
    + _df("100", a="Heylin, Clinton.", **{"9": "1702"})
    + _df("245", a="E Street shuffle :", b="the glory days /")
    + _df("260", a="New York, NY :", b="D. McKay Co.,", c="c1990.")
    + _df("650", a="Rock musicians", z="United States", v="Biography.", **{"9": "1705"})
    + _df("952", p="3999900000001")
)

UNIMARC_REC = _rec(
    _df("010", a="9782070360024")
    + _df("101", a="Fre")
    + _df("100", a="20070130              frey50        ")
    + _df("200", a="La Recherche", e="L'actualite des sciences", b="REV")
    + _df("210", a="Paris", c="Editions independantes", d="2008")
    + _df("606", a="Science")
    + _df("700", a="HUE", b="Jean Louis", **{"9": "361", "4": "651"})
    + _df("995", f="bc_1")
)


class TestMARC21:
    @pytest.fixture
    def work(self):
        return map_record(MARC21_REC, Flavour.MARC21, "KOHA-OAI-TEST:1")

    def test_title_joins_and_strips_isbd(self, work):
        assert work.title == "E Street shuffle : the glory days"

    def test_author_keeps_internal_comma_drops_terminator(self, work):
        assert work.authors == ["Heylin, Clinton"]

    def test_authority_link_is_not_content(self, work):
        """$9 is Koha's authority number; it must not leak into names."""
        assert not any(ch.isdigit() for ch in work.authors[0])

    def test_isbn_qualifier_removed(self, work):
        assert work.isbns == ["9780670026623"]

    def test_subject_subdivisions_join_as_one_heading(self, work):
        assert work.subjects == ["Rock musicians — United States — Biography"]

    def test_copyright_date_parses(self, work):
        """'c1990' has no word boundary before the digits."""
        assert work.publication_year == 1990

    def test_publisher_from_260b(self, work):
        assert work.publisher == "D. McKay Co"

    def test_language_from_008(self, work):
        assert work.languages == ["eng"]
        assert work.provenance["languages"] == "008"

    def test_items_counted(self, work):
        assert work.item_count == 1


class TestUNIMARC:
    @pytest.fixture
    def work(self):
        return map_record(UNIMARC_REC, Flavour.UNIMARC, "KOHA-OAI-TEST:1")

    def test_title_uses_200e_not_200b(self, work):
        """200$b is the medium designator ('REV'), not a subtitle."""
        assert work.title == "La Recherche : L'actualite des sciences"
        assert "REV" not in work.title

    def test_author_from_700_not_100(self, work):
        """UNIMARC 100 is general processing data, not an author."""
        assert work.authors == ["HUE Jean Louis"]
        assert "frey50" not in " ".join(work.authors)

    def test_publisher_from_210c(self, work):
        assert work.publisher == "Editions independantes"

    def test_year_from_210d(self, work):
        assert work.publication_year == 2008

    def test_isbn_from_010(self, work):
        assert work.isbns == ["9782070360024"]

    def test_language_from_101(self, work):
        assert work.languages == ["fre"]
        assert work.provenance["languages"] == "101$a"

    def test_items_counted_from_995(self, work):
        assert work.item_count == 1


class TestCollisions:
    """Tags 100, 300 and 020 exist in both standards with different
    meanings. Applying the wrong map yields plausible nonsense."""

    def test_100_is_an_author_only_in_marc21(self):
        m21 = map_record(MARC21_REC, Flavour.MARC21, "x")
        uni = map_record(UNIMARC_REC, Flavour.UNIMARC, "x")
        assert m21.provenance["authors"] == "100$a"
        assert uni.provenance["authors"] == "700$ab"

    def test_unimarc_100_never_becomes_an_author(self):
        work = map_record(UNIMARC_REC, Flavour.UNIMARC, "x")
        assert all("frey50" not in a for a in work.authors)

    def test_020_is_an_isbn_only_in_marc21(self):
        assert map_record(MARC21_REC, Flavour.MARC21, "x").provenance["isbns"] == "020$a"
        assert map_record(UNIMARC_REC, Flavour.UNIMARC, "x").provenance["isbns"] == "010$a"


class TestMissingData:
    def test_record_without_publication_field(self):
        """25 of 436 MARC21 records had neither 260 nor 264."""
        work = map_record(_rec(_df("245", a="Title")), Flavour.MARC21, "x")
        assert work.publisher == "" and work.publication_year is None
        assert "publication" not in work.provenance

    def test_empty_record_yields_empty_work(self):
        work = map_record(_rec(""), Flavour.MARC21, "x")
        assert work.title == "" and work.authors == [] and work.item_count == 0

    def test_five_digit_year_is_rejected(self):
        """'20134' is a typo; guessing 2013 would invent data."""
        work = map_record(_rec(_df("260", c="20134")), Flavour.MARC21, "x")
        assert work.publication_year is None
