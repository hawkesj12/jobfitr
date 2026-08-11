# jobfitr

**Answer four questions, get a ranked list of jobs that actually fit you — each with a link straight to apply.**

jobfitr is a small, self-hostable web app. An assistant asks you four things — the job you want, where, what should rank a role higher (skills, tools), and what to keep out — then hands back fit-scored, clickable, direct-to-company job listings. Mark the ones you apply to and they fly to a saved rail. No account, no tracking.

Each result carries its **fit as a plain integer** you can read off the card, with the signals that earned it listed underneath. Those signals add up to the number — that is a tested guarantee, not a hope. The score is **absolute**: 100 means an exact title match whether it is alone on your board or one of fifty, and it means the same thing tomorrow.

It's the consumer front end on top of the open-source [**job-radar**](https://github.com/hawkesj12/job-radar) engine, which does the harvesting.

## What this is, honestly

I built jobfitr as a **personal tool** — I wanted a job search that ranked things the way I'd rank them,
and told me why. It turned out to be useful enough to share, so it's open source and self-hostable.

**It is deliberately economical.** The whole thing runs on one small VPS — two cores, a few dollars a
month — against free API tiers. That constraint shaped every design decision worth talking about here:
scoring is plain integer arithmetic instead of a model, retrieval is SQLite FTS5 instead of a vector
service, breadth is fetched per search instead of warehoused, and there is a daily fetch ceiling so a
busy day degrades to the cache rather than to a bill. There's no cluster behind this and it isn't
pretending there is.

So: not a job board trying to index the market, and not a startup. A cheap, honest tool that does one
thing — take what you actually want, and sort real listings by it — with its reasoning printed on every
card so you can disagree with it.

## How it works

**Fetch live when it has to, serve the cache when it can.** A search either serves a fresh cache — that
title-and-location combination was fetched under the TTL, so it costs zero API calls — or it makes **one**
bounded live fetch of the fast keyed sources. Concurrent identical searches are coalesced into a single
upstream call.

**Two lanes, on purpose.** The nightly harvest polls company ATS boards directly, which gives real
_depth_ where those boards are — in practice that skews tech, and it is ~78% of the stored pool. It is
not, and is not trying to be, the whole labour market. **Breadth arrives on demand:** the first time
someone searches a role the harvest has never covered, the live fetch pulls it and it stays. A cold pool
holding zero occupational therapists answers that search with 33 of them in about nine seconds, and the
next person who asks gets them free from the cache. That is what keeps the whole thing runnable on a
small box and free API tiers instead of trying to warehouse every job that exists.

```
nightly:      resolve companies → harvest → jobs.json ──▶ the SQLite/FTS5 store
per request:  your 4 answers ──▶ fresh cache OR one live fetch ──▶ rank the store ──▶ ranked links
nightly:      evict what's gone stale
```

**The assistant also suggests five adjacent titles**, once your own list is final, and those join the
search as well as the scoring. It matters more than it sounds: a job board indexes the wording a posting
actually uses, so `"High School Teacher"` matches **0** listings while the suggested `Teacher` matches
**225**. They score a flat **30** — below anything you named yourself, because you did not ask for them —
and they are what stops a precisely-worded search returning nothing. They also quietly fix typos: type
`data analist` and the assistant writes `Data Analyst`.

Three things bound the cost, which is what makes it safe to run on free API tiers:

- **A freshness TTL per search**, so repeating a search is free.
- **A daily live-fetch ceiling.** Past it, a search degrades to the cache and says so on the page rather than burning quota.
- **Eviction**, so the store reaches a bounded steady state instead of growing forever.

## Quickstart (local)

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). jobfitr pulls the `job-radar` engine automatically from PyPI.

