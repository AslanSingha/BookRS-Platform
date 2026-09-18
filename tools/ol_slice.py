"""Build a catalogue slice from the Open Library dumps.

Three streaming passes, in this order:
  1. works    -> reservoir-sample works that carry a description and >= 2 subjects
  2. editions -> for those works, the first edition with an ISBN-13 in an accepted language
  3. authors  -> names for the authors of the matched works

Output: one JSON object per line, one per work, with everything the MARC
writer needs. Nothing is downloaded here; the dumps must already exist.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import time

YEAR_RE = re.compile(r"(1[5-9]\d\d|20[0-2]\d)")


def rows(path: str):
    """Yield (key, json_text) from a dump, cheaply."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.split("\t", 4)
            if len(parts) == 5:
                yield parts[1], parts[4]


def text_of(v) -> str:
    if isinstance(v, dict):
        v = v.get("value", "")
    return (v or "").strip() if isinstance(v, str) else ""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def pass_works(path: str, sample: int, seed: int) -> dict:
    rng = random.Random(seed)
    reservoir: list[dict] = []
    seen = 0
    for n, (key, js) in enumerate(rows(path), 1):
        if n % 2_000_000 == 0:
            log(f"works: {n:,} lines, {seen:,} eligible")
        if '"description"' not in js or '"subjects"' not in js:
            continue
        try:
            w = json.loads(js)
        except json.JSONDecodeError:
            continue
        desc = text_of(w.get("description"))
        subjects = [s.strip() for s in w.get("subjects", []) if isinstance(s, str) and s.strip()]
        title = text_of(w.get("title"))
        if len(desc) < 80 or len(subjects) < 2 or not title:
            continue
        seen += 1
        rec = {
            "work_key": key,
            "title": title,
            "subtitle": text_of(w.get("subtitle")),
            "description": desc[:4000],
            "subjects": subjects[:12],
            "author_keys": [a.get("author", {}).get("key") for a in w.get("authors", [])
                            if isinstance(a, dict) and isinstance(a.get("author"), dict)],
        }
        if len(reservoir) < sample:
            reservoir.append(rec)
        else:
            j = rng.randrange(seen)
            if j < sample:
                reservoir[j] = rec
    log(f"works: done, {seen:,} eligible, kept {len(reservoir):,}")
    return {r["work_key"]: r for r in reservoir}


def pass_editions(path: str, works: dict, langs: set[str], target: int) -> dict:
    lang_tokens = [f"/languages/{l}" for l in langs]
    matched: dict[str, dict] = {}
    for n, (key, js) in enumerate(rows(path), 1):
        if n % 5_000_000 == 0:
            log(f"editions: {n:,} lines, {len(matched):,} works matched")
        if '"isbn_13"' not in js or '"works"' not in js or not any(t in js for t in lang_tokens):
            continue
        try:
            e = json.loads(js)
        except json.JSONDecodeError:
            continue
        wk = (e.get("works") or [{}])[0].get("key")
        if wk not in works or wk in matched:
            continue
        lang = None
        for l in e.get("languages", []):
            code = (l.get("key", "") if isinstance(l, dict) else "").rsplit("/", 1)[-1]
            if code in langs:
                lang = code
                break
        isbns = [i.replace("-", "") for i in e.get("isbn_13", []) if isinstance(i, str)]
        isbns = [i for i in isbns if len(i) == 13 and i.isdigit()]
        if not lang or not isbns:
            continue
        m = YEAR_RE.search(text_of(e.get("publish_date")))
        matched[wk] = {
            "edition_key": key,
            "isbn13": isbns[0],
            "language": lang,
            "publisher": (e.get("publishers") or [""])[0],
            "year": int(m.group(1)) if m else None,
            "pages": e.get("number_of_pages"),
            "edition_title": text_of(e.get("title")),
        }
        if len(matched) >= target:
            log(f"editions: target {target:,} reached at line {n:,}")
            break
    log(f"editions: done, {len(matched):,} works matched")
    return matched


def pass_authors(path: str, wanted: set[str]) -> dict:
    names: dict[str, str] = {}
    for n, (key, js) in enumerate(rows(path), 1):
        if n % 5_000_000 == 0:
            log(f"authors: {n:,} lines, {len(names):,} found")
        if key not in wanted:
            continue
        try:
            names[key] = text_of(json.loads(js).get("name"))
        except json.JSONDecodeError:
            pass
        if len(names) == len(wanted):
            break
    log(f"authors: done, {len(names):,} of {len(wanted):,} resolved")
    return names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/ol")
    ap.add_argument("--out", default="data/ol/slice.jsonl")
    ap.add_argument("--target", type=int, default=50_000, help="works in the output")
    ap.add_argument("--sample", type=int, default=250_000, help="works sampled in pass 1")
    ap.add_argument("--langs", default="eng,fre")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    langs = set(a.langs.split(","))

    works = pass_works(f"{a.dir}/ol_dump_works_latest.txt.gz", a.sample, a.seed)
    editions = pass_editions(f"{a.dir}/ol_dump_editions_latest.txt.gz", works, langs, a.target)
    wanted = {k for wk in editions for k in works[wk]["author_keys"] if k}
    names = pass_authors(f"{a.dir}/ol_dump_authors_latest.txt.gz", wanted)

    n = 0
    with open(a.out, "w", encoding="utf-8") as out:
        for wk, ed in editions.items():
            w = works[wk]
            rec = {**w, **ed,
                   "authors": [names[k] for k in w["author_keys"] if k in names and names[k]]}
            rec.pop("author_keys", None)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    by_lang = {}
    for ed in editions.values():
        by_lang[ed["language"]] = by_lang.get(ed["language"], 0) + 1
    log(f"wrote {n:,} records to {a.out}  by language: {by_lang}")


if __name__ == "__main__":
    main()
