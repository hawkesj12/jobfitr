# jobfitr — the parts, and how finished each one is

**Updated:** 2026-08-20 · one line per subsystem, and an honest status.

This is the inventory: what jobfitr is made of, what each part does, and **whether it is
actually done**. It is deliberately not a roadmap and not a runbook — `CLAUDE.md` says how
the system works, `HANDOFF.md` says where it is right now and what has already cost time.
This says *what exists and what is half-built*, which is the question neither of those
answers.

**The status words mean specific things**, because "in progress" hides more than it says:

| status | means |
| --- | --- |
| **LIVE** | running in production and a user's request actually goes through it |
| **BUILT** | works and is tested, but nothing user-facing calls it yet |
| **PARTIAL** | works for some inputs or some sources, and the gap is known and named |
| **STALE** | shipped, still running, and describes a design that has been replaced |
| **PLANNED** | designed on paper, no code |
| **NONE** | doesn't exist, and the absence is a real gap rather than a decision |

---

## The shape of the thing

```
  job-radar (a dependency)            ──▶  nightly harvest  ──▶  jobs-v3.db
       ▲ boards discovered by resolve                              │
                                                                   ├──▶ FTS5 lexical arm ─┐
                                            vectors-v1.db  ◀───────┤                      ├─▶ RRF ─▶ candidates
                                            (Model2Vec)            └──▶ semantic arm  ────┘              │
                                                                                                         ▼
                                                             the agent: interview → search → read → recommend
                                                                                                         │
                                                                                                    the web app
```

---

## 1. Ingestion — getting the jobs in

| part | status | notes |
| --- | --- | --- |
| **job-radar dependency** | **LIVE** | pinned `>=0.8,<0.9`. The engine that talks to every board. |
| **Nightly harvest** (`snapshot.py`, `jobfitr-snapshot`) | **LIVE** | 03:00 UTC. ~101k harvested → ~65k servable → `jobs.json` → store. |
| **Board resolution** (`resolve.py`, `jobfitr-resolve`) | **LIVE** | company name → `(ats, slug)`, caches negatives. Runs before the harvest because it decides what gets polled. |
| **Board universe mining** (`universe.py`, `mine_universe.py`) | **PARTIAL** | 7,045 boards. **Must be run off-box** — Common Crawl's CDX host refuses the VPS. Monthly, by hand. |
| **Live fetch lane** (`live.py`) | **LIVE** | Adzuna + USAJOBS + Google Jobs per search, single-flighted, daily ceiling with a `degraded` banner. |
| **HTML in bodies** | **BROKEN UPSTREAM** | 65% of stored bodies carry raw markup — a `clean()` ordering bug in job-radar, not here. Diagnosed, reproduced, handed off; **0.9.0 in flight**. Store schema v4 waits for it. |

## 2. The store

| part | status | notes |
| --- | --- | --- |
| **Schema v3** (`store.py`) | **LIVE** | 38 columns, one shared SQLite store, `jobs-v{VERSION}.db`. Rebuilt, never migrated. |
| **FTS5 index** | **LIVE** | `porter unicode61`, title weighted 8× over body. |
| **Intake filters** | **LIVE** | US-servable only, direct-employer links only. Non-US is never *stored*, not filtered at query time. |
| **Eviction** (`jobfitr-evict`) | **LIVE** | 05:00 UTC, after the harvest. Unseen >14d, posted >60d, LRU cap 120k. |
| ~~Sections~~ | **DELETED** | 2026-08-20. It only worked because job-radar leaked raw HTML — an upstream bug being fixed. Cleaning the body removes the tags and with them anything to parse, so it would have silently returned nothing. Sections, if ever wanted, are the engine's to emit. |
| **Schema v4** | **PLANNED** | waits on job-radar 0.9.0 delivering clean bodies + a `sections` field. |

## 3. Retrieval

