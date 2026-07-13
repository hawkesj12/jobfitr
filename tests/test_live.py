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
    assert [c[0] for c in calls] == ["adz", "usa"]  # both keyed sources, in order
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
    live.coalesced_fetch(["accountant"], "remote")
    live.coalesced_fetch(["nurse"], "remote")
    assert fetches["n"] == 2  # different keys → separate fetches
