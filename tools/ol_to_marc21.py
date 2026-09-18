"""Write the Open Library slice as MARC21 for Koha's bulkmarcimport.pl.

One bibliographic record per line of slice.jsonl, with one 952 item per
record so the catalogue has holdings (availability) from the first
harvest. Branch and item type default to KTD's sample values; a real
library overrides them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date

from pymarc import Field, Indicators, MARCWriter, Record, Subfield

LEADER = "     nam a22     4a 4500"


def field(tag: str, ind: str, *pairs: tuple[str, str]) -> Field:
    return Field(tag=tag, indicators=Indicators(ind[0], ind[1]),
                 subfields=[Subfield(code=c, value=v) for c, v in pairs if v])


def nonfiling(title: str, lang: str) -> str:
    """Second indicator of 245: characters to skip before sorting."""
    arts = {"eng": ("the ", "a ", "an "), "fre": ("le ", "la ", "les ", "l'", "un ", "une ", "des ")}
    low = title.lower()
    for art in arts.get(lang, ()):
        if low.startswith(art):
            return str(len(art))
    return "0"


def to_record(r: dict, branch: str, itype: str, copies: int) -> Record:
    rec = Record(leader=LEADER, to_unicode=True, force_utf8=True)
    ol_id = r["edition_key"].rsplit("/", 1)[-1]
    year = r.get("year")
    y = f"{year:04d}" if year else "    "
    today = date.today().strftime("%y%m%d")
    # 008: date entered, type of date, date1, place, ..., language (35-37), source
    f008 = f"{today}s{y}    xx            000 0 {r['language']} d"
    rec.add_field(Field(tag="001", data=ol_id))
    rec.add_field(Field(tag="003", data="OL"))
    rec.add_field(Field(tag="008", data=f008[:40].ljust(40)))
    rec.add_field(field("020", "  ", ("a", r["isbn13"])))
    rec.add_field(field("041", "0 ", ("a", r["language"])))
    authors = [a for a in r.get("authors", []) if a]
    if authors:
        rec.add_field(field("100", "1 ", ("a", authors[0]), ("e", "author")))
    title = r.get("edition_title") or r["title"]
    rec.add_field(field("245", "1" + nonfiling(title, r["language"]),
                        ("a", title + (" :" if r.get("subtitle") else "")),
                        ("b", r.get("subtitle", "")),
                        ("c", ", ".join(authors) if authors else "")))
    if r.get("publisher") or year:
        rec.add_field(field("264", " 1", ("b", r.get("publisher", "")), ("c", str(year) if year else "")))
    if r.get("pages"):
        rec.add_field(field("300", "  ", ("a", f"{r['pages']} pages")))
    if r.get("description"):
        rec.add_field(field("520", "  ", ("a", re.sub(r"\s+", " ", r["description"])[:4000])))
    for s in r.get("subjects", [])[:10]:
        rec.add_field(field("650", " 4", ("a", s)))
    for a in authors[1:4]:
        rec.add_field(field("700", "1 ", ("a", a), ("e", "author")))
    rec.add_field(field("942", "  ", ("2", "ddc"), ("c", itype)))
    for i in range(copies):
        rec.add_field(field("952", "  ",
                            ("a", branch), ("b", branch), ("y", itype),
                            ("p", f"{ol_id}-{i + 1}"), ("o", r["isbn13"][-6:]),
                            ("7", "0"), ("8", "GEN")))
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="data/ol/slice.jsonl")
    ap.add_argument("--out", default="data/ol/slice-marc21.mrc")
    ap.add_argument("--branch", default="CPL")
    ap.add_argument("--itype", default="BK")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not (os.path.exists(a.slice) and os.path.getsize(a.slice) > 0):
        raise SystemExit(f"slice not ready: {a.slice}")
    n = 0
    with open(a.out, "wb") as fh:
        w = MARCWriter(fh)
        for line in open(a.slice, encoding="utf-8"):
            r = json.loads(line)
            w.write(to_record(r, a.branch, a.itype, a.copies))
            n += 1
            if a.limit and n >= a.limit:
                break
        w.close()
    print(f"wrote {n} records to {a.out}")


if __name__ == "__main__":
    main()
