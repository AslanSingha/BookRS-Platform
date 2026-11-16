#!/usr/bin/env python3
"""Write the UCSD slice as MARC21 so Koha can be populated with it.

This is fixture building, not the read path. Koha is a MARC system: its
OPAC renders a record page from the MARCXML in biblio_metadata, and the
supported way to create a record is to import one. So the test library is
stocked with MARC -- and BookRS-Platform then reads that library from its
database columns and never parses MARC. The rule is about what the model
reads, not about how a test library gets its books.

Where each UCSD field lands, and the Koha column it becomes:

  title         245$a   biblio.title
  authors       100$a   biblio.author      (kept whole -- see below)
  description   520$a   biblio.abstract
  isbn13        020$a   biblioitems.isbn
  goodreads id  856$u   biblioitems.url    (the id, as the link it is)
  genre         952$8   items.ccode        (collection code)
  one copy      952     items              (so the record can be borrowed)

The author string is not split. The parquet holds one string per book and
does not say how several authors are separated, and a comma is also how
"Surname, Forename" is written. Guessing would invent authors.

Descriptions are cleaned of control characters and cut to fit: an
ISO 2709 field cannot exceed 9,999 bytes, and a control character in a
record is what made Koha's own Zebra export fail on biblio 369.
"""
from __future__ import annotations

import argparse
import json
import re

from pymarc import Field, MARCWriter, Record

try:
    from pymarc import Subfield
except ImportError:          # pymarc < 5
    Subfield = None

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_WS = re.compile(r"\s+")


def clean(s: str) -> str:
    return _WS.sub(" ", _CTRL.sub(" ", s or "")).strip()


def clip(s: str, limit: int) -> tuple[str, bool]:
    raw = s.encode("utf-8")
    if len(raw) <= limit:
        return s, False
    return raw[:limit].decode("utf-8", "ignore").rstrip() + " ...", True


def field(tag: str, ind: str, *pairs: tuple[str, str]) -> Field | None:
    pairs = [(c, v) for c, v in pairs if v]
    if not pairs:
        return None
    if Subfield is not None:
        return Field(tag=tag, indicators=[ind[0], ind[1]],
                     subfields=[Subfield(code=c, value=v) for c, v in pairs])
    flat = [x for p in pairs for x in p]
    return Field(tag=tag, indicators=[ind[0], ind[1]], subfields=flat)


def to_record(b: dict, a, stats: dict) -> Record:
    rec = Record(force_utf8=True)
    try:
        rec.leader.record_status = "n"
        rec.leader.type_of_record = "a"
        rec.leader.bibliographic_level = "m"
    except AttributeError:
        pass

    isbn = re.sub(r"[^0-9Xx]", "", b.get("isbn13") or "")
    if len(isbn) not in (10, 13):
        isbn = ""
        stats["no_isbn"] += 1

    title, _ = clip(clean(b.get("title")) or "[untitled]", 2000)
    author, _ = clip(clean(b.get("authors")), 1000)
    desc, cut = clip(clean(b.get("description")), 8000)
    stats["clipped"] += cut
    stats["no_description"] += not desc
    genre = clean(b.get("genre"))[:80]
    stats["no_genre"] += not genre

    gid = b["goodreads_id"]
    for f in (
        field("020", "  ", ("a", isbn)),
        field("100", "1 ", ("a", author)),
        field("245", "10" if author else "00", ("a", title)),
        field("520", "  ", ("a", desc)),
        field("856", "4 ", ("u", f"{a.url_prefix}{gid}")),
        field("942", "  ", ("c", a.itype)),
        field("952", "  ", ("a", a.branch), ("b", a.branch), ("y", a.itype),
              ("p", f"UCSD-{gid}"), ("o", f"UCSD {gid}"), ("8", genre)),
    ):
        if f is not None:
            rec.add_ordered_field(f)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--books", default="data/ucsd/books.jsonl")
    ap.add_argument("--out", default="data/ucsd/ucsd.mrc")
    ap.add_argument("--branch", default="CPL")
    ap.add_argument("--itype", default="BK")
    ap.add_argument("--url-prefix", default="https://www.goodreads.com/book/show/")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    stats = dict(clipped=0, no_isbn=0, no_description=0, no_genre=0)
    n = 0
    with open(a.out, "wb") as fh:
        w = MARCWriter(fh)
        for line in open(a.books, encoding="utf-8"):
            w.write(to_record(json.loads(line), a, stats))
            n += 1
            if a.limit and n >= a.limit:
                break
        w.close()
    print(f"wrote {n:,} records to {a.out}")
    print(f"  without ISBN {stats['no_isbn']:,} | without description "
          f"{stats['no_description']:,} | without genre {stats['no_genre']:,} | "
          f"descriptions cut to fit {stats['clipped']:,}")


if __name__ == "__main__":
    main()
