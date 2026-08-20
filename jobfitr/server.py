"""The web API — a live-fetch-on-search hybrid over a SQLite/FTS5 store.

A search either serves a FRESH cache (a title|location fetched < TTL ago → zero API
calls) or does a bounded LIVE fetch (Adzuna + USAJOBS, ~1-2s, single-flighted so
concurrent identical searches share one upstream call), then ranks the store with
FTS5 BM25 + a personalized rerank. The daily-fetch ceiling load-sheds to the cache
with a `degraded` banner, so the free quota can never run away.

score_jobs is a sync def so FastAPI runs it in a threadpool — the blocking live
fetch never stalls the event loop, and live.coalesced_fetch (threading) coalesces.
The only metered LLM path is /api/chat.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from job_radar.util import age_int
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import chat as chatmod
from . import live, semantic, snapshot, store, vectors
from .config_builder import _clean_list, filters_from_dict, search_inputs
from .match import has_term, norm_key
from .snapshot import load_dotenv

_ET = ZoneInfo("America/New_York")

# Local dev: pull ./.env into os.environ so OPENROUTER_API_KEY (and the harvest keys)
# are present when the server is started directly (python -m jobfitr.server). In
# production these come from systemd's EnvironmentFile; load_dotenv only fills vars
# NOT already set, so it never overrides the deployed secrets — a safe no-op there.
load_dotenv()

log = logging.getLogger("jobfitr")

# MAKE THE DETECTORS THIS REPO ALREADY WROTE ACTUALLY EMIT.
#
# Nothing in this app configured logging, so the root logger sat at WARNING and every
# `log.info` was a no-op — including `chat._needs_related`'s, whose own comment says "logging
# it is what makes the miss countable; silently backfilling would hide how often the model
# ignores the instruction". Review checked: 0 matches for that line in the journal across 17
# /api/chat requests, while the search log independently proves `related_titles` was empty on
# 20 of 20 real searches. A 100% failure rate was invisible because the instrument built to
# count it could not speak.
#
# Deliberately narrow: `basicConfig` at WARNING only guarantees a handler EXISTS (it is a
# no-op when uvicorn has already installed one), and only the `jobfitr` tree is raised to
# INFO. Raising root would turn on INFO for httpx, urllib3 and friends and bury the signal
# in the noise, which is its own way of making a detector useless.
logging.basicConfig(level=logging.WARNING)
log.setLevel(os.environ.get("JOBFITR_LOG_LEVEL", "INFO").upper())

# Build the SQLite store schema + pull in the current harvest snapshot.
store.init()

# How often a running slot re-checks the shared jobs.json for a newer harvest.
# The harvest is nightly, so this is a cheap stat() ~96x/day; the import itself only runs
# when the mtime actually moved — EXCEPT for a misshapen snapshot, which never records an
# mtime on purpose and so is re-read on every tick until a good harvest replaces it. That is
# the intended trade: 96 loud retries a day beats one silent permanent freeze.
# 0 disables the poller (tests, one-off runs).
SNAPSHOT_SYNC_SECONDS = int(os.environ.get("JOBFITR_SNAPSHOT_SYNC_SECONDS", "900"))
_sync_stop = threading.Event()


def _snapshot_sync_loop() -> None:
    """Re-import the harvest snapshot on an interval, off the request path.

    Without this a slot only picked up new jobs on restart: the nightly harvest rewrites the
    shared jobs.json, and the store has to be told. Doing it on a background thread (not
    lazily in a request) means no user ever eats the multi-second import.

    OFF THE REQUEST PATH IS LOAD-BEARING, and more so since 2026-08-17. A misshapen snapshot
    now raises rather than returning zero, and the mtime is deliberately NOT recorded, so a
    bad file is retried indefinitely — correct, because recording it would freeze the pool
    permanently while /api/health stayed green. Being on a 900s timer is what makes
    "retry indefinitely" cheap: one streamed parse every 15 minutes with a loud log, not a
    per-request CPU pin. Do not move this onto a request.

    (The old note here said "each slot serves its OWN SQLite store". That stopped being true
    on 2026-08-17 — there is one shared store, jobs-v<schema>.db, and both slots read it.)
    """
    while not _sync_stop.wait(SNAPSHOT_SYNC_SECONDS):
        try:
            n = store.sync_snapshot()
            if n:
                log.info(
                    "snapshot sync: imported %d jobs (pool %d)", n, store.pool_size()
                )
        except Exception:  # noqa: BLE001 — a sync failure must never kill the server
            log.exception("snapshot sync failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = None
    if SNAPSHOT_SYNC_SECONDS > 0:
        thread = threading.Thread(
            target=_snapshot_sync_loop, name="jobfitr-snapshot-sync", daemon=True
        )
        thread.start()
    try:
        yield
    finally:
        _sync_stop.set()
        if thread:
            thread.join(timeout=5)



# ── the scoreboard is GONE, and this note is the marker ──────────────────────
# Deleted 2026-08-20 along with /api/score, match.py's title tiers, the 234 goldens and
# the board UI — ~3,000 lines. It computed `points = title tier + Σ boosts − penalties`,
# an integer a person could read off a card and check, and it was careful work.
#
# It went because the product no longer HAS a score. A model reads the postings and says
# why; a second, contradictory answer to "how does jobfitr decide what is good" is what
# made the codebase hard to read.
#
# The principle survived the implementation. The scoreboard's point was that a ranking
# should be checkable — nothing earned except by evidence, and evidence only ever adds.
# The conversation keeps that by showing its reasoning and naming what it rejected.
#
# What did NOT go: `match.has_term`, because `_eligible` filters exclusions with it and
# that runs on every /api/candidates request. Two functions out of a 404-line module.
#
# It is in git. Do not re-add it without a reason the conversation cannot serve.

RELATED_IN_RETRIEVAL = os.environ.get("JOBFITR_RELATED_IN_RETRIEVAL", "1") != "0"

# How many candidates a MODEL reads. The only delivery cap the server still imposes —
# /api/score's RESULT_CAP went with the scoreboard.
# 50 is the starting point rather than a settled answer — how many candidates a model
# should read is the one question today's measurements could not resolve, because on a
# 363-row test bed k=200 is 55% of the corpus and 0.33% in production.
CANDIDATE_CAP = int(os.environ.get("JOBFITR_CANDIDATE_CAP", "50"))
CANDIDATE_MAX = int(os.environ.get("JOBFITR_CANDIDATE_MAX", "200"))
# A model that has to judge fit needs the responsibilities, which routinely sit past the
# first screenful. Still capped — 50 rows x 4,000 chars is ~50k tokens, a cent on a cheap model.
CANDIDATE_CHARS = int(os.environ.get("JOBFITR_CANDIDATE_CHARS", "4000"))

CHAT_RATE_LIMIT = os.environ.get("CHAT_RATE_LIMIT", "20/minute")
SCORE_RATE_LIMIT = os.environ.get("SCORE_RATE_LIMIT", "40/minute")
# Daily cap on live keyed-source fetches (Adzuna + USAJOBS + Google/SerpApi) — the
# actuator saturation. When tripped, we serve the cache with a `degraded` banner
# instead of burning a free quota. Default is UNDER Adzuna's ~250/day free tier so
# the valve fires before the real quota is blown; raise the env to match a higher
# plan. (Env name kept as ADZUNA_DAILY_CEILING so the deployed box's config still
# applies; it now governs all three keyed sources, not just Adzuna.)
ADZUNA_DAILY_CEILING = int(os.environ.get("ADZUNA_DAILY_CEILING", "200"))

app = FastAPI(title="jobfitr", version="0.1.0", lifespan=lifespan)

# The scoring response is repetitive JSON, which is what gzip is best at: the measured
# 50-row payload is 55 KB raw and 13 KB compressed, so at a 200-row cap this is the
# difference between shipping ~220 KB and ~52 KB per search. Two lines, and it is what
# makes a bigger board cheap enough to hand a phone on cell data.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Per-IP rate limiting.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── daily live-fetch ceiling (the load-shed) ──────────────────────────────────
# The tally is persisted in the store (store.live_fetch_count/note_live_fetch), so a
# restart or crash cannot reset it and defeat the ceiling. Only the last-success
# timestamp stays in-process — it's a display value, fine to be per-process.
_last_fetch_ok = {"at": None}


def _today() -> str:
    return datetime.now(_ET).date().isoformat()


def _fetch_ceiling_reached() -> bool:
    return store.live_fetch_count() >= ADZUNA_DAILY_CEILING


def _note_fetch() -> None:
    store.note_live_fetch()
    _last_fetch_ok["at"] = datetime.now(_ET).isoformat(timespec="seconds")


# Greenhouse (and most ATS feeds) ship the JD as HTML. Rendered with textContent the
# markup came through as literal text on the card — the top result opened with
# '<div class="content-intro"><h3>About Arize</h3>'. Cleaning it here rather than in the
# client fixes every consumer at once and shrinks the payload.
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAG_RE = re.compile(r"<[^>]+>")
# A tag needs a closing '>' to match above, and the harvest caps body text at 8,000
# chars — so a body cut mid-tag ends with an UNTERMINATED one that survived the strip
# and rendered as literal markup on the card ('<a href="https://www.cnbc.com/2022/05').
# Anchored at the end AND requiring a tag name right after the '<', so a real
# less-than survives: "a < b" has a space next and is left alone, as is a bare
# trailing '<'.
_DANGLING_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*$")


def _plain_text(text) -> str:
    """HTML (or already-plain text) → clean display text.

    Tags become a SPACE, not nothing: '<p>One</p><p>Two</p>' must read "One Two", never
    "OneTwo". Entities are decoded after stripping, so text that was escaped in the
    source ('&lt;b&gt;') stays literal instead of becoming markup.
    """
    if not isinstance(text, str):
        return ""
    s = _SCRIPT_RE.sub(" ", text)  # drop script/style bodies outright
    s = _TAG_RE.sub(" ", s)
    s = _DANGLING_TAG_RE.sub(" ", s)  # a body truncated mid-tag leaves an unclosed one
    s = html.unescape(s)  # &amp; &nbsp; &#8217; …
    return " ".join(s.split())  # collapses \xa0 too — it is whitespace to str.split






# ── facet normalization ───────────────────────────────────────────────────────
# A filter chip is a PROMISE that picking it shows you every matching job. Raw source
# values break that promise: the live pool carries "full_time", "Full Time", "Full-Time"
# and "Full-time" as four separate chips over what is one thing (621 rows split four
# ways), plus five spellings of contract. Both fields are also used as dumping grounds
# for free text — schedule prose ("Monday-Friday 8:00am-4:30pm"), German seniority
# tokens, and whole sentences — which are not types at all.
#
# So this is a WHITELIST, not a cleanup: a value that does not map to a known token is
# dropped rather than surfaced. An unfilterable chip is worse than no chip.
_EMPLOYMENT_ALIASES = {
    "full time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
    "part time": "part_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "contract long": "contract",
    "contract short": "contract",
    "temporary": "temporary",
    "temp": "temporary",
    "internship": "internship",
    "intern": "internship",
    "freelance": "freelance",
    "seasonal": "seasonal",
    "flex time": "flexible",
    "flextime": "flexible",
    "flexitime": "flexible",
}

# `category` used to be cleaned here by a DENYLIST — regexes for USAJOBS agency names
# ("Department of the Navy" — an EMPLOYER, not a field), a seniority ("Mid-Senior
# Level"), an ATS code ("220 - Solutions PS"), plus a 40-character length cap. It is
# gone because jobfitr.vocab replaced it with an ALLOWLIST at the store boundary: 22
# canonical fields, and NULL for everything else.
#
# That is strictly better and the reason is structural, not stylistic. A denylist has
# to enumerate every bad value and passes anything it forgot — it let through "Solutions
# PS", "Engineering - Pipeline", "Go To Market", 2,239 distinct strings in all. An
# allowlist can only ever emit one of 22 known-good values, so the failure mode flips
# from "a garbage chip appears in the Field drawer" to "a real field is missing", which
# is visible, countable, and fixed by adding one line to vocab._CATEGORY_MAP.


def _norm_employment_type(value) -> str:
    """Canonical employment-type token, or '' when the value is not a type at all."""
    key = " ".join(
        "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split()
    ).lower()
    return _EMPLOYMENT_ALIASES.get(key, "")






def _shape_candidate(c: dict) -> dict:
    """A candidate row for a MODEL to read — facts plus enough body to judge fit.

    Not `_shape`: that builds a card (points, parts, tags, facets) and truncates the body
    to 1,200 characters. Nothing here is scored, because on this path the model does the
    judging and a number it did not compute would only mislead it.
    """
    body = c.get("body") or c.get("text") or ""
    return {
        "url": c.get("url", ""),
        "title": c.get("title", ""),
        "company": c.get("company", ""),
        "location": c.get("location", ""),
        "remote": c.get("remote") or "",
        "salary": c.get("salary", ""),
        "salary_min": store.annual_salary(c),
        "posted": c.get("posted", ""),
        "source": c.get("source", ""),
        "seniority": c.get("seniority") or "",
        "employment_type": _norm_employment_type(c.get("employment_type")),
        "description": _plain_text(body)[:CANDIDATE_CHARS],
    }


def _eligible(candidates: list, exclude, remote_only: bool, max_age_days) -> list:
    """The user's three HARD filters — terms they said to hide, remote-only if they
    chose it, their own recency preference. Everything else is ranking, not filtering.

    THIS MUST RUN BEFORE `_dedupe_listings`, AND THAT ORDER IS A BUG FIX, NOT A STYLE
    CHOICE. Dedupe collapses on (company, title) and keeps the row with the longest
    body — a tiebreak that knows nothing about whether the survivor is one the user can
    actually receive. Filtering afterwards therefore had a hole with no error in it: a
    REMOTE listing whose non-remote twin carried more text was discarded by dedupe, and
    the surviving twin was then removed by the remote-only filter. The job vanished from
    a search that had asked for exactly it.

    Measured on the frozen corpus: **42 remote listings erased across 10 of the 20
    remote-only profiles**, including the owner's own daily search. The same hole exists
    for `exclude` and `max_age_days` — it is a property of the ordering, not of remote —
    which is why the fix is to filter first rather than to push one predicate into SQL.

    Idempotent, so `_rank` calls it again harmlessly and there is no second copy of a
    predicate to drift.
    """
    out = []
    for c in candidates:
        title = (c.get("title") or "").lower()
        # Exclusions test the COMPANY as well as the title: "recruiting agency" is an
        # employer trait, not a job trait, so a title-only test let a staffing firm's
        # normal-sounding listings through under the very term meant to remove them.
        #
        # WHOLE-WORD, not substring. This read `x in blob` — the exact substring test
        # deliberately removed from scoring, still living in the one path that DELETES a
        # listing rather than ranking it. Measured: 220 of 2,573 exclusions (8.6%) were
        # false hides — `intern` deleting "Software Engineer, Internal Systems", `sales`
        # deleting "Sr. Forward Deployed Engineer - Salesforce Health Cloud", 13 of them
        # exact title matches. Hiding a job is the most destructive thing this code does
        # and it was the least precise test in it.
        if any(
            has_term(x, f"{title} {(c.get('company') or '').lower()}") for x in exclude
        ):
            continue
        if remote_only and c.get("remote") != "remote":
            continue
        # `None` means the user never expressed a recency preference, so we do not
        # invent one. The old ladder always imposed an age — 90 days at its loosest —
        # which meant nothing older was reachable no matter what anyone wanted.
        if max_age_days is not None:
            age = age_int(c.get("posted", ""))
            if age is not None and age > max_age_days:
                continue
        out.append(c)
    return out


def _dedupe_listings(candidates: list) -> list:
    """Collapse rows that are the same job posted twice, keeping the richest one.

    RUN `_eligible` FIRST — see its docstring. The longest-body tiebreak below is blind
    to the user's filters, so deduping an unfiltered set silently deletes jobs.

    The same opening reaches the pool from more than one source (an aggregator and the
    employer's own ATS), and the two rows are rarely byte-identical — different URLs,
    different salary formatting — so the store cannot dedup them on a key. To a user
    they are simply the same job listed twice, which reads as a broken search: the live
    run showed one role at both #17 and #18 and another at #5 and #6.

    Identity is (normalized company, normalized title). Where duplicates disagree we
    keep the row with the LONGEST body, because body text is what the rerank and the
    card's description bullets both feed on — keeping the thin copy would throw away
    evidence and, under the boost scoring above, quietly change where the job ranks.
    """
    best: dict[tuple[str, str], int] = {}
    out: list = []
    for c in candidates:
        key = (norm_key(c.get("company")), norm_key(c.get("title")))
        if not any(key):  # no company AND no title — nothing to dedup on
            out.append(c)
            continue
        idx = best.get(key)
        if idx is None:
            best[key] = len(out)
            out.append(c)
        elif len(c.get("body") or "") > len(out[idx].get("body") or ""):
            out[idx] = c
    return out






def _warm_cache(titles: list, location: str) -> str | None:
    """Ensure the store holds fresh jobs for this (titles, location): serve the fresh
    cache untouched, or do ONE bounded live fetch (Adzuna + USAJOBS, single-flighted).

    Returns a `degraded` reason (or None). Idempotent + coalesced, so calling it early
    from /api/prefetch and again from /api/score costs at most one upstream fetch —
    mark_fetched makes the second call see a fresh cache.
    """
    if not titles:
        return None
    key = store.search_key(titles, location)
    if store.search_fresh(key):
        return None  # fresh (< TTL) — no API call
    if _fetch_ceiling_reached():
        return "live_search_limit"  # load-shed: serve the cache
    try:
        rows = live.coalesced_fetch(
            titles, location
        )  # blocking (threadpool), single-flight
        if rows:
            store.upsert_jobs(rows)
            # EMBED AT INTAKE. A nightly pass cannot keep up with this lane: it runs all
            # day and its rows are the LOCAL ones. Measured 2026-08-20, before this
            # existed — 1,929 of 1,998 unvectorised rows were adzuna, and 97% of every
            # Louisville/KY row in the pool had no vector, so hybrid retrieval silently
            # collapsed to lexical-only exactly where local search lives.
            # Cost is why there is no argument for deferring it: Model2Vec is a table
            # lookup with no forward pass — 7,240 rows/s measured, 1 ms for five rows once
            # the model is warm — so a 177-row fetch is tens of ms against a fetch that
            # just spent ~22 s. It reuses semantic's already-resident model rather than
            # building its own, because from_pretrained can reach the network and was
            # measured taking 217 SECONDS on a cold HuggingFace fetch. embed_urls never
            # raises: a row that misses its vector is a slightly worse search, not a 500.
            n = vectors.embed_urls([r.get("url") for r in rows])
            if n:
                # The in-process matrix is a snapshot; without this the new vectors are on
                # disk and invisible until the next restart. Measured 63 ms to reload.
                semantic.reset_cache()
                log.info("embedded %d new row(s) at intake", n)
        store.mark_fetched(key)
        _note_fetch()
        # A fetch that SUCCEEDED can still be short a source. Google for Jobs is the only
        # lane reaching the Workday/iCIMS postings large local employers use — measured
        # on one Louisville search, 14 of its 17 employers appeared in no other source —
        # so losing it quietly costs a local searcher most of the real employers on their
        # board while everything reads healthy. Say so instead.
        why = live.last_source_report().get("google_jobs", {}).get("why", "")
        if why == "quota":
            return "monthly_source_limit"
        return None
    except Exception:  # noqa: BLE001
        return "fetch_error"  # serve whatever's cached






@app.post("/api/candidates")
@limiter.limit(SCORE_RATE_LIMIT)
def candidates_endpoint(request: Request, payload: dict = Body(...)) -> dict:
    """The AI path's retrieval: two arms, fused, pre-filtered, handed to a model to read.

    SEPARATE FROM /api/score ON PURPOSE. That endpoint is the free deterministic floor —
    it works with no API key, its number is absolute and explainable, and the README
    promises it. This one answers a different question ("give a model enough good rows to
    pick five from") and so wants a different contract: wider, no scoreboard, no facets,
    and bodies long enough to judge fit from.

    THE ORDER IS THE POINT. The hard filters run BEFORE either arm, not after. A dense KNN
    returns a fixed top-k, so filtering the result hands the model 18 rows where it asked
    for 50; filtering the corpus first spends the whole budget on rows the user can
    receive. Measured over a graded set, pre-filtering on stated dealbreakers keeps 91% of
    rows and 97% of the relevant ones.

    Both arms retrieve over the SAME filtered pool and are fused with RRF. Measured
    2026-08-19 over 4,000 live postings: of 50 delivered candidates, 16 came from both
    arms, 17 from lexical only and 17 from the dense arm only — two thirds of the set does
    not exist under a single arm, which is the whole argument for running two.

    Degrades to lexical-only whenever the dense arm is unavailable (no vector store, no
    model2vec installed, a corrupt matrix). The lexical arm is the floor.
    """
    f = filters_from_dict(payload, [])
    titles, location = search_inputs(payload)
    probes = _clean_list(payload.get("probes"))
    k = max(1, min(int(payload.get("k") or CANDIDATE_CAP), CANDIDATE_MAX))
    related = _clean_list(payload.get("related_titles"))
    raw_age = payload.get("max_age_days")
    max_age_days = (
        int(raw_age)
        if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool)
        else None
    )
    degraded = _warm_cache(titles, location)
    search_titles = titles + related if RELATED_IN_RETRIEVAL else titles

    # PRE-FILTER, then retrieve. `_eligible` is the same predicate /api/score applies, so
    # the two endpoints cannot disagree about who is eligible for what.
    pool_rows = _dedupe_listings(
        _eligible(
            store.bm25_candidates(search_titles),
            f.exclude_titles,
            f.remote_only,
            max_age_days,
        )
    ) if search_titles else []
    lexical_urls = [c.get("url", "") for c in pool_rows if c.get("url")]
    by_url = {c["url"]: c for c in pool_rows if c.get("url")}

    fused = semantic.hybrid(
        lexical_urls, probes, k=k,
        per_company={u: (r.get("company") or "") for u, r in by_url.items()},
    )
    dense_only = [u for u in fused if u not in set(lexical_urls)]
    # A dense hit outside the lexical pool never passed _eligible, because _eligible only
    # ever saw the lexical arm's rows. Fetch and filter those before they reach anyone.
    if dense_only:
        extra = _eligible(
            store.rows_by_url(dense_only),
            f.exclude_titles,
            f.remote_only,
            max_age_days,
        )
        by_url.update({c["url"]: c for c in extra if c.get("url")})
    out = [by_url[u] for u in fused if u in by_url][:k]

    return {
        "count": len(out),
        "degraded": degraded,
        "semantic": semantic.available(),
        "pool": store.pool_size(),
        "jobs": [_shape_candidate(c) for c in out],
    }


@app.post("/api/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def chat_endpoint(request: Request, payload: dict = Body(...)) -> dict:
    """THE conversation. A model interviews, then drives its own search loop.

    This replaced a structured turn that filled a CONFIG (titles, boosts, exclude,
    rank_down) which ran ONE keyword search against a scoreboard. The run that produced
    five jobs Justin confirmed were right ran NINE searches, each chosen from what the
    previous returned — and that difference, one query versus an adaptive loop, is the
    product. The old design is deleted, not deprecated: it is in git if it is ever wanted.

    Bounded by the TOOL budget rather than the turn count, because a model that picks its
    own searches can pick them forever.

    Fails CLOSED: no key or ceiling reached returns 503 and the UI says so.
    """
    if not chatmod.available():
        raise HTTPException(status_code=503, detail="agent_unavailable")
    if chatmod.daily_ceiling_reached():
        raise HTTPException(status_code=503, detail="daily_ceiling")
    messages = chatmod.sanitize_messages(payload.get("messages"))
    if not messages:
        raise HTTPException(status_code=422, detail="messages required")
    if chatmod.over_turn_cap(messages):
        raise HTTPException(status_code=429, detail="turn_cap")
    chatmod.note_request()
    try:
        return await chatmod.turn(messages, payload.get("model"))
    except Exception as e:  # noqa: BLE001 — upstream fault must not 500 the UI
        log.warning("chat turn failed: %s: %s", type(e).__name__, e)
        return {"reply": "", "trace": [], "error": f"upstream: {type(e).__name__}"}


def _code_sha() -> str:
    """The commit THIS process is running, resolved once at import.

    The server is the only thing that knows which checkout it was started from. A
    measurement harness reading git from its own directory records where the SCRIPT
    lives, not where the CODE came from — which silently mislabels every run made
    against a worktree, exactly the setup the three-version comparison depends on.
    """
    import subprocess

    try:
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(Path(__file__).parent.parent),
                    "rev-parse",
                    "--short",
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


CODE_SHA = _code_sha()


@app.get("/api/meta")
def meta() -> dict:
    """Pool freshness for the UI (how many jobs, newest posting) + which build is live."""
    return {
        "count": store.pool_size(),
        "harvested_at": store.newest_posted(),
        "code_sha": CODE_SHA,
    }


def _snap_meta() -> dict:
    """The current snapshot's meta block, or an empty one.

    Streams just `meta` (see snapshot.load_meta). This used to call `load_snapshot`, which
    parsed and then permanently cached the entire 363 MB document to read five numbers —
    1,168 MB per web process, measured. Note health() calls this THREE times per request, so
    it must stay cheap; the mtime cache makes the repeat calls free.
    """
    try:
        return snapshot.load_meta(store.JOBS_JSON_PATH) or {}
    except Exception:  # noqa: BLE001 — health must answer even with no snapshot
        return {}


@app.get("/api/health")
def health() -> dict:
    """Status for you + an uptime monitor: which feeds are live, budget used, freshness."""
    return {
        "ok": True,
        "adzuna_ok": bool(
            os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")
        ),
        "openrouter_ok": chatmod.available(),
        "daily_fetches_used": store.live_fetch_count(),
        "daily_fetch_ceiling": ADZUNA_DAILY_CEILING,
        "pool_size": store.pool_size(),
        # THE VECTOR GAP, and it has to be a NUMBER. `semantic` below only reports that
        # the file EXISTS — it cannot tell a current store from one built in June, which
        # is how the arm went dark on 97% of Louisville rows with every dashboard green.
        # unvectorised > 0 and rising means the nightly timer is not running.
        "vectors": vectors.vector_count(),
        "unvectorised": max(0, store.pool_size() - vectors.vector_count()),
        # The size of the harvest snapshot this slot should be serving. The pool is
        # that snapshot plus live-fetch accumulation, so pool_size < snapshot_count
        # means the slot UNDER-ingested — the exact regression verify-slot.sh gates on
        # with a ratio, instead of a fixed floor that a 90%-smaller harvest slips past.
        #
        # BOTH counts are published because the gate needs the SERVABLE one. `count` is
        # every harvested row; `servable_count` is how many survive US-only intake, and
        # the pool can only ever contain the latter. Comparing pool_size against `count`
        # measured two different populations, which was fine while the filter dropped
        # ~18% and fatal the moment it did: on a freshly rebuilt slot the ratio lands
        # near 0.67 against a 0.70 floor and the gate blocks a healthy deploy.
        #
        # Falls back to `count` when a snapshot predates this field, so an older
        # jobs.json still gates on something rather than on zero.
        "snapshot_count": _snap_meta().get("count", 0),
        "snapshot_servable": _snap_meta().get(
            "servable_count", _snap_meta().get("count", 0)
        ),
        "last_successful_fetch": _last_fetch_ok["at"],
        # The harvest snapshot this slot has actually ingested. Stale here means the
        # slot is serving an aging pool even while the nightly harvest is green — the
        # exact failure the background sync exists to prevent, so it's worth surfacing.
        "snapshot_imported_at": store.snapshot_imported_at(),
    }


def _web_dir() -> Path:
    """The static front end lives in ./web at the repo root; overridable for deploy."""
    override = os.environ.get("JOBFITR_WEB_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web"


# Serve the front end at / — mounted LAST so the /api/* routes above still win.
# Guarded so the API still boots headless (e.g. before the front end exists).
_WEB = _web_dir()
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")


def main(argv=None) -> int:  # pragma: no cover — exercised via jobfitr-serve
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(
        prog="jobfitr-serve", description="Run the jobfitr API locally."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    uvicorn.run(
        "jobfitr.server:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
