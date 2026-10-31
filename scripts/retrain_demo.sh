#!/usr/bin/env bash
# Show what a refit changes, for one patron and for the catalogue.
#
# The question this answers is not "are the recommendations good" -- that
# needs a pilot library and real borrowing. It is narrower and it is
# answerable here: when a library's circulation grows, does the model
# incorporate it, and what visibly changes for someone who did not
# borrow anything themselves.
#
# Two effects are separated deliberately, because conflating them would
# attribute a catalogue-wide shift to one reader's activity:
#
#   * a patron's own vector moves when that patron borrows more
#   * the item factors move when ANYONE borrows more, because the
#     factorisation is over the whole matrix
#
# This script demonstrates the second, which is the one that argues for
# refitting on a schedule: a reader who has done nothing since Tuesday
# still gets different recommendations on Friday.
#
#   scripts/retrain_demo.sh                 # pauses between steps
#   scripts/retrain_demo.sh --auto          # runs straight through
#
# Environment:
#   DEMO_PATRON     patron_ref to follow (default: first one found)
#   DEMO_NEW        new patrons to add          (default 100)
#   DEMO_OFFSET     patron index to start at    (default: past existing)
#   DEMO_SEED       plan seed                   (default 99)

set -euo pipefail
cd "$(dirname "$0")/.."

AUTO=0
[[ "${1:-}" == "--auto" ]] && AUTO=1

NEW=${DEMO_NEW:-100}
SEED=${DEMO_SEED:-99}
PLAN=data/ol/retrain-plan.tsv

pause() {
  [[ $AUTO -eq 1 ]] && return 0
  printf '\n    ── %s ──\n    press enter to continue, ctrl-c to stop' "$1"
  read -r _ </dev/tty
  printf '\n\n'
}

rule() { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '─%.0s' {1..64})"; }

state() {
  docker compose run --rm -T test python -c "
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    w, = c.execute('SELECT count(*) FROM works WHERE source_id=1').fetchone()
    l, = c.execute('SELECT count(*) FROM loans WHERE source_id=1').fetchone()
    p, = c.execute('SELECT count(DISTINCT patron_ref) FROM loans WHERE source_id=1').fetchone()
    f, = c.execute('''SELECT count(*) FROM work_factors f
                      JOIN works w ON w.id=f.work_id WHERE w.source_id=1''').fetchone()
    t, = c.execute('SELECT max(trained_at) FROM work_factors').fetchone()
    print(f'  works {w:>7,}   loans {l:>7,}   patrons {p:>5,}   factorised {f:>6,}')
    print(f'  last refit: {t}')
"
}

follow() {
  # One patron's collaborative ranking. --patron pins it across runs.
  if [[ -n "${DEMO_PATRON:-}" ]]; then
    docker compose run --rm -T recommend python -m bookrs.recommend.folding_demo \
      --patron "$DEMO_PATRON" | sed -n '/patron/,$p'
  else
    docker compose run --rm -T recommend python -m bookrs.recommend.folding_demo \
      | sed -n '/patron/,/^$/p'
  fi
}

# ---------------------------------------------------------------- before

rule "1. The catalogue and the model, now"
state
pause "this is the starting state"

rule "2. One patron's collaborative recommendations, now"
follow | tee /tmp/retrain-before.txt
printf '\n  Saved. The same patron will be shown again after the refit.\n'
printf '  Note the patron_ref if it was chosen automatically -- pass it as\n'
printf '  DEMO_PATRON next time to follow the same reader.\n'
pause "before the library gains any new borrowing"

# ---------------------------------------------------------------- change

rule "3. The library gains $NEW new borrowers"

OFFSET=${DEMO_OFFSET:-$(docker compose run --rm -T test python -c "
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    n, = c.execute('SELECT count(DISTINCT patron_ref) FROM loans WHERE source_id=1').fetchone()
    print(n + 1)
" 2>/dev/null | tr -d '[:space:]')}

printf '  Planning loans for patrons %s..%s (seed %s).\n' \
  "$OFFSET" "$((OFFSET + NEW - 1))" "$SEED"
printf '  These are new readers. The patron being followed borrows nothing.\n\n'

mkdir -p "$(dirname "$PLAN")"
PLAN_PATRON_OFFSET=$OFFSET PLAN_PATRONS=$NEW PLAN_SEED=$SEED \
  docker compose run --rm -T \
    -e PLAN_PATRON_OFFSET=$OFFSET -e PLAN_PATRONS=$NEW -e PLAN_SEED=$SEED \
    -v "$PWD/tools:/tools:ro" \
    api python /tools/gen_loan_plan.py > "$PLAN"
printf '  plan: %s lines\n' "$(wc -l < "$PLAN")"
pause "the plan is written but nothing has been borrowed yet"

rule "4. Executing through Koha's own circulation"
printf '  Not written to Koha'"'"'s database directly: AddIssue, AddReturn and\n'
printf '  AddRenewal, so the loans obey Koha'"'"'s own circulation rules and\n'
printf '  the harvester reads them the way it would read real ones.\n\n'
docker cp "$PLAN" kohadev-koha-1:/kohadevbox/plan.tsv
docker cp tools/run_loan_plan.pl kohadev-koha-1:/kohadevbox/run_loan_plan.pl
docker exec kohadev-koha-1 chmod 644 /kohadevbox/plan.tsv /kohadevbox/run_loan_plan.pl
docker exec kohadev-koha-1 koha-shell kohadev -c \
  "perl /kohadevbox/run_loan_plan.pl /kohadevbox/plan.tsv"
pause "the loans exist in Koha; the platform has not seen them"

rule "5. Harvesting the new circulation"
docker compose run --rm -T ingestion \
  python -m bookrs.ingestion.circulation --source-id 1
pause "the platform has the loans; the model has not been refitted"

rule "6. The model before refitting"
state
printf '\n  The loan count has risen and the factor count has not.\n'
printf '  Until a refit runs, the new borrowing changes nothing.\n'
pause "this is the state a stale refit leaves a library in"

rule "7. Refit"
docker compose run --rm -T recommend python -m bookrs.recommend.cli -v 2>&1 | tail -3
pause "the factorisation now includes the new borrowing"

# ---------------------------------------------------------------- after

rule "8. The catalogue and the model, after"
state

rule "9. The same patron, after"
follow | tee /tmp/retrain-after.txt

rule "10. What changed for a reader who borrowed nothing"
if command -v diff >/dev/null; then
  diff --side-by-side --width=130 \
    /tmp/retrain-before.txt /tmp/retrain-after.txt || true
fi

cat <<'NOTE'

  ──────────────────────────────────────────────────────────────────
  What this shows

  The followed patron borrowed nothing between the two readings. Their
  recommendations changed because other people borrowed, which moved
  the item factors they are ranked against. That is the argument for
  refitting on a schedule rather than on a patron's own activity.

  What this does not show

  Whether either ranking is any good. The circulation here is
  generated -- patrons given subject interests drawn from the
  catalogue's own headings, borrowing executed through Koha's own
  circulation. It demonstrates that the retrain path works end to end.
  It supports no claim about recommendation quality, which needs a
  library's real borrowing history.
NOTE
