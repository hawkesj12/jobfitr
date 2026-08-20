#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — embed unvectorised rows, from whichever slot is currently PRODUCTION.
#
# Same wrapper pattern as harvest-active.sh: a systemd ExecStart cannot read
# /etc/jobfitr/active-slot for itself, so this resolves it at run time. The effect
# is that embedding code ships with the release and rolls back with it.
#
# Reads the SHARED store and writes the SHARED vectors file. Both slots serve from
# that one vectors file, so this runs once for the whole box, not once per slot.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STATE=/etc/jobfitr/active-slot
DATA=/opt/jobfitr/data

slot=$(cat "$STATE" 2>/dev/null || echo blue)
root="/opt/jobfitr/${slot}/jobfitr"
bin="${root}/.venv/bin/jobfitr-vectors"

# Fail loudly rather than skipping a night. A silent no-op is indistinguishable from
# success, and this job's entire failure mode is being silently absent — that is how
# the arm went dark on 97% of Louisville rows with every dashboard reading healthy.
if [[ ! -x "$bin" ]]; then
	echo "vectors: no jobfitr-vectors in slot '${slot}' (${bin}) — is the slot built with --extra semantic?" >&2
	exit 1
fi

# The store filename carries the schema version (jobs-v{SCHEMA_VERSION}.db), so resolve
# it rather than hardcoding: a schema bump must not leave this pointed at a dead file,
# embedding nothing and reporting success.
store=$(ls -1t "${DATA}"/jobs-v*.db 2>/dev/null | head -1)
if [[ -z "$store" ]]; then
	echo "vectors: no jobs-v*.db in ${DATA}" >&2
	exit 1
fi

cd "$root"
exec "$bin" --store "$store" --vectors "${DATA}/vectors-v1.db"
