#!/usr/bin/env python3
"""Extend the test library from the thesis's slice to its full catalogue.

Fixture building, like tools/ucsd_koha_load.py: it stocks the test library
(Koha) so the model has a library to read. The model's read path never
writes to Koha; this is the only kind of thing that does.

What "full" means: the thesis's own catalogue after its entity resolution,
883,468 books (BookRS-System/data/bookrs_ucsd_books.parquet, exported by
BookRS-System/scripts/export_books_parquet.py), with every rating in
BookRS-DataPrep's interactions file that belongs to one of those books.
Ratings on the 150,105 editions the thesis's entity resolution deleted are
not loaded: the thesis did not move them to the edition it kept, so its
catalogue does not have them either.

What is already in Koha stays as it is -- the 2,000-book slice, its
446,664 readers and their 16 million ratings, the test and demo patrons --
and only what is new is added:

  * books: the 883,468 minus the slice's books already in Koha;
  * readers: those with a rating on the full catalogue who are not already
    UCSD borrowers;
  * ratings: every rating on the full catalogue except the slice's own.
    The slice is exactly "every rating by a slice reader on a slice book"
    (top books first, then readers with ten or more among them), so that
    is the rule that leaves it out -- no rating is sent twice.

Three steps, in the fixture container (compose profile "fixture"):

  python tools/ucsd_full.py prepare     # files in data/ucsd-full/, no Koha
  python tools/ucsd_full.py marc        # MARC in chunks of 100,000
  # ... import every chunk with Koha's bulkmarcimport.pl, then reindex
  python tools/ucsd_full.py load --check
  python tools/ucsd_full.py load        # borrowers, ratings, review flags
  python tools/ucsd_full.py load --undo # remove what `load` added

`marc --missing-only` writes MARC for the new books Koha does not hold
yet: the way to finish an import that stopped part-way.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

CARD = "UCSD-U"
REVIEW_TEXT = ("[UCSD Book Graph: this reader reviewed the book. "
               "The review text is not part of the fixture.]")
URL_PREFIX = "https://www.goodreads.com/book/show/"
CHUNK = 100_000


def die(msg: str):
    sys.exit("refusing: " + msg)


def n(x) -> str:
    return f"{int(x):,}"


# ---------------------------------------------------------------------------
# What is already in Koha, from the slice's own files
# ---------------------------------------------------------------------------

def slice_books(slice_dir: Path) -> tuple[set[str], set[str]]:
    """(UCSD csv ids, Goodreads ids) of the slice's books."""
    ucsd, gids = set(), set()
    for line in open(slice_dir / "books.jsonl", encoding="utf-8"):
        if line.strip():
            b = json.loads(line)
            ucsd.add(str(b["ucsd_book_id"]))
            gids.add(str(b["goodreads_id"]))
    return ucsd, gids