```bash
git clone https://github.com/hawkesj12/jobfitr
cd jobfitr

# create a venv and install jobfitr + extras (job-radar comes from PyPI)
uv venv
uv pip install -e ".[web,dev]"

# build the baseline pool from the free, no-key job sources
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

## How a job gets its score

Deliberately mechanical, so it can be read off the card and checked:

```
points = title tier + Σ boost points − penalties
```

| Component     | Rule                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Title**     | the best single tier — **100** exact · **80** every word of your title present · **60** same role, different seniority · **30** a title the assistant suggested. Tiers never stack. Each is tried against **both** the title the employer wrote and the same title with its seniority and decoration stripped, whichever scores better: `Senior Application Security Engineer (Remote)` is an exact match for _Application Security Engineer_. |
| **Boosts**    | **8/6/4/2** for the 1st–4th time a term appears (20 max per term), across the title and the body. Uncapped across terms.                                                                                                                                                                                                                                                                                                                       |
| **Penalties** | **−30** when an avoid-term is in the title or the employer's name, **−15** when it is buried in the body. Strongest hit only.                                                                                                                                                                                                                                                                                                                  |
| **Freshness** | not scored. A three-day-old wrong job is not a better fit than a month-old perfect one — recency is a filter.                                                                                                                                                                                                                                                                                                                                  |

Three properties this design guarantees, and all three are tested over every listing in the corpus:

- **The number means the same thing every day.** 92 is 92 whether it's alone on the board or one of fifty, and whether you search today or next month. Only the card's *bar* is relative — it's a fraction of the best match in front of you.
- **Naming another skill can never lower your score.** Evidence only ever adds.
- **The chips on the card sum to the number beside them.** A breakdown that doesn't reconcile is worse than showing none, because it invites you to trust a wrong explanation.

Two listings on the same score are shown in retrieval order — how well the search engine matched them — which is why a board can have a run of identical numbers in a deliberate sequence rather than an arbitrary one.

Matching is **whole-word, plus a plural** — not substring. Boosting "rag" matches _RAG_, not _storage_, _leverage_, or _coverage_. Multi-word terms match as a phrase, and accept the hyphen English writes half the time ("forward deployed" finds "forward-deployed").

## Configure

**The harvest** is driven by a YAML config. Copy the example and edit to taste:

```bash
cp web-harvest.example.yaml web-harvest.yaml
```

It's deliberately _wide_ (broad titles, remote and on-site, generous freshness) — each user's narrow lens is applied at request time, not here. Wide is relative, though: this is the depth lane, bounded by which companies you poll. Coverage outside it comes from the keyed sources below, pulled per search rather than warehoused.

> **jobfitr serves the United States, and salaries in US dollars.** Postings that state another country, or quote pay in another currency, are dropped as they enter the store — about 18% of a typical harvest. That includes remote roles advertised _within_ another country ("Canada - Remote", "Munich (Remote)"): remote is not the same as location-independent. A posting that names no country still passes, because a genuinely placeless remote job has none to name. The engine underneath ([job-radar](https://github.com/hawkesj12/job-radar)) stays international on purpose — this is jobfitr's opinion, not the engine's, and it lives in `store.servable_in_us`. Set `JOBFITR_US_ONLY=0` to keep everything.

> Run it from the repo root. The config is resolved relative to the working directory, and falling through to job-radar's built-in defaults is a cliff, not a soft default — those are narrow and tech-only, so a harvest launched from elsewhere quietly returns a fraction of the jobs with no error. It prints a loud warning if it can't find one.

**API keys** are optional and broaden coverage. Copy the template and fill in what you have:

```bash
cp .env.example .env
```

| Key                                 | What it adds                                                    | Free key                                                           |
| ----------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------ |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`  | general job market (all fields, any location)                   | [developer.adzuna.com](https://developer.adzuna.com/)              |
| `USAJOBS_API_KEY` / `USAJOBS_EMAIL` | US federal roles                                                | [developer.usajobs.gov](https://developer.usajobs.gov/apirequest/) |
| `OPENROUTER_API_KEY`                | the conversational front door (falls back to a form without it) | [openrouter.ai](https://openrouter.ai/)                            |

`.env` is gitignored — keys never land in the repo.

## Commands

| Command            | What it does                                                                     |
| ------------------ | -------------------------------------------------------------------------------- |
| `jobfitr-serve`    | run the API + front end                                                          |
| `jobfitr-snapshot` | the baseline harvest → `jobs.json`, and into the store                           |
| `jobfitr-resolve`  | turn company names into pollable ATS boards (`--stats`, `--audit`, `--discover`) |
| `jobfitr-evict`    | garbage-collect the stale pool                                                   |

## API

Same-origin; the front end talks to these directly.

| Method & path        | Purpose                                                                                                                                                                                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST /api/score`    | Score against a config body (`titles`, `related_titles`, `boosts`, `exclude`, `rank_down`, `location`, `remote_only`, `max_age_days`). Returns up to `JOBFITR_RESULT_CAP` (default 200) ranked jobs plus facet counts, gzipped.                                                |
| `POST /api/chat`     | One conversational turn: `{messages, config, refining}` → `{reply, config, ready, chips, hint, restart}` (structured output). The only thing that reaches scoring is the config it fills. `refining` switches from the intake interview to editing a search already on screen. |
| `POST /api/prefetch` | Warms the cache once titles + location are known, so the board is ready by the last answer. Also returns `candidates` — how many listings the search will have to rank — so the wait can say what it is doing instead of spinning.                                             |
| `GET /api/meta`      | `count` (pool size), `harvested_at`, and the `code_sha` this process is running.                                                                                                                                                                                               |
| `GET /api/health`    | Which feeds are live, the daily fetch budget used, pool size vs. the snapshot it should be serving, and when that snapshot was last ingested.                                                                                                                                  |

## Project layout

```
jobfitr/
  server.py           FastAPI: /api/chat + /api/score + /api/meta + /api/health; the scoreboard; serves web/
  store.py            SQLite + FTS5 — the job pool, BM25 retrieval, eviction, the company→ATS ledger
  match.py            term matching (whole-word + plural) and the title tiers
  live.py             the per-search live fetch, single-flighted
  snapshot.py         the baseline harvest → atomic jobs.json; the cached reader
  resolve.py          company → ATS board discovery, with a negative cache and an audit path
  chat.py             the structured-turn assistant behind /api/chat
  config_builder.py   the posted answers → a job_radar Config (the per-user lens)
web/
  index.html          the chat front door + the results board + applied rail
  chat.js             the four-question assistant (one structured turn per message)
  app.js              config → API → board rows, criteria bar, filters, localStorage state
  atmosphere.js       the time-of-day sky the whole UI floats on
  style.css           the theme (time-of-day atmosphere, responsive)
tests/                the store, the chat, the web API, and 234 golden cases whose expected
                      score was computed by hand from the spec — not from the code
bakeoff/              the model bake-off: which LLM should run the chat, and how we know
deploy/               systemd units, Caddyfile, bootstrap.sh, and the blue-green slots
web-harvest.example.yaml   the wide-harvest config
```

## Develop

```bash
pytest          # the everyday run (~10s)
pytest -m slow  # + the full-corpus sweep, before a version capture
ruff check      # lint
```

## Deploy

Production runs the front end and the API on one small server behind [Caddy](https://caddyserver.com/) (auto-HTTPS, same-origin), with the harvest, resolution, and eviction on scheduled timers.

There are two shapes, both in `deploy/`:

- **Single slot** — `bootstrap.sh` provisions a fresh Ubuntu box idempotently. See **[`deploy/README.md`](deploy/README.md)**.
- **Blue-green slots** — two warm copies, a public preview URL, an atomic flip, and instant rollback, gated by a pre-flip verification script. See **[`deploy/slots/README.md`](deploy/slots/README.md)**.

## License

Apache-2.0 © Justin Hawkes
