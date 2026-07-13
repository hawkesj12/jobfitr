# Deploying jobfitr to the VPS

The runbook for putting jobfitr live at **https://jobfitr.app** on the Hostinger VPS. The app's front end and API run same-origin behind Caddy; a systemd timer refreshes the job cache every few hours.

> **This is the irreversible half of the build.** It wipes/changes a live server and live DNS. Do it deliberately, in order. Every step here is reversible _except_ an SSH lockdown done wrong — which is why hardening is last and guarded.

## What gets set up

| Piece         | Where                                                                                 |
| ------------- | ------------------------------------------------------------------------------------- |
| App (uvicorn) | `jobfitr-web.service` → `127.0.0.1:8000`, non-root `jobfitr` user                     |
| Job cache     | `jobfitr-harvest.timer` → `jobfitr-snapshot` every 6h → `/opt/jobfitr/data/jobs.json` |
| TLS + routing | Caddy: serves `web/`, proxies `/api/*`, auto-HTTPS                                    |
| Secrets       | `/etc/jobfitr/jobfitr.env` (`chmod 640`, root:jobfitr) — **never in git**             |

## Prerequisites

- SSH access to the box as a sudo user (see step 1).
- The Hostinger API token (already configured locally for the DNS step) — **never printed or committed.**

## Steps

### 1. Get SSH access

The box is `<VPS_IP>` (Ubuntu 24.04) — replace `<VPS_IP>` with your VPS's public IP throughout. Establish access one of these ways:

- **Hostinger hPanel / API** — attach your SSH public key to the VPS (VPS → SSH keys), or set/reset the root password. Then `ssh root@<VPS_IP>`.
- **Hostinger browser console** — run the commands directly if you'd rather not open SSH.

Create a non-root sudo user for yourself if the box only has root:

```bash
adduser <you> && usermod -aG sudo <you>
# add your public key to /home/<you>/.ssh/authorized_keys
```

### 2. Provision

Copy this repo's `deploy/bootstrap.sh` to the box (or let it clone the repo itself) and run:

```bash
sudo bash bootstrap.sh
```

It's idempotent: installs Caddy + uv, clones `job-radar` + `jobfitr`, builds the venv, installs the units + Caddyfile, enables the web service + harvest timer, and runs a first harvest. It does **not** touch SSH or the firewall yet.

### 3. Add the real keys

```bash
sudo nano /etc/jobfitr/jobfitr.env      # fill in Adzuna / USAJOBS (optional)
sudo systemctl restart jobfitr-web
sudo systemctl start jobfitr-harvest    # re-harvest with the keys
```

The free boards already work with no keys; these just broaden coverage.

### 4. Point DNS at the box

`jobfitr.app`'s apex `A` record starts out on a Hostinger parking IP — move it to the VPS (`<VPS_IP>`). **GET the zone first, then PUT only the changed record**, keeping the existing `www` CNAME. Using the Hostinger DNS API (token sourced from the local secrets file, never echoed):

```bash
# inspect (safe)
curl -s -H "Authorization: Bearer $HOSTINGER_TOKEN" \
  https://developers.hostinger.com/api/dns/v1/zones/jobfitr.app | jq .

# repoint the apex A record to the VPS
curl -s -X PUT -H "Authorization: Bearer $HOSTINGER_TOKEN" -H 'Content-Type: application/json' \
  https://developers.hostinger.com/api/dns/v1/zones/jobfitr.app \
  -d '{"overwrite":true,"zone":[{"name":"@","type":"A","ttl":300,"records":[{"content":"<VPS_IP>"}]}]}'
```

Hostinger keeps zone snapshots, so a bad edit rolls back. Once DNS resolves to the box, **Caddy issues the TLS cert automatically** (give it a minute; it retries).

### 5. Verify

```bash
curl -s https://jobfitr.app/api/health           # {"ok":true}
curl -s https://jobfitr.app/api/meta | jq .count # nonzero
systemctl list-timers | grep jobfitr             # harvest scheduled
ss -tlnp | grep -E ':(80|443|22|8000)'           # 8000 is 127.0.0.1 only
```

Open `https://jobfitr.app` — the form should return cards with a padlock.

### 6. Harden — LAST, and only after confirming key login works

Open a **second** SSH session with your key (don't close the first) and confirm it logs in without a password. Then:

```bash
sudo HARDEN=1 SSH_USER=<your-sudo-user> bash bootstrap.sh
```

This enables ufw (22/80/443) + fail2ban and disables SSH password auth — but **only if `<your-sudo-user>` already has an `authorized_keys`**; otherwise it refuses, so it can't lock you out.

## Rollback

- App: `systemctl stop jobfitr-web jobfitr-harvest.timer`.
- DNS: re-PUT the previous record (or restore the Hostinger zone snapshot).
- The box is provision-in-place; nothing here is destroyed that a re-run of `bootstrap.sh` won't rebuild.

## Never

- Commit `/etc/jobfitr/jobfitr.env`, any key, or the Hostinger token.
- Disable SSH password auth before a second session proves your key works.
- Upgrade the VPS plan — KVM 2 is plenty; this deploy costs nothing new.
