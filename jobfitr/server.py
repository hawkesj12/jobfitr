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
from . import live, snapshot, store
from .config_builder import _clean_list, config_from_dict, search_inputs
from .snapshot import load_dotenv

_ET = ZoneInfo("America/New_York")

# Local dev: pull ./.env into os.environ so OPENROUTER_API_KEY (and the harvest keys)
# are present when the server is started directly (python -m jobfitr.server). In
# production these come from systemd's EnvironmentFile; load_dotenv only fills vars
# NOT already set, so it never overrides the deployed secrets — a safe no-op there.
load_dotenv()

log = logging.getLogger("jobfitr")

# Build the SQLite store schema + pull in the current harvest snapshot.
store.init()

# How often a running slot re-checks the shared jobs.json for a newer harvest.
# The harvest is nightly, so this is a cheap stat() ~96x/day; the import itself only
# runs when the mtime actually moved. 0 disables the poller (tests, one-off runs).
SNAPSHOT_SYNC_SECONDS = int(os.environ.get("JOBFITR_SNAPSHOT_SYNC_SECONDS", "900"))
_sync_stop = threading.Event()


def _snapshot_sync_loop() -> None:
    """Re-import the harvest snapshot on an interval, off the request path.

    Without this a slot only picked up new jobs on restart: the nightly harvest
    rewrites the SHARED jobs.json, but each slot serves its OWN SQLite store. Doing
    it on a background thread (not lazily in a request) means no user ever eats the
    multi-second import.
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


DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SNIPPET_CHARS = 240
DESC_CHARS = 1200
CANDIDATE_LIMIT = 500  # top-N BM25 candidates fetched before the personalized rerank

# Rerank weights (in BM25 units, ~0-5): a boost match nudges up, a rank_down sinks.
# BOOST_MAX is the TOTAL boost swing available to any one listing, no matter how many
# boosts the user entered. A flat per-match bonus made the swing scale with the boost
# count (nine boosts = up to +18), which swamped BM25 relevance entirely and let a
# keyword-stuffed off-title listing outrank an exact-title one. Capping the total keeps
# boosts a nudge on top of relevance instead of a replacement for it.
BOOST_MAX = 4.0
# A boost in the TITLE is strong evidence; one buried in the body is weaker.
TITLE_CREDIT = 1.0
BODY_CREDIT = 0.5
# A listing with NO body is missing evidence, not lacking it. Scoring an absent body as
# zero matches punished whole sources (Greenhouse rows arrive body-less) for something
# they never had a chance to carry, so the unobserved portion is imputed at a neutral
# rate rather than counted against them.
NO_BODY_PRIOR = 0.5
PENALTY_W = 3.0
# min_score keyword → keep candidates scoring >= frac × the top result's score.
MIN_SCORE_FRAC = {"plenty": 0.0, "balanced": 0.35, "strong": 0.6}

# Deterministic freshness/pickiness ladder — replaces the "how picky?" + recency
# questions. Start tight (fresh + strong), relax only as far as needed to reach TARGET
# results. (max_age_days, min_score), tight → loose.
#
# TARGET_RESULTS and RESULT_CAP are two different jobs that used to be one number, and
# conflating them capped the board at 50 rows:
#
#   TARGET_RESULTS — the SUFFICIENCY bar. "Has this tier found enough?" Raising it does
#   not give you more results, it makes the ladder relax further to hit a bigger number
#   — trading fresh+strong for old+weak. It stays at 50 on purpose.
#
#   RESULT_CAP — the DELIVERY slice. How many of the winning tier's rows we actually
#   hand the client. Nothing about tier selection changes when this moves; the same
#   tier wins, we just stop throwing away its tail.
#
# The tail is what the client-side filters eat. A user who filters 50 rows by salary
# and work style is left with a handful; the same filters over 200 still leave a board.
TARGET_RESULTS = 50
RESULT_CAP = int(os.environ.get("JOBFITR_RESULT_CAP", "200"))
# Most results one employer may contribute to the front of the board. See
# _spread_companies — this is a reordering, not a filter, so nothing is ever hidden.
MAX_PER_COMPANY = int(os.environ.get("JOBFITR_MAX_PER_COMPANY", "4"))
RESULT_LADDER = [
    (15, "strong"),
    (30, "strong"),
    (30, "balanced"),
    (60, "balanced"),
    (90, "plenty"),
]

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
# 50-row payload is 55 KB raw and 13 KB compressed, so at RESULT_CAP=200 this is the
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
# A tag needs a closing '>' to match above, and the harvest caps body text at ~2000
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


def _snippet(text) -> str:
    return _plain_text(text)[:SNIPPET_CHARS]


def _description(text) -> str:
    """A fuller (but still capped) JD body for the expand-to-detail view.

    Still served from the cached snapshot — never a live fetch. The full untruncated
    body is never returned; the harvest already caps text at ~2000 chars.
    """
    return _plain_text(text)[:DESC_CHARS]


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

# `category` drives the "Field" facet, so it must be a job FUNCTION. Three other kinds
# of value leak in: USAJOBS agency names (~550 rows of "Department of the Navy" — an
# EMPLOYER, not a field), a seniority ("Mid-Senior Level"), and one employer's internal
# ATS codes ("220 - Solutions PS"). Agencies are dropped rather than renamed because
# there is no employer facet to move them to; adding one is a separate feature.
_CATEGORY_CODE_RE = re.compile(r"^\d+\s*[-–—]\s*")
_CATEGORY_DENY_RE = re.compile(
    r"(?i)^(department of|office of|.*\bagencies\b|legislative branch|judicial branch|"
    r"mid[- ]senior level|entry level|executive|associate|director|not applicable)"
)
_CATEGORY_MAX_LEN = 40


def _norm_employment_type(value) -> str:
    """Canonical employment-type token, or '' when the value is not a type at all."""
    key = " ".join(
        "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split()
    ).lower()
    return _EMPLOYMENT_ALIASES.get(key, "")


def _norm_category(value) -> str:
    """A job-function category, or '' when the value is an employer/level/free text."""
    s = _plain_text(value)  # decodes 'Legal &amp; Compliance' → 'Legal & Compliance'
    s = _CATEGORY_CODE_RE.sub("", s).strip()  # '220 - Solutions PS' → 'Solutions PS'
    if not s or len(s) > _CATEGORY_MAX_LEN or _CATEGORY_DENY_RE.match(s):
        return ""
    return s


# The gauge is explicitly "relative to your best match", so it is normalized ACROSS the
# returned set rather than divided by the top score. The old ratio (score / top) died
# whenever the top score was <= 0, which is the normal case for a common one-word query:
# BM25 rates fifty jobs all titled "...Engineer" as equally relevant — correctly — so
# every card floored at the minimum and the board rendered fifty identical "3 · Fair"
# rows. Spreading the real spread over a readable band keeps the ranking legible without
# inventing one: when there is genuinely no spread, every card shows the same value,
# because they genuinely are the same match.
_FIT_FLOOR = 45  # the weakest SHOWN match still cleared the ladder, so it is not a 3
_FIT_FLAT = 60  # no spread at all — honest neutral, not a fake gradient


def _fit_pcts(scores: list) -> list:
    """Map the kept set's scores onto the 0-100 gauge, preserving relative spacing."""
    if not scores:
        return []
    hi, lo = max(scores), min(scores)
    span = hi - lo
    if span <= 0:
        return [_FIT_FLAT] * len(scores)
    return [
        max(3, min(100, round(_FIT_FLOOR + (100 - _FIT_FLOOR) * (s - lo) / span)))
        for s in scores
    ]


