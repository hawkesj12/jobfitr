"""Phase F: the live fetch — single-flight coalescing + no full harvest. No network
(the job_radar source functions are monkeypatched)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from jobfitr import live


def test_prep_location_maps_non_places_to_empty():
    for word in ["", "remote", "Remote Only", "anywhere", "everywhere"]:
        assert live._prep_location(word) == ""
    assert live._prep_location("Denver, CO") == "Denver, CO"


def test_live_fetch_calls_only_keyed_sources(monkeypatch):
    calls = []
    monkeypatch.setattr(
        live.sources,
        "search_adzuna",
        lambda q: calls.append(("adz", q)) or [{"url": "u", "title": "X"}],
    )
    monkeypatch.setattr(
        live.sources, "search_usajobs", lambda q: calls.append(("usa", q)) or []
    )
    monkeypatch.setattr(
        live.sources, "search_google_jobs", lambda q: calls.append(("goog", q)) or []
    )
    # engine.harvest must NEVER be reachable from live.py — poison it to be sure.
    import job_radar.engine as eng

    monkeypatch.setattr(
        eng,
        "harvest",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("live must not call engine.harvest")
        ),
    )

    rows = live.live_fetch(["product manager"], "remote")
    assert rows == [{"url": "u", "title": "X"}]
    # the three keyed sources, in order
    assert [c[0] for c in calls] == ["adz", "usa", "goog"]
    assert calls[0][1] == ["product manager"]


def test_live_fetch_empty_titles_no_call(monkeypatch):
    monkeypatch.setattr(
        live.sources,
        "search_adzuna",
        lambda q: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    assert live.live_fetch([], "remote") == []
    assert live.live_fetch(["  "], None) == []


def test_live_fetch_survives_a_dead_source(monkeypatch):
    monkeypatch.setattr(
        live.sources,
        "search_adzuna",
        lambda q: (_ for _ in ()).throw(RuntimeError("adzuna down")),
    )
    monkeypatch.setattr(
        live.sources, "search_usajobs", lambda q: [{"url": "u2", "title": "Y"}]
    )
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])
    assert live.live_fetch(["nurse"], "") == [
        {"url": "u2", "title": "Y"}
    ]  # one dead, one ok


def test_single_flight_coalesces_concurrent_identical_searches(monkeypatch):
    fetches = {"n": 0}
    fetch_lock = threading.Lock()

    def slow_adzuna(q):
        with fetch_lock:
            fetches["n"] += 1
        time.sleep(0.25)  # hold the fetch so followers pile up behind the leader
        return [{"url": "same", "title": "Grocery Store Manager"}]

    monkeypatch.setattr(live.sources, "search_adzuna", slow_adzuna)
    monkeypatch.setattr(live.sources, "search_usajobs", lambda q: [])
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(
            ex.map(
                lambda _: live.coalesced_fetch(["grocery store manager"], "remote"),
                range(8),
            )
        )

    assert fetches["n"] == 1  # 8 concurrent identical searches → ONE upstream fetch
    assert all(
        r == [{"url": "same", "title": "Grocery Store Manager"}] for r in results
    )


def test_single_flight_distinct_searches_each_fetch(monkeypatch):
    fetches = {"n": 0}
    monkeypatch.setattr(
        live.sources,
        "search_adzuna",
        lambda q: fetches.__setitem__("n", fetches["n"] + 1) or [],
    )
    monkeypatch.setattr(live.sources, "search_usajobs", lambda q: [])
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])
    live.coalesced_fetch(["accountant"], "remote")
    live.coalesced_fetch(["nurse"], "remote")
    assert fetches["n"] == 2  # different keys → separate fetches


def test_prep_location_catches_the_measured_dead_searches():
    """Adzuna's `where` resolves against a PLACE HIERARCHY, so a non-place returns zero.
    Probed live with `what=software engineer`, where blank returns 148,341: 'work from
    home', 'wfh', 'anywhere in the us', 'remote (us)', 'home based', 'virtual',
    'no preference' and 'flexible' all returned 0, and 'nationwide' returned 143 —
    worse than zero, because it matches SOMETHING and silently narrows the board."""
    for word in [
        "work from home", "WFH", "Nationwide", "anywhere in the US", "Remote (US)",
        "no preference", "flexible", "home based", "virtual", "telecommute",
    ]:
        assert live._prep_location(word) == "", word


def test_prep_location_maps_the_whole_country_to_blank():
    """`us`, `usa` and `united states` each return the identical 148,341 that blank
    does, so mapping them to blank is the same request said plainly. This is also why
    the planned 'send United States instead of blank' change was dropped."""
    for word in ["us", "USA", "United States", "u.s.", "U.S.A."]:
        assert live._prep_location(word) == "", word


def test_prep_location_keeps_a_real_place():
    for word in ["Denver, CO", "Louisville, KY", "Austin", "New York, NY"]:
        assert live._prep_location(word) == word


def test_a_location_that_returns_nothing_is_retried_nationwide(monkeypatch):
    """THE NET. No list of non-place words is ever finished — the eight above were found
    by probing, not by imagining. A location that returns nothing from every keyed source
    is one the APIs could not resolve, and a nationwide board beats an empty one."""
    seen = []

    def adzuna(q):
        seen.append(live.jr_config.active().location)
        return [] if seen[-1] else [{"url": "u1", "title": "Nationwide hit"}]

    monkeypatch.setattr(live.sources, "search_adzuna", adzuna)
    monkeypatch.setattr(live.sources, "search_usajobs", lambda q: [])
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])

    rows = live.live_fetch(["engineer"], "Zzyzx Township")
    assert seen == ["Zzyzx Township", ""]  # tried the place, then fell back
    assert rows == [{"url": "u1", "title": "Nationwide hit"}]


def test_the_retry_does_not_fire_when_the_location_was_already_blank(monkeypatch):
    """It cannot loop, and a genuinely empty nationwide result must not cost two calls."""
    calls = []
    monkeypatch.setattr(
        live.sources, "search_adzuna", lambda q: calls.append(1) or []
    )
    monkeypatch.setattr(live.sources, "search_usajobs", lambda q: [])
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])
    assert live.live_fetch(["engineer"], "remote") == []
    assert len(calls) == 1


def test_the_retry_does_not_fire_when_the_place_found_something(monkeypatch):
    calls = []

    def adzuna(q):
        calls.append(live.jr_config.active().location)
        return [{"url": "u1", "title": "Denver job"}]

    monkeypatch.setattr(live.sources, "search_adzuna", adzuna)
    monkeypatch.setattr(live.sources, "search_usajobs", lambda q: [])
    monkeypatch.setattr(live.sources, "search_google_jobs", lambda q: [])
    live.live_fetch(["engineer"], "Denver, CO")
    assert calls == ["Denver, CO"]
