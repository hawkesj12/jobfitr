"""Phase F: the SQLite/FTS5 store. No network; a fresh tmp DB per test."""

from __future__ import annotations

import json
import time

import pytest

from jobfitr import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    # no stray jobs.json import unless a test asks for it
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(tmp_path / "nope.json"))
    store.init(p)
    return p


def _job(url, title, text="", **kw):
    base = {
        "url": url,
        "title": title,
        "text": text,
        "company": "Acme",
        "location": "Remote",
        "posted": "2026-07-10",
        "salary": "",
        "source": "adzuna",
    }
    base.update(kw)
    return base


# ── normalize + tags ──────────────────────────────────────────────────────────
def test_normalize_derives_tags():
    r = store.normalize_job(
        _job(
            "u",
            "Senior Data Engineer",
            department="IT Jobs",
            employment_type="full_time",
            salary="$120,000–$140,000",
            location="Austin, TX",
        )
    )
    assert r["category"] == "IT Jobs"
    assert r["employment_type"] == "full_time"
    assert r["seniority"] == "senior"
    assert r["remote"] == "onsite"
    assert r["salary_band"] == "120-180k"
    assert (
        store.normalize_job(_job("u2", "Nurse", location="Remote"))["remote"]
        == "remote"
    )


def test_normalize_strips_adzuna_remote_artifact():
    # job_radar appends " (Remote)" to EVERY Adzuna location — noise, not signal.
    r = store.normalize_job(
        _job(
            "u",
            "Grocery Store Manager",
            location="Wahpeton, ND (Remote)",
            source="adzuna",
        )
    )
    assert r["location"] == "Wahpeton, ND"  # artifact stripped
    assert r["remote"] == "onsite"  # so an on-site grocery job isn't mislabeled remote
    # a genuinely remote-titled Adzuna job still reads remote (title signal)
    assert (
        store.normalize_job(
            _job(
                "u2",
                "Remote Customer Advocate",
                location="Austin (Remote)",
                source="adzuna",
            )
        )["remote"]
        == "remote"
    )
    # the free remote boards' conditional "(Remote)" is real signal — keep it
    assert (
        store.normalize_job(
            _job("u3", "Designer", location="Anywhere (Remote)", source="remotive")
        )["remote"]
        == "remote"
    )


def test_normalize_remote_from_body():
    # The keyed sources (Adzuna/USAJOBS) carry no remote flag and lose their
    # "(Remote)" artifact, so a genuinely-remote role reads onsite from title+loc
    # alone. The body scan recovers it — this is the fix for empty remote searches.
    r = store.normalize_job(
        _job(
            "b1",
            "Front End Developer",
            location="Austin, TX (Remote)",
            text="We are hiring a front end developer. This is a fully remote position open to any US state.",
        )
    )
    assert r["remote"] == "remote"
    # explicit negation in the body keeps an on-site role onsite
    assert (
        store.normalize_job(
            _job(
                "b2",
                "Front End Developer",
                location="Austin, TX (Remote)",
                text="On-site only. This is not a remote position; you must work from our Austin office.",
            )
        )["remote"]
        == "onsite"
    )
    # incidental "remote" in prose must NOT flip an on-site job (no false positive)
    assert (
        store.normalize_job(
            _job(
                "b3",
                "Systems Engineer",
                location="Dayton, OH (Remote)",
                text="You will administer remote servers and support remote teams across our data centers.",
            )
        )["remote"]
        == "onsite"
    )


# ── upsert dedup + refresh ────────────────────────────────────────────────────
def test_upsert_dedup_and_refresh(db):
    assert store.upsert_jobs([_job("u1", "Accountant", salary="$50k")], path=db) == 1
    assert store.pool_size(db) == 1
    # re-upsert same url → still 1 row, salary refreshed
    store.upsert_jobs([_job("u1", "Accountant", salary="$70k")], path=db)
    assert store.pool_size(db) == 1
    cands = store.bm25_candidates(["accountant"], path=db)
    assert cands and cands[0]["salary"] == "$70k"


