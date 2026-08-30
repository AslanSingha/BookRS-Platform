"""Tests for MARC 880 alternate-script linkage.

Two scripts are exercised deliberately. The Hebrew values are copied
from the one record in the reference corpus that carries 880 fields, so
the parser is tested against bytes a real Koha actually served. The
Khmer values are constructed, because no Khmer record was available --
and Khmer is the case this project exists for, differing from Hebrew in
ways that matter: it is written without spaces between words, uses
combining marks extensively, and is left-to-right where Hebrew is not.

Testing only on Hebrew would validate the convenient case.
"""

import pytest

from bookrs.ingestion.altscript import (
    Linkage, is_latin, pair_880, parse_linkage, prefer_script,
)

# From KOHA-OAI-TEST, the corpus's only 880-bearing record.
HEB_TITLE = "מאמר שיחת חולין של ת״ח"
HEB_ROMAN = "Maʼamar śiḥat ḥulin shel t. ṭ"

# Constructed: "History of Cambodia".
KHM_TITLE = "ប្រវត្តិសាស្ត្រកម្ពុជា"
KHM_ROMAN = "Pravattisastr Kampuchea"


class TestParseLinkage:
    def test_plain_reference(self):
        link = parse_linkage("880-03")
        assert link == Linkage(tag="880", occurrence="03")

    def test_reference_with_script_and_orientation(self):
        link = parse_linkage("245-03/(2/r")
        assert link.tag == "245"
        assert link.occurrence == "03"
        assert link.script == "(2"
        assert link.orientation == "r"

    def test_script_without_orientation(self):
        """Observed on a 590 in the reference record."""
        link = parse_linkage("590-00/(2")
        assert link.script == "(2"
        assert link.orientation is None

    def test_occurrence_zero_is_standalone(self):
        assert parse_linkage("590-00/(2").standalone is True

    def test_ordinary_occurrence_is_not_standalone(self):
        assert parse_linkage("245-03/(2/r").standalone is False

    def test_surrounding_whitespace(self):
        assert parse_linkage("  880-01  ").tag == "880"

    @pytest.mark.parametrize("value", [None, "", "   ", "880", "abc-01",
                                       "88-01", "8801", "880_01"])
    def test_malformed_values_return_none(self, value):
        """A linkage that cannot be parsed costs one field's alternate
        representation. It must not abort an otherwise usable record."""
        assert parse_linkage(value) is None


class TestPairing:
    def test_pairs_on_tag_and_occurrence(self):
        index = pair_880([
            ("100", "880-01"),
            ("245", "880-03"),
            ("880", "100-01/(2/r"),
            ("880", "245-03/(2/r"),
        ])
        assert ("100", "01") in index
        assert ("245", "03") in index

    def test_two_880s_for_one_tag_stay_distinct(self):
        """Matching on tag alone would resolve both to whichever came
        first, silently attaching the wrong script to a field."""
        index = pair_880([
            ("880", "245-03/(2/r"),
            ("880", "245-07/(2/r"),
        ])
        assert index[("245", "03")] != index[("245", "07")]

    def test_standalone_880_is_not_paired(self):
        """Occurrence 00 has no counterpart; pairing it would attach an
        alternate representation to a field that never claimed one."""
        index = pair_880([("880", "590-00/(2")])
        assert index == {}

    def test_non_880_fields_are_ignored(self):
        assert pair_880([("245", "880-03"), ("100", "880-01")]) == {}

    def test_malformed_linkage_is_skipped(self):
        assert pair_880([("880", "nonsense")]) == {}

    def test_empty_input(self):
        assert pair_880([]) == {}


class TestScriptDetection:
    def test_hebrew_is_not_latin(self):
        assert is_latin(HEB_TITLE) is False

    def test_khmer_is_not_latin(self):
        assert is_latin(KHM_TITLE) is False

    def test_romanisation_with_diacritics_is_latin(self):
        """A transliteration carries combining marks and remains Latin.
        Treating it as non-Latin would defeat the whole check."""
        assert is_latin(HEB_ROMAN) is True

    def test_khmer_with_a_latin_acronym_is_still_khmer(self):
        """Majority test, not all-or-nothing: real titles mix scripts."""
        assert is_latin(KHM_TITLE + " UNESCO") is False

    def test_letterless_text_counts_as_latin(self):
        """No evidence of another script, and the caller's fallback is
        the Latin-side field."""
        assert is_latin("1891") is True
        assert is_latin("") is True


class TestPreference:
    def test_hebrew_is_embedded_romanisation_is_searchable(self):
        embed, search = prefer_script(HEB_ROMAN, HEB_TITLE)
        assert embed == HEB_TITLE
        assert HEB_ROMAN in search and HEB_TITLE in search

    def test_khmer_is_embedded_romanisation_is_searchable(self):
        embed, search = prefer_script(KHM_ROMAN, KHM_TITLE)
        assert embed == KHM_TITLE
        assert KHM_ROMAN in search and KHM_TITLE in search

    def test_reversed_cataloguing_still_embeds_the_script(self):
        """A library cataloguing natively puts Khmer in 245 and the
        romanisation in 880 -- the reverse of the Hebrew record. A rule
        of 'prefer 880' would embed the transliteration here."""
        embed, search = prefer_script(KHM_TITLE, KHM_ROMAN)
        assert embed == KHM_TITLE
        assert KHM_ROMAN in search

    def test_two_latin_forms_keep_the_cataloguer_ordering(self):
        embed, _ = prefer_script("Primary title", "Parallel title")
        assert embed == "Primary title"

    def test_two_non_latin_forms_keep_the_cataloguer_ordering(self):
        embed, _ = prefer_script(KHM_TITLE, HEB_TITLE)
        assert embed == KHM_TITLE

    def test_absent_alternate(self):
        assert prefer_script(HEB_ROMAN, "") == (HEB_ROMAN, HEB_ROMAN)

    def test_absent_primary(self):
        assert prefer_script("", KHM_TITLE) == (KHM_TITLE, KHM_TITLE)

    def test_whitespace_only_alternate_is_absent(self):
        assert prefer_script(HEB_ROMAN, "   ") == (HEB_ROMAN, HEB_ROMAN)


class TestRegression:
    """The behaviour that prompted this module: the pipeline stored a
    transliteration and discarded the script, so the multilingual model
    received, on exactly the records that justified it, text it could
    not use."""

    def test_the_corpus_record_embeds_hebrew_not_romanisation(self):
        embed, _ = prefer_script(HEB_ROMAN, HEB_TITLE)
        assert is_latin(embed) is False, (
            "the embedder would receive a romanisation, which is not a "
            "string in any language the model was trained on"
        )
