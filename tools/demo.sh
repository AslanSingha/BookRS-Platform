#!/usr/bin/env bash
# The demo's commands: is everything ready, find a book, what a patron
# rated, what they are shown, read and retrain now. docs/demo.md is the
# run sheet that uses them.
#
#   tools/demo.sh check                      everything the demo needs, and the numbers
#   tools/demo.sh find WORDS...              books whose title holds WORDS, most rated first
#   tools/demo.sh patron USERID              the patron in Koha, and their latest ratings
#   tools/demo.sh list USERID [PASSWORD]     their homepage list, and what changed since the last
#   tools/demo.sh search USERID WORDS...     their search by meaning, and whether taste reordered it
#   tools/demo.sh read                       read Koha now
#   tools/demo.sh refit                      retrain now, then wait until the API has the new model
#
# Run from anywhere; it works in the project folder. Reads Koha's
# database read-only. Lists and searches go through Koha's own route,
# logged in as the patron, so they are exactly what the OPAC shows.
#
# Settings: DEMO_PASSWORD (the demo patrons' password, default
# Bookrs-Demo-2026), KOHA_OPAC (http://localhost:8080), BOOKRS_API
# (http://localhost:8000); KOHA_DB_URL and BOOKRS_SOURCE_ID from .env.

set -euo pipefail
cd "$(dirname "$0")/.."

