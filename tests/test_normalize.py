"""Tests for MARC text normalisation.

Every fixture is a real value observed in a Koha MARC21 corpus.
"""

import pytest

from bookrs.ingestion.normalize import (
    clean_isbn, join_title, strip_isbd, strip_nonfiling,
)


class TestStripISBD:
    @pytest.mark.parametrize("raw,expected", [
        ("E Street shuffle :", "E Street shuffle"),
        ("the glory days of Bruce Springsteen & the E Street Band /",
         "the glory days of Bruce Springsteen & the E Street Band"),
        ("Heylin, Clinton.", "Heylin, Clinton"),
        ("Fine, Reuben,", "Fine, Reuben"),
        ("Conway, Damian", "Conway, Damian"),
        ("336 pages ;", "336 pages"),
    ])
    def test_strips_trailing_punctuation(self, raw, expected):
        assert strip_isbd(raw) == expected

    @pytest.mark.parametrize("name", [
        "Mastronarde, Donald J.",
        "Kernighan, Brian W.",
        "Wilson, Peter H.",
    ])
    def test_preserves_terminal_initial(self, name):
        """A period after a lone capital is an initial, not a delimiter."""
        assert strip_isbd(name) == name

    def test_internal_punctuation_is_untouched(self):
        assert strip_isbd("Smith, John") == "Smith, John"

    def test_strips_multiple_trailing_delimiters(self):
        assert strip_isbd("Title / :") == "Title"

    def test_empty_and_punctuation_only(self):
        assert strip_isbd("") == ""
        assert strip_isbd("  :  ") == ""


class TestCleanISBN:
    @pytest.mark.parametrize("raw,expected", [
        ("9780670026623 (alk. paper)", "9780670026623"),
        ("0812917561 :", "0812917561"),
        ("0520078446 (pbk. : alk. paper)", "0520078446"),
        ("0131103628 (pbk.)", "0131103628"),
        ("0596001738", "0596001738"),
        ("978-0-670-02662-3", "9780670026623"),
    ])
    def test_extracts_bare_isbn(self, raw, expected):
        assert clean_isbn(raw) == expected

    def test_preserves_trailing_x_check_digit(self):
        assert clean_isbn("080442957X") == "080442957X"
        assert clean_isbn("080442957x") == "080442957X"

    @pytest.mark.parametrize("raw", ["", "   ", "(pbk.)", "12345", "not an isbn"])
    def test_rejects_implausible_values(self, raw):
        assert clean_isbn(raw) is None

    def test_qualifier_colon_does_not_truncate_early(self):
        """Order matters: remove the parenthetical before punctuation."""
        assert clean_isbn("0520078446 (pbk. : alk. paper)") == "0520078446"


class TestJoinTitle:
    def test_joins_title_and_subtitle(self):
        assert join_title(["E Street shuffle :", "the glory days /"]) == \
            "E Street shuffle : the glory days"

    def test_single_part_gains_no_separator(self):
        assert join_title(["The C programming language"]) == \
            "The C programming language"

    def test_empty_parts_are_dropped(self):
        assert join_title(["Title :", "", "  /  "]) == "Title"

    def test_no_parts(self):
        assert join_title([]) == ""


class TestStripNonfiling:
    """UNIMARC brackets the non-filing article -- the part a catalogue
    ignores when sorting. 367 of 4,849 records in the reference corpus.
    MARC21 uses the 245 second indicator instead and has none."""

    @pytest.mark.parametrize("raw,expected", [
        ("[L']aurore des bien-aimés", "L'aurore des bien-aimés"),
        ("[Un ]gâchis", "Un gâchis"),          # space inside the bracket
        ("[Les ]drogues", "Les drogues"),
        ("[La ]tunique d'infamie", "La tunique d'infamie"),
    ])
    def test_unwraps_leading_article(self, raw, expected):
        assert strip_nonfiling(raw) == expected

    @pytest.mark.parametrize("title", [
        "On the corner : [Sound recording]",
        "DOA - Dead Or Alive [DVD] [2006]",
        "Doctor who - the caves of androzani [1984] [dvd] [1963]",
    ])
    def test_mid_title_brackets_are_content(self, title):
        """MARC21 medium designators must survive."""
        assert strip_nonfiling(title) == title

    def test_untouched_when_no_bracket(self):
        assert strip_nonfiling("The C programming language") == \
            "The C programming language"

    def test_long_bracketed_phrase_is_not_an_article(self):
        """Non-filing articles are short; a long phrase is real content."""
        t = "[a very long bracketed phrase here] title"
        assert strip_nonfiling(t) == t

    def test_empty(self):
        assert strip_nonfiling("") == ""
