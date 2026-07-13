"""The live fetch — the per-search inflow to the store.

On a search the store misses (or whose TTL lapsed), we go get the jobs LIVE from
the fast keyed sources only — Adzuna + USAJOBS, ~1-2s — NOT job_radar's full
10-source `engine.harvest` (~50s; the slow free boards add nothing to a specific
title and are covered by the periodic baseline).

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

# Non-geographic location words: send Adzuna an EMPTY `where` (nationwide) rather
# than a literal 'remote', which matches a nonexistent city and returns zero.
_NON_PLACE = {
    "",
    "remote",
    "remote only",
    "remote-only",
    "anywhere",
    "any",
    "everywhere",
}


def _prep_location(location: str | None) -> str:
    loc = (location or "").strip()
    return "" if loc.lower() in _NON_PLACE else loc


def live_fetch(titles: list[str], location: str | None) -> list[dict]:
    """Fetch jobs for the user's titles from Adzuna + USAJOBS. Blocking (run me in
    a threadpool). Returns raw job_radar rows (normalized later by store.upsert)."""
    titles = [t for t in (titles or []) if t and t.strip()]
    if not titles:
        return []
    cfg = jr_config.Config()
    cfg.title_queries = titles
    cfg.location = _prep_location(location)
    cfg.remote_only = False
    cfg.radius_miles = 0
    cfg.breadth_sources = ["adzuna", "usajobs"]  # the fast keyed sources ONLY

    rows: list[dict] = []
    with _CFG_LOCK:
        jr_config.set_active(cfg)
        for fn in (sources.search_adzuna, sources.search_usajobs):
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