KOHA=${KOHA_OPAC:-http://localhost:8080}
API=${BOOKRS_API:-http://localhost:8000}
DEMO_PASSWORD=${DEMO_PASSWORD:-Bookrs-Demo-2026}
STATE=${DEMO_STATE:-data/demo}
PROBE=${BOOKRS_CACHE_PROBE_SECONDS:-30}

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

env_value() {   # KEY: its value in .env, without quotes; empty if absent
    { grep -m1 "^$1=" .env 2>/dev/null || true; } | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']$//"
}

KOHA_DB_URL=${KOHA_DB_URL:-$(env_value KOHA_DB_URL)}
KOHA_DB_URL=${KOHA_DB_URL:-mysql://root:password@kohadev-db-1:3306/koha_kohadev}
if [[ $KOHA_DB_URL =~ ^mysql://([^:]+):([^@]*)@([^:/]+)(:[0-9]+)?/([^?]+) ]]; then
    KUSER=${BASH_REMATCH[1]} KPASS=${BASH_REMATCH[2]}
    KHOST=${BASH_REMATCH[3]} KDB=${BASH_REMATCH[5]}
else
    echo "KOHA_DB_URL is not mysql://user:password@container:port/database" >&2
    exit 2
fi

koha_sql() {    # one read-only query against Koha's database; tab-separated rows
    docker exec -e MYSQL_PWD="$KPASS" "$KHOST" mariadb -u"$KUSER" -N -B "$KDB" -e "$1"
}

sql_str() {     # a value as an SQL string literal
    local s=${1//\\/\\\\}
    s=${s//\'/\'\'}
    printf "'%s'" "$s"
}

show() {        # a line of `check`
    printf '  %-36s %s\n' "$1" "$2"
}

# -- check -----------------------------------------------------------------

cmd_check() {
    local failed=0 code counts health
    echo "Koha"
    code=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$KOHA/cgi-bin/koha/opac-main.pl" || true)
    if [ "$code" = 200 ]; then show "OPAC" "answers"
    else show "OPAC" "FAIL: HTTP $code (run the restart recipe)"; failed=1; fi

    code=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$KOHA/api/v1/contrib/bookrs/recommendations" || true)
    if [ "$code" = 401 ]; then show "BookRS plugin" "installed (asks a logged-out visitor to log in)"
    else show "BookRS plugin" "FAIL: HTTP $code, expected 401 (step 2 of the restart recipe)"; failed=1; fi

    if counts=$(koha_sql "SELECT (SELECT count(*) FROM biblio),
                                 (SELECT count(*) FROM borrowers),
                                 (SELECT count(*) FROM ratings)" 2>/dev/null); then
        read -r biblios borrowers ratings <<<"$counts"
        show "records" "$(printf "%'d" "$biblios")"
        show "patrons" "$(printf "%'d" "$borrowers")"
        show "ratings" "$(printf "%'d" "$ratings")"
    else
        show "database" "FAIL: cannot query $KHOST"; failed=1
    fi

    echo "Platform"
    local source sources
    source=$(env_value BOOKRS_SOURCE_ID)
    if ! health=$(curl -s --max-time 15 "$API/health") || [ -z "$health" ] ||
       ! sources=$(curl -s --max-time 60 "$API/sources") || [ -z "$sources" ]; then
        show "API" "FAIL: not answering at $API (docker compose up -d)"; failed=1
    elif ! python3 - "$health" "$sources" "${source:-54}" <<'PY'
import json, sys

health, sources, source = json.loads(sys.argv[1]), json.loads(sys.argv[2]), int(sys.argv[3])

def row(key, value):
    print(f"  {key:<36} {value}")

row("API", "answers")
mine = next((s for s in sources["sources"] if s["id"] == source), None)
if mine is None:
    row(f"library (source {source})", "FAIL: not in the platform; check BOOKRS_SOURCE_ID")
    sys.exit(1)
row(f"books read from Koha (source {source})", f"{mine['works']:,}")
row("  embedded", f"{mine['embedded']:,} ({mine['works'] - mine['embedded']:,} waiting)")
row("  with ALS factors", f"{mine['factorised']:,}, last refit {health['last_factorised'] or 'never'}")
held, stored = health.get("vectors_held"), health["embeddings"]
if held is None:
    row("vectors in the API", "not loaded yet: wait 30 s after a restart")
else:
    row("vectors in the API", f"{held:,} of {stored:,} embedded, all sources"
        + ("" if held == stored else ", catching up"))
failed = False
for j in health.get("jobs", []):
    failed |= not j.get("ok")
    row(f"job {j['job']}", ("ok" if j.get("ok") else "FAILED") + f", next {j.get('next_due') or '?'}")
sys.exit(1 if failed else 0)
PY
    then
        failed=1
    fi

    local running
    running=$(docker compose --profile schedule ps --status running --services 2>/dev/null | grep -c '^schedule-' || true)
    if [ "$running" = 3 ]; then show "scheduler" "running (read, embed, refit)"
    else show "scheduler" "FAIL: $running of 3 running (docker compose --profile schedule up -d)"; failed=1; fi
    local r e f
    r=$(env_value BOOKRS_READ_EVERY) e=$(env_value BOOKRS_EMBED_EVERY) f=$(env_value BOOKRS_REFIT_EVERY)
    show "cadences (.env)" "read ${r:-1h}, embed ${e:-1h}, refit ${f:-1d}"

    echo
    if [ "$failed" = 0 ]; then echo "Ready."; else echo "Not ready: see FAIL above."; fi
    return "$failed"
}

# -- find, patron ----------------------------------------------------------

cmd_find() {
    [ $# -gt 0 ] || usage
    local words="$*"
    # The matches first, then their ratings: counting for every match
    # of a common word would take minutes at 883,000 records.
    koha_sql "SELECT b.biblionumber,
                     (SELECT count(*) FROM ratings r WHERE r.biblionumber = b.biblionumber) AS n,
                     b.title, coalesce(b.author, '')
              FROM (SELECT biblionumber, title, author FROM biblio
                    WHERE title LIKE $(sql_str "%$words%") LIMIT 500) b
              ORDER BY n DESC, b.biblionumber LIMIT 10" |
    while IFS=$'\t' read -r bn n title author; do
        printf '%7s ratings  %s%s\n' "$(printf "%'d" "$n")" "$title" "${author:+ / $author}"
        printf '                 %s/cgi-bin/koha/opac-detail.pl?biblionumber=%s\n' "$KOHA" "$bn"
    done
}

cmd_patron() {
    [ $# -eq 1 ] || usage
    local row bn
    row=$(koha_sql "SELECT borrowernumber, cardnumber, concat_ws(' ', firstname, surname), categorycode,
                           (SELECT count(*) FROM ratings r WHERE r.borrowernumber = b.borrowernumber)
                    FROM borrowers b WHERE userid = $(sql_str "$1")")
    if [ -z "$row" ]; then
        echo "No patron with the username $1 in Koha. Add them: staff interface, Patrons, New patron."
        return 1
    fi
    IFS=$'\t' read -r bn card name category n <<<"$row"
    echo "$name ($1): borrower $bn, card $card, category $category"
    echo "  ratings in Koha: $n"
    koha_sql "SELECT r.timestamp, r.rating_value, bi.title FROM ratings r
              JOIN biblio bi ON bi.biblionumber = r.biblionumber
              WHERE r.borrowernumber = $bn ORDER BY r.timestamp DESC LIMIT 20" |
    while IFS=$'\t' read -r when stars title; do
        printf '  %s  %s  %s\n' "$when" "$(printf '%*s' "$stars" '' | tr ' ' '*')$(printf '%*s' $((5 - stars)) '')" "$title"
    done
}

# -- list, search ----------------------------------------------------------

koha_get() {    # USER PASSWORD PATH [curl args...]: Koha's route, logged in as the patron
    local user=$1 pass=$2 path=$3 out code
    shift 3
    out=$(curl -s --max-time 30 -w '\n%{http_code}' -u "$user:$pass" "$@" "$KOHA$path") || {
        echo "Koha did not answer at $KOHA" >&2; return 1; }
    code=${out##*$'\n'}
    out=${out%$'\n'*}
    case $code in
        200) printf '%s' "$out" ;;
        401) echo "Koha refused the login $user (wrong password? set DEMO_PASSWORD or pass it)" >&2; return 1 ;;
        *)   echo "Koha answered HTTP $code: $out" >&2; return 1 ;;
    esac
}

cmd_list() {
    if [ $# -lt 1 ] || [ $# -gt 2 ]; then usage; fi
    local user=$1 pass=${2:-$DEMO_PASSWORD} body now prev
    body=$(koha_get "$user" "$pass" "/api/v1/contrib/bookrs/recommendations?limit=10")
    mkdir -p "$STATE"
    # shellcheck disable=SC2012  # names are USERID-YYYYmmdd-HHMMSS.json
    prev=$(ls -1 "$STATE/$user"-*.json 2>/dev/null | tail -1 || true)
    now="$STATE/$user-$(date +%Y%m%d-%H%M%S).json"
    printf '%s' "$body" >"$now"
    python3 - "$user" "$now" "$prev" <<'PY'
import json, os, sys

user, now_path, prev_path = sys.argv[1:4]
d = json.load(open(now_path))
heading = "Popular in this library" if d["method"] == "none" else "Recommended for you"
print(f"{user}: {heading}")
print(f"  the platform holds {d['ratings_used']} of their ratings: {d['explanation']}")

before = None
if prev_path and prev_path != now_path:
    p = json.load(open(prev_path))
    before = {r["biblionumber"]: r["title"] for r in p["results"]}
    stamp = os.path.basename(prev_path)[len(user) + 1:-5]
    print(f"  + new since the list of {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}")

def authors(r):
    a = r.get("authors") or []
    return ", ".join(a) if isinstance(a, list) else str(a)

for i, r in enumerate(d["results"], 1):
    mark = "+" if before is not None and r["biblionumber"] not in before else " "
    by = authors(r)
    print(f"  {i:2}. {mark} {r['title']}" + (f" / {by}" if by else ""))
if before is not None:
    shown = {r["biblionumber"] for r in d["results"]}
    gone = [t for b, t in before.items() if b not in shown]
    print("  left the list: " + ("; ".join(gone) if gone else "none"))
PY
}

cmd_search() {
    [ $# -ge 2 ] || usage
    local user=$1 body
    shift
    body=$(koha_get "$user" "$DEMO_PASSWORD" "/api/v1/contrib/bookrs/search" \
        -G --data-urlencode "q=$*" --data limit=10)
    python3 - "$body" <<'PY'
import json, sys

d = json.loads(sys.argv[1])
scores = [r["score"] for r in d["results"] if "score" in r]
how = ("reordered by meaning (0.7) and their ratings (0.3)" if d["reordered"]
       else "in plain meaning order (no ratings yet, or no close match)")
best = f"; best match {max(scores):.3f}" if scores else ""
print(f'"{d["query"]}": {d["count"]} books, {how}{best}')
for i, r in enumerate(d["results"], 1):
    print(f"  {i:2}. {r['title']}")
PY
}

# -- read, refit -----------------------------------------------------------

run_job() {
    local job=$1
    if ! docker compose --profile schedule ps --status running --services 2>/dev/null | grep -qx "schedule-$job"; then
        echo "The scheduler is not running: docker compose --profile schedule up -d" >&2
        return 1
    fi
    docker compose exec -T "schedule-$job" python -m bookrs.schedule "$job" --now
}

cmd_read() {
    run_job read
}

cmd_refit() {
    run_job refit
    # The API asks whether a refit happened at most every PROBE seconds;
    # past that, the next list is made from the new model.
    echo "Waiting $((PROBE + 1)) s for the API to take up the new model ..."
    sleep $((PROBE + 1))
    echo "Done: the next list comes from the new model."
}

[ $# -gt 0 ] || usage
command=$1
shift
case $command in
    check)  cmd_check "$@" ;;
    find)   cmd_find "$@" ;;
    patron) cmd_patron "$@" ;;
    list)   cmd_list "$@" ;;
    search) cmd_search "$@" ;;
    read)   cmd_read "$@" ;;
    refit)  cmd_refit "$@" ;;
    *)      usage ;;
esac
