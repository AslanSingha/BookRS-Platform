"""The revised evaluation (bookrs/evaluation/revised.py).

The arithmetic on cases done by hand; then the whole run against a small
library in the test database with a structure planted in it (readers
who read one genre, texts that cluster by genre), where every measure
has a direction it must point in.
"""

import json
import os

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from bookrs.evaluation import revised  # noqa: E402
from bookrs.evaluation.revised import (  # noqa: E402
    beyond, genre_match, heldout, loo_rank, reader_genres, similar_books,
)


class TestHeldOut:
    def test_a_case_done_by_hand(self):
        # 12 books; held out: 3 and 7. Ranked: 5, 7, 0, 1, 2, 4, 6, 8, 9, 10 | 3, 11.
        top = np.array([[5, 7, 0, 1, 2, 4, 6, 8, 9, 10, 3, 11]])
        rel = np.zeros((1, 12), dtype=bool)
        rel[0, [3, 7]] = True
        s = {k: float(v[0]) for k, v in heldout(top, rel).items()}
        assert s["p10"] == pytest.approx(0.1)
        assert s["ceiling"] == pytest.approx(0.2)        # two books: at best 2 of 10
        assert s["r10"] == pytest.approx(0.5)
        assert s["trunc_r10"] == pytest.approx(0.5)      # 1 of min(10, 2)
        assert s["map10"] == pytest.approx((1 / 2) / 2)  # P@2 = 1/2 at the one hit, over 2
        assert s["mrr"] == pytest.approx(0.5)
        assert s["hr10"] == 1.0
        ideal = 1 / np.log2(2) + 1 / np.log2(3)
        assert s["ndcg10"] == pytest.approx((1 / np.log2(3)) / ideal)

    def test_a_perfect_list_reaches_the_ceiling_and_one_on_the_rest(self):
        top = np.array([[2, 4, 0, 1, 3, 5, 6, 7, 8, 9, 10, 11]])
        rel = np.zeros((1, 12), dtype=bool)
        rel[0, [2, 4]] = True
        s = {k: float(v[0]) for k, v in heldout(top, rel).items()}
        assert s["p10"] == pytest.approx(s["ceiling"]) == pytest.approx(0.2)
        for k in ("trunc_r10", "map10", "mrr", "ndcg10", "hr10", "r10"):
            assert s[k] == pytest.approx(1.0), k

    def test_readers_without_a_relevant_book_are_left_out(self):
        top = np.tile(np.arange(12), (2, 1))
        rel = np.zeros((2, 12), dtype=bool)
        rel[1, 0] = True
        assert len(heldout(top, rel)["p10"]) == 1


class TestLeaveOneOut:
    def test_rank_counts_only_candidates(self):
        scores = np.array([[5.0, 4.0, 3.0, -np.inf, 2.0]])   # book 3 rated in training
        exclude = np.array([[True, False, False, False, False]])   # 0: another held-out book
        # target 2: only book 1 (4.0) is a candidate above it
        assert loo_rank(scores, np.array([2]), exclude).tolist() == [1]

    def test_no_target(self):
        assert loo_rank(np.zeros((1, 3)), np.array([-1]), np.zeros((1, 3), bool)).tolist() == [-1]


class TestGenres:
    def test_the_readers_own_genres(self):
        genre = np.array([0, 0, 0, 1, 1, 2, -1])
        own = reader_genres([np.array([0, 1, 2, 3]), np.array([5, 6])], genre, 3)
        # 0 is 75% and 1 is 25% of the first reader's books: both theirs
        assert own[0].tolist() == [0, 1]
        assert own[1].tolist() == [2]

    def test_match_against_random(self):
        genre = np.array([0, 0, 1, 1])
        share = np.array([0.5, 0.5])
        match, random, n = genre_match(np.array([[0, 1, 2, 3]]), [np.array([0])], genre, share)
        assert (match, random, n) == (0.5, 0.5, 1)


class TestBeyond:
    def test_coverage_head_and_genres(self):
        popularity = np.arange(20, 0, -1, dtype=np.float32)     # book 0 most rated
        genre = np.repeat(np.arange(4), 5)
        top10 = np.array([np.arange(10), np.arange(10)])        # the same 10 twice
        b = beyond(top10, popularity, 100, genre, None)
        assert b["coverage"] == pytest.approx(10 / 20)
        assert b["outside_head"] == pytest.approx(8 / 10)       # head: books 0 and 1
        assert b["genres_per_list"] == pytest.approx(2)
        assert "diversity" not in b

    def test_identical_texts_have_no_diversity(self):
        unit = np.tile([[1.0, 0.0]], (12, 1)).astype(np.float32)
        b = beyond(np.array([np.arange(10)]), np.ones(12, np.float32), 10,
                   np.zeros(12, int), unit)
        assert b["diversity"] == pytest.approx(0.0, abs=1e-6)