# ── BM25 differentiates a one-word query (the whole point) ────────────────────
def test_bm25_ranks_and_differentiates(db):
    store.upsert_jobs(
        [
            _job(
                "a1",
                "Senior Accountant",
                "audit gaap tax accountant accountant reporting",
            ),
            _job("a2", "Junior Accountant", "entry level accountant bookkeeping"),
            _job(
                "a3",
                "Accounting Manager",
                "team budgeting oversight some accountant duties",
            ),
            _job("m1", "Marketing Manager", "brand campaigns seo growth"),
            _job("e1", "Software Engineer", "python backend apis"),
            _job("n1", "Registered Nurse", "patient care icu clinical"),
            _job("s1", "Sales Representative", "quota pipeline crm outbound"),
            _job("d1", "Truck Driver", "cdl routes logistics deliveries"),
            _job("t1", "Teacher", "classroom curriculum students k-12"),
            _job("g1", "Designer", "figma brand ux visual"),
            _job("w1", "Warehouse Associate", "picking packing forklift"),
            _job("p1", "Product Manager", "roadmap discovery stakeholders"),
        ],
        path=db,
    )
    cands = store.bm25_candidates(["accountant"], path=db)
    urls = [c["url"] for c in cands]
    assert "m1" not in urls and "e1" not in urls  # non-matching excluded
    assert set(urls) == {"a1", "a2", "a3"}
    scores = [c["bm25"] for c in cands]
    assert scores == sorted(scores, reverse=True)  # ordered best-first
    assert len(set(round(s, 4) for s in scores)) > 1  # NOT tied (flat sum would tie)


# ── TTL freshness clock ───────────────────────────────────────────────────────
def test_ttl_fresh_then_stale(db):
    k = store.search_key(["grocery store manager"], "remote")
    assert store.search_fresh(k, path=db) is False  # never fetched
    store.mark_fetched(k, path=db)
    assert store.search_fresh(k, ttl=3600, path=db) is True  # just now
    assert store.search_fresh(k, ttl=0, path=db) is False  # 0s TTL → stale


# ── eviction: unseen>14d, posted>60d, LRU cap ────────────────────────────────
def test_evict_unseen_and_posted_and_cap(db, monkeypatch):
    now = time.time()
    store.upsert_jobs([_job("fresh", "Fresh Job", posted="2026-07-13")], path=db)
    store.upsert_jobs([_job("oldposted", "Old Posted", posted="2020-01-01")], path=db)
    store.upsert_jobs([_job("unseen", "Unseen Job", posted="2026-07-13")], path=db)
    # age "unseen"'s last_seen to 20 days ago via a direct write
    with store._conn(db) as c:
        c.execute("UPDATE jobs SET last_seen=? WHERE url='unseen'", (now - 20 * 86400,))
    removed = store.evict(now=now, path=db)
    remaining = {r["url"] for r in store.bm25_candidates(["job"], path=db)}
    assert removed >= 2
    assert "oldposted" not in remaining and "unseen" not in remaining
    assert "fresh" in remaining


def test_evict_lru_cap(db, monkeypatch):
    monkeypatch.setattr(store, "MAX_ROWS", 2)
    now = time.time()
    for i in range(5):
        store.upsert_jobs([_job(f"j{i}", "Widget Maker", posted="2026-07-13")], path=db)
        with store._conn(db) as c:
            c.execute("UPDATE jobs SET last_seen=? WHERE url=?", (now - i, f"j{i}"))
    store.evict(now=now, path=db)
    assert store.pool_size(db) == 2  # LRU capped


# ── facets ────────────────────────────────────────────────────────────────────
def test_facet_counts():
    rows = [
        {
            "category": "IT Jobs",
            "employment_type": "full_time",
            "remote": "remote",
            "seniority": "senior",
            "salary_band": "80-120k",
        },
        {
            "category": "IT Jobs",
            "employment_type": "full_time",
            "remote": "onsite",
            "seniority": "mid",
            "salary_band": "",
        },
        {
            "category": "Healthcare & Nursing Jobs",
            "employment_type": "part_time",
            "remote": "onsite",
            "seniority": "mid",
            "salary_band": "50-80k",
        },
    ]
    f = store.facet_counts(rows)
    assert f["category"]["IT Jobs"] == 2
    assert f["employment_type"]["full_time"] == 2
    assert f["remote"] == {"remote": 1, "onsite": 2}
    assert "" not in f["salary_band"]  # empty bands not counted


# ── one-time jobs.json import on init ─────────────────────────────────────────
def test_imports_jobs_json_once(tmp_path, monkeypatch):
    jj = tmp_path / "jobs.json"
    jj.write_text(
        json.dumps(
            {
                "meta": {"count": 2},
                "jobs": [_job("i1", "Imported One"), _job("i2", "Imported Two")],
            }
        )
    )
    monkeypatch.setattr(store, "JOBS_JSON_PATH", str(jj))
    p = str(tmp_path / "imp.db")
    store.init(p)
    assert store.pool_size(p) == 2
    # init again → does NOT double-import (pool already non-empty)
    store.init(p)
    assert store.pool_size(p) == 2
