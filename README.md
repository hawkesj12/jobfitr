# jobfitr

**Answer four questions, get a ranked list of jobs that actually fit you — each with a link straight to apply.**

jobfitr is a small, self-hostable web app. An assistant asks you four things — the job you want, where, what should rank a role higher (skills, tools), and what to keep out — then hands back fit-scored, clickable, direct-to-company job listings. Mark the ones you apply to and they fly to a saved rail. No account, no tracking.

Each result carries its **fit** as a number and a bar in its own column, plus the signals that earned it. Fit is normalized against your own best match, so it is a relative ranking, not an absolute grade — the board says so on its face.

It's the consumer front end on top of the open-source [**job-radar**](https://github.com/hawkesj12/job-radar) engine, which does the harvesting and scoring.

## How it works

**Cached harvest.** A scheduled job harvests a broad universe of postings into a `jobs.json` snapshot every few hours. Each user request just scores that snapshot against their answers — **zero external API calls per request**. So the number of visitors is decoupled from job-board traffic: no rate-limit bans, no shared-key quota burn, and instant results.

```
scheduled:   wide harvest ──▶ jobs.json  (the cache)
per request: your 4 answers ──▶ score the cache ──▶ ranked links
```

## Quickstart (local)

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). jobfitr pulls the `job-radar` engine automatically from PyPI.

```bash
git clone https://github.com/hawkesj12/jobfitr
cd jobfitr

# create a venv and install jobfitr + extras (job-radar comes from PyPI)
uv venv
uv pip install -e ".[web,dev]"

# build the cache from the free, no-key job sources
jobfitr-snapshot

# serve the app
jobfitr-serve
```

> **Hacking on the engine too?** To run against a local `job-radar` checkout instead of the PyPI release, clone it next door and install it editable first: `uv pip install -e ../job-radar`.

Open **http://localhost:8000** and answer the four questions the assistant asks. The board
appears as soon as it has enough. From there, **Refine** re-opens the assistant inline at
the top of the board — say "drop contract roles" or "senior only" and it re-scores — or
edit the criteria pills directly.

> **Heads-up:** the free, no-key boards skew toward remote tech roles. A broad non-tech search stays thin until you add a free Adzuna key (see _Configure_). The app tells you this when results are sparse — it won't silently hand you an empty list.

## Configure

**The harvest** is driven by a YAML config. Copy the example and edit to taste:

```bash
cp web-harvest.example.yaml web-harvest.yaml
```

It's deliberately _wide_ (broad titles, remote and on-site, generous freshness) so the cache holds the broad universe — each user's narrow lens is applied at request time, not here.

**API keys** are optional and broaden coverage. Copy the template and fill in what you have:

```bash
cp .env.example .env
```

| Key                                 | What it adds                                  | Free key                                                           |
| ----------------------------------- | --------------------------------------------- | ------------------------------------------------------------------ |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`  | general job market (all fields, any location) | [developer.adzuna.com](https://developer.adzuna.com/)              |
| `USAJOBS_API_KEY` / `USAJOBS_EMAIL` | US federal roles                              | [developer.usajobs.gov](https://developer.usajobs.gov/apirequest/) |
| `OPENROUTER_API_KEY`                | optional LLM re-ranking (off by default)      | [openrouter.ai](https://openrouter.ai/)                            |

`.env` is gitignored — keys never land in the repo.

## API

Same-origin; the front end talks to these directly.

| Method & path     | Purpose                                                                                                                                                                             |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/score` | Score the cached snapshot against a config body (`titles`, `boosts`, `exclude`, `rank_down`, `location`, `remote_only`, `max_age_days`, `min_score`, `limit`). Returns ranked jobs. |
| `POST /api/chat`  | One conversational turn: `{messages, config, refining}` → `{reply, config, ready, chips}` (structured output). The only thing that reaches scoring is the config it fills. `refining` switches from the intake interview to editing a search that is already on screen. |
| `POST /api/prefetch` | Warms the cache once titles + location are known, so the board is ready by the last answer. |
| `GET /api/meta`   | Snapshot freshness — `count` (pool size) and `harvested_at`. Drives the front door's proof line. |
| `GET /api/health` | Liveness check.                                                                                                                                                                     |

## Project layout

```
jobfitr/
  config_builder.py   the 4-answer JSON → a job_radar Config (the per-user lens)
  snapshot.py         wide harvest → atomic jobs.json; the cached reader
  chat.py             the structured-turn assistant behind /api/chat
  server.py           FastAPI: /api/chat + /api/score + /api/meta + /api/health; serves web/
web/
  index.html          the chat front door + the results board + applied rail
  chat.js             the four-question assistant (one structured turn per message)
  app.js              config → API → board rows, criteria bar, localStorage state
  atmosphere.js       the time-of-day sky the whole UI floats on
  style.css           the theme (time-of-day atmosphere, responsive)
tests/
  test_web.py         incl. the zero-network-on-request guarantee
web-harvest.example.yaml   the wide-harvest config
```

## Develop

```bash
pytest          # the suite, incl. the cached-harvest guarantee
ruff check      # lint
```

## Deploy

Production runs both the front end and the API on one small server behind [Caddy](https://caddyserver.com/) (auto-HTTPS, same-origin), with the harvest on a scheduled timer. The `deploy/` directory has everything: the Caddyfile, systemd units, and an idempotent `bootstrap.sh` that provisions a fresh Ubuntu box. See **[`deploy/README.md`](deploy/README.md)** for the full runbook.

## License

Apache-2.0 © Justin Hawkes
