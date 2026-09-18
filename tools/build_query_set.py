"""Build an evaluation query set from the catalogue's own vocabulary.

Independent of the recommender by construction: queries are derived from
subject headings in works, never from anything BookRS returns. Four
classes, each a way real catalogue search fails:

  natural   a question in ordinary words
  concept   a topic phrase no single record's title carries
  typo      a plausible misspelling
  abbrev    an abbreviation or shorthand

Output TSV: class, query, seed_subject
"""
import os, random, re, sys
import psycopg

N = int(os.environ.get("QS_N", "150"))
rng = random.Random(int(os.environ.get("QS_SEED", "3")))

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    rows = conn.execute(
        "SELECT unnest(subjects) AS s FROM works "
        "WHERE source_id = 1 AND deleted_at IS NULL").fetchall()

counts = {}
for (s,) in rows:
    t = re.split(r"\s+(?:--|—)\s+", s)[0].strip()
    if 4 <= len(t) <= 40 and re.fullmatch(r"[A-Za-z][A-Za-z \-']+", t):
        counts[t] = counts.get(t, 0) + 1
# mid-frequency topics: common enough to be held, not so common as to be trivial
pool = [t for t, c in counts.items() if 8 <= c <= 400]
rng.shuffle(pool)
print(f"{len(counts)} distinct topics, {len(pool)} in the mid-frequency band", file=sys.stderr)

# Templates that read naturally whatever the heading's number or form.
# "what is {t}" was dropped: it produces "what is merchants" on plural
# headings, which no patron would type.
NAT = ["books about {t}", "i want to learn about {t}", "something to read on {t}",
       "an introduction to {t}", "where can i read about {t}"]
CON = ["{t} explained simply", "the history of {t}", "{t} in everyday life",
       "a beginner's guide to {t}"]

def typo(t):
    w = max(t.split(), key=len)
    if len(w) < 5: return None
    i = rng.randrange(1, len(w) - 2)
    return t.replace(w, w[:i] + w[i + 1] + w[i] + w[i + 2:], 1)

out, i = [], 0
while len(out) < N and i < len(pool):
    t = pool[i]; i += 1
    kind = len(out) % 3
    if kind == 0:   q = rng.choice(NAT).format(t=t.lower())
    elif kind == 1: q = rng.choice(CON).format(t=t.lower())
    else:           q = typo(t.lower())
    if not q: continue
    out.append((("natural", "concept", "typo")[kind], q, t))

for c, q, t in out:
    print(f"{c}\t{q}\t{t}")
print(f"wrote {len(out)} queries", file=sys.stderr)
