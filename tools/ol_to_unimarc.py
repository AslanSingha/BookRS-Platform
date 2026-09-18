"""Write the Open Library slice as UNIMARC for a Koha UNIMARC instance.

Same slice.jsonl as the MARC21 writer, mapped to UNIMARC: 200 title,
330 summary, 606 subjects, 700/701 authors, 101 language, 010 ISBN.
Item subfields (Koha's 995 by default) are arguments because they come
from the instance's framework, not the standard; tools/stage1_unimarc.sh
reads them from the instance before importing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date

from pymarc import Field, Indicators, MARCWriter, Record, Subfield

LEADER = "     nam0 22        450 "


def field(tag: str, ind: str, *pairs: tuple[str, str]) -> Field:
    return Field(tag=tag, indicators=Indicators(ind[0], ind[1]),
                 subfields=[Subfield(code=c, value=v) for c, v in pairs if v])


def split_name(name: str) -> tuple[str, str]:
    """'Jean-Paul Sartre' -> ('Sartre', 'Jean-Paul'); single word -> (word, '')."""
    parts = name.strip().split()
    if len(parts) < 2:
        return name.strip(), ""
    return parts[-1], " ".join(parts[:-1])


def to_record(r: dict, a) -> Record:
    rec = Record(leader=LEADER, to_unicode=True)
    ol_id = r["edition_key"].rsplit("/", 1)[-1]
    lang = r["language"]
    year = r.get("year")
    y = f"{year:04d}" if year else "    "
    # 100$a: 36 positions -- date entered(8) status(1) year1(4) year2(4)
    # audience(3) gov(1) modified(1) cataloguing lang(3) translit(1)
    # charsets(4) extra charsets(4) script(2)
    f100 = f"{date.today():%Y%m%d}d{y}    {'   '}y0{lang}y0103    ba"
    rec.add_field(Field(tag="001", data=ol_id))
    rec.add_field(field("010", "  ", ("a", r["isbn13"])))
    rec.add_field(field("100", "  ", ("a", f100[:36].ljust(36))))
    rec.add_field(field("101", "0 ", ("a", lang)))
    authors = [x for x in r.get("authors", []) if x]
    title = r.get("edition_title") or r["title"]
    rec.add_field(field("200", "1 ", ("a", title), ("e", r.get("subtitle", "")),
                        ("f", ", ".join(authors) if authors else "")))
    if r.get("publisher") or year:
        rec.add_field(field("210", "  ", ("c", r.get("publisher", "")), ("d", str(year) if year else "")))
    if r.get("pages"):
        rec.add_field(field("215", "  ", ("a", f"{r['pages']} p.")))
    if r.get("description"):
        rec.add_field(field("330", "  ", ("a", re.sub(r"\s+", " ", r["description"])[:4000])))
    for s in r.get("subjects", [])[:10]:
        rec.add_field(field("606", " 1", ("a", s)))
    for i, name in enumerate(authors[:4]):
        sn, fn = split_name(name)
        rec.add_field(field("700" if i == 0 else "701", " 1", ("a", sn), ("b", fn), ("4", "070")))
    rec.add_field(field("942", "  ", ("c", a.itype)))
    for i in range(a.copies):
        rec.add_field(field(a.item_tag, "  ",
                            (a.sub_home, a.branch), (a.sub_hold, a.branch),
                            (a.sub_itype, a.itype), (a.sub_barcode, f"{ol_id}-{i + 1}"),
                            (a.sub_callno, r["isbn13"][-6:])))
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default="data/ol/slice.jsonl")
    ap.add_argument("--out", default="data/ol/slice-unimarc.mrc")
    ap.add_argument("--branch", default="CPL")
    ap.add_argument("--itype", default="BK")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--item-tag", default="995")
    ap.add_argument("--sub-home", default="b")
    ap.add_argument("--sub-hold", default="c")
    ap.add_argument("--sub-barcode", default="f")
    ap.add_argument("--sub-itype", default="r")
    ap.add_argument("--sub-callno", default="k")
    a = ap.parse_args()
    if not (os.path.exists(a.slice) and os.path.getsize(a.slice) > 0):
        raise SystemExit(f"slice not ready: {a.slice}")
    n = 0
    with open(a.out, "wb") as fh:
        w = MARCWriter(fh)
        for line in open(a.slice, encoding="utf-8"):
            w.write(to_record(json.loads(line), a))
            n += 1
            if a.limit and n >= a.limit:
                break
        w.close()
    print(f"wrote {n} records to {a.out}")


if __name__ == "__main__":
    main()
