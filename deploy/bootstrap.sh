#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# jobfitr — provision an Ubuntu 24.04 box to serve https://jobfitr.app
#
# Run as root on the VPS:   sudo bash bootstrap.sh
#
# Idempotent: safe to re-run. It installs Caddy + uv + git, clones job-radar and
# jobfitr under a non-root user, builds the venv, installs the systemd units and
# Caddyfile, enables the web service + harvest timer, and runs a first harvest.
#
# It does NOT wipe the box, NOT change SSH auth, and NOT enable the firewall by
# default — hardening is a separate, opt-in step at the bottom (HARDEN=1) so this
# script can never lock you out on its own. See deploy/README.md for the full
# runbook (DNS repoint, real keys, hardening order).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_USER="jobfitr"
BASE="/opt/jobfitr"
APP_DIR="$BASE/jobfitr"
ENGINE_DIR="$BASE/job-radar"
DATA_DIR="$BASE/data"
ENV_DIR="/etc/jobfitr"
ENV_FILE="$ENV_DIR/jobfitr.env"
JOBFITR_REPO="${JOBFITR_REPO:-https://github.com/hawkesj12/jobfitr}"
ENGINE_REPO="${ENGINE_REPO:-https://github.com/hawkesj12/job-radar}"

log() { printf '\n\033[1;34m▸ %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
	echo "Run as root: sudo bash bootstrap.sh" >&2
	exit 1
fi

# ── 1. system packages ───────────────────────────────────────────────────────
log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https ufw fail2ban

# ── 2. Caddy (official apt repo) ──────────────────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
	log "Installing Caddy"
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
		gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
		>/etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq
	apt-get install -y -qq caddy
else
	log "Caddy already installed — skipping"
fi

# ── 3. non-root app user ──────────────────────────────────────────────────────
if ! id "$APP_USER" >/dev/null 2>&1; then
	log "Creating app user: $APP_USER"
	useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$DATA_DIR"
chown -R "$APP_USER:$APP_USER" "$BASE"

# ── 4. uv (installed for the app user) ───────────────────────────────────────
UV="$BASE/.local/bin/uv"
if [[ ! -x "$UV" ]]; then
	log "Installing uv for $APP_USER"
	sudo -u "$APP_USER" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

# ── 5. clone / update both repos ──────────────────────────────────────────────
clone_or_pull() {
	local repo="$1" dir="$2"
	if [[ -d "$dir/.git" ]]; then
		log "Updating $(basename "$dir")"
		sudo -u "$APP_USER" git -C "$dir" pull --ff-only
	else
		log "Cloning $(basename "$dir")"
		sudo -u "$APP_USER" git clone --depth 1 "$repo" "$dir"
	fi
}
clone_or_pull "$ENGINE_REPO" "$ENGINE_DIR"
clone_or_pull "$JOBFITR_REPO" "$APP_DIR"

# ── 6. venv + install (job-radar editable, then jobfitr[web]) ────────────────
log "Building the virtualenv"
sudo -u "$APP_USER" sh -c "cd '$APP_DIR' && '$UV' venv && '$UV' pip install -e '$ENGINE_DIR' && '$UV' pip install -e '.[web]'"

# ── 7. server EnvironmentFile (placeholders — real keys added by hand) ───────
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
	log "Creating $ENV_FILE (placeholders — edit in the real keys, then restart)"
	cat >"$ENV_FILE" <<-EOF
		# jobfitr server environment. Real secrets live ONLY here (chmod 600),
		# never in the repo. Fill in the keys you have, then:
		#   systemctl restart jobfitr-web && systemctl start jobfitr-harvest
		JOBFITR_JOBS_PATH=$DATA_DIR/jobs.json
		JOBFITR_WEB_DIR=$APP_DIR/web

		# Optional — broaden coverage (the free boards run without these).
		ADZUNA_APP_ID=
		ADZUNA_APP_KEY=
		USAJOBS_API_KEY=
		USAJOBS_EMAIL=
		# OPENROUTER_API_KEY=
	EOF
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ── 8. systemd units + Caddyfile ─────────────────────────────────────────────
log "Installing systemd units and Caddyfile"
install -m 644 "$APP_DIR/deploy/jobfitr-web.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/jobfitr-harvest.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/jobfitr-harvest.timer" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy

systemctl daemon-reload
systemctl enable --now jobfitr-web.service
systemctl enable --now jobfitr-harvest.timer

# ── 9. first harvest + reload Caddy ──────────────────────────────────────────
log "Running the first harvest (this hits the free job sources — ~1 min)"
systemctl start jobfitr-harvest.service || true
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy || systemctl restart caddy

log "Provision complete."
cat <<-EOF

	Next (see deploy/README.md):
	  1. Edit real keys into $ENV_FILE, then: systemctl restart jobfitr-web
	  2. Point jobfitr.app A-record at this box, then Caddy auto-issues TLS.
	  3. Verify: curl -s https://jobfitr.app/api/health
	  4. THEN harden SSH:  HARDEN=1 SSH_USER=<your-sudo-user> bash bootstrap.sh

EOF

# ── 10. HARDENING — opt-in only, lockout-safe ────────────────────────────────
# Runs ONLY when HARDEN=1. Enables ufw (22/80/443) + fail2ban, and disables SSH
# password auth ONLY after confirming the named user already has an authorized
# key — so you can never lock yourself out by running this.
if [[ "${HARDEN:-0}" == "1" ]]; then
	log "HARDENING: firewall + fail2ban + key-only SSH"

	ufw --force reset >/dev/null
	ufw default deny incoming
	ufw default allow outgoing
	ufw allow 22/tcp
	ufw allow 80/tcp
	ufw allow 443/tcp
	ufw --force enable
	systemctl enable --now fail2ban

	SSH_USER="${SSH_USER:-}"
	if [[ -z "$SSH_USER" ]]; then
		echo "!! Set SSH_USER=<your-sudo-user> to disable password login safely. Skipping SSH lockdown." >&2
	else
		KEYS="$(eval echo "~$SSH_USER")/.ssh/authorized_keys"
		if [[ -s "$KEYS" ]]; then
			log "Confirmed authorized key for $SSH_USER — disabling SSH password auth"
			sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
			printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
				>/etc/ssh/sshd_config.d/99-jobfitr-hardening.conf
			systemctl reload ssh || systemctl reload sshd
			echo "SSH password auth disabled. Keep your key safe — password login is now off."
		else
			echo "!! No authorized_keys for $SSH_USER ($KEYS is empty/missing)." >&2
			echo "!! REFUSING to disable password auth (would lock you out). Add your key first." >&2
		fi
	fi
fi