def slice_readers(slice_dir: Path) -> set[str]:
    """The slice's readers: koha_borrowers.tsv, written when they were loaded."""
    out = set()
    with open(slice_dir / "koha_borrowers.tsv", encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            if line.strip():
                out.add(line.split("\t")[0])
    return out


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def plan(inter, books, mapping: dict[str, str], ucsd_in_slice: set[str],
         gids_in_slice: set[str], readers_in_slice: set[str]):
    """What the full catalogue adds to what Koha holds. Pure: frames in,
    frames and counts out.

    inter: user_id, book_id (UCSD csv id), rating, is_reviewed
    books: book_id (Goodreads id), title, authors, description, genre,
           isbn13, ratings_count -- the full catalogue
    """
    import pandas as pd

    known = set(books["book_id"].astype(str))
    ub = inter["book_id"].astype(str)
    per_ub = ub.value_counts()
    gids = pd.Series(per_ub.index, index=per_ub.index).map(mapping)
    in_catalogue = gids.isin(known)

    joined = ub.isin(per_ub.index[in_catalogue.to_numpy()])
    old = inter["user_id"].astype(str).isin(readers_in_slice) & ub.isin(ucsd_in_slice)
    new = inter.loc[joined & ~old, ["user_id", "rating", "is_reviewed"]].copy()
    new["goodreads_id"] = ub[joined & ~old].map(mapping).to_numpy()
    new = new[["user_id", "goodreads_id", "rating", "is_reviewed"]]

    interactions = pd.Series(per_ub[in_catalogue].to_numpy(),
                             index=gids[in_catalogue].to_numpy())
    add = books[~books["book_id"].astype(str).isin(gids_in_slice)].copy()
    add["interactions"] = add["book_id"].astype(str).map(interactions).fillna(0).astype(int)
    add = add.sort_values(["interactions", "book_id"], ascending=[False, True], kind="stable")

    readers = pd.unique(new["user_id"])
    stats = {
        "catalogue_books": len(books),
        "books_already_in_koha": int(books["book_id"].astype(str).isin(gids_in_slice).sum()),
        "books_to_add": len(add),
        "books_without_ratings": int((add["interactions"] == 0).sum()),
        "ratings_in_file": len(inter),
        "ratings_on_catalogue": int(joined.sum()),
        "ratings_already_in_koha": int((joined & old).sum()),
        "ratings_to_add": len(new),
        "ratings_not_loaded_edition_removed": int((~joined).sum()),
        "reviews_to_add": int((new["is_reviewed"] == 1).sum()),
        "readers_in_ratings_to_add": len(readers),
        "readers_to_add": int((~pd.Series(readers).isin(readers_in_slice)).sum()),
    }
    return add, new, stats


def cmd_prepare(a) -> int:
    import pandas as pd

    slice_dir, out = Path(a.slice), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ucsd_in_slice, gids_in_slice = slice_books(slice_dir)
    readers_in_slice = slice_readers(slice_dir)
    print(f"slice already in Koha: {n(len(gids_in_slice))} books, "
          f"{n(len(readers_in_slice))} readers")

    print("loading the catalogue ...", flush=True)
    books = pd.read_parquet(a.books, columns=["book_id", "title", "authors", "description",
                                              "genre", "isbn13", "ratings_count"])
    books["book_id"] = books["book_id"].astype(str)
    if books["book_id"].duplicated().any():
        die("the catalogue lists a book id twice")
    print(f"  {n(len(books))} books")

    m = pd.read_csv(a.id_map, dtype=str)
    mapping = dict(zip(m["book_id_csv"], m["book_id"]))
    del m

    print("loading the ratings ...", flush=True)
    inter = pd.read_parquet(a.interactions, columns=["user_id", "book_id", "rating",
                                                     "is_reviewed"])
    bad = ~inter["rating"].between(1, 5)
    if bad.any():
        die(f"{n(bad.sum())} ratings are outside 1-5; Koha stores 1-5")
    if inter.duplicated(["user_id", "book_id"]).any():
        die("a reader rates the same book twice; Koha keeps one rating per patron "
            "per record")
    print(f"  {n(len(inter))} ratings")

    add, new, stats = plan(inter, books, mapping, ucsd_in_slice, gids_in_slice,
                           readers_in_slice)
    del inter

    def text(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()

    print(f"writing books.jsonl ({n(len(add))} books) ...", flush=True)
    with open(out / "books.jsonl", "w", encoding="utf-8") as fh:
        for r in add.itertuples(index=False):
            fh.write(json.dumps({
                "goodreads_id": r.book_id, "title": text(r.title),
                "authors": text(r.authors), "description": text(r.description),
                "genre": text(r.genre), "isbn13": text(r.isbn13).split(".")[0],
                "ratings_count": None if pd.isna(r.ratings_count) else int(r.ratings_count),
                "interactions": int(r.interactions),
            }, ensure_ascii=False) + "\n")
    print(f"writing ratings.tsv ({n(len(new))} ratings) ...", flush=True)
    new.to_csv(out / "ratings.tsv", sep="\t", index=False)

    stats["seconds"] = round(time.time() - t0)
    (out / "summary.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2))
    return 0


# ---------------------------------------------------------------------------
# marc
# ---------------------------------------------------------------------------

def chunk_names(total: int, size: int) -> list[str]:
    return [f"ucsd-full-{i + 1:03d}.mrc" for i in range((total + size - 1) // size)]


def cmd_marc(a) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pymarc import MARCWriter

    from ucsd_to_marc21 import to_record

    d = Path(a.dir)
    books = [json.loads(ln) for ln in open(d / "books.jsonl", encoding="utf-8") if ln.strip()]
    if a.missing_only:
        held = KohaDb(a.url).goodreads_ids()
        books = [b for b in books if b["goodreads_id"] not in held]
        print(f"{n(len(books))} of the new books are not in Koha yet")
    outdir = d / ("marc-missing" if a.missing_only else "marc")
    outdir.mkdir(exist_ok=True)
    for old in outdir.glob("ucsd-full-*.mrc"):
        old.unlink()

    opts = SimpleNamespace(url_prefix=URL_PREFIX, itype=a.itype, branch=a.branch)
    stats = dict(clipped=0, no_isbn=0, no_description=0, no_genre=0)
    names = chunk_names(len(books), a.chunk)
    for i, name in enumerate(names):
        part = books[i * a.chunk:(i + 1) * a.chunk]
        with open(outdir / name, "wb") as fh:
            w = MARCWriter(fh)
            for b in part:
                w.write(to_record(b, opts, stats))
            w.close()
        print(f"  {name}: {n(len(part))} records", flush=True)
    print(f"wrote {n(len(books))} records in {len(names)} files to {outdir}/")
    print(f"  without ISBN {n(stats['no_isbn'])} | without description "
          f"{n(stats['no_description'])} | without genre {n(stats['no_genre'])} | "
          f"descriptions cut to fit {n(stats['clipped'])}")
    return 0


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

class KohaDb:
    """Koha's database, over KOHA_DB_URL. Writes are fixture building."""

    def __init__(self, url: str):
        import pymysql
        from urllib.parse import unquote, urlparse

        if not url:
            die("KOHA_DB_URL is not set")
        u = urlparse(url)
        self.conn = pymysql.connect(
            host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
            password=unquote(u.password or ""), database=u.path.lstrip("/"),
            charset="utf8mb4", local_infile=True, autocommit=True)

    def q(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def one(self, sql: str, params=None) -> int:
        return int(self.q(sql, params)[0][0])

    def goodreads_ids(self) -> dict[str, list[int]]:
        """Goodreads id -> Koha record numbers, from the link the MARC writer
        put in 856$u (Koha keeps it in biblioitems.url)."""
        out: dict[str, list[int]] = {}
        for bn, url in self.q("SELECT biblionumber, url FROM biblioitems WHERE url LIKE %s",
                              (URL_PREFIX + "%",)):
            out.setdefault(url[len(URL_PREFIX):].strip(), []).append(int(bn))
        return out

    def ucsd_borrowers(self) -> dict[str, int]:
        return {c[len(CARD):]: int(bn) for bn, c in self.q(
            "SELECT borrowernumber, cardnumber FROM borrowers WHERE cardnumber LIKE %s",
            (CARD + "%",))}

    def load(self, sql: str, rows) -> None:
        """LOAD DATA LOCAL INFILE from a temporary tab-separated file."""
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False,
                                         encoding="utf-8") as fh:
            for r in rows:
                fh.write("\t".join(str(v) for v in r) + "\n")
            path = fh.name
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.format(path=path))
        finally:
            os.unlink(path)


def ucsd_ratings(k: KohaDb, table: str) -> int:
    return k.one(f"SELECT COUNT(*) FROM {table} r JOIN borrowers b "
                 "USING (borrowernumber) WHERE b.cardnumber LIKE %s", (CARD + "%",))


def cmd_load(a) -> int:
    import pandas as pd

    d, slice_dir = Path(a.dir), Path(a.slice)
    k = KohaDb(a.url)
    readers_in_slice = slice_readers(slice_dir)
    new_books = [json.loads(ln)["goodreads_id"]
                 for ln in open(d / "books.jsonl", encoding="utf-8") if ln.strip()]

    if a.undo:
        return undo(k, readers_in_slice, new_books)

    print("checking Koha ...", flush=True)
    if not k.one("SELECT COUNT(*) FROM branches WHERE branchcode = %s", (a.branch,)):
        die(f"no branch {a.branch!r} in Koha")
    if not k.one("SELECT COUNT(*) FROM categories WHERE categorycode = %s", (a.category,)):
        die(f"no patron category {a.category!r} in Koha")
    held = k.goodreads_ids()
    missing = [g for g in new_books if g not in held]
    twice = [g for g in new_books if len(held.get(g, [])) > 1]
    print(f"  new books {n(len(new_books))} | in Koha {n(len(new_books) - len(missing))} "
          f"| Koha holds {n(k.one('SELECT COUNT(*) FROM biblio'))} records")
    if missing:
        die(f"{n(len(missing))} of the new books are not in Koha yet (first: Goodreads "
            f"{missing[0]}). Import the remaining MARC first; "
            "`marc --missing-only` writes exactly those.")
    if twice:
        die(f"{n(len(twice))} books are in Koha more than once (first: Goodreads "
            f"{twice[0]}): a chunk was imported twice.")

    borrowers = k.ucsd_borrowers()
    gone = readers_in_slice - borrowers.keys()
    if gone:
        die(f"{n(len(gone))} of the slice's readers are not UCSD borrowers in Koha")
    extra = borrowers.keys() - readers_in_slice
    if extra:
        die(f"{n(len(extra))} UCSD borrowers beyond the slice are already in Koha: this "
            "load ran before. `load --undo` removes what it added.")

    print("reading ratings.tsv ...", flush=True)
    r = pd.read_csv(d / "ratings.tsv", sep="\t", dtype={"user_id": str, "goodreads_id": str})
    unknown = ~r["goodreads_id"].isin(held.keys())
    if unknown.any():
        die(f"{n(unknown.sum())} ratings are for books Koha does not hold")
    users = pd.unique(r["user_id"])
    new_users = [u for u in users if u not in readers_in_slice]
    if new_users and max(map(len, new_users)) > 32 - len(CARD):
        die("user ids are too long for borrowers.cardnumber (32 characters)")
    n_rev = int((r["is_reviewed"] == 1).sum())
    print(f"  {n(len(r))} ratings | {n(len(users))} readers, {n(len(new_users))} of them "
          f"new | {n(n_rev)} with a review")
    if a.check:
        print("\ncheck passed. Nothing was written. Run again without --check to load.")
        return 0

    before_r, before_v = ucsd_ratings(k, "ratings"), ucsd_ratings(k, "reviews")

    t = time.time()
    print(f"\nadding {n(len(new_users))} borrowers ...", flush=True)
    k.load("LOAD DATA LOCAL INFILE '{path}' INTO TABLE borrowers CHARACTER SET utf8mb4 "
           "FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
           "(cardnumber, userid, surname) "
           f"SET branchcode='{a.branch}', categorycode='{a.category}', "
           "dateenrolled=CURDATE(), dateexpiry='2099-12-31'",
           ((CARD + u, "ucsd" + u, "UCSD reader " + u) for u in new_users))
    borrowers = k.ucsd_borrowers()
    if len(borrowers) != len(readers_in_slice) + len(new_users):
        die(f"sent {n(len(new_users))} borrowers, Koha has "
            f"{n(len(borrowers) - len(readers_in_slice))} new. Run `load --undo`, then "
            "look at why rows were skipped.")
    print(f"  done in {time.time() - t:.0f}s")

    r["bn"] = r["user_id"].map(borrowers).astype("int64")
    r["bib"] = r["goodreads_id"].map(lambda g: held[g][0]).astype("int64")
    r = r.sort_values(["bn", "bib"], kind="stable")     # primary-key order: fast insert

    t = time.time()
    print(f"adding {n(len(r))} ratings (the long step) ...", flush=True)
    k.load("LOAD DATA LOCAL INFILE '{path}' INTO TABLE ratings "
           "FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
           "(borrowernumber, biblionumber, rating_value)",
           r[["bn", "bib", "rating"]].itertuples(index=False))
    print(f"  done in {time.time() - t:.0f}s")

    t = time.time()
    print(f"adding {n(n_rev)} review flags ...", flush=True)
    k.load("LOAD DATA LOCAL INFILE '{path}' INTO TABLE reviews "
           "FIELDS TERMINATED BY '\\t' LINES TERMINATED BY '\\n' "
           "(borrowernumber, biblionumber) "
           f"SET review='{REVIEW_TEXT}', approved=0, datereviewed=NOW()",
           r.loc[r["is_reviewed"] == 1, ["bn", "bib"]].itertuples(index=False))
    print(f"  done in {time.time() - t:.0f}s")

    after_r, after_v = ucsd_ratings(k, "ratings"), ucsd_ratings(k, "reviews")
    print(f"\nUCSD ratings in Koha: {n(before_r)} -> {n(after_r)}; "
          f"reviews {n(before_v)} -> {n(after_v)}")
    if after_r - before_r != len(r) or after_v - before_v != n_rev:
        sys.exit(f"MISMATCH: sent {n(len(r))} ratings and {n(n_rev)} reviews; Koha gained "
                 f"{n(after_r - before_r)} and {n(after_v - before_v)}. Rows were "
                 "skipped. Run `load --undo` and report the numbers above.")

    pd.DataFrame({"user_id": new_users,
                  "borrowernumber": [borrowers[u] for u in new_users]}) \
        .to_csv(d / "koha_borrowers_new.tsv", sep="\t", index=False)
    pd.DataFrame({"goodreads_id": new_books,
                  "biblionumber": [held[g][0] for g in new_books]}) \
        .to_csv(d / "koha_biblios_new.tsv", sep="\t", index=False)
    print("all counts match what was sent. Wrote koha_borrowers_new.tsv and "
          "koha_biblios_new.tsv.")
    return 0


def undo(k: KohaDb, readers_in_slice: set[str], new_books: list[str]) -> int:
    """Remove the borrowers, ratings and reviews `load` added: every rating
    or review by a UCSD reader beyond the slice, or on a book beyond it.
    The books themselves stay; Koha's own tools remove records."""
    borrowers = k.ucsd_borrowers()
    extra = sorted(v for u, v in borrowers.items() if u not in readers_in_slice)
    held = k.goodreads_ids()
    bibs = sorted({b for g in new_books for b in held.get(g, [])})
    print(f"removing ratings and reviews of {n(len(extra))} borrowers beyond the slice "
          f"and on {n(len(bibs))} books beyond it ...", flush=True)
    for table, rows in (("_undo_borrowers", extra), ("_undo_biblios", bibs)):
        k.q(f"CREATE TEMPORARY TABLE IF NOT EXISTS {table} (id INT PRIMARY KEY)")
        k.q(f"TRUNCATE {table}")
        k.load(f"LOAD DATA LOCAL INFILE '{{path}}' INTO TABLE {table} (id)",
               ((v,) for v in rows))
    for t in ("reviews", "ratings"):
        k.q(f"DELETE x FROM {t} x JOIN _undo_borrowers u ON u.id = x.borrowernumber")
        k.q(f"DELETE x FROM {t} x JOIN _undo_biblios u ON u.id = x.biblionumber "
            "JOIN borrowers b ON b.borrowernumber = x.borrowernumber "
            "WHERE b.cardnumber LIKE %s", (CARD + "%",))
    k.q("DELETE b FROM borrowers b JOIN _undo_borrowers u ON u.id = b.borrowernumber")
    print(f"done. UCSD borrowers now {n(len(k.ucsd_borrowers()))} "
          f"(the slice: {n(len(readers_in_slice))})")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="files in --out; touches nothing else")
    p.add_argument("--interactions", default="/source/dataprep/bookrs_ucsd_interactions.parquet")
    p.add_argument("--books", default="/source/thesis/bookrs_ucsd_books.parquet")
    p.add_argument("--id-map", default="data/ucsd/book_id_map.csv")
    p.add_argument("--slice", default="data/ucsd")
    p.add_argument("--out", default="data/ucsd-full")

    p = sub.add_parser("marc", help="MARC for the new books, in chunks")
    p.add_argument("--dir", default="data/ucsd-full")
    p.add_argument("--chunk", type=int, default=CHUNK)
    p.add_argument("--branch", default="CPL")
    p.add_argument("--itype", default="BK")
    p.add_argument("--missing-only", action="store_true",
                   help="only the new books Koha does not hold yet (needs KOHA_DB_URL)")
    p.add_argument("--url", default=os.environ.get("KOHA_DB_URL", ""))

    p = sub.add_parser("load", help="borrowers, ratings and review flags into Koha")
    p.add_argument("--dir", default="data/ucsd-full")
    p.add_argument("--slice", default="data/ucsd")
    p.add_argument("--branch", default="CPL")
    p.add_argument("--category", default="PT")
    p.add_argument("--url", default=os.environ.get("KOHA_DB_URL", ""))
    p.add_argument("--check", action="store_true")
    p.add_argument("--undo", action="store_true")

    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "marc": cmd_marc, "load": cmd_load}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
