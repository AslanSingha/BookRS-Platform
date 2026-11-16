#!/usr/bin/env python3
"""Load the UCSD slice's readers, ratings and review flags into Koha.

Fixture building, run once. It is the only thing in this project that
writes to Koha, and it exists so that the model has a library to read:
after it has run, BookRS-Platform reads these rows back out of Koha's own
tables and never writes.

What goes where -- no column or table is added to Koha:

  UCSD user        borrowers   cardnumber 'UCSD-U<id>', no password
  rating r (1-5)   ratings     rating_value
  is_reviewed = 1  reviews     one row; the row's existence is the flag

The review text is not in the slice, so each review row carries a stated
placeholder and approved = 0, which keeps it off the public record page.

The books must already be in Koha (ucsd_to_marc21.py + bulkmarcimport).
They are found by the Goodreads link the MARC writer put in 856$u, which
Koha stores in biblioitems.url.

Everything is checked before anything is written, and counted afterwards.
LOAD DATA LOCAL turns a duplicate or a failed foreign key into a warning
and skips the row, so "rows sent == rows in the table" is checked here
rather than trusted.

  --check   run the checks, write nothing
  --undo    remove what this script loaded (UCSD borrowers; their ratings
            go with them by foreign key; their reviews are deleted first)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

CARD = "UCSD-U"
REVIEW_TEXT = ("[UCSD Book Graph: this reader reviewed the book. "
               "The review text is not part of the fixture.]")


class Koha:
    def __init__(self, a):
        self.base = ["docker", "exec", "-i", a.container, "mariadb",
                     f"-u{a.user}", f"-p{a.password}",
                     "--default-character-set=utf8mb4", a.database]

    def q(self, sql: str) -> list[list[str]]:
        r = subprocess.run(self.base + ["-N", "-B", "-e", sql],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"query failed:\n  {sql}\n  {r.stderr.strip()}")
        return [ln.split("\t") for ln in r.stdout.splitlines()]

    def one(self, sql: str) -> int:
        return int(self.q(sql)[0][0])

    def load(self, sql: str, frame: pd.DataFrame) -> None:
        p = subprocess.Popen(self.base + ["--local-infile=1", "-e", sql],
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            frame.to_csv(p.stdin, sep="\t", header=False, index=False)
            p.stdin.close()
        except BrokenPipeError:
            pass
        err = p.stderr.read()
        if p.wait():
            sys.exit(f"load failed:\n  {sql}\n  {err.strip()}")


def die(msg: str) -> None:
    sys.exit("refusing: " + msg)


def ucsd_where(alias: str = "") -> str:
    # '-' and 'U' are literal in LIKE; the prefix has no wildcard in it.
    return f"{alias}cardnumber LIKE '{CARD}%'"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="data/ucsd", help="output of ucsd_slice.py")
    ap.add_argument("--container", default="kohadev-db-1")
    ap.add_argument("--database", default="koha_kohadev")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="password")
    ap.add_argument("--branch", default="CPL")
    ap.add_argument("--category", default="PT")
    ap.add_argument("--url-prefix", default="https://www.goodreads.com/book/show/")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--undo", action="store_true")
    a = ap.parse_args()
    k, d = Koha(a), Path(a.dir)

    have_b = k.one(f"SELECT COUNT(*) FROM borrowers WHERE {ucsd_where()}")

    if a.undo:
        if not have_b:
            print("nothing to undo: no UCSD borrowers in Koha")
            return
        print(f"removing {have_b:,} UCSD borrowers, their ratings and reviews ...", flush=True)
        k.q(f"DELETE r FROM reviews r JOIN borrowers b USING (borrowernumber) WHERE {ucsd_where('b.')}")
        k.q(f"DELETE r FROM ratings r JOIN borrowers b USING (borrowernumber) WHERE {ucsd_where('b.')}")
        k.q(f"DELETE FROM borrowers WHERE {ucsd_where()}")
        print("left:", k.one(f"SELECT COUNT(*) FROM borrowers WHERE {ucsd_where()}"), "UCSD borrowers")
        return

    # ---- checks on Koha ------------------------------------------------
    print("checking Koha ...", flush=True)
    cols = {r[0] for r in k.q(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA='{a.database}' AND TABLE_NAME='borrowers'")}
    need = {"cardnumber", "userid", "surname", "branchcode", "categorycode",
            "dateenrolled", "dateexpiry"}
    if need - cols:
        die(f"borrowers has no column(s) {sorted(need - cols)}")
    if not k.one(f"SELECT COUNT(*) FROM branches WHERE branchcode='{a.branch}'"):
        die(f"no branch '{a.branch}'. Branches: "
            + ", ".join(r[0] for r in k.q("SELECT branchcode FROM branches")))
    if not k.one(f"SELECT COUNT(*) FROM categories WHERE categorycode='{a.category}'"):
        die(f"no patron category '{a.category}'. Categories: "
            + ", ".join(r[0] for r in k.q("SELECT categorycode FROM categories")))
    if have_b:
        die(f"{have_b:,} UCSD borrowers are already in Koha. "
            "Run with --undo first if you mean to reload.")

    # ---- books: UCSD id -> Goodreads id -> biblionumber ------------------
    books = [json.loads(ln) for ln in open(d / "books.jsonl", encoding="utf-8")]
    in_koha: dict[str, list[str]] = {}
    for bn, url in k.q("SELECT biblionumber, url FROM biblioitems "
                       f"WHERE url LIKE '{a.url_prefix}%'"):
        in_koha.setdefault(url[len(a.url_prefix):].strip(), []).append(bn)
    missing = [b for b in books if b["goodreads_id"] not in in_koha]
    twice = [b for b in books if len(in_koha.get(b["goodreads_id"], [])) > 1]
    total_biblio = k.one("SELECT COUNT(*) FROM biblio")
    print(f"  books in the slice {len(books):,} | found in Koha {len(books) - len(missing):,} "
          f"| Koha holds {total_biblio:,} records in all")
    if missing:
        die(f"{len(missing):,} of the slice's books are not in Koha "
            f"(first: {missing[0]['title'][:50]!r}). Import ucsd.mrc first. If it "
            "was imported, 856$u is not reaching biblioitems.url in this framework.")
    if twice:
        die(f"{len(twice):,} books are in Koha more than once "
            f"(first: {twice[0]['title'][:50]!r}) -- ucsd.mrc was imported twice. "
            "Clear the records and import once.")
    if total_biblio != len(books):
        print(f"  note: Koha holds {total_biblio - len(books):,} records that are not "
              "in the slice. The model will read those as catalogue too.")
    book_bn = {b["ucsd_book_id"]: int(in_koha[b["goodreads_id"]][0]) for b in books}

    # ---- ratings file ----------------------------------------------------
    print("reading ratings.tsv ...", flush=True)
    r = pd.read_csv(d / "ratings.tsv", sep="\t", dtype={"user_id": str, "book_id": str})
    n0 = len(r)
    bad = r[~r["rating"].between(1, 5)]
    if len(bad):
        die(f"{len(bad):,} rows have a rating outside 1-5 "
            f"(values {sorted(int(v) for v in bad['rating'].unique())[:6]}). Koha stores 1-5; the "
            "thesis slice has none of these, so the file is not the thesis slice.")
    unknown = ~r["book_id"].isin(book_bn.keys())
    if unknown.any():
        die(f"{int(unknown.sum()):,} ratings are for books not in books.jsonl")
    dup = int(r.duplicated(["user_id", "book_id"]).sum())
    if dup:
        die(f"{dup:,} (user, book) pairs appear more than once. Koha allows one "
            "rating per patron per record, so the counts could not match the thesis.")
    users = pd.unique(r["user_id"])                 # order of first appearance
    if max(map(len, users)) > 32 - len(CARD):
        die("user ids are too long for borrowers.cardnumber (32 characters)")
    n_rev = int((r["is_reviewed"] == 1).sum())
    print(f"  {n0:,} ratings | {len(users):,} readers | {r['book_id'].nunique():,} books "
          f"| {n_rev:,} with a review")

    if a.check:
        print("\ncheck passed. Nothing was written. Run again without --check to load.")
        return

    # ---- borrowers -------------------------------------------------------
    t = time.time()
    print(f"\nloading {len(users):,} borrowers ...", flush=True)
    k.load(
        "LOAD DATA LOCAL INFILE '/dev/stdin' INTO TABLE borrowers "
        "CHARACTER SET utf8mb4 FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
        "(cardnumber, userid, surname) "
        f"SET branchcode='{a.branch}', categorycode='{a.category}', "
        "dateenrolled=CURDATE(), dateexpiry='2099-12-31'",
        pd.DataFrame({"c": [CARD + u for u in users],
                      "u": ["ucsd" + u for u in users],
                      "s": ["UCSD reader " + u for u in users]}))
    got = k.q(f"SELECT borrowernumber, cardnumber FROM borrowers WHERE {ucsd_where()}")
    user_bn = {c[len(CARD):]: int(bn) for bn, c in got}
    if len(user_bn) != len(users):
        die(f"sent {len(users):,} borrowers, Koha has {len(user_bn):,}. "
            "Run --undo, then look at why rows were skipped.")
    print(f"  done in {time.time() - t:.0f}s")

    r["bn"] = r["user_id"].map(user_bn).astype("int64")
    r["bib"] = r["book_id"].map(book_bn).astype("int64")
    r = r.sort_values(["bn", "bib"], kind="stable")     # primary-key order: fast insert

    # ---- ratings ---------------------------------------------------------
    t = time.time()
    print(f"loading {n0:,} ratings (the long step) ...", flush=True)
    k.load("LOAD DATA LOCAL INFILE '/dev/stdin' INTO TABLE ratings "
           "FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
           "(borrowernumber, biblionumber, rating_value)",
           r[["bn", "bib", "rating"]])
    print(f"  done in {time.time() - t:.0f}s")

    # ---- reviews ---------------------------------------------------------
    t = time.time()
    print(f"loading {n_rev:,} review flags ...", flush=True)
    k.load("LOAD DATA LOCAL INFILE '/dev/stdin' INTO TABLE reviews "
           "FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
           "(borrowernumber, biblionumber) "
           f"SET review='{REVIEW_TEXT}', approved=0, datereviewed=NOW()",
           r.loc[r["is_reviewed"] == 1, ["bn", "bib"]])
    print(f"  done in {time.time() - t:.0f}s")

    # ---- count what Koha now holds ----------------------------------------
    j = f"JOIN borrowers b USING (borrowernumber) WHERE {ucsd_where('b.')}"
    kr = k.one(f"SELECT COUNT(*) FROM ratings r {j}")
    kv = k.one(f"SELECT COUNT(*) FROM reviews r {j}")
    dist = {int(v): int(n) for v, n in k.q(
        f"SELECT rating_value, COUNT(*) FROM ratings r {j} GROUP BY rating_value ORDER BY 1")}
    want = {int(v): int(n) for v, n in r["rating"].value_counts().sort_index().items()}
    print("\nin Koha now:")
    print(f"  borrowers {len(user_bn):,}   ratings {kr:,}   reviews {kv:,}")
    print(f"  rating distribution {dist}")
    ok = kr == n0 and kv == n_rev and dist == want
    if not ok:
        sys.exit(f"MISMATCH: sent {n0:,} ratings / {n_rev:,} reviews, distribution {want}. "
                 "Rows were skipped. Run --undo and report the numbers above.")

    # Which Koha patron is which UCSD reader -- for the evaluation harness,
    # which has to find the thesis's 2,000 evaluated readers again.
    pd.DataFrame({"user_id": list(user_bn), "borrowernumber": list(user_bn.values())}) \
        .to_csv(d / "koha_borrowers.tsv", sep="\t", index=False)
    pd.DataFrame({"ucsd_book_id": list(book_bn), "biblionumber": list(book_bn.values())}) \
        .to_csv(d / "koha_biblios.tsv", sep="\t", index=False)
    print("all counts match what was sent. Wrote koha_borrowers.tsv and koha_biblios.tsv.")


if __name__ == "__main__":
    main()
