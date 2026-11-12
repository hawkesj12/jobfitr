# jobfitr

**Talk about the work you want. Get up to five jobs worth applying to, each checked against what you asked for, and the reasoning.**

jobfitr is a small, self-hostable web app over about 65,000 live job postings, harvested nightly from employers' own hiring systems. You have a conversation with it — it asks what it needs to, not a fixed list of questions — and then it searches the pool several times, changing what it looks for based on what came back, reads the promising postings in full, and hands you **up to five jobs with its reasoning and a line quoted from each posting**, plus the ones it ruled out and why. Before you see them, the server checks every one against what you said you need; a posting that breaks a dealbreaker is removed and listed with the reason. Most link straight to the employer, and each card says when one does not. No account, no tracking.

**There is no fit score.** There used to be, and it was a careful one — a plain integer you could read off the card with the signals that earned it listed underneath. It went because a number is easier to show and harder to trust, and it cannot tell you *why*. A sentence about the work can.

**Search runs free and offline. The conversation is the part that costs.** Retrieval — harvesting, indexing, and the hybrid keyword-plus-meaning search — needs no API key and no spend at all. Only the conversation calls a model, and which model is one line of config.

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

**The same is true of _where_, not just _what_.** ATS boards skew remote as well as tech, so the stored
pool is thin on local work by construction — searching it for warehouse jobs in Louisville finds almost
nothing. That is not a gap; it is the division of labour. A real search for `Warehouse Supervisor` in
`Louisville, KY` pulls the local employers in on demand, and the board comes back led by Louisville,
Watson, New Albany and Shepherdsville. Judge coverage by running a search, never by querying the pool.

```
nightly:      resolve companies → harvest → jobs.json ──▶ the SQLite/FTS5 store
per request:  what you said ──▶ fresh cache OR one live fetch ──▶ keyword + meaning search ──▶ the model reads and chooses
nightly:      verify the unverifiable, then evict what's gone
```

**It searches under titles you did not say.** A job board indexes the wording a posting actually uses, so
`"High School Teacher"` matches **0** listings while `Teacher` matches **225**. It works out the canonical
wording itself, and searches more than once when the first framing comes back thin — a bad result is
information, not an answer.

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

Open **http://localhost:8000** and tell it what you are looking for. It will ask what it
needs to — as many questions as that takes, not a fixed list — then search, read, and come
back with up to five jobs and why. It shows what it searched for as it goes. Say
"just search" at any point and it does.

Without an `OPENROUTER_API_KEY` the conversation is unavailable and the app says so;
retrieval itself needs no key.

> **Heads-up:** the free, no-key boards skew toward remote tech roles. A broad non-tech search stays thin until you add a free Adzuna key (see _Configure_). The app tells you this when results are sparse — it won't silently hand you an empty list.

## How it decides

Two searches run over the pool, and they find different things:

- a **keyword search** over job titles — precise, and blind to a job whose title uses words you did not think of
- a **meaning search** over what the posting actually says — it reaches `Forward Deployment Engineer` when you asked for `Forward Deployed Engineer`, which the keyword search cannot, because those are different words

Their results are fused, and the measured reason both exist: on a graded set they overlapped on **one** job out of twenty-five each. Together they beat title-only search 16-10 (p = 0.031). Two thirds of the candidates would not exist under a single arm.

Then the model reads the postings — the full text, not a snippet — and picks up to five.

### What you asked for is enforced in code, not left to the model

A model told "remote only, $200K floor, no big consulting" will still recommend an on-site job, a $135K job and a consulting subsidiary, and describe each as a good fit. That was measured here, on four test runs, with the instruction already in the prompt. So the model no longer decides whether a rule was met. It writes down what you said once, as data, and `jobfitr/checks.py` tests every pick against it using the posting's own text:

| the posting | what happens |
| --- | --- |
| says on-site or hybrid, and you said remote only | **removed**, and listed as ruled out with the sentence that says so. The posting's text beats its "Remote" tag. |
| pays up to less than your floor | **removed** |
| is from an employer you ruled out, or names one as its parent company | **removed** |
| reads as a staffing agency, and you ruled those out | **removed** |
| has been taken down since it was collected | **removed** |
| lists no salary, or a range that starts under your floor | kept, with a **warning** on the card |
| is in a different town than you asked for | kept, with a **warning**. There are no distances in the data, so place never removes. |
| never mentions the industry or skill you asked for | kept, with a **warning** |
| asks for many more years than you have, or is more than 45 days old | kept, with a **warning** |

Removal needs evidence from the posting. Silence never removes a job. A removed job is still shown, under "Looked at and ruled out", so a wrong removal is visible and one click away.

Every pick also carries a **quote**: a sentence the model copied from the posting to back up its reason. The server looks for it in the posting, word for word. A quote that is not there is sent back once and never printed; praise with nothing behind it ("aligns well with your background") is sent back too. That can leave fewer than five picks, or none, and the page says so rather than padding.

**What it will not do:** name a job that does not exist. Every url it gives you is checked against the pool before you see it — a listing invented on the way to an answer is dropped, because the one failure this cannot survive is sending someone after a job that was never there.

