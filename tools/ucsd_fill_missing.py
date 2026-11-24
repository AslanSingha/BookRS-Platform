#!/usr/bin/env python3
"""Give the slice's placeholder records their real title, author and text.

Nineteen of the 2,000 most-rated books in the thesis slice are missing
from BookRS-DataPrep's books parquet, so ucsd_slice.py loaded them into
Koha as "[UCSD book N: not in the prepared metadata]". They are popular
books -- readers rated them thousands of times -- so they are recommended
often, and a placeholder on an OPAC page looks broken.

Their metadata is in the raw UCSD files DataPrep started from. This
script finds it there and writes it into each Koha record.

**How Koha is changed.** Through Koha's REST API: the current MARC
record is fetched, the title (245), author (100), summary (520) and ISBN
(020) are replaced, and the record is sent back. Koha then updates its
own tables, its MARC copy and its search index together, which a direct
SQL update would not do. Items, ratings and reviews are untouched.

This is fixture maintenance, like ucsd_koha_load.py. The model's read
path still never writes to Koha.

Afterwards books.jsonl is rewritten with the filled values (the
original is kept as books.jsonl.orig), so the fixture files and Koha
agree.

  --check   find the metadata and print it; change nothing
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from pymarc import Field, parse_xml_to_array, record_to_xml

try:
    from pymarc import Subfield
except ImportError:          # pymarc < 5
    Subfield = None

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_WS = re.compile(r"\s+")


def clean(s) -> str:
    return _WS.sub(" ", _CTRL.sub(" ", s or "")).strip()


def clip(s: str, limit: int) -> str:
    raw = s.encode("utf-8")
    return s if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore").rstrip() + " ..."


def field(tag, ind, *pairs):
    pairs = [(c, v) for c, v in pairs if v]
    if not pairs:
        return None
    if Subfield is not None:
        return Field(tag=tag, indicators=[ind[0], ind[1]],
                     subfields=[Subfield(code=c, value=v) for c, v in pairs])
    return Field(tag=tag, indicators=[ind[0], ind[1]], subfields=[x for p in pairs for x in p])


def scan(path: Path, key: str, wanted: set[str]) -> dict[str, dict]:
    """Records from a gzipped JSON-lines file whose ``key`` is in ``wanted``.

    The raw books file is about two gigabytes; testing for the id as a
    substring before parsing keeps this to a few minutes.
    """
    found: dict[str, dict] = {}
    needles = {f'"{key}": "{w}"': w for w in wanted}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            for needle, w in needles.items():
                if needle in line:
                    rec = json.loads(line)
                    if str(rec.get(key)) == w:
                        found[w] = rec
            if len(found) == len(wanted):
                break
            if n % 500_000 == 0:
                print(f"  {path.name}: {n:,} lines, {len(found)} of {len(wanted)} found",
                      flush=True)
    return found


class Koha:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/") + "/api/v1"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = {"Authorization": f"Basic {token}"}

    def _call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={**self.auth, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_marc(self, biblionumber: int):
        status, body = self._call("GET", f"/biblios/{biblionumber}",
                                  headers={"Accept": "application/marcxml+xml"})
        if status != 200:
            raise RuntimeError(f"GET biblio {biblionumber}: HTTP {status} "
                               f"{body[:200].decode('utf-8', 'replace')}")
        from io import BytesIO
        return parse_xml_to_array(BytesIO(body))[0]

    def put_marc(self, biblionumber: int, record) -> None:
        xml = record_to_xml(record, namespace=True)
        status, body = self._call("PUT", f"/biblios/{biblionumber}", body=xml,
                                  headers={"Content-Type": "application/marcxml+xml",
                                           "Accept": "application/json"})
        if status not in (200, 204):
            raise RuntimeError(f"PUT biblio {biblionumber}: HTTP {status} "
                               f"{body[:300].decode('utf-8', 'replace')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="data/ucsd")
    ap.add_argument("--raw-books", required=True, help="goodreads_books.json.gz")
    ap.add_argument("--raw-authors", required=True, help="goodreads_book_authors.json.gz")
    ap.add_argument("--koha", default="http://localhost:8081",
                    help="Koha staff interface, which serves the REST API.")
    ap.add_argument("--user", default="koha")
    ap.add_argument("--password", default="koha")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    d = Path(a.dir)

    for p in (a.raw_books, a.raw_authors):
        if not Path(p).is_file():
            print(f"error: no file {p}", file=sys.stderr)
            return 2

    books = [json.loads(ln) for ln in open(d / "books.jsonl", encoding="utf-8")]
    missing = {b["goodreads_id"]: b for b in books if not b.get("has_metadata", True)}
    if not missing:
        print("no placeholder records in books.jsonl: nothing to do")
        return 0
    biblio = {}
    with open(d / "koha_biblios.tsv", encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            uid, bn = line.rstrip("\n").split("\t")
            biblio[uid] = int(bn)
    print(f"{len(missing)} placeholder records. Reading {Path(a.raw_books).name} ...", flush=True)

    raw = scan(Path(a.raw_books), "book_id", set(missing))
    author_ids = {x["author_id"] for r in raw.values() for x in r.get("authors", [])
                  if x.get("author_id")}
    print(f"found {len(raw)} of {len(missing)}. Reading {Path(a.raw_authors).name} "
          f"for {len(author_ids)} authors ...", flush=True)
    names = {k: v.get("name", "") for k, v in
             scan(Path(a.raw_authors), "author_id", author_ids).items()}

    filled = {}
    print()
    for gid, b in sorted(missing.items(), key=lambda kv: kv[1]["rank"]):
        r = raw.get(gid)
        if r is None:
            print(f"  #{b['rank']:>4} {gid:>10}  NOT in the raw file -- left as a placeholder")
            continue
        authors = [clean(names.get(x.get("author_id"), "")) for x in r.get("authors", [])]
        authors = [x for x in authors if x]
        title = clean(r.get("title") or r.get("title_without_series"))
        if not title:
            print(f"  #{b['rank']:>4} {gid:>10}  raw record has no title -- left as is")
            continue
        isbn = re.sub(r"[^0-9Xx]", "", r.get("isbn13") or r.get("isbn") or "")
        filled[gid] = {
            "title": title,
            "authors": ", ".join(authors),
            "description": clip(clean(r.get("description")), 8000),
            "isbn13": isbn if len(isbn) in (10, 13) else "",
        }
        f = filled[gid]
        print(f"  #{b['rank']:>4} {gid:>10}  {title[:48]:48}  {f['authors'][:24]:24}"
              f"  lang={r.get('language_code') or '-':5} desc={len(f['description']):>5}")

    if a.check:
        print(f"\ncheck only: {len(filled)} records would be updated. Nothing was changed.")
        return 0
    if not filled:
        return 1

    koha = Koha(a.koha, a.user, a.password)
    print(f"\nupdating {len(filled)} records in Koha ...", flush=True)
    done = 0
    for gid, f in filled.items():
        bn = biblio[missing[gid]["ucsd_book_id"]]
        try:
            rec = koha.get_marc(bn)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                print("Koha refused the login. Basic authentication for the API is "
                      "probably off after the last restart; see docs/koha-setup.md, "
                      "section RESTBasicAuth.", file=sys.stderr)
            return 1
        for tag in ("020", "100", "245", "520"):
            for old in rec.get_fields(tag):
                rec.remove_field(old)
        for new in (field("020", "  ", ("a", f["isbn13"])),
                    field("100", "1 ", ("a", f["authors"])),
                    field("245", "10" if f["authors"] else "00", ("a", f["title"])),
                    field("520", "  ", ("a", f["description"]))):
            if new is not None:
                rec.add_ordered_field(new)
        koha.put_marc(bn, rec)
        back = koha.get_marc(bn)
        titles = [sf for f in back.get_fields("245") for sf in f.get_subfields("a")]
        got = titles[0] if titles else ""
        if got != f["title"]:
            print(f"error: biblio {bn} reads back as {got!r}, expected {f['title']!r}",
                  file=sys.stderr)
            return 1
        done += 1
    print(f"  {done} updated and read back")

    shutil.copyfile(d / "books.jsonl", d / "books.jsonl.orig")
    with open(d / "books.jsonl", "w", encoding="utf-8") as out:
        for b in books:
            if b["goodreads_id"] in filled:
                b = {**b, **filled[b["goodreads_id"]], "has_metadata": True}
            out.write(json.dumps(b, ensure_ascii=False) + "\n")
    print("books.jsonl updated (previous version: books.jsonl.orig)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
