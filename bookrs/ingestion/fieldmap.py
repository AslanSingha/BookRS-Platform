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
from bookrs.ingestion.normalize import (
    clean_isbn, join_title, strip_isbd, strip_nonfiling,
)

import hashlib

MARCXML_NS = "http://www.loc.gov/MARC21/slim"
OAI_NS = "http://www.openarchives.org/OAI/2.0/"

log = logging.getLogger(__name__)

# Bump when extraction changes in a way that alters the Work produced
# from an unchanged source record: a corrected subfield, a new
# normalisation rule, an added field.
#
# The content hash covers the MARC record, not our interpretation of it.
# Without this in the hash, a library's records would keep whatever
# extraction was current when they were first harvested, and a mapping
# fix would never reach them -- the source is unchanged, so the hash
# matches and the loader correctly skips the rewrite. Observed with the
# non-filing-bracket fix: 4,849 records reported unchanged while every
# extracted title had in fact changed.
#
# History:
#   1  initial
#   2  strip non-filing brackets from titles (UNIMARC "[L\']aurore")
MAPPER_VERSION = 2

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
class ItemMap:
    """Item subfield assignments, measured against both corpora.

    The letters differ between flavours for the same concept, and three
    collide outright: $c is the shelving location in MARC21 but the
    owning branch in UNIMARC, and $r is a timestamp in MARC21 but the
    item type in UNIMARC. Reusing one map for both would silently store
    a date in the item_type column.
    """

    barcode: str
    owning_branch: str
    holding_branch: str
    location: str
    call_number: str | None
    item_type: str
    due_date: str
    issue_count: str | None


MARC21_ITEM = ItemMap(barcode="p", owning_branch="a", holding_branch="b",
                      location="c", call_number=None, item_type="y",
                      due_date="q", issue_count="l")

# $n carried a due date on 35 of 1,633 sampled items -- the only live
# circulation state observed in either corpus.
UNIMARC_ITEM = ItemMap(barcode="f", owning_branch="c", holding_branch="b",
                       location="e", call_number="k", item_type="r",
                       due_date="n", issue_count=None)


@dataclass
class Item:
    barcode: str = ""
    owning_branch: str = ""
    holding_branch: str = ""
    location: str = ""
    call_number: str = ""
    item_type: str = ""
    due_date: str | None = None
    issue_count: int | None = None

    @property
    def on_loan(self) -> bool:
        """Availability is the presence of a due date, not a status flag."""
        return self.due_date is not None


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
    item_map: ItemMap


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
    item_map=MARC21_ITEM,
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
    item_map=UNIMARC_ITEM,
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
    items: list["Item"] = dc_field(default_factory=list)
    deleted: bool = False
    # MARC 005 changes only when the bibliographic record genuinely
    # changes; the OAI datestamp also moves on circulation. Absent under
    # UNIMARC, so content_hash is the flavour-neutral check.
    marc_005: str | None = None
    # Bibliography only, holdings excluded. Governs re-embedding.
    content_hash: str | None = None
    # Holdings only. Governs whether item rows need rewriting. Separate
    # because the two change independently: a checkout moves the items
    # and not the bibliography, and a catalogue correction the reverse.
    items_hash: str | None = None
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


def _content_hash(marc_record: ET.Element, item_tag: str) -> str:
    """Hash the record with its holdings excluded.

    Circulation activity bumps a record's OAI datestamp without touching
    its bibliographic content, so re-embedding on datestamp alone would
    be enormously wasteful. Excluding the item field gives a check that
    moves only when the bibliography does -- and unlike MARC 005, it
    works under UNIMARC too.

    The serialisation must be canonical. Koha emits attributes in
    nondeterministic order: two consecutive GetRecord calls for the same
    record returned identical byte counts but with tag/ind1/ind2 in
    different sequence, which is Perl hash iteration order surfacing in
    the output. ElementTree preserves that order faithfully, so a plain
    tostring() hash changes on every harvest and every record looks
    modified. ET.canonicalize (C14N) sorts attributes and normalises
    namespaces, which is what makes the comparison meaningful.
    """
    clone = ET.Element(marc_record.tag, marc_record.attrib)
    for child in marc_record:
        if child.get("tag") != item_tag:
            clone.append(child)
    canonical = ET.canonicalize(
        ET.tostring(clone, encoding="unicode"), strip_text=True
    )
    return hashlib.sha256(
        f"v{MAPPER_VERSION}\n{canonical}".encode("utf-8")
    ).hexdigest()


def _items_hash(marc_record: ET.Element, item_tag: str) -> str:
    """Hash only the holdings fields.

    Canonicalised for the same reason as the bibliographic hash: Koha
    emits attributes in nondeterministic order, so an uncanonicalised
    hash would report every item as changed on every harvest.
    """
    clone = ET.Element(marc_record.tag, marc_record.attrib)
    for child in marc_record:
        if child.get("tag") == item_tag:
            clone.append(child)
    canonical = ET.canonicalize(
        ET.tostring(clone, encoding="unicode"), strip_text=True
    )
    return hashlib.sha256(
        f"v{MAPPER_VERSION}\n{canonical}".encode("utf-8")
    ).hexdigest()


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
            work.title = strip_nonfiling(join_title(parts))
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

    im = fm.item_map
    for field in _fields(marc_record, fm.item):
        codes = {sub.get("code"): (sub.text or "").strip()
                 for sub in field.findall(f"{{{MARCXML_NS}}}subfield")}
        issues = codes.get(im.issue_count) if im.issue_count else None
        work.items.append(Item(
            barcode=codes.get(im.barcode, ""),
            owning_branch=codes.get(im.owning_branch, ""),
            holding_branch=codes.get(im.holding_branch, ""),
            location=codes.get(im.location, ""),
            call_number=codes.get(im.call_number, "") if im.call_number else "",
            item_type=codes.get(im.item_type, ""),
            due_date=codes.get(im.due_date) or None,
            issue_count=int(issues) if issues and issues.isdigit() else None,
        ))
    work.item_count = len(work.items)
    used.add(fm.item)

    control_005 = marc_record.find(f"{{{MARCXML_NS}}}controlfield[@tag='005']")
    if control_005 is not None and control_005.text:
        work.marc_005 = control_005.text.strip()
    work.content_hash = _content_hash(marc_record, fm.item)
    work.items_hash = _items_hash(marc_record, fm.item)

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
