"""Tests for MARC language detection.

Fixtures are real values from a Koha MARC21 corpus, including the
malformed ones.
"""

from xml.etree import ElementTree as ET

import pytest

from bookrs.ingestion.language import _chunk, detect_languages

MX = "http://www.loc.gov/MARC21/slim"


def _record(*, f008: str | None = None, f041: list[tuple[str, str]] | None = None) -> ET.Element:
    parts = []
    if f008 is not None:
        parts.append(f"<controlfield tag='008'>{f008}</controlfield>")
    if f041:
        subs = "".join(f"<subfield code='{c}'>{v}</subfield>" for c, v in f041)
        parts.append(f"<datafield tag='041' ind1='0' ind2=' '>{subs}</datafield>")
    return ET.fromstring(f"<record xmlns='{MX}'>{''.join(parts)}</record>")


def _f008(lang: str = "eng") -> str:
    """Build an 008 with the language at exactly positions 35-37.

    Constructed by position rather than transcribed, because an 008 is
    fixed-width and a hand-copied prefix that is one character short
    silently shifts every field after it. That is what happened the
    first time this helper was written.
    """
    field = list("120829t20132012nyu      bk   001 0c".ljust(40))
    field[35:38] = lang.ljust(3)[:3]
    return "".join(field)


class TestChunking:
    def test_splits_concatenated_codes(self):
        """MARC packs multiple codes into one subfield."""
        assert _chunk("engfreger") == ["eng", "fre", "ger"]

    def test_single_code(self):
        assert _chunk("eng") == ["eng"]

    def test_rejects_non_multiple_of_three(self):
        """Chunking 'English' anyway would yield ['eng', 'lis']."""
        assert _chunk("English") == []
        assert _chunk("ru") == []

    def test_blank_and_empty(self):
        assert _chunk("   ") == []
        assert _chunk("") == []


class TestSourcePrecedence:
    def test_041_wins_over_008(self):
        """041 exists to express what a single 008 position cannot."""
        rec = _record(f008=_f008("gla"), f041=[("a", "gleeng")])
        assert detect_languages(rec) == (["gle", "eng"], "041$a")

    def test_008_used_when_041_absent(self):
        assert detect_languages(_record(f008=_f008("fre"))) == (["fre"], "008")

    def test_041_rescues_an_uncoded_008(self):
        rec = _record(f008=_f008("   "), f041=[("a", "rus")])
        assert detect_languages(rec) == (["rus"], "041$a")

    def test_subfield_h_is_ignored(self):
        """$h is the language of the ORIGINAL. An English translation of
        a German work must not be labelled German."""
        rec = _record(f041=[("a", "eng"), ("h", "ger")])
        assert detect_languages(rec) == (["eng"], "041$a")

    def test_falls_through_to_008_when_041_is_junk(self):
        rec = _record(f008=_f008("spa"), f041=[("a", "zzz")])
        assert detect_languages(rec) == (["spa"], "008")


class TestMalformedData:
    @pytest.mark.parametrize("bad", ["||e", "|||", "   ", "xxx"])
    def test_fill_characters_and_junk_yield_nothing(self, bad):
        assert detect_languages(_record(f008=_f008(bad))) == ([], None)

    @pytest.mark.parametrize("code", ["und", "mul", "zxx"])
    def test_non_language_codes_are_not_languages(self, code):
        """Structurally valid, but they name no language."""
        assert detect_languages(_record(f008=_f008(code))) == ([], None)

    def test_short_008_has_no_language_position(self):
        """Positions 35-37 exist only in a full-length 008."""
        assert detect_languages(_record(f008="120829t2013")) == ([], None)

    def test_no_language_fields_at_all(self):
        assert detect_languages(_record()) == ([], None)


class TestDeprecatedCodes:
    @pytest.mark.parametrize("code", ["gae", "gla", "iri", "gle"])
    def test_both_deprecated_and_current_forms_accepted(self, code):
        """The reference corpus contains gae/gla and iri/gle together.
        A strict ISO 639-2 list would reject half of them."""
        assert detect_languages(_record(f008=_f008(code))) == ([code], "008")


class TestDeduplication:
    def test_repeated_codes_collapse_once(self):
        rec = _record(f041=[("a", "engeng"), ("a", "eng")])
        assert detect_languages(rec) == (["eng"], "041$a")

    def test_order_is_preserved(self):
        """The first code is the primary language."""
        rec = _record(f041=[("a", "grceng")])
        assert detect_languages(rec)[0] == ["grc", "eng"]
