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
