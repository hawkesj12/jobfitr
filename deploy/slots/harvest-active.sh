#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — run the baseline harvest from whichever slot is currently PRODUCTION.
#
# Why a wrapper instead of a path in the unit file: under blue-green there is no
# fixed install directory. A systemd ExecStart can't read /etc/jobfitr/active-slot
# for itself, so this script resolves it at run time and execs that slot's binary.
# The effect is that harvest code ships with the release — flip to a slot whose
# harvester changed, and the next nightly run uses the new one; roll back, and it
# reverts with everything else.
#
# Writes the SHARED snapshot (/opt/jobfitr/data/jobs.json). Each slot's own store
# picks it up on its next sync — see store.sync_snapshot().
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STATE=/etc/jobfitr/active-slot
OUT=/opt/jobfitr/data/jobs.json

# WAIT FOR RESOLVE. It decides which boards this harvest polls, so running before it finishes
# means harvesting last night's universe. The clock gap is an hour and resolve takes ~2
# minutes, so this should never fire — which is the point: it costs nothing and removes the
# assumption.
waited=0
while systemctl is-active --quiet jobfitr-resolve.service; do
	if [ "$waited" -ge 1800 ]; then
		echo "harvest: resolve still running after 30m — proceeding with the existing ledger" >&2
		break
	fi
	sleep 15
	waited=$((waited + 15))
done
[ "$waited" -gt 0 ] && echo "harvest: waited ${waited}s for resolve to finish"

slot=$(cat "$STATE" 2>/dev/null || echo blue)
root="/opt/jobfitr/${slot}/jobfitr"
bin="${root}/.venv/bin/jobfitr-snapshot"

# Fail loudly rather than silently skipping a night's harvest — a missing binary
# means a half-finished deploy, and a silent no-op would look identical to success.
if [[ ! -x "$bin" ]]; then
	echo "harvest: no jobfitr-snapshot in slot '${slot}' (${bin}) — is the slot built?" >&2
	exit 1
fi

# MUST run from the slot root. jobfitr-snapshot resolves its harvest config
# (web-harvest.yaml, then web-harvest.example.yaml) RELATIVE TO THE WORKING DIRECTORY,
# and falls back to job_radar's built-in defaults when it finds neither. Those defaults
# are narrow and tech-only — a harvest that ran from anywhere else quietly produced
# ~1,700 jobs instead of ~20,000, with no error and no warning. Measured 2026-07-22.
cd "$root"

# --watchlist is now only a SEED for the resolution ledger, which is the real company
# universe (see jobfitr/snapshot.py:_harvest_universe). Read from the SAME slot as the
# binary so the seed and the harvester that reads it are always the same release.
exec "$bin" --out "$OUT" --watchlist "${root}/deploy/tech-watchlist.json"
