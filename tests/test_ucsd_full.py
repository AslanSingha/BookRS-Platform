"""tools/ucsd_full.py: what the full catalogue adds to the library.

The rule that matters most is the one that keeps the load from sending a
rating twice: the slice already in Koha is exactly "every rating by a
slice reader on a slice book", so everything else on the full catalogue
is new, and that is what must be sent.
"""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ucsd_full  # noqa: E402

# Books by UCSD csv id -> Goodreads id. 1 and 2 are the slice; 3 and 4 are
# new to Koha; 5 is an edition the thesis's entity resolution removed (not
# in the catalogue); 6 is in the catalogue and nobody rated it.
MAPPING = {"c1": "g1", "c2": "g2", "c3": "g3", "c4": "g4", "c5": "g5", "c6": "g6"}
SLICE_UCSD, SLICE_GIDS, SLICE_READERS = {"c1", "c2"}, {"g1", "g2"}, {"ua", "ub"}


def _catalogue():
    return pd.DataFrame({
        "book_id": ["g1", "g2", "g3", "g4", "g6"],
        "title": ["One", "Two", "Three", "Four", "Six"], "authors": ["A"] * 5,
        "description": ["d"] * 5, "genre": ["fiction", "romance", "poetry", "children", "fiction"],
        "isbn13": ["9780000000001.0", None, "", "x", "9780000000006"],
        "ratings_count": [10, 20, 30, 40, 0]})


def _ratings():
    rows = [
        ("ua", "c1", 5, 1), ("ua", "c2", 4, 0), ("ub", "c1", 3, 0),   # the slice
        ("ua", "c3", 2, 1),                                            # slice reader, new book
        ("uc", "c1", 4, 0),                                            # new reader, slice book
        ("uc", "c4", 5, 1), ("uc", "c3", 1, 0),                        # new reader, new books
        ("ud", "c5", 4, 1),                                            # removed edition only
        ("ub", "c5", 3, 0),                                            # removed edition
    ]
    return pd.DataFrame(rows, columns=["user_id", "book_id", "rating", "is_reviewed"])


def _plan():
    return ucsd_full.plan(_ratings(), _catalogue(), MAPPING, SLICE_UCSD, SLICE_GIDS,
                          SLICE_READERS)


class TestPlan:
    def test_the_slices_own_ratings_are_never_sent_again(self):
        _, new, _ = _plan()
        sent = set(zip(new["user_id"], new["goodreads_id"]))
        assert not sent & {("ua", "g1"), ("ua", "g2"), ("ub", "g1")}

    def test_every_other_rating_on_the_catalogue_is_sent(self):
        _, new, _ = _plan()
        assert set(zip(new["user_id"], new["goodreads_id"], new["rating"])) == {
            ("ua", "g3", 2), ("uc", "g1", 4), ("uc", "g4", 5), ("uc", "g3", 1)}

    def test_ratings_on_removed_editions_are_not_loaded_and_are_counted(self):
        _, new, stats = _plan()
        assert "g5" not in set(new["goodreads_id"])
        assert stats["ratings_not_loaded_edition_removed"] == 2

    def test_only_books_not_already_in_koha_are_added_most_rated_first(self):
        add, _, stats = _plan()
        assert list(add["book_id"]) == ["g3", "g4", "g6"]
        assert list(add["interactions"]) == [2, 1, 0]
        assert (stats["books_to_add"], stats["books_without_ratings"]) == (3, 1)

    def test_the_counts_add_up(self):
        _, new, stats = _plan()
        assert stats["ratings_on_catalogue"] == (stats["ratings_already_in_koha"]
                                                 + stats["ratings_to_add"])
        assert stats["ratings_in_file"] == (stats["ratings_on_catalogue"]
                                            + stats["ratings_not_loaded_edition_removed"])
        assert (stats["readers_in_ratings_to_add"], stats["readers_to_add"]) == (2, 1)
        assert stats["reviews_to_add"] == int(new["is_reviewed"].sum()) == 2


@pytest.fixture
def slice_dir(tmp_path):
    d = tmp_path / "ucsd"
    d.mkdir()
    with open(d / "books.jsonl", "w", encoding="utf-8") as fh:
        for c, g in (("c1", "g1"), ("c2", "g2")):
            fh.write(json.dumps({"ucsd_book_id": c, "goodreads_id": g}) + "\n")
    (d / "koha_borrowers.tsv").write_text("user_id\tborrowernumber\nua\t1\nub\t2\n")
    pd.DataFrame({"book_id_csv": list(MAPPING), "book_id": list(MAPPING.values())}) \
        .to_csv(d / "book_id_map.csv", index=False)
    return d


