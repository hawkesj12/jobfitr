"""NOTE 2026-08-20: 46 tests were removed from this file with the scoreboard — they
covered /api/score, the fit number, the facet drawer, the result cap and the search log's
score-path shape. What is left covers the parts that survived: the store, the harvest, the
ledger, health/meta, and the static front end. /api/candidates has its own coverage.

Phase F web tests. The old zero-network guarantee is retired — a request now
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
from jobfitr.config_builder import filters_from_dict

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
        # See tests/test_store.py::_job — a real row always carries this, and intake now
        # requires positive evidence of directness.
        "direct_apply": True,
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
    # the live-fetch tally now lives in the (fresh, per-test) store, so nothing to reset
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




def test_filters_no_location_shows_all():
    # The live-fetch default: no location named → show ALL jobs, not remote-only.
    assert filters_from_dict({}).remote_only is False
    assert filters_from_dict({"titles": ["nurse"]}).remote_only is False




def test_filters_do_not_inherit_a_recruiting_exclude_list():
    """An accountant must not have accountant jobs hidden from them.

    This used to build a job_radar Config and assert `scoring.relevant()` — which jobfitr
    never called. The property is now structural: exclusions start from the user's own
    list and nothing else can be inherited, so there is no default to leak.
    """
    f = filters_from_dict({"titles": ["accountant"]})
    assert f.exclude_titles == []
    assert f.titles == ["accountant"]


def test_an_exclusion_may_not_delete_the_role_you_asked_for():
    """Observed live 2026-08-17: an interview put "ai engineer" in BOTH titles and
    exclude. Exclusions are a hard filter, so every AI Engineer posting was removed
    before anything saw it and the results came back entirely DevOps. Nothing said why."""
    notes: list[str] = []
    f = filters_from_dict({"titles": ["AI Engineer"], "exclude": ["ai engineer", "intern"]}, notes)
    assert f.exclude_titles == ["intern"]
    assert notes and "ai engineer" in notes[0]


def test_the_guard_uses_the_same_matcher_the_filter_does():
    """A word OF the requested title counts. `_eligible` filters with match.has_term, so
    the guard has to agree with the thing it is protecting against."""
    f = filters_from_dict({"titles": ["Forward Deployed Engineer"], "exclude": ["engineer"]})
    assert f.exclude_titles == []


# ── snapshot round-trip (still writes jobs.json; now also feeds the store) ─────
def test_snapshot_roundtrip(tmp_path, monkeypatch):
    from job_radar.config import Config

    # keep the baseline harvest's store-upsert isolated to a tmp db
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()

    rows = [
        # longer than the cap ON PURPOSE, expressed relative to it so raising the
        # constant can never quietly stop exercising the truncation it guards.
        {
            **_job("Data Engineer", text="x" * (snapshot.TEXT_CAP + 1000)),
            "sources": {"remotive", "remoteok"},
        },
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




# ── Phase A: ranking correctness ──────────────────────────────────────────────
def _no_fetch(monkeypatch):
    monkeypatch.setattr(
        live,
        "coalesced_fetch",
        lambda *a: (_ for _ in ()).throw(AssertionError("fresh cache must not fetch")),
    )


def _filler(n=40):
    """Unrelated rows so BM25 behaves like it does against the real ~10k pool.

    NOT padding — load-bearing. BM25 scores a term by how RARE it is, so in a corpus
    of two documents that both contain the query terms the IDF collapses to zero and
    every candidate ties at 0.000, leaving the rerank to decide everything. That is an
    artifact of a toy corpus, not of the app. These rows restore the discrimination
    real users get; delete them and the ranking assertions below stop meaning anything.
    """
    return [
        _job(
            f"Warehouse Associate {i}",
            text="forklift picking packing shift work",
            company=f"Filler{i}",
            location="Louisville, KY",
            url=f"https://x/filler-{i}",
        )
        for i in range(n)
    ]










































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
        lambda cfg, *a, **kw: seen.update(kw) or ([], [], []),
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
        _json.dumps(
            {"companies": [{"name": "Seed Co", "ats": "lever", "slug": "seedco"}]}
        )
    )
    seen = {}
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: seen.update(kw) or ([], [], []),
    )
    snapshot.build_snapshot(Config(), str(wl), str(tmp_path / "out.json"))
    assert [c["slug"] for c in seen["companies"]] == ["seedco"]


# ── employer concentration: visible, not corrected ───────────────────────────
# Replaces the six _spread_companies tests. The cap DROPPED an employer's roles past
# the fourth, which solved a real problem (Veterans Health has 915 rows in the corpus,
# Anduril 1,063) with an instrument that deleted evidence invisibly — the user could
# not tell the difference between "this employer has 4 openings" and "this employer has
# 900 and we hid 896". The promise has changed shape: concentration is now something
# the user SEES and can filter on the board, not something the server silently fixes.






def test_load_dotenv_survives_an_unreadable_directory(tmp_path, monkeypatch):
    """REGRESSION: load_dotenv stat'd .env in the CWD and let PermissionError escape.
    The CLI runs as the `jobfitr` service user, often from a directory that user
    cannot stat — which killed a whole resolution run before it read one company,
    over an optional file production does not even use (systemd supplies the env)."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / ".env").write_text("FOO=bar\n")
    locked.chmod(0o000)
    try:
        monkeypatch.chdir(tmp_path)
        assert snapshot.load_dotenv(str(locked / ".env")) == 0
    finally:
        locked.chmod(0o755)


