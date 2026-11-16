#!/usr/bin/env python3
"""Cut the thesis's evaluation slice out of the UCSD parquets.

The thesis experiments (BookRS-System/experiments/05_implicit_feedback.py)
do not rank against the whole catalogue. They keep the 2,000 most-rated
books, then the users with at least ten interactions among those books,
and evaluate there. This script reproduces that cut with the same two
operations in the same order, so that what is loaded into Koha is the
population the thesis numbers describe.

It reads the parquets BookRS-DataPrep already produced and writes new
files. Nothing in BookRS-System or BookRS-DataPrep is modified.

Output (in --out):
  books.jsonl             one book per line, most-rated first
  ratings.tsv             user_id, book_id, rating, is_reviewed
  thesis_eval_users.txt   the first 2,000 users in order of appearance,
                          which is the sample the thesis evaluated
  summary.json            counts, to compare against the thesis

The join between the two parquets is checked rather than assumed. The
interactions file may key books by Goodreads id or by the row index of
goodreads_interactions.csv, and a small integer is a valid value in both
id spaces, so a wrong join would attach real titles to the wrong books
and nothing would fail. The script measures both joins. When both cover
the slice it compares each against Goodreads' own ratings_count and takes
the join under which popular books are popular; if that does not separate
them it stops.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


def as_str(s: pd.Series) -> pd.Series:
    return s if pd.api.types.is_string_dtype(s) else s.astype(str)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interactions", required=True)
    ap.add_argument("--books", required=True)
    ap.add_argument("--id-map", default=None,
                    help="UCSD book_id_map.csv (book_id_csv,book_id). Lets the "
                         "script test the csv-index join as well as the direct one.")
    ap.add_argument("--join", choices=["auto", "direct", "map"], default="auto")
    ap.add_argument("--out", default="data/ucsd")
    ap.add_argument("--top-books", type=int, default=2000)
    ap.add_argument("--min-user", type=int, default=10)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    print("loading interactions ...", flush=True)
    inter = pd.read_parquet(a.interactions)
    inter["user_id"] = as_str(inter["user_id"])
    inter["book_id"] = as_str(inter["book_id"])
    print(f"  {len(inter):,} interactions")

    # --- the thesis's two filters, same operations, same order -----------
    counts = inter["book_id"].value_counts()
    top = counts.head(a.top_books)
    if len(counts) > a.top_books:
        last_in, first_out = int(counts.iloc[a.top_books - 1]), int(counts.iloc[a.top_books])
        tie = " -- TIE at the boundary, membership depends on sort order" \
            if last_in == first_out else ""
        print(f"  book #{a.top_books} has {last_in:,} interactions, "
              f"#{a.top_books + 1} has {first_out:,}{tie}")

    d2 = inter[inter["book_id"].isin(top.index)]
    uc = d2["user_id"].value_counts()
    keep = uc[uc >= a.min_user].index
    d3 = d2[d2["user_id"].isin(keep)]
    del inter, d2

    n_users, n_books = d3["user_id"].nunique(), d3["book_id"].nunique()
    print(f"after filters: {len(d3):,} interactions, {n_users:,} users, {n_books:,} books")

    # --- join to the book metadata, verified ------------------------------
    print("loading books ...", flush=True)
    books = pd.read_parquet(a.books)
    books["book_id"] = as_str(books["book_id"])
    known = set(books["book_id"])
    wanted = list(top.index)

    direct = sum(b in known for b in wanted) / len(wanted)
    via, mapping = None, {}
    if a.id_map:
        m = pd.read_csv(a.id_map, dtype=str)
        mapping = dict(zip(m["book_id_csv"], m["book_id"]))
        via = sum(mapping.get(b) in known for b in wanted) / len(wanted)

    print(f"  join coverage, ids used directly:     {direct:.1%}")
    if via is not None:
        print(f"  join coverage, through book_id_map:   {via:.1%}")

    books = books.drop_duplicates("book_id").set_index("book_id")

    # Coverage cannot settle it when both joins cover: a small integer is a
    # valid id in both spaces. What can settle it is Goodreads' own count.
    # ratings_count in the metadata is how many people rated the book on
    # Goodreads; under the right join it rises and falls with the number of
    # interactions in this file, and under the wrong join it does not.
    def agreement(fn) -> float:
        ids = [fn(b) for b in wanted]
        ours = pd.Series([int(top[b]) for b, i in zip(wanted, ids) if i in books.index], dtype="float64")
        theirs = pd.to_numeric(pd.Series([books.at[i, "ratings_count"] for i in ids if i in books.index]),
                               errors="coerce")
        ok = theirs.notna()
        return float(ours[ok].rank().corr(theirs[ok].rank())) if ok.sum() > 10 else float("nan")

    cand = {"direct": (direct, lambda b: b)}
    if via is not None:
        cand["map"] = (via, lambda b: mapping.get(b))
    cand = {k: v for k, v in cand.items() if v[0] >= 0.95}
    if not cand:
        print("error: neither join covers the slice. The interactions file "
              "keys books by something this script does not know.", file=sys.stderr)
        return 1
    agree = {k: agreement(fn) for k, (_, fn) in cand.items()}
    for k, v in agree.items():
        print(f"  agreement with Goodreads' ratings_count, {k:6}: {v:+.2f}  (rank correlation)")

    if a.join != "auto":
        if a.join not in cand:
            print(f"error: --join {a.join} does not cover the slice.", file=sys.stderr)
            return 1
        chosen = a.join
    elif len(cand) == 1:
        chosen = next(iter(cand))
    else:
        best, other = sorted(agree, key=lambda k: -agree[k] if agree[k] == agree[k] else 9)
        if not (agree[best] >= 0.3 and agree[best] - agree[other] >= 0.3):
            print("error: both joins cover the slice and the popularity check does "
                  "not separate them. Not guessing; pass --join direct or --join map "
                  "once it is settled by hand.", file=sys.stderr)
            return 1
        chosen = best
    use_map = chosen == "map"
    to_meta = cand[chosen][1]
    print(f"  using: {'book_id_map' if use_map else 'direct ids'}")


    def text(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()

    rows, missing, unmapped = [], 0, 0
    for rank, b in enumerate(wanted, 1):
        mid = to_meta(b)
        if mid is None:
            unmapped += 1
            continue
        if mid not in books.index:
            # The readers rated it, so it stays in the population the thesis
            # evaluated. It has no text, so the content model has nothing
            # for it -- which is what happened in the thesis too.
            missing += 1
            rows.append({"rank": rank, "ucsd_book_id": b, "goodreads_id": mid,
                         "interactions": int(top[b]), "has_metadata": False,
                         "title": f"[UCSD book {mid}: not in the prepared metadata]",
                         "authors": "", "description": "", "genre": "",
                         "isbn13": "", "ratings_count": None})
            continue
        r = books.loc[mid]
        rc = r.get("ratings_count")
        rows.append({
            "rank": rank, "has_metadata": True,
            "ucsd_book_id": b,            # the id the ratings use
            "goodreads_id": mid,          # the id the metadata uses
            "interactions": int(top[b]),
            "title": text(r.get("title")),
            "authors": text(r.get("authors")),
            "description": text(r.get("description")),
            "genre": text(r.get("genre")),
            "isbn13": text(r.get("isbn13")).split(".")[0],
            "ratings_count": None if pd.isna(rc) else int(rc),
        })

    # The check a coverage figure cannot make: do the most-rated books in
    # the slice look like the most-rated books on Goodreads?
    print("\n  top of the slice -- these should be famous books:")
    for r in rows[:8]:
        print(f"    {r['interactions']:>9,}  {r['title'][:52]:52}  {r['authors'][:24]}")

    with open(out / "books.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    have = {r["ucsd_book_id"] for r in rows}
    d3 = d3[d3["book_id"].isin(have)]
    print(f"\nwriting ratings.tsv ({len(d3):,} rows) ...", flush=True)
    d3[["user_id", "book_id", "rating", "is_reviewed"]].to_csv(
        out / "ratings.tsv", sep="\t", index=False)

    # test_lookup in the thesis is keyed by u_idx, assigned in order of
    # first appearance, and the first 2,000 keys are evaluated.
    order = d3["user_id"].unique()
    (out / "thesis_eval_users.txt").write_text("\n".join(order[:2000]) + "\n")

    summary = {
        "interactions": int(len(d3)),
        "users": int(d3["user_id"].nunique()),
        "books": len(rows),
        "books_without_metadata": missing,
        "books_dropped_no_id": unmapped,
        "reviewed": int(d3["is_reviewed"].sum()),
        "rating_distribution": {int(k): int(v) for k, v in
                                d3["rating"].value_counts().sort_index().items()},
        "share_rated_4_or_5": round(float((d3["rating"] >= 4).mean()), 4),
        "join": "book_id_map" if use_map else "direct",
        "top_books": a.top_books, "min_user_interactions": a.min_user,
        "pandas": pd.__version__,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"\nthesis, same filters: 446,664 users over 2,000 books. "
          f"This run: {summary['users']:,} over {summary['books']:,}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