def _shape(c: dict, fit_score: int, why: str, fit_pct: int) -> dict:
    """The lean per-card payload the front end renders (store row → card)."""
    body = c.get("body") or c.get("text") or ""
    # the derived facet tags (real facets category/employment_type sit in their own keys)
    tags = [t for t in (c.get("remote"), c.get("seniority"), c.get("salary_band")) if t]
    return {
        "title": c.get("title", ""),
        "company": c.get("company", ""),
        "location": c.get("location", ""),
        "url": c.get("url", ""),
        "posted": c.get("posted", ""),
        "source": c.get("source", ""),
        "salary": c.get("salary", ""),
        "category": _norm_category(c.get("category")),
        "employment_type": _norm_employment_type(c.get("employment_type")),
        "tags": tags,
        "fit_score": fit_score,  # the reranked score (canonical)
        "fit_pct": fit_pct,  # derived gauge value (presentation only)
        "why": why,  # the title/boost signals that matched
        "snippet": _snippet(body),
        "description": _description(body),
    }


def _spread_companies(scored: list, cap: int | None = None) -> list:
    """Drop each employer's roles beyond `cap` so no one company monopolises the board.

    Ranking by score alone is fine when the biggest employer has a handful of roles.
    It stops being fine at scale: the pool carries employers with 900+ open jobs
    (Veterans Health Administration) and 600+ (Accenture Federal Services), and a
    title that matches them well would otherwise hand a user fifty near-identical rows
    from one company. That reads as a broken search, not a thorough one.

    This DROPS the overflow rather than demoting it. An earlier version pushed the
    excess to the back of the list, but the caller then truncates to a fixed window
    (`_rank` does `[:limit]`), and for a shallow query the truncation sliced straight
    back into that demoted overflow — so "nurse" showed 19 of 50 rows from one
    employer. Dropping is also what lets the RESULT_LADDER work: a capped set that
    comes up short is the honest signal that makes the ladder relax and pull a more
    diverse set, instead of being silently padded back to full by the dominant
    employer. A user who wants only that employer searches more specifically.
    """
    cap = MAX_PER_COMPANY if cap is None else cap
    if cap <= 0:
        return scored
    seen: dict[str, int] = {}
    keep = []
    for item in scored:
        company = (item[0].get("company") or "").strip().lower()
        n = seen.get(company, 0) + 1
        seen[company] = n
        if n <= cap:
            keep.append(item)
    return keep


