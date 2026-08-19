#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — deploy a git ref to the STAGING (inactive) slot, then restart it.
# Production is never touched; you preview at https://staging.jobfitr.app and
# only go live when you run flip.sh.
#
#   sudo bash deploy-slot.sh phase-e      # build the phase-e branch on staging
#   sudo bash deploy-slot.sh v1.2.0       # a tag works too
#   sudo bash deploy-slot.sh <sha>        # or an exact commit
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Re-exec from a copy OUTSIDE the working tree before doing anything else.
# This script lives in the repo it checks out, and bash reads a script incrementally —
# so `git checkout` swaps the file out from under the running shell and the remaining
# lines are read from the NEW file at the OLD byte offset. Observed 2026-07-31: a deploy
# printed the previous version's log line and silently ran the previous version's install
# command, which resolved a version range instead of the lockfile and put an untested
# dependency on the slot. It reported success. Copy first, then run.
if [[ "${JOBFITR_DEPLOY_REEXEC:-}" != "1" ]]; then
	_self=$(mktemp /tmp/deploy-slot.XXXXXX.sh)
	cp "$0" "$_self"
	JOBFITR_DEPLOY_REEXEC=1 exec bash "$_self" "$@"
fi
trap 'rm -f "$0"' EXIT   # $0 is the temp copy in the re-exec'd process

REF="${1:-main}"
STATE=/etc/jobfitr/active-slot
UV="/opt/jobfitr/.local/bin/uv"

active=$(cat "$STATE" 2>/dev/null || echo blue)
if [[ "$active" == blue ]]; then slot=green; else slot=blue; fi   # deploy to the inactive one
dir="/opt/jobfitr/${slot}/jobfitr"

echo "▸ deploying ref '${REF}' to the STAGING slot: ${slot}  (${dir})"
sudo -u jobfitr git -C "$dir" fetch --all --tags --quiet
sudo -u jobfitr git -C "$dir" checkout --quiet "$REF"
sudo -u jobfitr git -C "$dir" pull --ff-only --quiet 2>/dev/null || true   # no-op for a tag/sha
echo "▸ installing deps (from the lockfile — a replay, not a re-solve)"
# --frozen installs exactly what uv.lock pins and never re-resolves. The old
# `uv pip install -e '.[web]'` resolved the pyproject version RANGES at install time, so
# two deploys from identical source could land different dependency versions — which
# also meant a rollback restored the old code against a possibly-different graph. The
# range is a wish; the lockfile is the contract. If this errors with a stale-lockfile
# complaint, the fix is `uv lock` committed from a dev machine, never a re-solve here.
# --extra semantic pulls model2vec (+ tokenizers/safetensors) for the dense retrieval arm.
# Deliberately NOT fastembed: that drags onnxruntime, 69 MB on its own, for a model that
# measured no better. The whole semantic extra is ~30 MB and has no torch.
# The arm degrades to lexical-only if it is ever missing, so a slot that somehow installs
# without it still serves searches — it just answers `"semantic": false`.
sudo -u jobfitr sh -c "cd '$dir' && '$UV' sync --frozen --extra web --extra semantic --quiet"

systemctl restart "jobfitr-web@${slot}"
sleep 1
if systemctl is-active --quiet "jobfitr-web@${slot}"; then
	echo "✔ staging slot '${slot}' now runs '${REF}'"
	echo "  preview → https://staging.jobfitr.app     (go live with: sudo bash flip.sh)"
else
	echo "x staging slot '${slot}' failed to start — check: journalctl -u jobfitr-web@${slot}" >&2
	exit 1
fi
