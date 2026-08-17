#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — garbage-collect the ONE shared store, from whichever slot is PRODUCTION.
#
# Sibling of harvest-active.sh and resolve-active.sh, and it runs LAST of the three.
# Same reason for the wrapper: a systemd ExecStart cannot read /etc/jobfitr/active-slot
# for itself, and under blue-green there is no fixed install directory.
#
# WHY THIS REPLACED `jobfitr-evict@.service`. Eviction used to run once PER SLOT, because
# each slot owned its own jobs.db. Consolidating to one shared store (see
# store._default_db_path) makes a second pass not merely wasteful but wrong — two
# processes running the same LRU cap and the same 14/60-day deletes over one file, at
# whatever interval their two timers happened to drift to. One store, one collector.
#
# THE ORDER IS THE WHOLE THING, and getting it wrong is a bug this project already
# shipped: eviction at 03:30 removed ~5,400 stale rows and the 04:07 harvest put them
# straight back, because intake has no age filter. That left 14% of the live pool older
# than the 60-day rule while every log read clean. Evict AFTER the harvest — the timer
# fires at 04:45 UTC (00:45 ET) for exactly that reason.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STATE=/etc/jobfitr/active-slot

# WAIT FOR THE HARVEST, rather than trusting the clock gap.
#
# The schedule leaves 38 nominal minutes between them (04:07 vs 04:45 UTC), but both timers
# carry a RandomizedDelaySec — 5 min on the harvest, 15 on this one — so the real worst-case
# margin is 23 minutes against a harvest that already grew from ~6 to ~10 minutes today when
# board discovery finished. That margin is fine and is exactly the kind of thing that stops
# being fine without anyone editing a file.
#
# Running the collector while the harvest is still writing would mean two processes applying
# opposite rules to one store: the harvest inserting rows the LRU cap and the age rules are
# concurrently deleting. This project already shipped the reverse ordering bug once — eviction
# at 03:30 removed ~5,400 rows and the 04:07 harvest put them straight back — so the ordering
# is load-bearing and now enforced rather than assumed.
waited=0
while systemctl is-active --quiet jobfitr-harvest.service; do
	if [ "$waited" -ge 3600 ]; then
		echo "evict: harvest still running after 60m — skipping tonight rather than racing it" >&2
		exit 0
	fi
	sleep 30
	waited=$((waited + 30))
done
[ "$waited" -gt 0 ] && echo "evict: waited ${waited}s for the harvest to finish"

slot=$(cat "$STATE" 2>/dev/null || echo blue)
bin="/opt/jobfitr/${slot}/jobfitr/.venv/bin/jobfitr-evict"

if [[ ! -x "$bin" ]]; then
	echo "evict: no jobfitr-evict in slot '${slot}' (${bin}) — is the slot built?" >&2
	exit 1
fi

# No --db argument: the binary reads JOBFITR_DB_DIR from jobfitr.env and derives
# `jobs-v{SCHEMA_VERSION}.db` itself, so a schema bump moves the collector to the new file
# without anyone editing a unit.
exec "$bin"