class TestSimilarBooks:
    def test_texts_that_cluster_by_genre_agree(self):
        rng = np.random.default_rng(0)
        genre = np.repeat(np.arange(4), 25)
        centres = rng.normal(size=(4, 16))
        vectors = centres[genre] + 0.1 * rng.normal(size=(100, 16))
        unit = (vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).astype(np.float32)
        noise = rng.normal(size=(100, 8)).astype(np.float32)
        noise /= np.linalg.norm(noise, axis=1, keepdims=True)
        s = similar_books(np.arange(0, 100, 5), unit, noise, genre)
        assert s["meaning"]["agreement10"] > 0.95
        assert s["meaning"]["random"] == pytest.approx(0.25)
        assert s["ALS"]["agreement10"] < 0.6               # random factors know no genre


# -- the whole run, on a library with a structure planted in it --------------

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def library():
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("implicit")
    if not DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    rng = np.random.default_rng(3)
    genres, per_genre, n_readers = 4, 30, 400
    with conn:
        conn.execute("TRUNCATE sources RESTART IDENTITY CASCADE")
        sid = conn.execute("INSERT INTO sources (name, base_url) VALUES ('k', 'mysql://k') "
                           "RETURNING id").fetchone()[0]
        centres = rng.normal(size=(genres, 16))
        works = []
        for g in range(genres):
            for b in range(per_genre):
                wid = conn.execute(
                    "INSERT INTO works (source_id, source_record_id, title, content_hash, "
                    "items_hash) VALUES (%s, %s, %s, 'c', 'i') RETURNING id",
                    (sid, f"{g}-{b}", f"book {g}-{b}")).fetchone()[0]
                conn.execute("INSERT INTO items (work_id, barcode, collection) "
                             "VALUES (%s, %s, %s)", (wid, f"B{wid}", f"G{g}"))
                v = centres[g] + 0.2 * rng.normal(size=16)
                v = (v / np.linalg.norm(v)).astype(np.float32)
                conn.execute("INSERT INTO embeddings (work_id, vector, dimensions, model, "
                             "embedder_version, source_hash) VALUES (%s, %s, 16, 'm', 1, 'h')",
                             (wid, v.tolist()))
                works.append((wid, g))
        by_genre = {g: [w for w, gg in works if gg == g] for g in range(genres)}
        with conn.cursor() as cur:
            with cur.copy("COPY ratings (source_id, patron_ref, work_id, rating, reviewed) "
                          "FROM STDIN") as cp:
                for reader in range(n_readers):
                    g = reader % genres
                    for w in rng.choice(by_genre[g], size=14, replace=False):
                        cp.write_row((sid, f"p{reader:04d}", int(w),
                                      int(rng.choice([3, 4, 5])), False))
        conn.commit()
    yield sid
    conn.close()


def test_the_whole_run(library, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    out_json = tmp_path / "revised.json"
    assert revised.main(["--source-id", str(library), "--readers", "0", "--factors", "16",
                         "--similar-books", "40", "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    for section in ("1. Held-out books, liked", "2. Held-out books, any rating",
                    "3. Leave one out", "4. Genre match", "5. Beyond accuracy",
                    "6. Similar books"):
        assert section in text, section

    result = json.loads(out_json.read_text())
    m = {x["name"]: x for x in result["methods"]}
    als, pop, content = m["ALS"], m["most-rated books"], m["content (SBERT profile)"]
    hybrid = m["hybrid, alpha 0.1"]
    for x in m.values():
        h = x["heldout_liked"]
        assert 0 < h["ceiling"] <= 1 and h["p10"] <= h["ceiling"] + 1e-9
        assert 0 <= h["of_ceiling"] <= 1 + 1e-9
    # Readers read one genre: ALS and content must find it, popularity cannot.
    assert als["heldout_liked"]["p10"] > 2 * pop["heldout_liked"]["p10"]
    assert als["loo"]["hr10"] > pop["loo"]["hr10"]
    assert als["genre"]["match"] > 0.9 > 2 * als["genre"]["random"]
    assert content["genre"]["match"] > 0.9
    assert hybrid["heldout_liked"]["readers"] == als["heldout_liked"]["readers"]
    # The most-rated list is the same for everyone: it covers least.
    assert pop["beyond"]["coverage"] < als["beyond"]["coverage"]
    s = result["similar"]
    assert s["meaning"]["agreement10"] > 0.9 > 2 * s["meaning"]["random"]
