"""The live fetch — the per-search inflow to the store.

On a search the store misses (or whose TTL lapsed), we go get the jobs LIVE from
the fast keyed sources only — Adzuna + USAJOBS + Google for Jobs (SerpApi), ~1-3s
— NOT job_radar's full 10-source `engine.harvest` (~50s; the slow free boards add
nothing to a specific title and are covered by the periodic baseline). Google for
Jobs is metered (SerpApi free tier 250 searches/mo); the single-flight + store TTL
below bound how often it is actually called.

Two concurrency controls:
  1. A module LOCK around set_active+call, because job_radar's source functions
     read the process-global `config.active()` — two concurrent fetches with
     different locations would otherwise race that global.
  2. SINGLE-FLIGHT: N concurrent requests for the SAME (title,location) ride ONE
     upstream fetch (anti-thundering-herd — saves quota, not just work). The scorer
     runs this in a threadpool, so the coalescing is threading-based.
"""

from __future__ import annotations

import os
import threading

from job_radar import config as jr_config
from job_radar.engine import _coerce
from job_radar import sources

from . import store

# job_radar's source functions read the global config.active(); serialize set+call.
_CFG_LOCK = threading.Lock()

# Non-geographic location words: send Adzuna an EMPTY `where` (nationwide) rather than
# a literal 'remote', which resolves against a PLACE HIERARCHY and returns zero for
# anything that is not a real place.
#
# This list used to hold seven entries and it was nowhere near enough. Probed against the
# live Adzuna API with `what=software engineer`, where a real place returns thousands and
# blank returns 148,341:
#
#     work from home  0     anywhere in the us  0     home based  0     no preference  0
#     wfh             0     remote (us)         0     virtual     0     flexible       0
#     nationwide    143  <- WORSE than zero: it matches SOMETHING and silently narrows
#
# Eight of twelve plausible answers produced a dead search, and live_fetch wraps every
# source in `except: continue`, so the user saw no error — just no fresh jobs.
#
# `us` / `usa` / `united states` are here for a different reason: they all return the
# full 148,341, identical to blank, so mapping them to blank is equivalent and says what
# is actually happening. (This is also why the planned "send 'United States' instead of
# blank" change was dropped — measured, the two are the same request.)
_NON_PLACE = {
    "",
    "remote",
    "remote only",
    "remote-only",
    "remote us",
    "remote (us)",
    "anywhere",
    "anywhere in the us",
    "anywhere in the united states",
    "any",
    "everywhere",
    "virtual",
    "wfh",
    "work from home",
    "home based",
    "home-based",
    "telecommute",
    "nationwide",
    "national",
    "us",
    "usa",
    "u.s.",
    "u.s.a.",
    "united states",
    "no preference",
    "none",
    "n/a",
    "flexible",
    "open",
}


# The most recent per-source outcome, for the search log and the notice. A module-level
# dict rather than a return value because `live_fetch` is called through
# `coalesced_fetch`, whose followers share the LEADER's result — threading a second
# return value through that handoff would mean followers reporting sources they never
# called. Last-writer-wins is right here: the log records one line per request and a
# follower's line honestly describes the fetch its board came from.
_LAST_REPORT: dict[str, dict] = {}


def last_source_report() -> dict:
    return dict(_LAST_REPORT)


def _serp_exhausted() -> bool:
    """Is the SerpApi plan actually out? Asked of SerpApi, never inferred.

    Called ONLY when google_jobs already returned nothing, so it costs one HTTP round
    trip on a path that has just failed — never on a healthy search. `total_searches_left`
    is the account-wide truth, so it counts CLI runs and every other consumer of the same
    key, which is the property a local counter could never have.

    Any failure here answers False: not knowing why a source was quiet must never turn
    into telling the user something confident and wrong.
    """
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        return False
    try:
        from job_radar.config import Config
        from job_radar.sources import _serpapi_searches_left

        left = _serpapi_searches_left(key)
        if left is None:
            return False
        # job-radar holds a reserve back rather than spending to zero, so "exhausted"
        # for our purposes is "at or under the reserve it refuses to touch".
        return left <= max(0, getattr(Config(), "serpapi_reserve", 0))
    except Exception:  # noqa: BLE001 — a diagnostic must not break the search
        return False


def _prep_location(location: str | None) -> str:
    loc = (location or "").strip()
    return "" if loc.lower() in _NON_PLACE else loc


def live_fetch(titles: list[str], location: str | None) -> list[dict]:
    """Fetch jobs for the user's titles from Adzuna + USAJOBS + Google for Jobs.
    Blocking (run me in a threadpool). Returns raw job_radar rows (normalized later
    by store.upsert)."""
    titles = [t for t in (titles or []) if t and t.strip()]
    if not titles:
        return []
    cfg = jr_config.Config()
    cfg.title_queries = titles
    cfg.location = _prep_location(location)
    cfg.remote_only = False
    cfg.radius_miles = 0
    cfg.breadth_sources = ["adzuna", "usajobs", "google_jobs"]  # fast keyed sources

    rows = _fetch_all(cfg, titles)
    # THE NET. `_NON_PLACE` is a list of things people say, and no list of those is ever
    # finished — the eight dead searches it now catches were found by probing, not by
    # imagining. So rather than trust the list, notice the symptom: a location that
    # returns NOTHING from every keyed source is a location the APIs could not resolve,
    # and a nationwide answer beats an empty board every time.
    #
    # Bounded on purpose. It fires only when a non-blank location produced zero rows, so
    # a real place that legitimately has no openings costs one extra call and a genuine
    # nonsense answer costs one extra call. It cannot loop: the retry runs with a blank
    # location, which is the case that skips it.
    if not rows and cfg.location:
        cfg.location = ""
        rows = _fetch_all(cfg, titles)
    return rows


