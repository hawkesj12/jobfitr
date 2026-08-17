#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — check a slot is actually fit to serve, BEFORE flipping to it.
#
#   sudo bash verify-slot.sh green
#
# Exists because "the service started" is a much weaker claim than "this slot will
# serve users well", and the gap between them is where today's bugs lived: a slot can
# be up and healthy while serving a pool frozen days ago, or while every Workday job
# in it has no description and therefore cannot rank or be read.
#
# Exit code is the verdict — non-zero means do not flip.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

slot="${1:-}"
[[ -z "$slot" ]] && { echo "usage: verify-slot.sh <blue|green>" >&2; exit 2; }
port=$([[ "$slot" == blue ]] && echo 8000 || echo 8001)
base="http://127.0.0.1:${port}"
fail=0

say() { printf '%-46s %s\n' "$1" "$2"; }
bad() { say "$1" "✗ $2"; fail=1; }
ok()  { say "$1" "✓ $2"; }

echo "── verifying slot '${slot}' on :${port} ─────────────────────"

# 1. the service answers at all
health=$(curl -s --max-time 15 "${base}/api/health" 2>/dev/null)
if [[ -z "$health" ]]; then
	bad "service responds" "no answer from ${base}"
	echo; echo "VERDICT: DO NOT FLIP"; exit 1
fi
ok "service responds" "200"

jqv() { printf '%s' "$health" | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1'))" 2>/dev/null; }

pool=$(jqv pool_size)
snapcount=$(jqv snapshot_count)
# THE SERVABLE count, not the raw one — see the ratio test below.
snapservable=$(jqv snapshot_servable)
imported=$(jqv snapshot_imported_at)
adz=$(jqv adzuna_ok)

# 2b. THE RELEASE'S OWN COLUMNS ARE ACTUALLY FILLED.
#
# This gate had no idea what a v2 store is supposed to contain. A slot rebuilt from a
# snapshot written by an OLDER job-radar comes up healthy in every other check here —
# service responds, pool the right size, snapshot fresh — with title_root, category and
# seniority at 0%. The board renders, every filter drawer is empty, and the title scorer
# runs on one surface instead of two. Nothing errors, so nothing here noticed.
#
# title_root is the tell: a 0.7.0 harvest fills it on 100% of rows, so anything near zero
# means the snapshot predates the engine. `rebuild_store.py` now refuses that rebuild
# outright — this is the second line of defence for a store that got there another way.
db="/opt/jobfitr/${slot}/data/jobs.db"
fillpy='import sqlite3,sys
c=sqlite3.connect("file:%s?mode=ro"%sys.argv[1],uri=True)
n=c.execute("SELECT count(*) FROM jobs").fetchone()[0]
if not n: print("STALE|no rows"); raise SystemExit
def f(col): return c.execute("SELECT count(*) FROM jobs WHERE \"%s\" IS NOT NULL AND \"%s\" <> \x27\x27"%(col,col)).fetchone()[0]
r=f("title_root")
print(("OK" if r*2>n else "STALE")+"|"+" · ".join("%s %.0f%%"%(k,100*f(k)/n) for k in ("title_root","category","seniority")))'
if [[ -r "$db" ]]; then
	fillrep=$(python3 -c "$fillpy" "$db" 2>/dev/null)
	case "${fillrep%%|*}" in
	OK) ok "new-schema columns" "${fillrep#*|}" ;;
	STALE) bad "new-schema columns" "${fillrep#*|} — rebuilt from a PRE-0.7.0 snapshot; re-harvest then rerun rebuild_store.py" ;;
	*) bad "new-schema columns" "could not read ${db}" ;;
	esac
else
	say "new-schema columns" "– skipped (${db} not readable from here)"
fi

