"""Run the query set through Koha's own search and through BookRS.

For each query: how many results Zebra returned, and what BookRS returns
above threshold. The headline number is the rescue rate -- of the
queries the catalogue's own search could not answer, how many BookRS
answered. Relevance is NOT decided here: the judgement sheet it writes
is for a librarian to score, because a generated label would only be the
embedding grading its own output.
"""
import csv, json, re, sys, urllib.parse, urllib.request

OPAC = "http://localhost:8080/cgi-bin/koha/opac-search.pl"
API = "http://localhost:8000/search/semantic"
MIN = 0.55
rows, judge = [], []

def zebra(q):
    u = OPAC + "?" + urllib.parse.urlencode({"idx": "", "q": q})
    html = urllib.request.urlopen(u, timeout=60).read().decode("utf-8", "replace")
    if re.search(r"No results found", html): return 0
    m = re.search(r"returned ([\d,]+) result", html)
    return int(m.group(1).replace(",", "")) if m else -1

def bookrs(q):
    u = API + "?" + urllib.parse.urlencode({"q": q, "source_id": 1, "limit": 5, "min_score": MIN})
    return json.load(urllib.request.urlopen(u, timeout=60))["results"]

for line in open("data/ol/query-set.tsv", encoding="utf-8"):
    cls, q, seed = line.rstrip("\n").split("\t")
    z = zebra(q)
    r = bookrs(q)
    rows.append({"class": cls, "query": q, "seed": seed, "zebra": z,
                 "bookrs": len(r), "top_score": round(r[0]["score"], 4) if r else 0,
                 "top_title": r[0]["title"] if r else ""})
    if z == 0 and r:
        for w in r[:3]:
            judge.append({"query": q, "title": w["title"], "score": round(w["score"], 4),
                          "relevant (2=yes 1=partly 0=no)": ""})
    print(".", end="", flush=True)
print()

with open("data/ol/evaluation.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
with open("data/ol/judgement-sheet.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(judge[0])); w.writeheader(); w.writerows(judge)

n = len(rows)
zero = [r for r in rows if r["zebra"] == 0]
rescued = [r for r in zero if r["bookrs"]]
print(f"\nqueries: {n}")
print(f"Zebra found nothing: {len(zero)} ({100*len(zero)/n:.0f}%)")
print(f"  BookRS answered:   {len(rescued)} ({100*len(rescued)/len(zero):.0f}% rescue rate)" if zero else "")
for c in ("natural", "concept", "typo"):
    sub = [r for r in rows if r["class"] == c]; z = [r for r in sub if r["zebra"] == 0]
    res = [r for r in z if r["bookrs"]]
    print(f"  {c:8} zero {len(z):3}/{len(sub):3}   rescued {len(res):3}" + (f" ({100*len(res)/len(z):.0f}%)" if z else ""))
print(f"judgement sheet: {len(judge)} rows -> data/ol/judgement-sheet.csv")