def _norm_key(value) -> str:
    """Lowercase + collapse whitespace/punctuation, for identity comparison."""
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split()
    ).lower()


def _dedupe_listings(candidates: list) -> list:
    """Collapse rows that are the same job posted twice, keeping the richest one.

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
        key = (_norm_key(c.get("company")), _norm_key(c.get("title")))
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


def _boost_bonus(title: str, body: str, boosts: list) -> float:
    """The capped, evidence-weighted boost bonus for one listing.

    Returns a value in [0, BOOST_MAX] scaled by the FRACTION of the user's boosts the
    listing matches — not a flat sum per match — so adding more boosts sharpens the
    signal instead of inflating the ceiling.
    """
    terms = [x for x in boosts if x]
    if not terms:
        return 0.0
    has_body = bool(body.strip())
    credit = 0.0
    for x in terms:
        if x in title:
            credit += TITLE_CREDIT
        elif has_body and x in body:
            credit += BODY_CREDIT
        elif not has_body:
            credit += BODY_CREDIT * NO_BODY_PRIOR  # unobserved, not absent
    return BOOST_MAX * min(1.0, credit / len(terms))


def _rank(
    candidates,
    titles,
    boosts,
    penalties,
    exclude,
    min_score_key,
    remote_only,
    max_age_days,
    limit,
):
    """Personalized rerank over BM25 candidates: relevance + boosts − penalties,
    hard-filtered by exclude/remote/age, cut relative to the top score by pickiness."""
    scored = []
    why_terms = [t for t in (titles + boosts) if t]
    for c in candidates:
        title = (c.get("title") or "").lower()
        # Exclusions test the COMPANY as well as the title: "recruiting agency" is an
        # employer trait, not a job trait, so a title-only test let a staffing firm's
        # normal-sounding listings through under the very term meant to remove them.
        if any(x in f"{title} {(c.get('company') or '').lower()}" for x in exclude):
            continue
        if remote_only and c.get("remote") != "remote":
            continue
        age = age_int(c.get("posted", ""))
        if age is not None and age > max_age_days:
            continue
        body = (c.get("body") or "").lower()
        blob = f"{title} {body}"
        bonus = _boost_bonus(title, body, boosts)
        pen = sum(PENALTY_W for x in penalties if x and x in blob)
        final = float(c.get("bm25", 0.0)) + bonus - pen
        why = ", ".join([t for t in why_terms if t in blob][:4])
        scored.append((c, final, why))
    scored.sort(key=lambda t: t[1], reverse=True)
    scored = _spread_companies(scored)
    top = scored[0][1] if scored else 0.0
    floor = top * MIN_SCORE_FRAC.get(min_score_key, 0.35) if top > 0 else -1e18
    return [x for x in scored if x[1] >= floor][:limit], top


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
        store.mark_fetched(key)
        _note_fetch()
        return None
    except Exception:  # noqa: BLE001
        return "fetch_error"  # serve whatever's cached


@app.post("/api/prefetch")
@limiter.limit(SCORE_RATE_LIMIT)
def prefetch(request: Request, payload: dict = Body(...)) -> dict:
    """Warm the cache for a search-in-progress the moment titles + location are known,
    so the 3-4s live fetch overlaps the rest of the chat and /api/score is instant.
    Reuses _warm_cache — coalesced + mark_fetched dedup it against the later score."""
    titles, location = search_inputs(payload)
    degraded = _warm_cache(titles, location)
    return {"ok": degraded is None, "warmed": bool(titles), "degraded": degraded}


@app.post("/api/score")
@limiter.limit(SCORE_RATE_LIMIT)
def score_jobs(request: Request, payload: dict = Body(...)) -> dict:
    """Live-fetch (or serve the fresh cache) → BM25 candidates → personalized rerank.

    Runs as a sync def, so FastAPI executes it in a threadpool — the blocking live
    fetch never stalls the event loop, and live.coalesced_fetch (threading) coalesces
    concurrent identical searches. Degrades to the cache when the daily ceiling trips.
    """
    cfg = config_from_dict(payload)
    titles, location = search_inputs(payload)
    boosts = _clean_list(payload.get("boosts"))
    penalties = list(
        cfg.agency_penalty.keys()
    )  # user rank_down or the generic staffing terms
    exclude = list(cfg.exclude_titles)

    degraded = _warm_cache(titles, location)

    # Dedup ONCE here rather than inside _rank — the ladder below re-ranks this same
    # candidate set up to five times, so collapsing duplicates per pass would repeat
    # identical work for an identical result.
    candidates = (
        _dedupe_listings(store.bm25_candidates(titles, limit=CANDIDATE_LIMIT))
        if titles
        else []
    )
    # The deterministic ladder: start fresh + strong, relax only as far as needed to
    # reach TARGET_RESULTS. The first tier that clears the bar wins (freshest/strongest);
    # if none does, the loosest tier's set is kept. Cheap — a re-rank over the same pool.
    kept, top, tier = [], 0.0, RESULT_LADDER[-1]
    for max_age, min_key in RESULT_LADDER:
        kept, top = _rank(
            candidates,
            titles,
            boosts,
            penalties,
            exclude,
            min_key,
            cfg.remote_only,
            max_age,
            RESULT_CAP,  # deliver the tier's whole tail…
        )
        tier = {"max_age_days": max_age, "min_score": min_key}
        if len(kept) >= TARGET_RESULTS:  # …but judge sufficiency on the first 50
            break
    pcts = _fit_pcts([final for _, final, _ in kept])
    results = [
        _shape(c, round(final), why, pct)
        for (c, final, why), pct in zip(kept, pcts, strict=True)
    ]
    # Count facets over NORMALIZED rows so the counts match the chips the cards produce.
    # Normalizing on a copy (not the shaped result) keeps remote/seniority/salary_band,
    # which live as top-level columns here but are folded into `tags` by _shape.
    facets = store.facet_counts(
        [
            {
                **c,
                "category": _norm_category(c.get("category")),
                "employment_type": _norm_employment_type(c.get("employment_type")),
            }
            for c, _, _ in kept
        ]
    )
    return {
        "count": len(results),
        "degraded": degraded,
        "facets": facets,
        "pool": store.pool_size(),
        "tier": tier,
        "jobs": results,
    }


@app.post("/api/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def chat_endpoint(request: Request, payload: dict = Body(...)) -> dict:
    """One structured chat turn → {reply, config, ready}. The ONLY metered path;
    never touches scoring.

    Fails CLOSED to the form: a 503 (no key / daily ceiling) or 429 (rate/turn cap)
    tells the front end to fall back to the search form.
    """
    if not chatmod.chat_available():
        raise HTTPException(status_code=503, detail="chat_unavailable")
    if chatmod.daily_ceiling_reached():
        raise HTTPException(status_code=503, detail="daily_ceiling")

    messages = chatmod.sanitize_messages(payload.get("messages"))
    if not messages:
        raise HTTPException(status_code=422, detail="messages required")
    if chatmod.over_turn_cap(messages):
        raise HTTPException(status_code=429, detail="turn_cap")

    current = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    # The client flips this on once the board has been shown — after that the user is
    # adjusting a live search, not answering intake questions.
    refining = bool(payload.get("refining"))
    chatmod.note_request()
    return await chatmod.turn(messages, current, refining=refining)


@app.get("/api/meta")
def meta() -> dict:
    """Pool freshness for the UI (how many jobs, newest posting)."""
    return {"count": store.pool_size(), "harvested_at": store.newest_posted()}


@app.get("/api/health")
def health() -> dict:
    """Status for you + an uptime monitor: which feeds are live, budget used, freshness."""
    return {
        "ok": True,
        "adzuna_ok": bool(
            os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY")
        ),
        "openrouter_ok": chatmod.chat_available(),
        "daily_fetches_used": store.live_fetch_count(),
        "daily_fetch_ceiling": ADZUNA_DAILY_CEILING,
        "pool_size": store.pool_size(),
        # The size of the harvest snapshot this slot should be serving. The pool is
        # that snapshot plus live-fetch accumulation, so pool_size < snapshot_count
        # means the slot UNDER-ingested — the exact regression verify-slot.sh gates on
        # with a ratio, instead of a fixed floor that a 90%-smaller harvest slips past.
        "snapshot_count": snapshot.load_snapshot(store.JOBS_JSON_PATH)["meta"].get(
            "count", 0
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