| part | status | notes |
| --- | --- | --- |
| **Lexical arm** (`store.bm25_candidates`) | **LIVE** | title-scoped `NEAR`. Precise, and blind to a job whose title uses different words. |
| **Semantic arm** (`semantic.py`) | **LIVE** | Model2Vec `potion-retrieval-32M`, 65,394 vectors, 512-dim. Degrades to lexical on any failure. |
| **Vector builder** (`vectors.py`, `jobfitr-vectors`) | **LIVE** | incremental by content hash. Full pool in **2.1 min on the box**. |
| **Hybrid fusion** (`semantic.hybrid`) | **LIVE** | RRF k=60, per-company cap, pre-filter before both arms. |
| **`/api/candidates`** | **LIVE** | the AI path's retrieval. Nothing user-facing calls it yet. |
| **Nightly re-embed** | **NONE** | **A real gap.** New rows from each harvest have no vector until someone runs `jobfitr-vectors` by hand. No timer exists. |
| **The scoreboard** (`server.scoreboard`) | **STALE** | the old 100/80/60/30 tier ladder + boosts. Still serves `/api/score`; the product no longer has a score. |

## 4. The conversation

| part | status | notes |
| --- | --- | --- |
| **The conversation** (`chat.py`) | **BUILT** | interview with no tool calls → repeated adaptive searches → read → `recommend`. Verified end-to-end against production data. |
| **Tools** | **BUILT** | `search_jobs`, `read_jobs`, `recommend`. `recommend` checks every url against the pool — it has caught real fabrication. |
| **Prompts as files** (`prompts/chat_*.md`) | **BUILT** | every system prompt is a `.md`, loaded by `prompts.load()`. |
| **Model bench** (`agent.MODELS`) | **PARTIAL** | 13 candidates, live-verified tool-capable. **Quality unverified — no bakeoff has run.** |
| **Live store glimpse** (`chat.store_glimpse`) | **BUILT** | two real rows + live fill rates, read at runtime. Replaced a hand-written note whose numbers rotted as the harvester improved. |
| **Interview structure** | **PLANNED** | measured: prose instructions do not hold. Needs `ask(question, slot)` + a slot ledger + a server-side gate. Design in `_private/ORACLE-interview-design-2026-08-19.md`. |
| **Refinement loop** | **NONE** | the biggest missing feature. "this one's perfect" / "not this" has no path. The model **fabricates urls** on a revision turn, so it must be a typed tool call, never a prose rewrite. |
| ~~Old config chat~~ | **DELETED** | 2026-08-20. Filled a config for the scoreboard; recoverable from git. Its cost controls and message sanitising were carried into the new `chat.py` — they were never part of that design. |

## 5. The web app

| part | status | notes |
| --- | --- | --- |
| **Door / hero** (`index.html`) | **STALE** | title, meta description and og:image still sell "ranks every open role" and a fit score that no longer exists. Still contains the four-question config form. |
| **Chat UI** (`chat.js`) | **BUILT** | rewritten for the agent: renders the reply, the picks as cards, and what it searched. Not deployed. |
| **Results board** (`app.js`) | **STALE** | 52 KB for 200 scored rows with facets and gauges. No longer loaded. Delete with `#results-view`. |
| **Atmosphere** (`atmosphere.js`) | **LIVE** | time-of-day sky behind glass. The product's identity; keep. |
| **Rejections** | **NONE** | `/api/agent` returns them, nothing renders them, and the prompt says they matter as much as the five. Cheapest real win available. |
| **Live worklog (SSE)** | **PLANNED** | `turn()` already collects the trace per tool call and holds it to the end. ~40 lines of client JS. |
| **The full redesign** | **PLANNED** | "the round" — prototype at `_private/design-proto/index.html`, brief alongside it. |

## 6. API surface

