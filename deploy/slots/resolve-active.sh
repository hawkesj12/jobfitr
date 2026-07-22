#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — resolve store companies to ATS boards, from whichever slot is PRODUCTION.
#
# Sibling of harvest-active.sh, and it runs BEFORE it: resolution decides which
# companies the harvest will poll, so the order matters. Same reason for the wrapper —
# a systemd ExecStart cannot read /etc/jobfitr/active-slot for itself.
#
# Writes the SHARED store (/opt/jobfitr/data/jobs.db, via jobfitr.env). The ledger is
# knowledge about the world, not per-slot state: both slots serve from it indirectly
# through the harvest snapshot, so resolving twice would just be paying twice.
#
# Cheap after the first pass. Every company that fails is recorded as a NEGATIVE, so a
# nightly run only probes employers it has never seen — measured 200 companies in 60s
# on a cold ledger and 0.00s on a warm one.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STATE=/etc/jobfitr/active-slot
LIMIT="${JOBFITR_RESOLVE_LIMIT:-500}"

slot=$(cat "$STATE" 2>/dev/null || echo blue)
bin="/opt/jobfitr/${slot}/jobfitr/.venv/bin/jobfitr-resolve"

if [[ ! -x "$bin" ]]; then
	echo "resolve: no jobfitr-resolve in slot '${slot}' (${bin}) — is the slot built?" >&2
	exit 1
fi

# --discover mines Common Crawl for boards the store has never seen; it is the ONLY
# route to the Workday/enterprise tier, since those employers never appear in the
# aggregator feeds at all. It fails soft and says so when Common Crawl refuses us
# (it rate-limits by IP), leaving the resolve pass below to run regardless.
exec "$bin" --limit "$LIMIT" --discover --cdx
