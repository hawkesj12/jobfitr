"""Phase F web tests. The old zero-network guarantee is retired — a request now
does a BOUNDED live fetch on a cache miss and serves the fresh cache on a hit. These
tests pin: config mapping, the snapshot round-trip, the store-backed BM25 + rerank
score path (tags + facets), the live-fetch-on-miss + fresh-cache-hit branches, and
graceful degradation when the daily ceiling trips.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from jobfitr import live, server, snapshot, store
from jobfitr.config_builder import config_from_dict

RECENT = (date.today() - timedelta(days=3)).isoformat()


def _job(
    title,
    text="",
    company="Acme",
    location="Remote",
    posted=RECENT,
    url=None,
    **kw,
):
    # default a UNIQUE url per title so upsert (dedups by url) keeps distinct rows
    row = {
        "title": title,
        "text": text,
        "company": company,
        "location": location,
        "posted": posted,
        "url": url or f"https://x/{title.lower().replace(' ', '-')}",
        "source": "adzuna",
    }
    row.update(kw)
    return row


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over an isolated tmp store, the rate limiter off, fetch usage reset."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()  # DB_PATH now points at the tmp db
    server.limiter.enabled = False  # don't let 40/min trip across the suite
    server._fetch_usage.update(date="", count=0)
    server._last_fetch_ok["at"] = None
    monkeypatch.setattr(
        server, "ADZUNA_DAILY_CEILING", 800
    )  # a known ceiling for the degrade test
    yield TestClient(server.app)
    server.limiter.enabled = True


def _seed(rows):
    store.upsert_jobs(rows)


def _mark_fresh(titles, location=""):
    store.mark_fetched(store.search_key(titles, location))


# ── config_from_dict ──────────────────────────────────────────────────────────
def test_config_from_dict_maps_the_five_answers():
    cfg = config_from_dict(
        {
            "titles": ["Zookeeper", "Animal Keeper"],
            "boosts": ["Reptiles", "biology degree"],
            "exclude": ["Intern", "volunteer"],
            "rank_down": ["staffing"],
            "location": "Louisville, KY",
            "max_age_days": 45,
            "min_score": "strong",
        }
    )
    assert cfg.title_queries == ["zookeeper", "animal keeper"]
    assert cfg.exclude_titles == ["intern", "volunteer"]
    assert cfg.agency_penalty == {"staffing": 8}
    assert cfg.location == "Louisville, KY"
    assert cfg.remote_only is False  # a real place turns off remote-only


def test_config_from_dict_no_location_shows_all():
    # The live-fetch default: no location named → show ALL jobs, not remote-only.
    assert config_from_dict({}).remote_only is False
    assert config_from_dict({"titles": ["nurse"]}).remote_only is False


def test_config_from_dict_remote_and_anywhere():
    assert config_from_dict({"location": "remote"}).remote_only is True
    assert config_from_dict({"location": "anywhere"}).remote_only is False
    assert (
        config_from_dict({"location": "Denver", "remote_only": True}).remote_only
        is True
    )


def test_config_from_dict_does_not_inherit_tech_exclude_defaults():
    from job_radar.scoring import relevant

    cfg = config_from_dict({"titles": ["accountant"]})
    assert cfg.exclude_titles == []
    assert relevant("General Accountant", cfg) is True
    cfg2 = config_from_dict({"titles": ["engineer"], "exclude": ["intern"]})
    assert cfg2.exclude_titles == ["intern"]
    assert relevant("Engineering Intern", cfg2) is False


# ── snapshot round-trip (still writes jobs.json; now also feeds the store) ─────
def test_snapshot_roundtrip(tmp_path, monkeypatch):
    from job_radar.config import Config

    # keep the baseline harvest's store-upsert isolated to a tmp db
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()

    rows = [
        {**_job("Data Engineer", text="x" * 5000), "sources": {"remotive", "remoteok"}},
        {**_job("Product Manager", url="https://x/pm"), "sources": {"jobicy"}},
    ]
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (rows, [], ["boom: himalayas timed out"]),
    )
    out = tmp_path / "jobs.json"
    meta = snapshot.build_snapshot(Config(), None, str(out))

    assert out.exists()
    assert meta["count"] == 2
    assert "himalayas" in meta["errors"][0]
    # the harvest also fed the store (the demoted baseline inflow)
    assert store.pool_size() == 2

    snap = snapshot.load_snapshot(str(out))
    j0 = snap["jobs"][0]
    assert isinstance(j0["sources"], list) and j0["sources"] == ["remoteok", "remotive"]
    assert len(j0["text"]) == snapshot.TEXT_CAP


def test_load_snapshot_missing_file_is_empty(tmp_path):
    snap = snapshot.load_snapshot(str(tmp_path / "nope.json"))
    assert snap["jobs"] == [] and snap["meta"]["count"] == 0


# ── /api/score: BM25 candidates + personalized rerank + tags + facets ─────────
def test_score_ranks_boosts_excludes_and_tags(client, monkeypatch):
    _seed(
        [
            _job(
                "Senior Python Engineer",
                text="python kubernetes docker accountant-free",
                location="Austin, TX",
                salary="$140,000",
                department="IT Jobs",
                employment_type="full_time",
            ),
            _job("Data Engineer", text="python etl pipelines", department="IT Jobs"),
            _job("Marketing Engineer", text="seo content calendar growth"),
            _job("Engineering Intern", text="python kubernetes internship"),
        ]
    )
    _mark_fresh(["engineer"])  # fresh cache → no live fetch
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("fresh cache must not fetch")),
    )

    d = client.post(
        "/api/score",
        json={
            "titles": ["engineer"],
            "boosts": ["python", "kubernetes"],
            "exclude": ["intern"],
            "min_score": "plenty",
        },
    ).json()
    titles = [j["title"] for j in d["jobs"]]
    assert "Engineering Intern" not in titles  # hard-excluded
    assert titles[0] == "Senior Python Engineer"  # both boosts + title
    scores = [j["fit_score"] for j in d["jobs"]]
    assert scores == sorted(scores, reverse=True)

    top = d["jobs"][0]
    assert top["why"]  # matched signals
    assert "text" not in top and "snippet" in top  # body not leaked
    assert top["category"] == "IT Jobs" and top["employment_type"] == "full_time"
    assert "senior" in top["tags"] and "onsite" in top["tags"]  # derived tags
    assert 0 < top["fit_pct"] <= 100
    # facets counted over the returned set
    assert d["facets"]["category"]["IT Jobs"] >= 1
    assert d["pool"] == store.pool_size()
    assert d["degraded"] is None


def test_score_miss_triggers_live_fetch(client, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return [
            _job(
                "Grocery Store Manager",
                text="retail grocery store manager",
                url="https://x/gm",
                department="Retail Jobs",
            )
        ]

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    d = client.post(
        "/api/score",
        json={
            "titles": ["grocery store manager"],
            "location": "ohio",
            "min_score": "plenty",
        },
    ).json()
    assert calls["n"] == 1  # the miss went live
    assert d["jobs"] and d["jobs"][0]["title"] == "Grocery Store Manager"
    assert store.pool_size() == 1  # upserted into the store
    # a second identical search is now fresh → no second fetch
    client.post(
        "/api/score",
        json={
            "titles": ["grocery store manager"],
            "location": "ohio",
            "min_score": "plenty",
        },
    )
    assert calls["n"] == 1


def test_prefetch_warms_cache_then_score_does_not_refetch(client, monkeypatch):
    # The progressive-harvest path: prefetch the moment titles + location are known,
    # then the later score reuses the warm cache — ONE upstream fetch across both.
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return [_job("Line Cook", text="restaurant line cook", url="https://x/lc")]

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    p = client.post(
        "/api/prefetch", json={"titles": ["line cook"], "location": "reno, nv"}
    ).json()
    assert p["ok"] is True and p["warmed"] is True
    assert calls["n"] == 1  # prefetch did the live fetch
    assert store.pool_size() == 1

    d = client.post(
        "/api/score",
        json={"titles": ["line cook"], "location": "reno, nv", "min_score": "plenty"},
    ).json()
    assert calls["n"] == 1  # score saw the fresh cache → NO second fetch
    assert d["jobs"] and d["jobs"][0]["title"] == "Line Cook"


def test_prefetch_without_titles_is_a_noop(client, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(titles, location):
        calls["n"] += 1
        return []

    monkeypatch.setattr(live, "coalesced_fetch", fake_fetch)
    p = client.post("/api/prefetch", json={"location": "denver, co"}).json()
    assert p["warmed"] is False
    assert calls["n"] == 0  # nothing to fetch without a title


def test_score_ladder_relaxes_freshness_to_find_matches(client):
    # A 40-day-old job is excluded by the tight 15/30-day tiers but included once the
    # deterministic ladder relaxes past 30 days — no "how picky?" or recency question.
    old = (date.today() - timedelta(days=40)).isoformat()
    _seed(
        [
            _job(
                "Staff Data Scientist",
                text="ml data scientist role",
                posted=old,
                url="https://x/sds",
            )
        ]
    )
    _mark_fresh(["data scientist"], "remote")  # cache fresh → no live fetch
    d = client.post(
        "/api/score", json={"titles": ["data scientist"], "location": "remote"}
    ).json()
    assert d["jobs"] and d["jobs"][0]["title"] == "Staff Data Scientist"
    assert d["tier"]["max_age_days"] >= 60  # relaxed past the tight tiers to include it


def test_score_degrades_to_cache_when_ceiling_reached(client, monkeypatch):
    _seed(
        [
            _job(
                "Registered Nurse",
                text="patient care icu nurse",
                url="https://x/rn",
                department="Healthcare & Nursing Jobs",
            )
        ]
    )
    monkeypatch.setattr(server, "ADZUNA_DAILY_CEILING", 0)  # force the ceiling shut
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("ceiling must skip the fetch")),
    )
    d = client.post(
        "/api/score", json={"titles": ["nurse"], "min_score": "plenty"}
    ).json()
    assert d["degraded"] == "adzuna_daily_limit"  # load-shed
    assert (
        d["jobs"] and d["jobs"][0]["title"] == "Registered Nurse"
    )  # served from cache


def test_meta_and_health(client):
    _seed([_job("Engineer")])
    m = client.get("/api/meta").json()
    assert m["count"] == 1
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert "adzuna_ok" in h and "openrouter_ok" in h
    assert h["daily_fetch_ceiling"] == 800
    assert h["pool_size"] == 1
    assert h["last_successful_fetch"] is None


def test_garbage_post_is_clean_4xx(client):
    resp = client.post("/api/score", json=["not", "an", "object"])
    assert resp.status_code == 422
    assert client.post("/api/score", json={}).status_code == 200  # empty → defaults


# ── the front end is served same-origin ───────────────────────────────────────
def test_static_front_end_is_served():
    c = TestClient(server.app)
    root = c.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "jobfitr" in root.text
    assert c.get("/api/health").json()["ok"] is True


# ── the ledger drives the harvest (the wiring that makes resolution pay) ─────
def test_harvest_polls_the_LEDGER_not_the_watchlist_file(tmp_path, monkeypatch):
    """REGRESSION: the resolution ledger was a table nothing read. Resolving a company
    produced zero extra jobs until build_snapshot fed the ledger to the engine."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()
    store.record_resolution(
        "Discovered Co", {"ats": "greenhouse", "slug": "discovered", "roles": 9}
    )

    seen = {}
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (seen.update(kw) or ([], [], [])),
    )
    snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert [c["slug"] for c in seen["companies"]] == ["discovered"]


def test_discovered_companies_flow_back_into_the_ledger(tmp_path, monkeypatch):
    """Discovery RETURNS candidates now instead of appending to a file, so the store
    has to catch them or they are lost."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led2.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()
    found = [{"name": "Fresh Co", "ats": "ashby", "slug": "freshco", "roles": 4}]
    monkeypatch.setattr(
        snapshot.engine, "harvest", lambda cfg, *a, **kw: ([], found, [])
    )
    snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert [c["slug"] for c in store.resolved_companies()] == ["freshco"]


def test_harvest_falls_back_to_the_watchlist_when_the_ledger_is_empty(
    tmp_path, monkeypatch
):
    """The depth lane is ~40% of the corpus and 23x more productive per company than
    breadth — it must never silently vanish because the store had a bad day."""
    import json as _json

    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "led3.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    wl = tmp_path / "wl.json"
    wl.write_text(
        _json.dumps({"companies": [{"name": "Seed Co", "ats": "lever", "slug": "seedco"}]})
    )
    seen = {}
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (seen.update(kw) or ([], [], [])),
    )
    snapshot.build_snapshot(Config(), str(wl), str(tmp_path / "out.json"))
    assert [c["slug"] for c in seen["companies"]] == ["seedco"]