| endpoint | status | notes |
| --- | --- | --- |
| `POST /api/score` | **LIVE** | the free deterministic floor. No API key. A README promise — do not break it. |
| `POST /api/candidates` | **LIVE** | hybrid retrieval for an AI consumer. Wider, no scoring, longer bodies. |
| `POST /api/chat` | **BUILT** | the tool loop. Metered. Took the name the old config turn had. |
| `POST /api/prefetch` | **LIVE** | warms the live-fetch cache so a search feels instant. |
| `GET /api/health`, `/api/meta` | **LIVE** | `pool_size` vs `snapshot_count` is the regression that matters. |

## 7. Deploy and operations

| part | status | notes |
| --- | --- | --- |
| **Blue-green slots** | **LIVE** | two warm uvicorns, Caddy routes the active one. `/etc/jobfitr/active-slot` is the truth. |
| **`deploy-slot.sh` / `flip.sh`** | **LIVE** | `uv sync --frozen` — replays the lockfile, never re-solves. |
| **`verify-slot.sh`** | **LIVE** | the gate. Non-zero means do not flip. |
| **Nightly timers** | **LIVE** | resolve 03:00 · harvest 04:00 · evict 05:00 UTC, spaced an hour apart deliberately. |
| **Unit-file installs** | **PARTIAL** | `deploy-slot.sh` never installs unit files or `/opt/jobfitr/bin/*`. Those need a manual `install` + `daemon-reload` or they silently never take effect. |
| **Failure notification** | **PARTIAL** | `OnFailure=` appends to `failures.log`. Nothing reads it; nothing alerts. |

## 8. Observability

| part | status | notes |
| --- | --- | --- |
| **Search log** (`searchlog.py`) | **LIVE** | one JSONL line per search, no identifiers of any kind — asserted by a test that reads the function signature. |
| **Digest** (`review_searches.py`) | **BUILT** | run by hand. |
| **Agent observability** | **NONE** | the agent's searches, reads and picks are not logged anywhere. `searchlog.record` has no notion of them. **The real-user signal for the new product does not exist.** |
| **Application logging** | **PARTIAL** | handler at WARNING, `jobfitr` tree at INFO. Nothing ships logs off the box. |

## 9. Evaluation

| part | status | notes |
| --- | --- | --- |
| **Test suite** | **LIVE** | 637 passing, ~11 s. `pytest -m slow` for the full-corpus sweep. `ruff` is separate — a green pytest says nothing about lint. |
| **Scoreboard goldens** | **STALE** | 234 hand-computed goldens for a scoreboard the product no longer uses. |
| **57 synthetic profiles** | **PARTIAL** | exist, never graded against the current corpus. Grading them is what turns n=1 into n=57. |
| **Graded bed** | **PARTIAL** | 400 live postings judged 0-3. **Measures reranking, not retrieval** — a bag-of-words query reaches 94% of it. |
| **Model bakeoff** | **PLANNED** | protocol pre-registered and gate-passed at `_private/retrieval/bakeoff/`. Never run. |
| **Front-end tests** | **PARTIAL** | `facets.test.mjs` covers the old board's facet logic. Nothing tests the new chat. |

## 10. Documentation

| part | status | notes |
| --- | --- | --- |
| `README.md` | **STALE** | describes the scored-board product. |
| `CLAUDE.md` | **PARTIAL** | updated for the semantic arm and prompts; still describes the scoreboard as the product. Gitignored. |
| `HANDOFF.md` | **LIVE** | current as of 2026-08-19 21:58 ET. Gitignored, imported by `CLAUDE.md`. |
| `deploy/README.md` | **PARTIAL** | predates the `semantic` extra and the vector store. |
| **This file** | **LIVE** | — |

---

## The honest summary

**Retrieval is finished and running.** Ingestion is finished apart from one upstream bug
already handed off. **The conversation is built but unreachable**, and **the web app is still
the old product** — a visitor today gets four questions and a scored board that the rest of
the system no longer produces.

The three gaps that are absences rather than decisions:

1. **No nightly re-embed** — new jobs have no vector until someone runs a command by hand.
2. **No refinement loop** — the thing that makes a conversation worth having.
3. **No observability on the agent** — the new product cannot be measured against real use.
