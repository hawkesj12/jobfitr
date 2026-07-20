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
echo "▸ installing deps"
sudo -u jobfitr sh -c "cd '$dir' && '$UV' pip install -e '.[web]' --quiet"

systemctl restart "jobfitr-web@${slot}"
sleep 1
if systemctl is-active --quiet "jobfitr-web@${slot}"; then
	echo "✔ staging slot '${slot}' now runs '${REF}'"
	echo "  preview → https://staging.jobfitr.app     (go live with: sudo bash flip.sh)"
else
	echo "x staging slot '${slot}' failed to start — check: journalctl -u jobfitr-web@${slot}" >&2
	exit 1
fi
