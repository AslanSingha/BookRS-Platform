"""Plan synthetic borrowing over a harvested catalogue.

Patrons get one to three interests drawn from the catalogue's own
subject headings and borrow mostly inside them; a global Zipf-ish
popularity skews everything the way real circulation is skewed.
Renewals, returns and repeat borrows are flagged so the confidence
weighting has terms to weigh. Output: TSV on stdout, one loan per line:
patron_idx  biblionumber  renew  return  repeat
Run inside the api container (it has psycopg and DATABASE_URL).
"""
import os, random, re, sys
from collections import defaultdict
import psycopg

SOURCE_ID = int(os.environ.get("PLAN_SOURCE_ID", "1"))
N_PATRONS = int(os.environ.get("PLAN_PATRONS", "400"))
SEED = int(os.environ.get("PLAN_SEED", "42"))
MIN_TOPIC = 15
GLOBAL_HEAD = int(os.environ.get("PLAN_GLOBAL_HEAD", "300"))  # the genuinely popular shelf
ACTIVE_TOPICS = int(os.environ.get("PLAN_TOPICS", "80"))   # the community reads in clusters
TOPIC_HEAD = int(os.environ.get("PLAN_HEAD", "12"))         # loans concentrate on a topic's head
rng = random.Random(SEED)

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    rows = conn.execute(
        "SELECT source_record_id, subjects FROM works "
        "WHERE source_id = %s AND deleted_at IS NULL", (SOURCE_ID,)).fetchall()

def bib(rid):  # "KOHA-OAI-TEST:42" -> "42"
    return rid.rpartition(":")[2]

def topic_of(s):
    s = re.split(r"\s+(?:--|—)\s+", s)[0]
    return re.sub(r"[^\w\s]", " ", s.lower()).strip()

works = [(bib(r[0]), r[1] or []) for r in rows if bib(r[0]).isdigit()]
by_topic = defaultdict(list)
for b, subs in works:
    for t in {topic_of(s) for s in subs if s}:
        if t: by_topic[t].append(b)
topics = [t for t, ws in by_topic.items() if len(ws) >= MIN_TOPIC]
rng.shuffle(topics); topics = topics[:ACTIVE_TOPICS]
for t in topics: rng.shuffle(by_topic[t]); by_topic[t] = by_topic[t][:TOPIC_HEAD]
topic_w = [len(by_topic[t]) ** 0.5 for t in topics]
print(f"{len(works)} works; {len(topics)} active topics x {TOPIC_HEAD} head works", file=sys.stderr)

# global popularity: a Zipf rank per work
order = list(range(len(works))); rng.shuffle(order)
all_bibs = [works[i][0] for i in order][:GLOBAL_HEAD]
pop_w = [1.0 / (r + 1) ** 0.5 for r in range(len(all_bibs))]

_w_cache = {}
def pick_from(bibs):
    # mild Zipf within the head: the first entries are the popular ones,
    # but the rest are reachable, so co-borrowing has breadth
    n = len(bibs)
    w = _w_cache.get(n) or _w_cache.setdefault(n, [1.0 / (r + 1) ** 0.6 for r in range(n)])
    return rng.choices(bibs, weights=w, k=1)[0]

n_loans = 0
for p in range(N_PATRONS):
    activity = min(int(rng.paretovariate(1.3)) + 6, 50)       # mean ~11, heavy tail, capped
    k = rng.choice((1, 1, 2, 2, 3))
    interests = rng.choices(topics, weights=topic_w, k=k) if topics else []
    seen = set()
    for _ in range(activity):
        if interests and rng.random() < 0.75:
            b = pick_from(by_topic[rng.choice(interests)])
        else:
            b = rng.choices(all_bibs, weights=pop_w, k=1)[0]
        if b in seen: continue
        seen.add(b)
        renew = int(rng.random() < 0.25)
        ret = int(rng.random() < 0.85)
        rep = int(ret and rng.random() < 0.08)
        print(f"{p}\t{b}\t{renew}\t{ret}\t{rep}")
        n_loans += 1
print(f"planned {n_loans} loans for {N_PATRONS} patrons", file=sys.stderr)
