"""Assemble the text that represents a work to the embedding model.

The composition differs from the BookRS-System thesis, which encoded
title, authors, description and genre labels over Goodreads data. Two
fields have no like-for-like MARC equivalent:

* Goodreads genres are a short closed vocabulary. MARC subject headings
  are open-ended and structured -- "Europe — History, Military —
  1492-1648" is a topic plus subdivisions, closer to a compressed
  abstract than a tag. They are used here in place of genre, but they
  are not the same signal.
* Descriptions are near-universal on Goodreads and rare in MARC: 520 is
  present on 8.9% of the MARC21 reference corpus and 330 on 16.1% of the
  UNIMARC one.

Authors are currently excluded. A name is not semantic content, and half
the UNIMARC corpus embeds from a median of 13 tokens, where a name would
dominate the input. This is an assumption, not a measurement, and is a
candidate arm for the Decision 3 experiment.
"""

from __future__ import annotations

from dataclasses import dataclass

# Subdivisions within one heading are already joined by an em dash by
# the field mapper. Distinct headings are separated by a period so the
# assembled text reads as sentences rather than a list.
from bookrs.ingestion.altscript import prefer_script

HEADING_SEPARATOR = ". "


@dataclass
class WorkText:
    """The text of a work, split by how it must be encoded.

    ``core`` (title and subject headings) is short and always present.
    ``description`` is rare and can be long. They are kept apart because
    the two together exceed the model's input window for half the works
    that have a description at all -- 19 of 39 in the MARC21 reference
    corpus against a 128-token limit. Encoding them separately and
    averaging preserves the whole text; concatenating would silently
    discard the tail of exactly the richest field available.
    """

    core: str
    description: str = ""

    @property
    def is_title_only(self) -> bool:
        """True when nothing but the title was available.

        Roughly a third of both reference corpora: 29.4% MARC21, 38.5%
        UNIMARC. Callers may want to treat these differently, since a
        title alone is a much weaker signal than the rest of the
        catalogue provides.
        """
        return not self.description and HEADING_SEPARATOR not in self.core


def build_text(title: str, subjects: list[str], summary: str = "",
               contents: str = "", title_alternate: str = "") -> WorkText:
    """Assemble the encodable text for one work.

    ``contents`` (MARC 505/327 table-of-contents notes) is accepted and
    appended to the description, since it is descriptive prose of the
    same kind. It adds little coverage -- 63.3% to 65.4% in the MARC21
    corpus -- but for the records that carry it, it may be the richest
    text available.
    """
    # Where a record carries its title in two scripts, encode the one
    # that carries language rather than a transliteration of it.
    #
    # Which of the two is the romanisation is not fixed by the standard
    # -- a library cataloguing natively in Khmer would put Khmer in 245
    # and the romanisation in the linked 880, the reverse of the Hebrew
    # record in the reference corpus. So the choice is made by
    # inspecting the text, not by field number.
    #
    # Only the embedding side is affected. The romanisation stays in
    # `title` for display and is indexed for search, so a patron may
    # still type either form.
    if title_alternate.strip():
        title, _ = prefer_script(title, title_alternate)

    core_parts = [title.strip()] if title.strip() else []
    core_parts.extend(s.strip() for s in subjects if s.strip())

    description_parts = [p.strip() for p in (summary, contents) if p.strip()]

    return WorkText(
        core=HEADING_SEPARATOR.join(core_parts),
        description=HEADING_SEPARATOR.join(description_parts),
    )
