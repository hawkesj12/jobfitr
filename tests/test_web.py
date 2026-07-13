"""Phase A tests. The load-bearing one is test_zero_network_on_request: it proves
a user request scores the cache and never touches an external job API.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from jobfitr import server, snapshot
from jobfitr.config_builder import config_from_dict

RECENT = (date.today() - timedelta(days=3)).isoformat()

# Real JDs run ~400 tokens; job_radar's score() length-normalizes against that,
# so a tiny fixture body inflates scores ~4x. Pad bodies with keyword-free filler
# so scores reflect the fit weights, not blob length.
FILLER = "we build and ship great things for our users every single day " * 40


def _job(
    title,
    text="",
    company="Acme",
    location="Remote",
    posted=RECENT,
    url="https://x/job",
):
    return {
        "title": title,
        "text": (text + " " + FILLER).strip(),
        "company": company,
        "location": location,
        "posted": posted,
        "url": url,
        "source": "remotive",
        "sources": ["remotive"],
    }


# ── Step 1: config_from_dict ──────────────────────────────────────────────────
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
    # lowercased + deduped, signal seeded from titles + boosts
    assert cfg.title_queries == ["zookeeper", "animal keeper"]
    assert cfg.title_signal == [
        "zookeeper",
        "animal keeper",
        "reptiles",
        "biology degree",
    ]
    # tech defaults are replaced, not merged
    assert cfg.fit_weights == {
        "zookeeper": 3,
        "animal keeper": 3,
        "reptiles": 5,
        "biology degree": 5,
    }
    assert cfg.exclude_titles == ["intern", "volunteer"]
    assert cfg.agency_penalty == {"staffing": 8}
    assert cfg.location == "Louisville, KY"
    assert cfg.remote_only is False  # a real place turns off remote-only
    assert cfg.max_age_days == 45
    assert cfg.min_score == 20  # "strong"


def test_config_from_dict_empty_falls_back_to_defaults():
    cfg = config_from_dict({})
    assert cfg.title_signal  # non-empty default, so relevant() doesn't drop everything
    assert cfg.min_score == 12  # "balanced" default


def test_config_from_dict_remote_and_anywhere():
    assert config_from_dict({"location": "remote"}).remote_only is True
    assert config_from_dict({"location": "anywhere"}).remote_only is False
    # explicit flag wins over inference
    assert (
        config_from_dict({"location": "Denver", "remote_only": True}).remote_only
        is True
    )


# ── Step 2: snapshot round-trip ───────────────────────────────────────────────
def test_snapshot_roundtrip(tmp_path, monkeypatch):
    from job_radar.config import Config

    long_text = "x" * 5000
    rows = [
        {
            **_job("Data Engineer", text=long_text),
            "score": 42,
            "sources": {"remotive", "remoteok"},
        },
        {**_job("Product Manager"), "score": 10, "sources": {"jobicy"}},
    ]
    monkeypatch.setattr(
        snapshot.engine,
        "harvest",
        lambda cfg, wl: (rows, [], ["boom: himalayas timed out"]),
    )

    out = tmp_path / "jobs.json"
    meta = snapshot.build_snapshot(Config(), None, str(out))

    assert out.exists()
    assert meta["count"] == 2
    assert "himalayas" in meta["errors"][0]

    snap = snapshot.load_snapshot(str(out))
    j0 = snap["jobs"][0]
    assert isinstance(j0["sources"], list) and j0["sources"] == [
        "remoteok",
        "remotive",
    ]  # set→sorted list
    assert len(j0["text"]) == snapshot.TEXT_CAP  # truncated
    assert snap["meta"]["harvested_at"] and "T" in snap["meta"]["harvested_at"]


def test_load_snapshot_missing_file_is_empty(tmp_path):
    snap = snapshot.load_snapshot(str(tmp_path / "nope.json"))
    assert snap["jobs"] == [] and snap["meta"]["count"] == 0


# ── Step 4: the /api/score endpoint ───────────────────────────────────────────
def _seed(tmp_path, monkeypatch, jobs):
    path = tmp_path / "jobs.json"
    import json

    path.write_text(
        json.dumps(
            {
                "meta": {"count": len(jobs), "harvested_at": "2026-07-12T16:00:00"},
                "jobs": jobs,
            }
        )
    )
    monkeypatch.setattr(server, "JOBS_PATH", str(path))
    return TestClient(server.app)


def test_score_endpoint_ranks_and_filters(tmp_path, monkeypatch):
    jobs = [
        _job("Python Engineer", text="python kubernetes docker"),  # strong: both boosts
        _job("Data Engineer", text="python etl pipelines"),  # medium: one boost
        _job(
            "Marketing Engineer", text="seo content calendar"
        ),  # weak: title word only
        _job("Engineering Intern", text="python kubernetes"),  # excluded by title
    ]
    body = {
        "titles": ["engineer"],
        "boosts": ["python", "kubernetes"],
        "exclude": ["intern"],
        "remote_only": False,
    }
    client = _seed(tmp_path, monkeypatch, jobs)

    # Loose pass: everything relevant survives, ranked by fit.
    data = client.post("/api/score", json={**body, "min_score": "plenty"}).json()
    titles = [j["title"] for j in data["jobs"]]
    assert "Engineering Intern" not in titles  # hard-excluded
    # boost-rich → boost-lean → title-only: the ranking guarantee
    assert titles == ["Python Engineer", "Data Engineer", "Marketing Engineer"]
    scores = [j["fit_score"] for j in data["jobs"]]
    assert scores == sorted(scores, reverse=True)
    assert data["jobs"][0]["why"]  # top_signals attached
    assert "text" not in data["jobs"][0]  # full JD body not leaked; snippet only
    assert "snippet" in data["jobs"][0]

    # fit_pct: a derived gauge value, 0-100, that tracks the fit_score ordering.
    pcts = [j["fit_pct"] for j in data["jobs"]]
    assert all(0 < p <= 100 for p in pcts)
    assert pcts == sorted(pcts, reverse=True)  # monotonic with fit_score
    assert pcts[0] == max(pcts)  # the top match reads highest on the gauge
    # description: fuller than the snippet, still from the cache (not a live fetch).
    top = data["jobs"][0]
    assert len(top["description"]) >= len(top["snippet"])
    assert "fit_score" in top  # the raw engine score stays canonical alongside fit_pct

    # Tight pass: a high threshold drops the weak matches, keeps the strong one.
    strict = client.post("/api/score", json={**body, "min_score": 15}).json()
    strict_titles = [j["title"] for j in strict["jobs"]]
    assert "Python Engineer" in strict_titles
    assert "Marketing Engineer" not in strict_titles
    assert len(strict_titles) < len(titles)  # the threshold actually filtered


def test_meta_and_health(tmp_path, monkeypatch):
    client = _seed(tmp_path, monkeypatch, [_job("Engineer")])
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/meta").json()["count"] == 1


def test_garbage_post_is_clean_4xx(tmp_path, monkeypatch):
    client = _seed(tmp_path, monkeypatch, [_job("Engineer")])
    # a JSON array, not an object → 422 (a clean validation error, never a 500)
    resp = client.post("/api/score", json=["not", "an", "object"])
    assert resp.status_code == 422
    # an empty object is valid → 200 with defaults
    assert client.post("/api/score", json={}).status_code == 200


# ── The guarantee: zero external calls on a request ───────────────────────────
def test_zero_network_on_request(tmp_path, monkeypatch):
    client = _seed(tmp_path, monkeypatch, [_job("Python Engineer", text="python")])

    def _boom(*a, **k):
        raise AssertionError(
            "a request must NOT hit the network — the cache is pre-harvested"
        )

    # Poison every path the engine could use to reach a job API.
    import urllib.request

    import job_radar.util as jr_util

    monkeypatch.setattr(jr_util, "get_json", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    resp = client.post(
        "/api/score",
        json={
            "titles": ["engineer"],
            "boosts": ["python"],
            "remote_only": False,
            "min_score": "plenty",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


# ── Step 1: the front end is served same-origin ───────────────────────────────
def test_static_front_end_is_served():
    client = TestClient(server.app)
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "jobfitr" in root.text
    # the API still resolves — the static mount didn't shadow it
    assert client.get("/api/health").json() == {"ok": True}
