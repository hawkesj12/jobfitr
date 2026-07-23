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
imported=$(jqv snapshot_imported_at)
adz=$(jqv adzuna_ok)

# 2. the pool is not empty
if [[ "$pool" =~ ^[0-9]+$ ]] && [[ "$pool" -gt 1000 ]]; then
	ok "pool size" "$(printf "%'d" "$pool") jobs"
else
	bad "pool size" "only ${pool} jobs — the slot has not ingested a snapshot"
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
for title in engineer nurse driver; do
	res=$(curl -s --max-time 45 -X POST "${base}/api/score" \
		-H 'Content-Type: application/json' \
		-d "{\"titles\":[\"${title}\"],\"location\":\"\",\"min_score\":\"plenty\"}" 2>/dev/null)
	if [[ -z "$res" ]]; then
		bad "search: ${title}" "/api/score returned nothing"
		continue
	fi
	TITLE="$title" python3 - "$res" <<'PY'
import json, os, sys
from collections import Counter
d = json.loads(sys.argv[1]); t = os.environ["TITLE"]
jobs = d.get("jobs", [])
n = len(jobs)
comp = Counter(j.get("company") for j in jobs)
top = comp.most_common(1)[0][1] if comp else 0
withdesc = sum(1 for j in jobs if (j.get("description") or "").strip())
ok_n = n > 0
ok_div = top <= 6
ok_desc = withdesc >= n * 0.7
mark = "✓" if (ok_n and ok_div and ok_desc) else "✗"
print(f"{'search: '+t:<40} {mark} {n} results · {len(comp)} companies · "
      f"max {top}/one · {withdesc}/{n} readable")
sys.exit(0 if (ok_n and ok_div and ok_desc) else 1)
PY
	[[ $? -ne 0 ]] && fail=1
done

echo
if [[ "$fail" -eq 0 ]]; then
	echo "VERDICT: OK to flip  →  sudo bash flip.sh"
	exit 0
fi
echo "VERDICT: DO NOT FLIP — fix the ✗ items first"
exit 1
