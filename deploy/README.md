# Deploying jobfitr to the VPS

The runbook for putting jobfitr live at **https://jobfitr.app** on the Hostinger VPS. The app's front end and API run same-origin behind Caddy. jobfitr is a **live-fetch-on-search hybrid**: a search serves a fresh SQLite cache when it has one, or fetches Adzuna + USAJOBS live (single-flighted, ~1–2s) and ranks with FTS5 BM25 + a personalized rerank. A daily baseline harvest keeps a broad floor; a nightly eviction garbage-collects; a daily fetch ceiling load-sheds to the cache.

> **This is the irreversible half of the build.** It wipes/changes a live server and live DNS. Do it deliberately, in order. Every step here is reversible _except_ an SSH lockdown done wrong — which is why hardening is last and guarded.

## What gets set up

| Piece          | Where                                                                                     |
| -------------- | ----------------------------------------------------------------------------------------- |
| App (uvicorn)  | `jobfitr-web.service` → `127.0.0.1:8000`, non-root `jobfitr` user; reads/writes the store |
| Job store      | SQLite/FTS5 at `/opt/jobfitr/data/jobs.db` (`JOBFITR_DB_PATH`) — the pool the app ranks   |
| Baseline floor | `jobfitr-harvest.timer` → `jobfitr-snapshot` **daily** → jobs.json + upserts the store    |
| Pool eviction  | `jobfitr-evict.timer` → `jobfitr-evict` nightly 03:30 — deletes unseen>14d / posted>60d   |
| Load-shed      | `ADZUNA_DAILY_CEILING` (env, default 200) → past it, a search degrades to the cache       |
| TLS + routing  | Caddy: serves `web/`, proxies `/api/*`, auto-HTTPS                                        |
| Secrets        | `/etc/jobfitr/jobfitr.env` (`chmod 640`, root:jobfitr) — **never in git**                 |

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

It's idempotent: installs Caddy + uv, clones `job-radar` + `jobfitr`, builds the venv, installs the units + Caddyfile, enables the web service + harvest timer + **evict timer**, and runs a first harvest. On first request the app **auto-imports any existing `jobs.json` into the new SQLite store once** (the migration is seamless — no manual step). It does **not** touch SSH or the firewall yet.

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
curl -s https://jobfitr.app/api/health | jq .    # ok + adzuna_ok/openrouter_ok/pool_size/daily_fetches_used
curl -s https://jobfitr.app/api/meta | jq .count  # pool size, nonzero after first harvest
systemctl list-timers | grep jobfitr              # harvest (daily) + evict (nightly) scheduled
ss -tlnp | grep -E ':(80|443|22|8000)'            # 8000 is 127.0.0.1 only
```

Open `https://jobfitr.app` — a non-tech search (e.g. "grocery store manager") should fetch live in ~2–4s and return ranked cards with a padlock. A repeat of the same search is instant (served from the fresh cache).

### 6. Harden — LAST, and only after confirming key login works

Open a **second** SSH session with your key (don't close the first) and confirm it logs in without a password. Then:

```bash
sudo HARDEN=1 SSH_USER=<your-sudo-user> bash bootstrap.sh
```

This enables ufw (22/80/443) + fail2ban and disables SSH password auth — but **only if `<your-sudo-user>` already has an `authorized_keys`**; otherwise it refuses, so it can't lock you out.

## Upgrading a live box to the live-fetch hybrid (zero-downtime cutover)

For a box already running the old pure-cache build:

```bash
# 1. add the new env vars (JOBFITR_DB_PATH already defaulted by a fresh bootstrap;
#    on an existing box add them by hand)
sudo nano /etc/jobfitr/jobfitr.env        # + JOBFITR_DB_PATH=/opt/jobfitr/data/jobs.db
                                          # + ADZUNA_DAILY_CEILING=200
# 2. pull the new code + reinstall (sqlite3+fts5 are stdlib — no new deps)
sudo -u jobfitr sh -c "cd /opt/jobfitr/jobfitr && git pull && .venv/bin/uv pip install -e '.[web]'"
# 3. install the new/updated units
sudo install -m 644 /opt/jobfitr/jobfitr/deploy/jobfitr-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobfitr-evict.timer
# 4. restart the web service — it auto-imports the existing jobs.json into jobs.db on
#    the first request (seamless), then live-fetches on misses
sudo systemctl restart jobfitr-web
```

On restart, `store.init()` creates `jobs.db` and imports the current `jobs.json` **once**, so the pool is never empty during the cutover.

## Updating a live box (routine day-2 pull)

The everyday "I merged a change, get it on the box" flow. jobfitr and the job-radar engine it imports are both **editable git checkouts** on the VPS (`/opt/jobfitr/jobfitr`, `/opt/jobfitr/job-radar`), so an update is a `git pull` — no PyPI, no version bump. Do all four steps, in order:

```bash
# 1. pull both repos — the app AND the engine it imports (a change may span both)
sudo -u jobfitr git -C /opt/jobfitr/job-radar pull --ff-only
sudo -u jobfitr git -C /opt/jobfitr/jobfitr   pull --ff-only

# 2. ALWAYS reinstall after a pull — even for a pure-.py change (see the gotcha below)
sudo -u jobfitr sh -lc "cd /opt/jobfitr/jobfitr && uv pip install -e '.[web]' --quiet"

# 3. restart the web service (editable reinstall doesn't disturb the running process)
sudo systemctl restart jobfitr-web

# 4. verify
curl -s http://127.0.0.1:8000/api/health
systemctl is-failed jobfitr-harvest.service jobfitr-evict.service   # want 'inactive', never 'failed'
```

> **Gotcha — reinstall even when only `.py` changed.** A `git pull` updates the source an editable install points at, so code changes take effect on the next restart with no reinstall. **But console-script shims (`.venv/bin/jobfitr-*`) and other entry points are written at _install_ time and are NOT regenerated by `git pull`.** If a pull adds or renames a `[project.scripts]` entry (or changes deps), the shim won't exist until you reinstall — and the systemd unit that calls it fails with `203/EXEC` (binary not found). This is exactly what left `jobfitr-evict` broken after a pull-only update on 2026-07-14. Making the reinstall an unconditional part of every update means it never bites.

If the pull changed a unit file under `deploy/`, also refresh the installed units:

```bash
sudo install -m 644 /opt/jobfitr/jobfitr/deploy/jobfitr-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
```

## Rollback

- **To the pre-hybrid build:** `git checkout <prev-commit>` + reinstall + `systemctl restart jobfitr-web`. The old code reads `jobs.json`, which is left untouched (the harvest still writes it), so a rollback needs nothing else. Optionally `systemctl disable --now jobfitr-evict.timer`.
- App: `systemctl stop jobfitr-web jobfitr-harvest.timer jobfitr-evict.timer`.
- DNS: re-PUT the previous record (or restore the Hostinger zone snapshot).
- The box is provision-in-place; nothing here is destroyed that a re-run of `bootstrap.sh` won't rebuild.

## Never

- Commit `/etc/jobfitr/jobfitr.env`, any key, the Hostinger token, or the box's `jobs.json` / `jobs.db`.
- Disable SSH password auth before a second session proves your key works.
- Upgrade the VPS plan — KVM 2 is plenty; this deploy costs nothing new.
