"""MARC 880 alternate graphic representation.

A record catalogued in a non-Latin script commonly carries each field
twice: once in a Latin transliteration, and once in the original script
as an 880 field. The two are joined by subfield $6.

    100  $6 880-01        $a Slez, Ts.
    245  $6 880-03        $a Ma'amar sihat hulin shel t. t :
    880  $6 100-01/(2/r   $a [Hebrew]
    880  $6 245-03/(2/r   $a [Hebrew]

Neither representation is subordinate to the other, and **which script
lands in 245 depends on the cataloguing agency**. A record created in a
Latin-script environment romanises into 245 and puts the original in
880; a library cataloguing natively in its own script may do the
reverse, or omit 880 entirely. So a rule of the form "prefer 880" is
wrong. What can be relied on is the linkage itself.

Why this matters here rather than being a completeness exercise: the
field map reads 245 and ignores 880, so for these records the pipeline
stores a transliteration. That is worse than storing nothing. An absent
title is visible -- coverage metrics report it. A transliteration looks
like ordinary text, so the embedder encodes it confidently into a vector
that means very little, because "Ma'amar sihat hulin" is not a string in
any language the model was trained on. The multilingual model was
selected on measured evidence (see docs/marc-field-analysis.md 13) and
would, on exactly the records that justified it, receive text it cannot
use.

This module resolves the linkage. It does not decide which form to
prefer; that is the caller's policy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["Linkage", "parse_linkage", "pair_880", "is_latin", "prefer_script"]

#: `$6` is `tag-occurrence` optionally followed by `/script/orientation`,
#: e.g. `880-03`, `245-03/(2/r`, `590-00/(2`. Occurrence `00` means the
#: field has no counterpart to pair with -- it stands alone.
_LINKAGE = re.compile(
    r"^(?P<tag>\d{3})-(?P<occurrence>\d{2})"
    r"(?:/(?P<script>[^/]*)(?:/(?P<orientation>.*))?)?$"
)


@dataclass(frozen=True)
class Linkage:
    """A parsed `$6` value."""

    tag: str
    occurrence: str
    script: str | None = None
    orientation: str | None = None

    @property
    def standalone(self) -> bool:
        """Occurrence 00 means no counterpart field exists."""
        return self.occurrence == "00"

    @property
    def key(self) -> tuple[str, str]:
        """Pairing key. Tag alone is not enough: a record may carry two
        880s both linked to 245, and matching on tag would resolve them
        to whichever came first."""
        return (self.tag, self.occurrence)


def parse_linkage(value: str | None) -> Linkage | None:
    """Parse a `$6` value, or return None if it is absent or malformed.

    Malformed values are ignored rather than raised on. A linkage that
    cannot be parsed means one field's alternate representation is not
    found; it should not abort the harvest of an otherwise usable
    record.
    """
    if not value:
        return None
    m = _LINKAGE.match(value.strip())
    if not m:
        return None
    return Linkage(
        tag=m.group("tag"),
        occurrence=m.group("occurrence"),
        script=m.group("script"),
        orientation=m.group("orientation"),
    )


def pair_880(fields: list[tuple[str, str | None]]) -> dict[tuple[str, str], str]:
    """Index 880 fields by the `(tag, occurrence)` they link back to.

    ``fields`` is a list of ``(tag, subfield_6_value)``. Returns a map
    usable as ``index[("245", "03")]`` from the perspective of the 245
    field, whose own `$6` reads ``880-03``.

    Standalone 880s (occurrence 00) are excluded: they have no
    counterpart, so pairing them would attach an alternate
    representation to a field that never claimed one.
    """
    index: dict[tuple[str, str], str] = {}
    for position, (tag, six) in enumerate(fields):
        if tag != "880":
            continue
        link = parse_linkage(six)
        if link is None or link.standalone:
            continue
        index.setdefault(link.key, str(position))
    return index


def is_latin(text: str) -> bool:
    """True when the text's letters are predominantly Latin script.

    Used to decide which of two representations carries the language
    rather than a transliteration of it. Deliberately a majority test
    rather than an all-or-nothing one: a Khmer title may legitimately
    contain a Latin acronym or a date, and a romanisation carries
    diacritics that are still Latin.

    An empty or letterless string is reported as Latin, because there is
    no evidence of another script and the caller's fallback is the
    Latin-side field.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(
        1 for c in letters
        if unicodedata.name(c, "").startswith("LATIN")
    )
    return latin * 2 >= len(letters)


def prefer_script(primary: str, alternate: str) -> tuple[str, str]:
    """Return ``(for_embedding, for_search)`` from two representations.

    The embedder should see the form that carries actual language;
    search should be able to match either, since a patron may type
    romanised or original script.

    Which field holds which is not fixed by the standard, so the choice
    is made by inspecting the text rather than by field number. Where
    both are Latin, or both are not, the primary field wins -- there is
    no evidence to overturn the cataloguer's ordering.
    """
    if not alternate.strip():
        return primary, primary
    if not primary.strip():
        return alternate, alternate
    if is_latin(primary) and not is_latin(alternate):
        return alternate, f"{primary} {alternate}"
    if is_latin(alternate) and not is_latin(primary):
        return primary, f"{primary} {alternate}"
    return primary, f"{primary} {alternate}"
