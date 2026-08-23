"""MARC record to normalised work.

Field numbers mean different things in MARC21 and UNIMARC, and three of
them collide outright -- 100, 300 and 020 exist in both with unrelated
meanings. The map is therefore selected by detected flavour and never by
tag number alone; see flavour.py for why the metadata prefix cannot be
trusted to tell us which we have.

Every extraction rule here was measured against a real corpus rather
than taken from the standard. Where the two disagreed, the corpus won.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from xml.etree import ElementTree as ET

from bookrs.ingestion.flavour import Flavour
from bookrs.ingestion.language import detect_languages
from bookrs.ingestion.normalize import clean_isbn, join_title, strip_isbd

MARCXML_NS = "http://www.loc.gov/MARC21/slim"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"

log = logging.getLogger(__name__)

# 'c1993.' is a copyright date and the commonest form in the reference
# corpus (91 of 411). \b does not match between 'c' and '1', so a digit
# boundary is used instead. The trailing guard rejects '20134' rather
# than silently reading it as 2013.
_YEAR = re.compile(r"(?<![0-9])(1[0-9]{3}|20[0-9]{2})(?![0-9])")

# Subject subdivisions. $9 is Koha's authority link and $2 the thesaurus
# code -- neither is content, and including them appends stray numbers
# to every heading.
_SUBJECT_SUBFIELDS = ("a", "x", "y", "z", "v")
_SUBDIVISION_SEP = " — "


@dataclass
class FieldMap:
    """Tag and subfield assignments for one MARC flavour."""

    title: tuple[str, tuple[str, ...]]
    author: tuple[str, tuple[str, ...]]
    added_authors: tuple[str, tuple[str, ...]]
    isbn: tuple[str, tuple[str, ...]]
    subjects: tuple[str, ...]
    summary: tuple[str, tuple[str, ...]]
    contents: tuple[str, tuple[str, ...]]
    publication: tuple[str, ...]
    publisher_code: str
    date_code: str
    item: str


# 245$h is the medium designator ("[electronic resource]") and carries
# ISBD punctuation like the title parts, but is not part of the title.
MARC21 = FieldMap(
    title=("245", ("a", "b")),
    author=("100", ("a",)),
    added_authors=("700", ("a",)),
    isbn=("020", ("a",)),
    subjects=("650", "651"),
    summary=("520", ("a",)),
    contents=("505", ("a",)),
    # 264 (RDA) and 260 (AACR2) never co-occurred in the reference
    # corpus, so precedence is a simple preference, not a merge.
    publication=("264", "260"),
    publisher_code="b",
    date_code="c",
    item="952",
)

# In UNIMARC, 100 is general processing data and 700 is the main author
# -- the reverse of MARC21 for 700, and unrelated for 100.
#
# Subfield letters differ from MARC21 within the same-purpose fields and
# were verified against a real UNIMARC corpus. Notably 200$b is NOT a
# subtitle: it is the medium designator, carrying "REV" on all 400
# sampled records. The subtitle is 200$e. Publisher is 210$c (395
# occurrences) and date is 210$d (346), where MARC21 uses 260$b and
# 260$c respectively.
UNIMARC = FieldMap(
    title=("200", ("a", "e")),
    author=("700", ("a", "b")),
    added_authors=("701", ("a", "b")),
    isbn=("010", ("a",)),
    subjects=("606", "607"),
    summary=("330", ("a",)),
    contents=("327", ("a",)),
    publication=("210",),
    publisher_code="c",
    date_code="d",
    item="995",
)

FIELD_MAPS = {Flavour.MARC21: MARC21, Flavour.UNIMARC: UNIMARC}


@dataclass
class Work:
    """A bibliographic record, normalised."""

    source_record_id: str
    flavour: Flavour
    title: str = ""
    authors: list[str] = dc_field(default_factory=list)
    isbns: list[str] = dc_field(default_factory=list)
    subjects: list[str] = dc_field(default_factory=list)
    summary: str = ""
    contents: str = ""
    languages: list[str] = dc_field(default_factory=list)
    publisher: str = ""
    publication_year: int | None = None
    item_count: int = 0
    deleted: bool = False
    # Which field each value came from. Kept because a wrong title is
    # far easier to diagnose when you can see it came from 200$a rather
    # than 245$a.
    provenance: dict[str, str] = dc_field(default_factory=dict)


def _fields(record: ET.Element, tag: str) -> list[ET.Element]:
    ns = f"{{{MARCXML_NS}}}"
    return [f for f in record.findall(f"{ns}datafield") if f.get("tag") == tag]


def _subfields(field: ET.Element, codes: tuple[str, ...]) -> list[str]:
    ns = f"{{{MARCXML_NS}}}"
    return [
        sub.text or ""
        for sub in field.findall(f"{ns}subfield")
        if sub.get("code") in codes
    ]


def _first_text(record: ET.Element, tag: str, codes: tuple[str, ...]) -> str:
    for field in _fields(record, tag):
        if values := [v for v in _subfields(field, codes) if v.strip()]:
            return strip_isbd(" ".join(values))
    return ""


def _extract_year(value: str) -> int | None:
    match = _YEAR.search(value or "")
    return int(match.group(1)) if match else None


def map_record(marc_record: ET.Element, flavour: Flavour,
               source_record_id: str, *, log_unmapped: bool = True) -> Work:
    """Extract a Work from a parsed MARCXML record."""
    fm = FIELD_MAPS[flavour]
    work = Work(source_record_id=source_record_id, flavour=flavour)
    used: set[str] = set()

    tag, codes = fm.title
    for field in _fields(marc_record, tag):
        if parts := _subfields(field, codes):
            work.title = join_title(parts)
            work.provenance["title"] = f"{tag}${''.join(codes)}"
            used.add(tag)
            break

    for role, (tag, codes) in (("author", fm.author), ("added", fm.added_authors)):
        for field in _fields(marc_record, tag):
            if name := strip_isbd(" ".join(v for v in _subfields(field, codes) if v.strip())):
                work.authors.append(name)
                work.provenance.setdefault("authors", f"{tag}${''.join(codes)}")
                used.add(tag)

    tag, codes = fm.isbn
    for field in _fields(marc_record, tag):
        for raw in _subfields(field, codes):
            if (isbn := clean_isbn(raw)) and isbn not in work.isbns:
                work.isbns.append(isbn)
                work.provenance.setdefault("isbns", f"{tag}${''.join(codes)}")
        used.add(tag)

    for tag in fm.subjects:
        for field in _fields(marc_record, tag):
            parts = [strip_isbd(v) for v in _subfields(field, _SUBJECT_SUBFIELDS)]
            if heading := _SUBDIVISION_SEP.join(p for p in parts if p):
                work.subjects.append(heading)
                work.provenance.setdefault("subjects", tag)
            used.add(tag)

    for attr, (tag, codes) in (("summary", fm.summary), ("contents", fm.contents)):
        if text := _first_text(marc_record, tag, codes):
            setattr(work, attr, text)
            work.provenance[attr] = f"{tag}${''.join(codes)}"
        used.add(tag)

    for tag in fm.publication:
        fields = _fields(marc_record, tag)
        used.add(tag)
        if not fields:
            continue
        field = fields[0]
        work.publisher = strip_isbd(" ".join(_subfields(field, (fm.publisher_code,))))
        work.publication_year = _extract_year(
            " ".join(_subfields(field, (fm.date_code,)))
        )
        work.provenance["publication"] = tag
        break

    work.item_count = len(_fields(marc_record, fm.item))
    used.add(fm.item)

    work.languages, source = detect_languages(marc_record, flavour)
    if source:
        work.provenance["languages"] = source
    used.update({"041", "008", "101"})

    if log_unmapped:
        unmapped = sorted(
            {t for f in marc_record.findall(f"{{{MARCXML_NS}}}datafield")
             if (t := f.get("tag")) and t not in used}
        )
        if unmapped:
            log.debug("record %s: unmapped tags %s", source_record_id, unmapped)

    return work