def _fetch_all(cfg, titles: list[str]) -> list[dict]:
    """One pass over the keyed sources under the config lock (job_radar's adapters read
    a process-global active config, so set+call must not interleave).

    Every row goes through `engine._coerce`, which is what makes it a job_radar RECORD
    rather than raw adapter output. Calling the adapters directly — which this module
    does, to skip the harvest machinery — bypassed it, and `_coerce`'s own docstring
    calls itself "the one boundary every posting crosses". It was not.

    Measured: a live row arrived with **14 of the 21 contract fields absent**, including
    `title_root` (so the root-title tier could never fire on a live-fetched job),
    `salary_min`/`salary_currency` (so the salary filter and the USD test could not see
    it), and `direct_apply` — which mattered most, because a MISSING direct_apply
    renders as `apply_via: "aggregator"`, an assertion we had not earned. This is the
    class of bug the store's own facet rule exists to prevent: never claim what the
    source did not say.

    `_coerce` is private. Reaching for it is deliberate and has precedent
    (`store` imports `job_radar.discover._norm_name`); the alternative is a job_radar
    release to make it public, which is worth doing when that repo next opens.
    """
    rows: list[dict] = []
    # PER-SOURCE OUTCOMES, not a silent swallow. The `except: continue` below is still
    # right — one dead vendor must never fail a whole search — but until 2026-08-15 it
    # was the ONLY record that anything went wrong, so a board could arrive thin with no
    # banner, no log line and no health signal. Two Louisville searches an hour apart
    # differed by 130 rows and nothing on the box could say which source moved.
    #
    # `report` is written into the search log and drives the user-facing notice, so a
    # source that returns nothing now says WHY: no_key, quota, error, or a real empty.
    report: dict[str, dict] = {}
    with _CFG_LOCK:
        jr_config.set_active(cfg)
        for name, fn in (
            ("adzuna", sources.search_adzuna),
            ("usajobs", sources.search_usajobs),
            ("google_jobs", sources.search_google_jobs),
        ):
            if name == "google_jobs" and not os.environ.get("SERPAPI_KEY"):
                report[name] = {"n": 0, "why": "no_key"}
                continue
            try:
                got = fn(titles) or []
                rows.extend(got)
                why = "" if got else "empty"
                # DIAGNOSE, DO NOT PREDICT. An earlier version kept a local monthly
                # tally and refused to call the source once it hit 250. That was wrong
                # twice over: the same SerpApi plan is also spent by job-radar CLI runs
                # this process cannot see, so the tally drifts low and stops gating
                # exactly when it matters; and a local guess about a remote budget is
                # the same unearned assertion this codebase keeps removing elsewhere.
                #
                # So nothing is gated. The source runs, and ONLY if it came back empty
                # do we spend one cheap call on the real account to say WHY. job-radar
                # already asks the same endpoint before spending (sources._serpapi_budget)
                # and prints the answer; that print goes to journald and never reaches
                # the person waiting on the board, which is the whole gap being closed.
                if name == "google_jobs" and not got:
                    why = "quota" if _serp_exhausted() else "empty"
                report[name] = {"n": len(got), "why": why}
            except Exception as e:  # a dead source never fails the whole fetch
                report[name] = {"n": 0, "why": f"error:{type(e).__name__}"}
                continue
    _LAST_REPORT.update(report)
    return [_coerce(r) for r in rows]


# ── single-flight (threading; the scorer calls this from a threadpool) ────────
_inflight: dict[str, tuple[threading.Event, dict]] = {}
_inflight_lock = threading.Lock()
COALESCE_TIMEOUT = 40  # seconds a follower waits for the leader's fetch


def coalesced_fetch(titles: list[str], location: str | None) -> list[dict]:
    """N concurrent identical searches → ONE upstream fetch; all share the result."""
    key = store.search_key(titles, location)
    with _inflight_lock:
        entry = _inflight.get(key)
        if entry is None:
            ev, holder = threading.Event(), {}
            _inflight[key] = (ev, holder)
            leader = True
        else:
            ev, holder = entry
            leader = False

    if leader:
        try:
            holder["result"] = live_fetch(titles, location)
        except Exception as e:  # noqa: BLE001 — carried to followers below
            holder["error"] = e
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
            ev.set()
    else:
        ev.wait(timeout=COALESCE_TIMEOUT)

    if "error" in holder:
        raise holder["error"]
    return holder.get("result", [])
