# Handoff — job-radar is on PyPI; jobfitr now depends on it cleanly

**Date:** 2026-07-19

**The takeaway in one line:** the `job-radar` engine is now published on PyPI (`pip install job-radar`, v0.2.0), and jobfitr depends on it like any normal package — the old "clone job-radar next door" hack is gone. One operational decision remains about the **live VPS** (see the ⚠️ section).

---

## The relationship (for whoever reads this next)

- **job-radar** = the engine. A public, Apache-2.0 Python library + CLI that harvests the job market, scores roles, and de-dupes them. Repo: `hawkesj12/job-radar`. Now on PyPI.
- **jobfitr** = this repo. The consumer web app on top of that engine. It **imports** job-radar (never copies it): `from job_radar import scoring, engine, config, sources, util`. Its whole scoring core is job-radar's `scoring.py`, reused unchanged. jobfitr adds the cached-snapshot architecture, the FastAPI server, the 5-question front end, and the deploy.

So job-radar is a dependency of jobfitr exactly like `pyyaml` is. Publishing it to PyPI is what lets jobfitr be a real, installable, self-hostable package instead of something that only builds if you happen to have a second repo checked out beside it.

---

## What changed (2026-07-19)

**job-radar (the engine):**

- Published **v0.2.0 to PyPI** — `pip install job-radar` now works. The published build is current `main` HEAD, so it includes the recent panel-review improvements (`--verbose`/`--strict`, SSRF/slug guards, atomic writes, scoring speedups), not just the original three-PR cleanup.
- Tagged **`v0.2.0`** in git so the PyPI version maps to a known commit.
- Added `.github/workflows/release.yml` — a Trusted-Publishing (tokenless) release workflow for future versions (needs a one-time PyPI activation, see Next Steps #2).

**jobfitr (this repo):**

- `pyproject.toml`: the dependency is now `job-radar>=0.2,<0.3` (a real pinned range), and the `[tool.uv.sources]` local-path override was **removed**.
- `README.md`: the quickstart no longer tells you to clone job-radar next door — it comes from PyPI. (A note remains for when you want to hack on the engine locally.)

**Verified:** a fresh `uv pip install -e ".[web,dev]"` resolves job-radar from PyPI (site-packages, not an editable path), and **all 74 jobfitr tests pass** against it. Every job-radar symbol jobfitr imports exists in the published 0.2.0 public API.

---

## ⚠️ The one decision you need to make: the live VPS

jobfitr is already deployed at **jobfitr.app**. That box installs **both repos as editable git checkouts** (`/opt/jobfitr/jobfitr` and `/opt/jobfitr/job-radar`), and the deploy runbook's update flow is "`git pull` both — no PyPI, no version bump." That worked _because_ the old `[tool.uv.sources]` override pointed the install at the sibling `/opt/jobfitr/job-radar` checkout.

**I removed that override.** So the next time anything runs `uv pip install` on the box, job-radar will resolve **from PyPI (0.2.0)** and the local `/opt/jobfitr/job-radar` checkout will be ignored. Nothing breaks _right now_ (the running service keeps using whatever's already installed), but the "git pull the engine to ship an engine change" flow no longer does what the deploy README says.

Pick one before the next deploy — don't let a routine `uv pip install` silently switch it:

- **(a) Track PyPI on the box (recommended).** The engine reaches prod via a **PyPI release + version bump**, same as any dependency. Drop the job-radar clone and the `git -C .../job-radar pull` step from `deploy/bootstrap.sh` + `deploy/README.md`. Cleaner, reproducible, versioned — and it's the whole reason we published. Cost: an engine change now needs a release to reach prod (which the release workflow makes a one-tag operation).
- **(b) Keep the engine editable on the box.** Re-add an explicit editable install of the sibling checkout in the deploy (`uv pip install -e /opt/jobfitr/job-radar` _before_ installing jobfitr), so `git pull` still ships engine changes. Keeps the fast dev-on-the-box loop, but the box no longer matches how everyone else installs jobfitr.

I did **not** touch `deploy/` — that's live production infra and the build-plan's own rule is to change the VPS deliberately, with you present. This is yours to decide and apply.

---

## Suggested next steps (ordered)

1. **Resolve the VPS engine-source decision above** and update `deploy/bootstrap.sh` + `deploy/README.md` to match. Do it as a deliberate deploy, not a drive-by.
2. **Activate Trusted Publishing** so future job-radar releases are automatic: on pypi.org → the `job-radar` project → _Publishing_ → add a trusted publisher for repo `hawkesj12/job-radar`, workflow `release.yml`, environment `pypi`. After that, cutting a release = tag a version and publish a GitHub Release; CI builds and uploads. (Until then, releases still work manually via `uv publish` with the token in `life-ops/data/secrets/pypi.env`.)
3. **Commit the currently-untracked `design/` and `deploy/slots/`** before starting Phase E — the build-plan explicitly says to cut the `phase-e` branch _with_ the design folder committed.
4. **Phase E — the conversational front door** (already planned, plan-only in `BUILD-PLAN.md`). Additive: one streaming `/api/chat` endpoint + a `web/` rebuild that ports the locked "atmosphere" design. The sacred `/api/score` zero-external-call invariant stays untouched.
5. **No 0.2.1 needed for now** — the recent engine improvements are already in the published 0.2.0. Bump the version when you make the _next_ engine change you want on the box (and mind the `>=0.2,<0.3` pin here — a job-radar 0.3 with breaking API changes won't be picked up until you widen it).

---

_Written by Nova after publishing job-radar 0.2.0 and switching this repo's dependency to it. Engine + app both green._