# 2. the pool is not just NON-EMPTY but the right SIZE. A fixed >1000 floor let a
#    harvest that silently dropped 90% of the depth lane pass (the pool is ~34k). Gate
#    on the ratio to what the slot should be serving: the pool is the snapshot plus
#    live-fetch accumulation, so it should meet or exceed the snapshot; below 70% of
#    it means the slot under-ingested. Keep a small absolute floor for a cold snapshot.
#
#    COMPARE AGAINST snapshot_SERVABLE, NOT snapshot_count. `count` is every harvested
#    row; the pool can only ever hold the rows that survive US-only intake. Those were
#    the same number while nothing was filtered, and stopped being the same the moment
#    the intake filter started dropping ~18% — at which point this gate reported
#    DO NOT FLIP for a perfectly healthy slot, because on a freshly rebuilt one (which
#    is exactly when the gate runs) the ratio lands near 0.67 against a 0.70 floor.
#    A gate that fails for its own reasons is worse than no gate: it trains you to
#    override it. Falls back to snapshot_count for a snapshot written before the field.
if ! [[ "$pool" =~ ^[0-9]+$ ]] || [[ "$pool" -lt 500 ]]; then
	bad "pool size" "only ${pool} jobs — the slot has not ingested a snapshot"
else
	basis="$snapservable"
	label="servable"
	if ! [[ "$basis" =~ ^[0-9]+$ ]] || [[ "$basis" -eq 0 ]]; then
		basis="$snapcount"
		label="total (pre-filter — old snapshot)"
	fi
	if [[ "$basis" =~ ^[0-9]+$ ]] && [[ "$basis" -gt 0 ]] &&
		[[ $((pool * 10)) -lt $((basis * 7)) ]]; then
		bad "pool size" "${pool} jobs is <70% of the ${basis} ${label} — under-ingested"
	else
		ok "pool size" "$(printf "%'d" "$pool") jobs (${basis} ${label}, ${snapcount} harvested)"
	fi
fi

# 3. THE ONE THAT BIT US: is the slot serving a CURRENT snapshot, or a frozen one?
snap_mtime=$(stat -c %Y /opt/jobfitr/data/jobs.json 2>/dev/null || echo 0)
if [[ "$imported" == "None" || -z "$imported" ]]; then
	bad "snapshot ingested" "never — this slot will serve a stale pool forever"
else
	imported_epoch=$(date -d "$imported" +%s 2>/dev/null || echo 0)
	age=$(( (snap_mtime - imported_epoch) / 3600 ))
	if [[ "$age" -le 24 ]]; then
		ok "snapshot ingested" "$imported (current)"
	else
		bad "snapshot ingested" "$imported — ~${age}h behind the latest harvest"
	fi
fi

# 4. keys present (a slot with no Adzuna key silently loses the live-fetch lane)
[[ "$adz" == "True" ]] && ok "adzuna configured" "yes" || bad "adzuna configured" "no key"

# 5. real searches return real, diverse results. Test several titles, INCLUDING the
#    ones that expose a broken diversity cap: "nurse" and "driver" are dominated by a
#    few huge employers (Veterans Health Administration, McLane), so a cap that only
#    reorders instead of filtering fails here while "engineer" passes. That exact gap
#    shipped once — the gate must cover it.
# The search log's path comes from the service's own env file, so this checks the
# CONFIGURED location rather than a guess. Unset is a legitimate config (logging off).
searchlog=$(sed -n 's/^[[:space:]]*JOBFITR_SEARCH_LOG=//p' /etc/jobfitr/jobfitr.env 2>/dev/null \
	| tr -d "\"'" | tail -1)
searchlog_before=0
if [[ -n "$searchlog" && -f "$searchlog" ]]; then
	searchlog_before=$(wc -l < "$searchlog")
fi

for title in engineer nurse driver; do
	# "probe":true keeps these three out of the quality digest — see searchlog.record.
	res=$(curl -s --max-time 45 -X POST "${base}/api/score" \
		-H 'Content-Type: application/json' \
		-d "{\"titles\":[\"${title}\"],\"location\":\"\",\"min_score\":\"plenty\",\"probe\":true}" 2>/dev/null)
	if [[ -z "$res" ]]; then
		bad "search: ${title}" "/api/score returned nothing"
		continue
	fi
	# The response goes via a FILE, not argv. It used to be passed as a command-line
	# argument, which worked while a board was ~50 rows and broke the moment the delivery
	# cap went to 200 with descriptions attached: execve() has a hard ARG_MAX, so the gate
	# started dying with "Argument list too long" and reporting DO NOT FLIP for a slot that
	# was fine. A gate that fails for its own reasons is worse than no gate — it trains you
	# to override it.
	tmp=$(mktemp /tmp/verify-slot.XXXXXX.json)
	printf '%s' "$res" > "$tmp"
	TITLE="$title" JSON_FILE="$tmp" python3 - <<'PY'
