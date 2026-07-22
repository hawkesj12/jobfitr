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

slot=$(cat "$STATE" 2>/dev/null || echo blue)
root="/opt/jobfitr/${slot}/jobfitr"
bin="${root}/.venv/bin/jobfitr-snapshot"

# Fail loudly rather than silently skipping a night's harvest — a missing binary
# means a half-finished deploy, and a silent no-op would look identical to success.
if [[ ! -x "$bin" ]]; then
	echo "harvest: no jobfitr-snapshot in slot '${slot}' (${bin}) — is the slot built?" >&2
	exit 1
fi

# --watchlist turns on the DEPTH lane: ~94 verified tech companies' ATS feeds
# (Greenhouse/Lever/Ashby/SmartRecruiters). Read from the SAME slot as the binary so
# the watchlist and the harvester that reads it are always the same release.
exec "$bin" --out "$OUT" --watchlist "${root}/deploy/tech-watchlist.json"
