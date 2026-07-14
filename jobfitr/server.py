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

import os
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from job_radar.util import age_int
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import chat as chatmod
from . import live, store
from .config_builder import _clean_list, config_from_dict, search_inputs
from .snapshot import load_dotenv

_ET = ZoneInfo("America/New_York")

# Local dev: pull ./.env into os.environ so OPENROUTER_API_KEY (and the harvest keys)
# are present when the server is started directly (python -m jobfitr.server). In
# production these come from systemd's EnvironmentFile; load_dotenv only fills vars
# NOT already set, so it never overrides the deployed secrets — a safe no-op there.
load_dotenv()

# Build the SQLite store schema + one-time import of any legacy jobs.json.
store.init()

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SNIPPET_CHARS = 240
DESC_CHARS = 1200
CANDIDATE_LIMIT = 500  # top-N BM25 candidates fetched before the personalized rerank

# Rerank weights (in BM25 units, ~0-5): a boost match nudges up, a rank_down sinks.
BOOST_W = 2.0
PENALTY_W = 3.0
# min_score keyword → keep candidates scoring >= frac × the top result's score.
MIN_SCORE_FRAC = {"plenty": 0.0, "balanced": 0.35, "strong": 0.6}

# Deterministic freshness/pickiness ladder — replaces the "how picky?" + recency
# questions. Start tight (fresh + strong), relax only as far as needed to reach TARGET
# results; cap the shown set at TARGET. (max_age_days, min_score), tight → loose.
TARGET_RESULTS = 50
RESULT_LADDER = [
    (15, "strong"),
    (30, "strong"),
    (30, "balanced"),
    (60, "balanced"),
    (90, "plenty"),
]

CHAT_RATE_LIMIT = os.environ.get("CHAT_RATE_LIMIT", "20/minute")
SCORE_RATE_LIMIT = os.environ.get("SCORE_RATE_LIMIT", "40/minute")
# Daily cap on live Adzuna/USAJOBS fetches — the actuator saturation. When tripped,
# we serve the cache with a `degraded` banner instead of burning the free quota.
ADZUNA_DAILY_CEILING = int(os.environ.get("ADZUNA_DAILY_CEILING", "800"))

app = FastAPI(title="jobfitr", version="0.1.0")

# Per-IP rate limiting.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── daily live-fetch ceiling (the load-shed) ──────────────────────────────────
_fetch_usage = {"date": "", "count": 0}
_last_fetch_ok = {"at": None}


def _today() -> str:
    return datetime.now(_ET).date().isoformat()


def _fetch_ceiling_reached() -> bool:
    if _fetch_usage["date"] != _today():
        _fetch_usage["date"] = _today()
        _fetch_usage["count"] = 0
    return _fetch_usage["count"] >= ADZUNA_DAILY_CEILING


def _note_fetch() -> None:
    if _fetch_usage["date"] != _today():
        _fetch_usage["date"] = _today()
        _fetch_usage["count"] = 0
    _fetch_usage["count"] += 1
    _last_fetch_ok["at"] = datetime.now(_ET).isoformat(timespec="seconds")


def _snippet(text) -> str:
    if not isinstance(text, str):
        return ""
    s = " ".join(text.split())
    return s[:SNIPPET_CHARS]


def _description(text) -> str:
    """A fuller (but still capped) JD body for the expand-to-detail view.

    Still served from the cached snapshot — never a live fetch. The full untruncated
    body is never returned; the harvest already caps text at ~2000 chars.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:DESC_CHARS]


def _fit_pct(fit_score: float, ref: float) -> int:
    """Normalize the reranked score to a 0-100 gauge value, relative to the top match."""
    if ref <= 0:
        return 3
    return max(3, min(100, round(100 * fit_score / ref)))


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
        "category": c.get("category", ""),
        "employment_type": c.get("employment_type", ""),
        "tags": tags,
        "fit_score": fit_score,  # the reranked score (canonical)
        "fit_pct": fit_pct,  # derived gauge value (presentation only)
        "why": why,  # the title/boost signals that matched
        "snippet": _snippet(body),
        "description": _description(body),
    }


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
        if any(x in title for x in exclude):
            continue
        if remote_only and c.get("remote") != "remote":
            continue
        age = age_int(c.get("posted", ""))
        if age is not None and age > max_age_days:
            continue
        blob = f"{title} {(c.get('body') or '').lower()}"
        bonus = sum(BOOST_W for x in boosts if x and x in blob)
        pen = sum(PENALTY_W for x in penalties if x and x in blob)
        final = float(c.get("bm25", 0.0)) + bonus - pen
        why = ", ".join([t for t in why_terms if t in blob][:4])
        scored.append((c, final, why))
    scored.sort(key=lambda t: t[1], reverse=True)
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
        return "adzuna_daily_limit"  # load-shed: serve the cache
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

    candidates = store.bm25_candidates(titles, limit=CANDIDATE_LIMIT) if titles else []
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
            TARGET_RESULTS,
        )
        tier = {"max_age_days": max_age, "min_score": min_key}
        if len(kept) >= TARGET_RESULTS:
            break
    ref = max(top, 0.1)
    results = [
        _shape(c, round(final), why, _fit_pct(final, ref)) for c, final, why in kept
    ]
    facets = store.facet_counts([c for c, _, _ in kept])
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
    chatmod.note_request()
    return await chatmod.turn(messages, current)


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
        "daily_fetches_used": _fetch_usage["count"]
        if _fetch_usage["date"] == _today()
        else 0,
        "daily_fetch_ceiling": ADZUNA_DAILY_CEILING,
        "pool_size": store.pool_size(),
        "last_successful_fetch": _last_fetch_ok["at"],
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