import json, os, sys
from collections import Counter
t = os.environ["TITLE"]
with open(os.environ["JSON_FILE"]) as fh:
    d = json.load(fh)
jobs = d.get("jobs", [])
n = len(jobs)
comp = Counter(j.get("company") for j in jobs)
top = comp.most_common(1)[0][1] if comp else 0
# Readability is judged on the TOP OF THE BOARD, not the whole delivered set. The 0.7
# threshold was calibrated when a board was 50 rows, where it measured exactly what a
# user reads. Step 1.3 raised the delivery cap to 200, and the extra 150 are a tail that
# was previously never sent at all — measured on the live 'engineer' board, ranks 1-75
# are 96% readable while ranks 76-125 are 28%, so the whole-set average failed a board
# whose visible part is fine. Judging the head is a HIGHER bar for what matters, not a
# lowered one.
HEAD = 50
head = jobs[:HEAD]
withdesc = sum(1 for j in head if (j.get("description") or "").strip())
tail_desc = sum(1 for j in jobs if (j.get("description") or "").strip())

ok_n = n > 0
ok_desc = not head or withdesc >= len(head) * 0.8

# Every card must be able to show its own arithmetic: `parts` has to sum to `points`.
# This replaced a "no employer holds more than 6 slots" assertion, which encoded the
# employer cap that step 1.3 deliberately removed — concentration is now something a
# user SEES and filters, not something the server silently corrects, so the old check
# gated on a promise the system had stopped making.
bad_math = [j for j in jobs
            if not isinstance(j.get("points"), int)
            or sum(delta for _, delta in (j.get("parts") or [])) != j["points"]]
ok_math = not bad_math

# Not a cap — a smoke alarm. One employer owning the ENTIRE board means retrieval or
# dedup broke, which is a different thing from an employer legitimately dominating a niche.
ok_sane = n == 0 or top < n

good = ok_n and ok_desc and ok_math and ok_sane
mark = "✓" if good else "✗"
print(f"{'search: '+t:<40} {mark} {n} results · {len(comp)} companies · "
      f"max {top}/one · {withdesc}/{len(head)} readable in the head, {tail_desc}/{n} overall"
      + ("" if ok_math else f" · {len(bad_math)} DO NOT RECONCILE"))
sys.exit(0 if good else 1)
PY
	rc=$?
	rm -f "$tmp"
	[[ $rc -ne 0 ]] && fail=1
done

# 6. the search log actually received those three searches.
#
# This check exists because EVERY way the log can be broken is silent. record() swallows
# all exceptions on purpose (the observer must never take down the thing it observes), so
# a missing ReadWritePaths, a wrong owner, or a full disk all present identically: a file
# that simply stays empty, discovered weeks later when you sit down to review a month of
# searches and there are none. The three searches above just ran; if the log is
# configured and did not grow, it is broken NOW, while there is still a gate to fail.
if [[ -z "$searchlog" ]]; then
	ok "search log" "not configured (JOBFITR_SEARCH_LOG unset)"
else
	searchlog_after=0
	[[ -f "$searchlog" ]] && searchlog_after=$(wc -l < "$searchlog")
	grew=$((searchlog_after - searchlog_before))
	if [[ "$grew" -ge 3 ]]; then
		ok "search log" "+${grew} lines at ${searchlog}"
	else
		bad "search log" "configured at ${searchlog} but grew ${grew}/3 — check ReadWritePaths in jobfitr-web@.service, then owner"
	fi
fi

echo
if [[ "$fail" -eq 0 ]]; then
	echo "VERDICT: OK to flip  →  sudo bash flip.sh"
	exit 0
fi
echo "VERDICT: DO NOT FLIP — fix the ✗ items first"
exit 1
