"""Text normalisation for MARC field values.

MARC records carry ISBD (International Standard Bibliographic
Description) punctuation as data: a title proper ends with " :" when a
subtitle follows, an author name ends with "." or ",". This punctuation
is presentational -- it exists so a catalogue card reads correctly when
subfields are concatenated -- and must be removed before the values are
used as data.

Measured against a 436-record Koha MARC21 corpus; see
docs/marc-field-analysis.md.
"""

from __future__ import annotations

import re

# ISBD delimiters that can appear at the end of a subfield.
ISBD_TRAILING = " :/;,=."

# A period preceded by a lone capital letter terminates an initial, not
# a subfield. "Kernighan, Brian W." must keep its period; stripping it
# corrupts the name. 82 of 328 author names in the reference corpus end
# this way, so this is a common case rather than an edge one.
_TRAILING_INITIAL = re.compile(r"(?:^|[^A-Za-z])[A-Z]\.$")


def strip_isbd(value: str) -> str:
    """Remove trailing ISBD punctuation, preserving terminal initials."""
    value = value.rstrip()
    while value and value[-1] in ISBD_TRAILING:
        if value[-1] == "." and _TRAILING_INITIAL.search(value):
            break
        value = value[:-1].rstrip()
    return value


def clean_isbn(value: str) -> str | None:
    """Extract a bare ISBN from a MARC 020$a value.

    Real values carry qualifiers and stray punctuation:

        "9780670026623 (alk. paper)"
        "0812917561 :"
        "0520078446 (pbk. : alk. paper)"

    Qualifiers are the majority case, not an exception -- 253 of 466
    values in the reference corpus contain one. Note the colon inside
    the third example: splitting on punctuation before removing the
    parenthetical would corrupt it, so order matters here.

    Returns None when the result is not a plausible ISBN-10 or ISBN-13.
    Length is checked but the check digit is not; a malformed check
    digit still identifies the intended record for matching purposes,
    and rejecting it would discard usable data.
    """
    value = re.sub(r"\(.*", "", value)          # qualifier and everything after
    value = re.sub(r"[^0-9Xx]", "", value)      # then all non-ISBN characters
    value = value.upper()
    return value if len(value) in (10, 13) else None


# A leading bracket in a title marks the non-filing article -- the part
# a catalogue ignores when sorting. UNIMARC records it this way (367 of
# 4,849 in the reference corpus: "[L']aurore des bien-aimes",
# "[Un ]gachis"), while MARC21 uses the 245 second indicator instead and
# has none. The brackets are a sorting convention, not content: they are
# noise to an embedding model and wrong to display.
#
# Only a LEADING bracket is treated this way. Mid-title brackets are
# real content -- "On the corner : [Sound recording]", "Doctor who ...
# [DVD] [2006]" -- and stripping those would destroy information.
_NONFILING = re.compile(r"^\[([^\]]{0,12})\]")


def strip_nonfiling(title: str) -> str:
    """Remove non-filing brackets, keeping the article inside them.

    The space may sit either inside or outside the bracket depending on
    the article, so both characters are removed and everything else
    kept: "[Un ]gachis" -> "Un gachis", "[L\']aurore" -> "L\'aurore".

    The length bound keeps this from matching a bracketed phrase that
    happens to lead a title; non-filing articles are short.
    """
    return _NONFILING.sub(r"\1", title, count=1).lstrip()


def join_title(parts: list[str], separator: str = " : ") -> str:
    """Assemble a title from its subfields.

    Each part is ISBD-stripped first, then rejoined with an explicit
    separator, so the result does not depend on whichever punctuation
    the cataloguer happened to use.
    """
    cleaned = [stripped for part in parts if (stripped := strip_isbd(part))]
    return separator.join(cleaned)
