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

import threading

from job_radar import config as jr_config
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
    a process-global active config, so set+call must not interleave)."""
    rows: list[dict] = []
    with _CFG_LOCK:
        jr_config.set_active(cfg)
        for fn in (
            sources.search_adzuna,
            sources.search_usajobs,
            sources.search_google_jobs,
        ):
            try:
                rows.extend(fn(titles) or [])
            except Exception:  # a dead source never fails the whole fetch
                continue
    return rows


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
