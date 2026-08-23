"""Language detection from MARC records.

Two fields can carry language, and they disagree more often than
expected -- on 18 of 93 records in the reference corpus where both were
present.

``041`` wins where it exists. That is what it is for: ``008/35-37`` is a
single fixed three-character position and cannot express a work in more
than one language, which is precisely the case ``041`` was added to
handle. Where ``008`` says ``gla`` and ``041$a`` says ``gleeng``, the
latter is the more complete statement, not a contradiction.

Only ``041$a`` is consulted. ``$h`` is the language of the *original*
work and appears on 39 records in the reference corpus -- an English
translation of a German book carries ``$a eng`` and ``$h ger``, so a
fallback that accepted any subfield would label it German.

See docs/marc-field-analysis.md section 7.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

MARCXML_NS = "http://www.loc.gov/MARC21/slim"

# Codes that are structurally valid but name no language. Treated as
# absent so that callers fall through to a configured default rather
# than embedding "und" in a corpus as though it were a language.
NON_LANGUAGE = frozenset({"und", "mul", "zxx", "sgn", "xxx", "mis"})

# KNOWN LIMITATION: this is a subset, not the full MARC 21 list (~490
# codes). It covers everything in the reference corpus plus common
# languages, and an unlisted code falls through to the caller's
# configured default rather than corrupting anything -- but a library
# holding material in an uncovered language would silently lose that
# signal. Before any real deployment, replace this with the Library of
# Congress list loaded from a data file:
# https://www.loc.gov/standards/codelists/languages.xml
#
# MARC 21 language codes. This is deliberately not ISO 639-2: the
# Library of Congress list retains deprecated codes that remain in
# circulation, and the reference corpus contains both the deprecated and
# the current form of the same language -- "gae"/"gla" (Scottish Gaelic)
# and "iri"/"gle" (Irish). A general ISO library would reject half of
# them.
MARC_LANGUAGES = frozenset("""
afr alb amh ara arm asm aze bak bal bam baq bel ben bos bre bul bur cat
cel cha chi cop cor cze dan dut egy eng epo esp est fao fin fre fri gae
geo ger gle gla goh grc gre guj hat hau haw heb hin hun ibo ice ind iri
ita jav jpn kan kas kaz khm kir kon kor kur lao lat lav lit ltz mac mal
mao mar may mlt mon nah nav nep nor nya oji ori orm pan per pol por pra
pus que roh rom rum run rus san sco scr slo slv som sot spa srp sun swa
swe syr tah tam tat tel tgk tha tib tir ton tuk tur ukr urd uzb vie wel
wen wol xho yid yor zul
""".split())

_CODE = re.compile(r"^[a-z]{3}$")


def _chunk(value: str) -> list[str]:
    """Split a language subfield into three-character codes.

    MARC permits several codes to be concatenated in one subfield --
    "engfreger" is three languages, not one. All 103 values in the
    reference corpus were of length 3, 6 or 9.

    A value whose length is not a multiple of three is malformed and
    yields nothing. Chunking it anyway produces plausible-looking
    nonsense: "English" would become ["eng", "lis"].
    """
    value = value.strip().lower()
    if not value or len(value) % 3:
        return []
    return [value[i:i + 3] for i in range(0, len(value), 3)]


def _valid(codes: list[str]) -> list[str]:
    seen: list[str] = []
    for code in codes:
        if (_CODE.match(code) and code in MARC_LANGUAGES
                and code not in NON_LANGUAGE and code not in seen):
            seen.append(code)
    return seen


def detect_languages(marc_record: ET.Element,
                     flavour: "Flavour | None" = None) -> tuple[list[str], str | None]:
    """Return ``(codes, source)`` for a MARC record.

    UNIMARC has no 008 and no 041; language lives in 101$a, present on
    392 of 400 records in the UNIMARC reference corpus. Values there are
    mixed-case ("Fre" and "fre" both occur in the same catalogue), which
    the shared chunking already normalises.

    The first code is the primary language. ``source`` names the field
    the codes came from, or is None when neither field yielded anything
    usable -- in which case the caller should apply a configured
    default rather than guess.
    """
    ns = f"{{{MARCXML_NS}}}"

    # Flavour is optional so callers without one still work: 101, 041
    # and 008 do not collide, so trying all three is safe.
    from_101: list[str] = []
    for field in marc_record.findall(f"{ns}datafield"):
        if field.get("tag") != "101":
            continue
        for sub in field.findall(f"{ns}subfield"):
            if sub.get("code") == "a":
                from_101.extend(_chunk(sub.text or ""))
    if codes := _valid(from_101):
        return codes, "101$a"

    from_041: list[str] = []
    for field in marc_record.findall(f"{ns}datafield"):
        if field.get("tag") != "041":
            continue
        for sub in field.findall(f"{ns}subfield"):
            if sub.get("code") == "a":
                from_041.extend(_chunk(sub.text or ""))
    if codes := _valid(from_041):
        return codes, "041$a"

    control = marc_record.find(f"{ns}controlfield[@tag='008']")
    text = (control.text or "") if control is not None else ""
    # Positions 35-37 exist only in a full-length 008. 38 of 436 records
    # in the reference corpus had no usable 008 at all.
    if len(text) >= 38:
        if codes := _valid(_chunk(text[35:38])):
            return codes, "008"

    return [], None
