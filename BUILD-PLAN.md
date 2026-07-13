# Build plan — jobfitr (cached-harvest job web app, on the VPS)

**Date:** 2026-07-12 · **Product:** **jobfitr** · **Domain:** `jobfitr.app` · **New repo:** `~/Dev/jobfitr` (public), depends on the `job-radar` package · **Gate:** ✅ `ok:true`, 0 hard issues
**Grounds on:** the Oracle run `2026-07-12-job-radar-web-app-cached-harvest-architecture.md`

## Latest status (2026-07-12)

- **Domain purchased:** `jobfitr.app` registered at Hostinger (DNS controllable via the Hostinger API; A-record → VPS happens in Phase D, not before).
- **Repo:** `~/Dev/jobfitr` is a **new, empty repo to scaffold** (git init, `pyproject.toml` depending on `job-radar`, package dir `jobfitr/`). The public `job-radar` repo already exists and is the engine.
- **Keys pending (Justin's to get, not blocking A–C):** Adzuna (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, developer.adzuna.com) + USAJOBS (`USAJOBS_API_KEY`/`USAJOBS_EMAIL`, developer.usajobs.gov). OpenRouter key already available (for Phase-2 BYOK re-rank).
- **Applied-rail delight is CORE** (Phase B), not phase-2.
- **Build order:** A → B → C fully working locally on the Mac first; **pause before Phase D** (the VPS wipe is irreversible — do it with Justin present).

## Repo structure — what lives where (the separate-repo reality)

The web app is its **own** repo; it does not edit the `job_radar` package except possibly one small upstream. New code lives under a `jobfitr/` package:

- **In `jobfitr/` (new):** `snapshot.py` (build/load the cache — calls `job_radar.engine.harvest`), `server.py` (the FastAPI app), `config_builder.py` (turn the posted 5-answer JSON into a `job_radar.config.Config`), plus `web/` (front end), `deploy/`, `pyproject.toml` (deps: `job-radar`, and a `web` extra), `tests/`.
- **The one optional upstream to `job-radar`:** add `config.from_dict(doc)->Config` to the library (a clean, reusable library method) and depend on that version. If you'd rather not cut a library release mid-build, `jobfitr/config_builder.py` maps the dict onto the public `Config` dataclass locally instead. Builder's call; default to the local mapper to keep the build self-contained, upstream later.
- **Reused from `job-radar` unchanged (imported, never copied):** `scoring.score/relevant/is_remote`, `engine.harvest`, `config.Config`, `util.age_int`. No scoring logic is ever re-implemented in jobfitr or in JS.

## Context

**jobfitr** is a separate public repo — the consumer web app — that `pip install job-radar`s the open-source engine and rides on top of it. Anyone answers ~5 questions and gets a personalized, fit-scored list of clickable direct-to-company job links, at **jobfitr.app**. **Both the front end and the backend live on the VPS** — Caddy serves the static page and reverse-proxies `/api/*` on the same origin, so there's no CORS. A static-only host can't work because the harvest needs a server. The VPS gets **wiped and provisioned fresh**.

**Why a separate repo:** `job-radar` is the engine/library (a CLI developers install); jobfitr is a product with a different audience. Keeping them apart keeps the library free of web deps, and jobfitr imports the engine (`from job_radar import scoring, engine, config`).

The architecture is **cached-harvest**: a scheduled job harvests a broad-superset universe into a `jobs.json` snapshot every few hours; each user request just scores that snapshot against their config with the existing engine — **zero external API calls per request**, so user count is decoupled from job-API traffic (no IP bans, no shared-key quota burn).

## Grounding confirmed (fresh reads this session)

`engine.py` (`harvest()→(rows,discovered,errors)`, rows keep `text`), `scoring.py` (`score`/`relevant`/`is_remote` — pure, reused as-is), `config.py` (`load_config` merge; `LLMConfig` already OpenAI-compatible), `store.py` (atomic `temp→os.replace` pattern to mirror), `cli.py` (subcommand structure), `util.py` (`age_int`, `has`), `funnel.py`, `llm.py`, `pyproject.toml`, `README.md`.

## Recommended approach

| Concern   | Decision                                                                                                                           |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Serving   | Static front page (Caddy `file_server`) + **FastAPI** `/api/score` (server-side scoring reusing `scoring.py`)                      |
| Cache     | Atomic **`jobs.json`** snapshot, loaded in memory, hot-reloaded on mtime                                                           |
| Scheduler | **systemd timer** (every 4–6h, `Persistent=true`)                                                                                  |
| Front end | Hand-written HTML/JS, **no accounts**; config in localStorage + shareable URL hash                                                 |
| HTTPS     | `jobfitr.app` A-record → VPS + **Caddy** auto-TLS                                                                                  |
| AI        | Deterministic score is the free per-user layer; LLM is **default-off**, BYOK or generic-cached (never Justin's bill for strangers) |
| Security  | key-only SSH + ufw 80/443 + fail2ban + Tailscale admin + non-root app + rate-limited `/api`                                        |

**Key refactor — harvest wide, score narrow.** Today `engine._consume()` scores + filters _during_ harvest. The scheduled harvest runs a **permissive superset** config (broad titles, `remote_only:false`, no location excludes, generous age) so it stores the broad deduped universe _with_ text; the **user's narrow lens** (their titles, min_score, location) is applied at request time in `/api/score`. The API reuses `scoring.py` primitives directly — no `_consume` refactor, low blast radius, and no scoring logic ever forked into JS.

## Decisions locked (the gate's contract)

**Must add:** both FE+BE on the VPS · requests score the cached snapshot only (never live APIs) · reuse `scoring.py` unchanged · scheduled atomic snapshot · keys in server env only · LLM never bills Justin for strangers (default off / BYOK) · no accounts / no server-side PII · Caddy auto-HTTPS subdomain · min hardening (key-only SSH, ufw, fail2ban, non-root, rate-limit).

**Must not:** score via live APIs per request · commit any secret/VPS token · fork scoring into JS · require login / store PII · touch life-ops or the private engine · expose SSH beyond hardened 80/443.

## Sequence

_All paths below are in the **new `~/Dev/jobfitr` repo** unless marked (job-radar). Gate-consistent: every new file is an `add`; the only possible edit to a ledger file is the optional `job_radar/config.py` upstream._

**Phase A — scaffold + engine layer (local, testable)**

0. **Scaffold `~/Dev/jobfitr`** (add) — `git init`; `jobfitr/__init__.py`; `pyproject.toml` (deps: `job-radar`, `pyyaml`; `[web]` extra: `fastapi`, `uvicorn[standard]`; console scripts `jobfitr-snapshot`, `jobfitr-serve`); `.gitignore` (`.env`, `jobs.json`, `.venv`); `LICENSE` (Apache-2.0); a uv venv with `job-radar` installed (editable-local or from PyPI once published).
1. `jobfitr/config_builder.py` (add) — `config_from_dict(doc)->job_radar.config.Config`: map the posted 5-answer JSON onto the public `Config` dataclass. (Or upstream `from_dict` to job-radar and import it — builder's call.)
2. `jobfitr/snapshot.py` (add) — `build_snapshot(cfg, wl, out)` runs `job_radar.engine.harvest`, dumps deduped postings (text truncated ~2000 chars) + meta to `jobs.json` atomically (`temp→os.replace`); `load_snapshot(path)` mtime-cached.
3. `web-harvest.example.yaml` (add) — the WIDE superset harvest config (broad titles, `remote_only:false`, no excludes, generous age).
4. `jobfitr/server.py` (add) — FastAPI: `POST /api/score` (dict→`config_from_dict`→`load_snapshot`→per-post `relevant`/`is_remote`/`age_int`/`score` against the user cfg→filter `min_score`→sort→top-N JSON), `GET /api/meta`, `GET /api/health`. **Never fetches an external API on a request.**
5. `tests/test_web.py` (add) — `config_from_dict` builds a Config; snapshot round-trips; `/api/score` ranks+filters and makes **zero** network calls (monkeypatched); garbage POST handled; `jobfitr-snapshot` writes the file.

**Phase B — the front end** (all in `web/`) 6. `web/index.html` + `web/app.js` + `web/style.css` (add) — the 5-question form → `POST /api/score` → clickable cards with the results "moment" (count-up), jobfitr theme, shareable URL hash. **State lives in localStorage** (no accounts): the user's config _and_ per-role `applied`/`dismissed` status persist across visits.

- **The applied rail (delight):** clicking "Applied" on a card **animates it flying to a minimized rail pinned on the right edge**, where it stays (a collapsed stack of applied roles, click to expand). Dismissed roles fade out. All client-side, persisted in localStorage — so returning users keep their applied history. This is the "moment" that makes it feel like a product, not a form.

**Phase C — packaging polish** 7. Finalize `pyproject.toml` + a `README.md` for jobfitr (what it is, `pip install`, run locally: `jobfitr-snapshot` then `jobfitr-serve`). The `snapshot`/`serve` entry points are **jobfitr's own** console scripts — not edits to the job-radar CLI.

**Phase D — deploy (the VPS — PAUSE for Justin, irreversible)** 8. `deploy/Caddyfile` (add) — `jobfitr.app { root web/; file_server; reverse_proxy /api/* localhost:8000; rate_limit }`. 9. `deploy/jobfitr-harvest.service` + `.timer` (add) — oneshot `jobfitr-snapshot` every 4–6h, `Persistent=true`, `EnvironmentFile` (keys). 10. `deploy/jobfitr-web.service` (add) — uvicorn `jobfitr.server:app` on localhost:8000 as a **non-root** app user, `restart=always`, `EnvironmentFile`. 11. `deploy/bootstrap.sh` (add) — wipe→harden→deploy: key-only SSH, ufw 80/443, fail2ban, Tailscale (admin plane), install Caddy + uv, non-root app user, clone jobfitr, `uv pip install .[web]`, root-readable `EnvironmentFile` (chmod 600), enable the units + timer. 12. `deploy/README.md` (add) — runbook: wipe the VPS; add the `jobfitr.app` A-record via the Hostinger API (**GET the zone first, then PUT**); where the Adzuna/USAJOBS/OpenRouter keys go (server `EnvironmentFile` only, never the repo); first-run verification; harvest cadence.

## Critical files

`jobfitr/server.py` (the new API — the load-bearing piece), `jobfitr/snapshot.py` (the cache), `jobfitr/config_builder.py` (dict→Config), `deploy/bootstrap.sh` (the hardening — the security posture lives here).

## Verification

- `pytest` green incl. the zero-network assertion on `/api/score`.
- Local: `jobfitr-snapshot` writes `jobs.json`; `jobfitr-serve` + a curl to `/api/score` returns ranked JSON with no outbound job-API call.
- VPS: `https://jobfitr.app` loads (padlock), the form returns cards, `systemctl list-timers` shows the harvest timer, `ss -tlnp` shows only 80/443 public (SSH on Tailscale).

## Phase 2 (flagged, not in the core build)

BYOK OpenRouter re-rank in `jobfitr/server.py`; the **radius filter** (Adzuna `distance` km + USAJOBS `Radius` mi — the "200 miles around Louisville" feature; re-read `sources.py` first); an always-on depth poll for the top watchlist if you want fresher-than-cache. _(Applied/dismissed localStorage + the applied rail is now **core**, in Phase B.)_

## Open questions

Caddy `rate_limit` plugin (xcaddy) vs app-level slowapi · harvest cadence (4h vs 6h) · snapshot store JSON vs Parquet vs SQLite (start JSON).

---

# Phase E — the conversational, atmospheric front door (planned 2026-07-13)

**Status:** planned · gated `ok:true` (0 hard issues) · **plan-only — build is the next turn.**
**Grounds on:** the Oracle run `~/.claude/library/oracle/2026-07-13-jobfitr-ai-chat-experience.md`, the locked design memory (`design-direction.md`), and the design artifacts in `design/` (`atmosphere-study.html` = the locked ambient layer, `design-board-v2.html` = the experience).

## The takeaway

jobfitr is **already live** at jobfitr.app, so this is an **additive** phase that builds on a branch, verifies locally, and deploys deliberately — nothing half-built hits prod. It adds one streaming endpoint (`/api/chat`), two small `/api/score` fields, and a vanilla `web/` rebuild that ports the locked "atmosphere" design. The whole scoring core — the cached snapshot, `config_from_dict`, the `scoring.py` reuse, and the **sacred zero-external-call-on-`/api/score` invariant** — is untouched.

The architecture is **two planes, one gate**: the metered AI chat fills a config; the only thing that crosses into the free, zero-network scoring plane is a validated `config_from_dict` dict. That gate is both the security boundary (a spike proved hostile AI output is inert once it hits `config_from_dict`) and the reason the app stays cheap and fast.

## Recommended approach

Build **`/api/chat` first** — it is the riskiest new piece and it unblocks the front end. Keep the existing 5-question form as the **no-AI fallback** (and the fallback when the daily cost ceiling trips). No framework — `web/` stays hand-written vanilla; the `job_radar` engine is imported, never forked into JS.

## Decisions locked (the gate's contract)

**Must add:** streaming `/api/chat` (OpenRouter, cheap model) whose only tool `set_config` feeds `config_from_dict` and nothing else · prompt-injection scoping with no consequential tools · cost controls (turn cap, per-IP `slowapi` limit, daily ceiling → form fallback, key in server env only) · `/api/score` gains `fit_pct` (absolute-hybrid, server-side) + a fuller `description`, zero-network invariant intact and tested · the vanilla front end (atmosphere ambient layer, streaming chat + echo, page-lift, gauge-card carousel, two-step apply, left filter drawer, right applied board, header toolbar) with the form kept as fallback · build on a branch, verify locally, deploy deliberately.

**Must not:** score via live APIs per request or break the `/api/score` zero-network guarantee · fork scoring into JS · commit any secret/VPS token · push a half-built front end to prod.

**Defaulted:** gauge corner label = **tier word** (primary) + **rank** (secondary), no synthetic % · filters = **header toolbar (quick) + left drawer (deep)** split.

## Sequence

_Build order top-to-bottom; `/api/chat` first. New file = `add`; everything else edits a file confirmed by a real read._

| #   | File                 | Type | What                                                                                                                                                                                                                                                                                                                                                                                                                                               | Depends  | Verify                                                                                                                    |
| --- | -------------------- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------- |
| 1   | `pyproject.toml`     | edit | Add to `[web]`: `httpx`, `sse-starlette`, `slowapi` (OpenRouter is OpenAI-compatible — call over httpx).                                                                                                                                                                                                                                                                                                                                           | —        | `uv pip install -e .[web,dev]` resolves.                                                                                  |
| 2   | `jobfitr/chat.py`    | add  | OpenRouter httpx client; the `set_config` JSON-Schema tool mirroring `config_from_dict`'s 8 fields; async `stream_chat(messages, current_config)` SSE generator; injection-scoped system prompt (fill a job-search config, refuse else); turn cap; daily-ceiling counter; model+key from env. Only `set_config` args ever head toward scoring.                                                                                                     | 1        | Mocked OpenRouter (no network) → yields tokens + a `set_config` delta `config_from_dict` accepts.                         |
| 3   | `jobfitr/server.py`  | edit | Add `POST /api/chat` (`StreamingResponse` → `chat.stream_chat`); `slowapi` per-IP limit; daily-ceiling gate → `503` "use the form". `/api/score` untouched here.                                                                                                                                                                                                                                                                                   | 2        | TestClient streams `/api/chat` mocked; `429` after the limit; `/api/score` unchanged.                                     |
| 4   | `jobfitr/server.py`  | edit | In `score_jobs`: derived `fit_pct` (absolute-hybrid over the result set) + a fuller `description` in `_shape()`. Raw `fit_score` stays canonical; full JD `text` still not leaked.                                                                                                                                                                                                                                                                 | —        | `fit_pct` 0–100 monotonic with `fit_score`; `description` longer than `snippet`; `text` absent.                           |
| 5   | `tests/test_chat.py` | add  | `/api/chat` tests: mocked OpenRouter (zero network); `set_config` round-trips `config_from_dict`; turn cap; per-IP limit; daily ceiling → fallback; `/api/chat` reaches no job API.                                                                                                                                                                                                                                                                | 3        | pytest green; no real network.                                                                                            |
| 6   | `tests/test_web.py`  | edit | Add `fit_pct` + `description` assertions; re-assert `test_zero_network_on_request` passes unchanged.                                                                                                                                                                                                                                                                                                                                               | 4        | pytest green incl. the zero-network guarantee.                                                                            |
| 7   | `web/index.html`     | edit | Add atmosphere layers (`sky`/`glow`/`wash`/`stars`), the chat front door (echo stack + input + starter chips), the results carousel + gauge-card template (tier word + rank, gauge fill, no %), the left filter drawer, the header toolbar; keep the applied rail; keep the 5-question form as a hidden no-AI fallback.                                                                                                                            | 4        | Page loads; form present as fallback; `test_static_front_end_is_served` green.                                            |
| 8   | `web/style.css`      | edit | Add the atmosphere token system (glass-over-sky, time-driven accent vars), glass surfaces, carousel taper, the two edge drawers, the toolbar — ported from `atmosphere-study.html`. Keep existing tokens for the fallback form. `prefers-reduced-motion`-safe.                                                                                                                                                                                     | 7        | Local render; clean DevTools; reduced-motion collapses motion.                                                            |
| 9   | `web/atmosphere.js`  | add  | The sky engine ported from `atmosphere-study.html`: 1440-min keyframes, local-clock apply, glass tint + auto-contrast flip, time-driven accent, living halo (breathes slower at night), deep-night twinkles, slow drift. Recomputes on the minute; reduced-motion gated.                                                                                                                                                                           | 7,8      | Sky advances on the minute; boots on the browser clock; reduced-motion disables motion.                                   |
| 10  | `web/chat.js`        | add  | The streaming chat: consume `/api/chat` SSE into the echo stack, capture the `set_config` delta, merge partial updates into the config, run the page-lift transition (View Transitions/FLIP) into results, hand the config to the existing `runSearch`. Fall back to the form on `503`/error.                                                                                                                                                      | 3,7      | End-to-end (cheap model) + a mocked path; TTFT ~<1s; `503` → form fallback.                                               |
| 11  | `web/app.js`         | edit | Evolve results into the gauge-card carousel (focused + tapering masked), two-step apply (view≠apply; reuse `flyToRail` for the FLIP fly-to-rail), the left filter drawer (client-side re-filter of the in-memory scored set; re-POST `/api/score` only when the _search_ changes), the header toolbar (sort + quick filters + live count), the `fit_pct` gauge fill + tier word + rank. Reuse `store`, `renderRail`, `writeSummary`, `decodeHash`. | 4,7,8,10 | Carousel promotes on apply/dismiss; filters update the count live; apply flies to rail and persists; reduced-motion safe. |

## Critical files

`jobfitr/chat.py` (the new metered plane + the injection scope — the load-bearing new piece), `jobfitr/server.py` (`/api/chat` + `fit_pct`, and the guardian of the zero-network invariant), `web/chat.js` + `web/app.js` (the streaming front door + the carousel/apply loop), `web/atmosphere.js` (the locked identity).

## Risks

- **Cost (high)** — metered LLM on a public portfolio can be abused. → cheap model, turn cap, per-IP `slowapi`, daily ceiling that **fails closed to the working form**.
- **Injection (high)** — → only `set_config` args reach scoring, and `config_from_dict` is inert to hostile input (spike-proven); tight system prompt; no consequential tools.
- **Invariant (med)** — don't couple the metered chat to the sacred `/api/score`. → separate path; the zero-network test stays green; a new test asserts `/api/chat` reaches no job API.
- **Prod (med)** — jobfitr is live. → branch → local pytest + serve → deliberate deploy.

## Migration / deploy

Cut a **`phase-e`** branch off `main` before any edits (and commit the currently-untracked `design/` folder with it). Deploy only after local green: set `OPENROUTER_API_KEY`, `CHAT_MODEL` (a cheap model), and `CHAT_DAILY_CEILING` in `/etc/jobfitr/jobfitr.env` (the web service `EnvironmentFile`); `uv pip install .[web]` in `/opt/jobfitr/jobfitr/.venv`; `systemctl restart jobfitr-web`. **Caddy already proxies `/api/*` → no Caddyfile change** (confirmed).

## Open questions

Which specific cheap OpenRouter model (pick by cost/latency at build) · the exact absolute-hybrid reference max for `fit_pct` (calibrate so a strong match reads ~85–100%) · confirm `bootstrap.sh` runs `uv pip install .[web]` before relying on re-provision to pull the new deps.

## Verification (phase gate)

`pytest` green including `test_zero_network_on_request` and the new `/api/chat` tests · `jobfitr-serve` locally: the chat streams, fills a config, page-lifts to the gauge carousel, apply flies to the rail, the form still works with the AI off · DevTools clean, every moment degrades under `prefers-reduced-motion` · only then deploy.