def test_the_slice_files_are_read_as_written(slice_dir):
    assert ucsd_full.slice_books(slice_dir) == (SLICE_UCSD, SLICE_GIDS)
    assert ucsd_full.slice_readers(slice_dir) == SLICE_READERS


def test_prepare_writes_the_books_ratings_and_counts(slice_dir, tmp_path):
    _catalogue().to_parquet(tmp_path / "books.parquet")
    _ratings().assign(is_read=1).to_parquet(tmp_path / "inter.parquet")
    out = tmp_path / "full"
    assert ucsd_full.main(["prepare", "--interactions", str(tmp_path / "inter.parquet"),
                           "--books", str(tmp_path / "books.parquet"),
                           "--id-map", str(slice_dir / "book_id_map.csv"),
                           "--slice", str(slice_dir), "--out", str(out)]) == 0
    books = [json.loads(ln) for ln in (out / "books.jsonl").read_text().splitlines()]
    assert [b["goodreads_id"] for b in books] == ["g3", "g4", "g6"]
    assert books[0]["isbn13"] == ""                # an empty ISBN stays empty
    ratings = pd.read_csv(out / "ratings.tsv", sep="\t", dtype=str)
    assert list(ratings.columns) == ["user_id", "goodreads_id", "rating", "is_reviewed"]
    assert len(ratings) == 4
    assert json.loads((out / "summary.json").read_text())["ratings_to_add"] == 4


def test_prepare_refuses_a_reader_rating_a_book_twice(slice_dir, tmp_path):
    _catalogue().to_parquet(tmp_path / "books.parquet")
    pd.concat([_ratings(), _ratings().head(1)]).to_parquet(tmp_path / "inter.parquet")
    with pytest.raises(SystemExit, match="same book twice"):
        ucsd_full.main(["prepare", "--interactions", str(tmp_path / "inter.parquet"),
                        "--books", str(tmp_path / "books.parquet"),
                        "--id-map", str(slice_dir / "book_id_map.csv"),
                        "--slice", str(slice_dir), "--out", str(tmp_path / "full")])


class TestMarc:
    def _books(self, d, count):
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "books.jsonl", "w", encoding="utf-8") as fh:
            for i in range(count):
                fh.write(json.dumps({"goodreads_id": f"g{i}", "title": f"Book {i}",
                                     "authors": "A", "description": "d",
                                     "genre": "fiction", "isbn13": ""}) + "\n")

    def test_chunks_are_named_in_order_and_cover_everything(self):
        assert ucsd_full.chunk_names(250, 100) == [
            "ucsd-full-001.mrc", "ucsd-full-002.mrc", "ucsd-full-003.mrc"]
        assert ucsd_full.chunk_names(200, 100) == ["ucsd-full-001.mrc", "ucsd-full-002.mrc"]

    def test_every_book_is_written_once_with_its_link_and_genre(self, tmp_path):
        from pymarc import MARCReader
        d = tmp_path / "full"
        self._books(d, 5)
        assert ucsd_full.main(["marc", "--dir", str(d), "--chunk", "2"]) == 0
        files = sorted((d / "marc").glob("*.mrc"))
        assert [f.name for f in files] == ucsd_full.chunk_names(5, 2)
        records = [r for f in files for r in MARCReader(f.read_bytes())]
        assert [r["856"]["u"] for r in records] == [
            f"{ucsd_full.URL_PREFIX}g{i}" for i in range(5)]
        assert {r["952"]["8"] for r in records} == {"fiction"}

    def test_missing_only_writes_just_what_koha_lacks(self, tmp_path, monkeypatch):
        from pymarc import MARCReader
        d = tmp_path / "full"
        self._books(d, 4)

        class _Koha:
            def __init__(self, url):
                pass

            def goodreads_ids(self):
                return {"g0": [10], "g2": [12]}
        monkeypatch.setattr(ucsd_full, "KohaDb", _Koha)
        assert ucsd_full.main(["marc", "--dir", str(d), "--missing-only", "--url", "x"]) == 0
        (f,) = (d / "marc-missing").glob("*.mrc")
        assert [r["856"]["u"][-2:] for r in MARCReader(f.read_bytes())] == ["g1", "g3"]