Matching for your exclusions is **whole-word, plus a plural** — not substring. Excluding "intern" does not delete `Software Engineer, Internal Systems`, and excluding "sales" does not delete `Salesforce Health Cloud`.

## Configure

**The harvest** is driven by a YAML config. Copy the example and edit to taste:

```bash
cp web-harvest.example.yaml web-harvest.yaml
```

It's deliberately _wide_ (broad titles, remote and on-site, generous freshness) — each user's narrow lens is applied at request time, not here. Wide is relative, though: this is the depth lane, bounded by which companies you poll. Coverage outside it comes from the keyed sources below, pulled per search rather than warehoused.

> **jobfitr serves the United States, and salaries in US dollars.** Postings that state another country, or quote pay in another currency, are dropped as they enter the store — about 18% of a typical harvest. That includes remote roles advertised _within_ another country ("Canada - Remote", "Munich (Remote)"): remote is not the same as location-independent. A posting that names no country still passes, because a genuinely placeless remote job has none to name. The engine underneath ([job-radar](https://github.com/hawkesj12/job-radar)) stays international on purpose — this is jobfitr's opinion, not the engine's, and it lives in `store.servable_in_us`. Set `JOBFITR_US_ONLY=0` to keep everything.

> **Most stored postings link to the employer; the rest are labelled, not hidden.** Every row
> carries job-radar's `direct_apply` and renders it as `apply_via` — `employer` or
> `aggregator` — so the claim is made per row rather than in aggregate. An ATS host counts as
> direct: `job-boards.greenhouse.io/acme` IS Acme's own board, with nobody in between and no
> fee taken. Aggregators whose links are pure interstitials are dropped at intake;
> **Adzuna is carried deliberately** despite being a redirect, because it supplies **83% of
> in-metro results on local, non-tech searches** (180 of 217 across four metros, 2026-08-17)
> and dropping it cost **92.6% of local coverage**. `store.CARRIED_AGGREGATORS` names the
> carried set — everything else must show positive evidence. `JOBFITR_DIRECT_ONLY=0` keeps
> everything.

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

**The conversation** has its own settings, all optional:

| Variable | Default | What it does |
| --- | --- | --- |
| `CHAT_MODEL` | `anthropic/claude-sonnet-5.5` | The OpenRouter model id. Must support tool calls. About $0.10-0.40 a conversation on the default; cheaper models run it for a cent and follow the interview less well. |
| `CHAT_DAILY_CEILING` | `100` | Turns per day (Eastern) before the chat answers 503. A turn is one message; a conversation is about five. This is the spend cap. |
| `CHAT_RATE_LIMIT` | `20/minute` | Per-address limit on both chat endpoints. |
| `CHAT_MAX_SEARCHES` | `4` | Searches per answer. Past this the search tool refuses and the model has to read and decide. |
| `CHAT_WARM_WAIT` | `12` | Seconds a search waits for the live job sources before answering from what is stored. The fetch finishes in the background and the next search sees it. |
| `CHAT_CHECK_LIVE` | `1` | Ask each chosen posting whether it is still up before showing it. `0` turns it off. |
| `CHAT_STREAM` | `1` | Stream the model's words as they are written. |
| `CHAT_SESSION_TTL` | `7200` | Seconds a conversation is kept in memory after its last message. |
| `CHAT_ALLOW_CLIENT_HISTORY`, `CHAT_ALLOW_MODEL_OVERRIDE` | off | For a local measurement harness only: accept a client-supplied transcript, and let the caller pick the model. Never set these on a public box. |

## Commands

| Command            | What it does                                                                     |
| ------------------ | -------------------------------------------------------------------------------- |
| `jobfitr-serve`    | run the API + front end                                                          |
| `jobfitr-snapshot` | the baseline harvest → `jobs.json`, and into the store                           |
| `jobfitr-resolve`  | turn company names into pollable ATS boards (`--stats`, `--audit`, `--discover`) |
| `jobfitr-evict`    | verify liveness, then garbage-collect (`--no-verify`, `--verify-batch`)          |

**A posting disappears when the employer takes it down, not just when it gets old.** `last_seen`
is a heartbeat: the nightly harvest re-fetches every resolved board and refreshes it, so a
withdrawn listing simply stops being returned and is dropped. Two windows, because the
heartbeat is not equally trustworthy everywhere — **3 days** for the 11 sources the harvest
re-fetches (97.7% of the pool: absence is confirmed every night), **14 days** for the
per-search live lane (adzuna, usajobs, google_jobs), whose `last_seen` only means "someone
searched something like this recently". For that 2.3%, `jobfitr-evict` HEADs the
least-recently-seen rows and removes anything answering 404/410 — and a 200 refreshes the
heartbeat, so a live job is not evicted for want of a search. Ambiguous answers (403, 429, 5xx,
a network fault) never delete anything: the cost of guessing wrong is removing a job somebody
could have applied to. Separately, `posted` older than **60 days** goes regardless, and a
**120,000-row** LRU cap bounds the pool.

## API

Same-origin; the front end talks to these directly.

| Method & path        | Purpose                                                                                                                                                                                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST /api/candidates` | Retrieval, and **no key required**. Body: `titles`, `probes` (sentences describing the work), `location`, `remote_only`, `max_age_days`, `k`. Pre-filters, runs the keyword and meaning searches, fuses them, and returns candidates with enough body text to judge from. This is the free, deterministic half of the product. |
| `POST /api/chat`       | The conversation, one turn. `{session, message}` → `{session, reply, picks, ruled_out, brief, trace, receipt, nothing_fits}`. Send `session: null` to start; the server returns an id and holds the transcript, so the client sends only the new message. The model runs its own search loop server-side; `trace` is what it searched and why, `receipt` is what the turn cost. The **only** metered path — needs `OPENROUTER_API_KEY`, fails closed with a 503 when absent or over the daily ceiling. |
| `POST /api/chat/stream` | The same turn as Server-Sent Events: a frame as each search, read and check happens, the model's words as they are written, then the result. This is what the page uses. |
| `GET /api/meta`      | `count` (pool size), `harvested_at`, and the `code_sha` this process is running.                                                                                                                                                                                               |
| `GET /api/health`    | Which feeds are live, the daily fetch budget used, pool size vs. the snapshot it should be serving, and when that snapshot was last ingested.                                                                                                                                  |

## What gets recorded

**No account, no tracking is meant literally.** There is no login, no cookie, no
analytics script, and nothing that follows a person between visits or between searches.

**The conversation is held in the server's memory while you are having it, and nowhere
else.** The page sends a random session id and your new message; the server keeps the
transcript against that id so it can answer the next message. It is never written to disk
and never logged. It is dropped two hours after your last message, and immediately if the
server restarts. The id is made by the server, lives only in the open page (no cookie, no
local storage), and is gone when you close the tab. The text of each turn goes to the
model provider (OpenRouter) to be answered, as it must.

Why it is held on the server at all: the alternative is the browser sending the whole
transcript each turn, which lets anyone rewrite what the assistant "said" earlier.

Every response also tells you what it took: searches run, postings read, seconds, the
model, and the model spend for that answer. It is printed under the results on purpose.

A self-hosted instance can optionally keep a **search-quality log** — set
`JOBFITR_SEARCH_LOG` to a file path, and each search appends one JSON line: the query
shape (titles, exclusions, location), how the funnel went (candidates retrieved, jobs
delivered, latency), the top ten results, and — when it happened — which part of the
request the app had to override to answer it at all. It exists because
every retrieval number in this repo is measured against synthetic, author-written profiles,
which cannot tell you whether a _real_ search returned something a real person wanted.

What that log deliberately does not contain: no IP address, no user agent, no referer,
no session or request id, and no text from the chat interview. Two identical searches an
hour apart are indistinguishable from two different people. That is a real cost — one
person's refine loop is not reconstructable — and it is accepted rather than engineered
around, because the alternative is an identifier.

**It is off unless you set the variable.** Read it with:

```bash
python scripts/review_searches.py /path/to/searches.jsonl --days 7
```

## Project layout

```
jobfitr/
  server.py           FastAPI: /api/chat + /api/candidates + /api/meta + /api/health; serves web/
  store.py            SQLite + FTS5 — the job pool, BM25 retrieval, eviction, the company→ATS
                      ledger. ONE store, shared by both deploy slots, named jobs-v<schema>.db
  searchlog.py        optional append-only record of searches + what they returned (off unless configured)
  match.py            term matching (whole-word + plural) — what your exclusions are filtered with
  live.py             the per-search live fetch, single-flighted
  snapshot.py         the baseline harvest → atomic jobs.json; the cached reader
  resolve.py          company → ATS board discovery, with a negative cache and an audit path
  universe.py         reads deploy/board-universe.json — the set of ATS boards known to exist
  chat.py             THE conversation: interviews, searches repeatedly, reads, delivers five
  semantic.py         the meaning-search arm (Model2Vec vectors) and the fusion with keyword search
  vectors.py          builds the vector store; incremental, keyed by a hash of the embedded text
  prompts/            every system prompt, as .md — nothing is inlined in Python
  config_builder.py   the posted answers → a job_radar Config (the FILTER half: what you may receive)
web/
  index.html          the chat front door + the results board + applied rail
  chat.js             the four-question assistant (one structured turn per message)
  app.js              config → API → board rows, criteria bar, filters, localStorage state
  atmosphere.js       the time-of-day sky the whole UI floats on
  style.css           the theme (time-of-day atmosphere, responsive)
scripts/
  mine_universe.py    regenerate deploy/board-universe.json from Common Crawl (run OFF-box)
  rebuild_store.py    back up, rebuild on the current schema, report per-column fill
tests/                the store, the conversation, the web API, and term matching. The 234
                      hand-computed scoring goldens went with the scoreboard in Aug 2026
bakeoff/              the model bake-off: which LLM should run the conversation, and how we know
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
