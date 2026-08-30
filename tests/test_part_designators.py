"""Tests for part designators in the title.

MARC 245 carries a work's part in $n (number) and $p (name). The field
map took only $a and $b, so six records in the reference corpus stored a
bare series name -- "Philippics.", "TCP/IP illustrated." -- rather than
a title. Two of those six are halves of the duplicate pairs that no
instrument could separate, because a second volume and a second edition
looked identical once the designator was discarded.

The assembly is punctuation-sensitive, which is the part worth testing:
joining every subfield with " : " gives "TCP/IP illustrated : Vol. 2 :
The implementation" rather than the prescribed "TCP/IP illustrated. Vol.
2, The implementation".
"""

import xml.etree.ElementTree as ET

import pytest

from bookrs.ingestion.fieldmap import map_record
from bookrs.ingestion.flavour import Flavour

M21_NS = "http://www.loc.gov/MARC21/slim"


def marc21(subfields, tag="245"):
    body = "".join(
        f'<subfield code="{c}">{t}</subfield>' for c, t in subfields
    )
    xml = (f'<record xmlns="{M21_NS}">'
           f'<datafield tag="{tag}" ind1="1" ind2="0">{body}</datafield>'
           f'</record>')
    return map_record(ET.fromstring(xml), Flavour.MARC21, "TEST:1")


def unimarc(subfields, tag="200"):
    body = "".join(
        f'<subfield code="{c}">{t}</subfield>' for c, t in subfields
    )
    xml = (f'<record xmlns="{M21_NS}">'
           f'<datafield tag="{tag}" ind1="1" ind2="0">{body}</datafield>'
           f'</record>')
    return map_record(ET.fromstring(xml), Flavour.UNIMARC, "TEST:1")


class TestPartDesignators:
    def test_number_and_name_are_included(self):
        work = marc21([("a", "TCP/IP illustrated."),
                       ("n", "Vol. 2,"),
                       ("p", "The implementation /")])
        assert work.title == "TCP/IP illustrated. Vol. 2, The implementation"

    def test_number_without_a_name(self):
        work = marc21([("a", "The Arden edition of Shakespeare."),
                       ("n", "Second series")])
        assert work.title == "The Arden edition of Shakespeare. Second series"

    def test_the_two_volumes_become_distinguishable(self):
        """The failure that prompted this. Without $n both titles are
        identical, and a second volume is indistinguishable from a
        second edition."""
        one = marc21([("a", "The art of computer programming."),
                      ("n", "Volume 1,"), ("p", "Fundamental algorithms /")])
        two = marc21([("a", "The art of computer programming."),
                      ("n", "Volume 2,"), ("p", "Seminumerical algorithms /")])
        assert one.title != two.title


class TestPunctuation:
    """ISBD prescribes the punctuation introducing each subfield, and it
    differs by subfield. A uniform separator produces a string no
    cataloguer would recognise."""

    def test_part_number_takes_a_full_stop(self):
        work = marc21([("a", "Base title"), ("n", "Vol. 3")])
        assert work.title == "Base title. Vol. 3"

    def test_part_name_takes_a_comma(self):
        work = marc21([("a", "Base title"), ("n", "Vol. 3"), ("p", "Part")])
        assert work.title == "Base title. Vol. 3, Part"

    def test_remainder_of_title_takes_a_colon(self):
        work = marc21([("a", "Base title"), ("b", "a subtitle")])
        assert work.title == "Base title : a subtitle"

    def test_all_four_in_standard_order(self):
        work = marc21([("a", "Base title."), ("n", "Vol. 3,"),
                       ("p", "Part name :"), ("b", "remainder /")])
        assert work.title == "Base title. Vol. 3, Part name : remainder"


class TestOrdering:
    def test_document_order_is_preserved(self):
        """$a $n $p $b is the prescribed order, so a part designator
        precedes the remainder rather than trailing it. Sorting the
        subfields by code would reorder them into nonsense."""
        work = marc21([("a", "Title."), ("n", "Vol. 2,"), ("b", "remainder")])
        assert work.title.index("Vol. 2") < work.title.index("remainder")


class TestNoRegression:
    def test_a_plain_title_is_unchanged(self):
        work = marc21([("a", "An ordinary title :"), ("b", "with a subtitle /")])
        assert work.title == "An ordinary title : with a subtitle"

    def test_title_proper_alone(self):
        assert marc21([("a", "Just a title /")]).title == "Just a title"

    def test_medium_designator_stays_out(self):
        """$h describes the carrier, not the work. Including it would
        make an electronic edition embed differently from its print
        counterpart for a reason that is not about content."""
        work = marc21([("a", "Data mining tools"),
                       ("h", "[electronic resource]")])
        assert "electronic resource" not in work.title

    def test_empty_subfields_are_dropped(self):
        work = marc21([("a", "Title"), ("n", "  "), ("p", "Part")])
        assert work.title == "Title, Part"


class TestUnimarc:
    """UNIMARC uses $h and $i where MARC21 uses $n and $p.

    Their frequency is unmeasured -- the part profile covered the MARC21
    corpus only -- so these tests establish the mapping works, not how
    often it applies.
    """

    def test_part_designators_are_included(self):
        work = unimarc([("a", "Base title"), ("h", "Vol. 2"), ("i", "Part")])
        assert work.title == "Base title. Vol. 2, Part"

    def test_subtitle_still_takes_a_colon(self):
        work = unimarc([("a", "Base title"), ("e", "a subtitle")])
        assert work.title == "Base title : a subtitle"

    def test_a_plain_unimarc_title_is_unchanged(self):
        assert unimarc([("a", "Just a title /")]).title == "Just a title"


class TestMapperVersion:
    def test_version_was_bumped(self):
        """Extraction changed, so content_hash must change, or every
        affected record is reported unchanged and never rewritten --
        the failure that created this constant."""
        from bookrs.ingestion.fieldmap import MAPPER_VERSION
        assert MAPPER_VERSION >= 4