def test_load_dotenv_still_reads_a_readable_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport ALPHA=1\nBETA=2\n\n")
    import os as _os

    _os.environ.pop("ALPHA", None)
    _os.environ.pop("BETA", None)
    assert snapshot.load_dotenv(str(env)) == 2
    assert _os.environ["ALPHA"] == "1" and _os.environ["BETA"] == "2"


def test_missing_harvest_config_warns_loudly(tmp_path, monkeypatch, capsys):
    """REGRESSION: the config is resolved relative to the CWD, and falling through to
    job_radar's built-in defaults is a cliff, not a soft default — those defaults are
    narrow and tech-only. A harvest launched from the wrong directory silently
    returned ~1,700 jobs instead of ~20,000, with no error at all."""
    monkeypatch.chdir(tmp_path)
    assert snapshot._resolve_config(None) is None
    assert "NARROW" in capsys.readouterr().out


def test_harvest_config_is_found_from_the_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "web-harvest.example.yaml").write_text("profile: {}\n")
    assert snapshot._resolve_config(None) == "web-harvest.example.yaml"
    assert "NARROW" not in capsys.readouterr().out


# ── M3/M4: health surfaces the persisted tally and the snapshot size ─────────
def test_health_exposes_snapshot_count_for_the_ratio_gate(client, tmp_path):
    """M4: verify-slot.sh gates on pool_size vs snapshot_count instead of a fixed
    floor, so the slot must publish what it should be serving."""
    d = client.get("/api/health").json()
    assert "snapshot_count" in d and isinstance(d["snapshot_count"], int)


def test_health_reports_the_persisted_fetch_tally(client):
    """M3: the count survives outside the request handler's memory."""
    store.note_live_fetch()
    store.note_live_fetch()
    assert client.get("/api/health").json()["daily_fetches_used"] == 2


# ── M5: a failed discovered-company write is reported, not swallowed ─────────
def test_discovered_write_failure_is_reported_not_silent(tmp_path, monkeypatch, capsys):
    """REGRESSION (panel M5): build_snapshot swallowed a ledger-write failure with a
    bare `except: pass`, so a harvest could discover companies and persist none of
    them, invisibly and forever."""
    from job_radar.config import Config

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init()

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, *a, **kw: (
            [],
            [{"name": "New Co", "ats": "ashby", "slug": "n"}],
            [],
        ),
    )
    monkeypatch.setattr(store, "record_resolution", boom)
    meta = snapshot.build_snapshot(Config(), None, str(tmp_path / "out.json"))
    assert meta["count"] == 0  # harvest still succeeded (didn't raise)
    assert "could not record" in capsys.readouterr().out  # but it said so


# ── related titles: the model's suggestions ──────────────────────────────────












# ── penalties that read the meaning, not just the word ───────────────────────


















# ── the search log ───────────────────────────────────────────────────────────














